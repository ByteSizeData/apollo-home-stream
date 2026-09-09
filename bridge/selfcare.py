"""Self-care for the bridge: health checks, self-test, and self-update.

Everything here is stdlib-only and safe to import on any platform.
"""
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

VERSION = "0.2.0"
REPO = "ByteSizeData/apollo-home-stream"          # the ONLY place updates are ever fetched from
BRANCH = "main"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SHA_FILE = os.path.join(ROOT, ".apollo-sha")        # written by zip updates (git installs use .git)
APOLLO_WEB_UI_PORT = 47990                          # Apollo / Sunshine settings page
NET_TIMEOUT = 4

# ----------------------------------------------------------------------------- checks
def _tcp(host, port, timeout=NET_TIMEOUT):
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def check_python():
    ok = sys.version_info >= (3, 8)
    return _c("python", ok, platform.python_version() + ("" if ok else " — need 3.8+"), fail=True)


def check_files():
    web = os.path.join(ROOT, "web")
    missing = [f for f in ("index.html", "pin.html") if not os.path.isfile(os.path.join(web, f))]
    return _c("web files", not missing, "web/index.html + pin.html present" if not missing else "missing: " + ", ".join(missing), fail=True)


def check_steam(bridge, allow_missing=False):
    root = getattr(bridge, "steam_root", None)
    if not root or not os.path.isdir(root):
        return _c("steam", False, "Steam folder not found — pass --steam \"C:\\Program Files (x86)\\Steam\"", fail=not allow_missing)
    try:
        n = len(bridge.games())
    except Exception as e:  # noqa: BLE001
        return _c("steam", False, "found %s but couldn't read it: %s" % (root, e), fail=not allow_missing)
    return _c("steam", n > 0, "%d installed game%s in %s" % (n, "" if n == 1 else "s", root), fail=False)


def check_port(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
        return _c("port %d" % port, True, "free")
    except OSError:
        return _c("port %d" % port, False, "already in use — is another bridge running? (fine if it's this one)", fail=False)
    finally:
        s.close()


def check_apollo():
    up = _tcp("127.0.0.1", APOLLO_WEB_UI_PORT, timeout=1.5)
    return _c("apollo host", up,
              "answering on :%d" % APOLLO_WEB_UI_PORT if up else "nothing on :%d — Apollo isn't running on this PC (install it from the Install tab)" % APOLLO_WEB_UI_PORT,
              fail=False)


def check_tailscale():
    exe = shutil.which("tailscale") or (r"C:\Program Files\Tailscale\tailscale.exe" if os.name == "nt" else "/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    if exe and os.path.exists(exe):
        try:
            out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=5).stdout
            st = json.loads(out or "{}")
            state = st.get("BackendState", "?")
            ip = (st.get("Self") or {}).get("TailscaleIPs", [None])[0]
            dns = ((st.get("Self") or {}).get("DNSName") or "").rstrip(".")
            ok = state == "Running"
            return _c("tailscale", ok, ("%s · %s · %s" % (state, ip, dns)).strip(" ·") if ok else "installed but %s" % state, fail=False)
        except Exception as e:  # noqa: BLE001
            return _c("tailscale", False, "installed, status unreadable (%s)" % type(e).__name__, fail=False)
    # no CLI: look for a CGNAT 100.64/10 address, which is what Tailscale hands out
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            a, b = (int(x) for x in ip.split(".")[:2])
            if a == 100 and 64 <= b <= 127:
                return _c("tailscale", True, "interface %s (CLI not found)" % ip, fail=False)
    except OSError:
        pass
    return _c("tailscale", False, "not detected — only needed for playing away from home", fail=False)


def check_internet():
    steam = _tcp("cdn.cloudflare.steamstatic.com", 443)
    gh = _tcp("api.github.com", 443)
    return [
        _c("steam cdn", steam, "reachable — cover art will load" if steam else "unreachable — covers fall back to generated art", fail=False),
        _c("github", gh, "reachable — updates can be checked" if gh else "unreachable — self-update unavailable", fail=False),
    ]


def _c(name, ok, detail, fail=True):
    return {"name": name, "status": "ok" if ok else ("fail" if fail else "warn"), "detail": detail}


def health(bridge, port=8777, network=True, allow_missing_steam=False, serving=False):
    port_check = _c("port %d" % port, True, "serving") if serving else check_port(port)   # the running bridge owns its own port
    checks = [check_python(), check_files(), check_steam(bridge, allow_missing_steam), port_check, check_apollo(), check_tailscale()]
    if network:
        checks += check_internet()
    worst = "ok"
    for c in checks:
        if c["status"] == "fail":
            worst = "fail"
        elif c["status"] == "warn" and worst == "ok":
            worst = "warn"
    return {"version": VERSION, "sha": (local_sha() or "")[:7], "status": worst, "checks": checks, "checked_at": int(time.time())}


# ----------------------------------------------------------------------------- self-test
def run_unit_tests():
    tests = os.path.join(ROOT, "tests")
    if not os.path.isdir(tests):
        return {"name": "unit tests", "status": "warn", "detail": "tests/ folder not present in this copy"}
    r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", tests], capture_output=True, text=True, timeout=300)
    tail = (r.stderr or r.stdout).strip().splitlines()
    summary = next((l for l in reversed(tail) if l.startswith(("OK", "FAILED", "Ran "))), "no output")
    ran = next((l for l in tail if l.startswith("Ran ")), "")
    return {"name": "unit tests", "status": "ok" if r.returncode == 0 else "fail", "detail": (ran + " — " + summary).strip(" —")}


def self_test(bridge, port=8777, network=True, allow_missing_steam=False, with_unit_tests=True):
    h = health(bridge, port, network, allow_missing_steam)
    if with_unit_tests:
        h["checks"].append(run_unit_tests())
        if h["checks"][-1]["status"] == "fail":
            h["status"] = "fail"
    return h


def print_report(h):
    mark = {"ok": "OK  ", "warn": "warn", "fail": "FAIL"}
    print("Apollo Home Stream bridge %s (%s)" % (h["version"], h.get("sha") or "no git"))
    for c in h["checks"]:
        print("  [%s] %-11s %s" % (mark[c["status"]], c["name"], c["detail"]))
    print("  =>", {"ok": "everything works", "warn": "works, with notes above", "fail": "something needs fixing"}[h["status"]])


# ----------------------------------------------------------------------------- update
def local_sha():
    git_dir = os.path.join(ROOT, ".git")
    if os.path.isdir(git_dir) and shutil.which("git"):
        try:
            return subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip() or None
        except Exception:  # noqa: BLE001
            return None
    try:
        with open(SHA_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def remote_sha(timeout=NET_TIMEOUT):
    req = urllib.request.Request("https://api.github.com/repos/%s/commits/%s" % (REPO, BRANCH),
                                 headers={"User-Agent": "apollo-home-stream/" + VERSION, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["sha"]


def update_status(timeout=NET_TIMEOUT):
    """{'available': bool|None, 'local': sha, 'remote': sha, 'error': str|None} — never raises."""
    loc = local_sha()
    try:
        rem = remote_sha(timeout)
    except Exception as e:  # noqa: BLE001
        return {"available": None, "local": loc, "remote": None, "error": "couldn't reach GitHub (%s)" % type(e).__name__}
    if not loc:
        return {"available": True, "local": None, "remote": rem, "error": None}   # unknown local version: treat as updatable
    return {"available": loc != rem, "local": loc, "remote": rem, "error": None}


def _safe_members(zf, prefix):
    for m in zf.infolist():
        name = m.filename
        if not name.startswith(prefix) or ".." in name.split("/") or name.startswith("/"):
            raise ValueError("refusing suspicious zip entry: " + name)
    return True


def apply_update(log=print):
    """Bring bridge/, web/ and tests/ up to date with the repo. Returns (ok, message).
    Git checkout → git pull --ff-only. Plain copy → download the zip of `main` from GitHub,
    verify its shape, back up the current files, swap them in, run the unit tests, and roll
    back if they fail."""
    git_dir = os.path.join(ROOT, ".git")
    if os.path.isdir(git_dir) and shutil.which("git"):
        r = subprocess.run(["git", "-C", ROOT, "pull", "--ff-only", "origin", BRANCH], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return False, "git pull failed: " + (r.stderr or r.stdout).strip()[-400:]
        t = run_unit_tests()
        if t["status"] == "fail":
            return False, "updated, but the tests fail: " + t["detail"] + "  (run `git -C %s log` to inspect, or `git reset --hard HEAD@{1}` to go back)" % ROOT
        return True, "updated via git → " + (local_sha() or "?")[:7] + " · " + t["detail"]

    url = "https://codeload.github.com/%s/zip/refs/heads/%s" % (REPO, BRANCH)
    log("downloading " + url)
    tmp = tempfile.mkdtemp(prefix="apollo-update-")
    zpath = os.path.join(tmp, "main.zip")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "apollo-home-stream/" + VERSION})
        with urllib.request.urlopen(req, timeout=60) as r, open(zpath, "wb") as f:
            shutil.copyfileobj(r, f)
        with zipfile.ZipFile(zpath) as zf:
            top = zf.namelist()[0].split("/")[0] + "/"
            _safe_members(zf, top)
            names = set(zf.namelist())
            for must in ("bridge/apollo_bridge.py", "bridge/selfcare.py", "web/index.html", "web/pin.html"):
                if top + must not in names:
                    return False, "download doesn't look like this project (missing %s) — not touching anything" % must
            zf.extractall(tmp)
        src = os.path.join(tmp, top.rstrip("/"))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(ROOT, ".backup-" + stamp)
        os.makedirs(backup)
        for d in ("bridge", "web", "tests"):
            cur = os.path.join(ROOT, d)
            if os.path.isdir(cur):
                shutil.copytree(cur, os.path.join(backup, d))
        try:
            for d in ("bridge", "web", "tests"):
                new = os.path.join(src, d)
                if not os.path.isdir(new):
                    continue
                cur = os.path.join(ROOT, d)
                if os.path.isdir(cur):
                    shutil.rmtree(cur)
                shutil.copytree(new, cur)
            new_sha = remote_sha()
            with open(SHA_FILE, "w") as f:
                f.write(new_sha + "\n")
            t = run_unit_tests()
            if t["status"] == "fail":
                raise RuntimeError("tests failed after update: " + t["detail"])
        except Exception as e:  # noqa: BLE001 — anything wrong: put the old files back
            for d in ("bridge", "web", "tests"):
                cur = os.path.join(ROOT, d)
                old = os.path.join(backup, d)
                if os.path.isdir(cur):
                    shutil.rmtree(cur)
                if os.path.isdir(old):
                    shutil.copytree(old, cur)
            return False, "update rolled back: %s (previous files restored from %s)" % (e, backup)
        shutil.rmtree(backup, ignore_errors=True)
        return True, "updated to %s · %s" % (new_sha[:7], t["detail"])
    except Exception as e:  # noqa: BLE001
        return False, "update failed before touching anything: %s" % e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
