"""Self-care for the bridge: health checks, self-test, and self-update.

Stdlib only. Every network call is bounded; every update path is verified,
backed up, tested, and rolled back on failure. Updates come from ONE place.
"""
import json
import os
import platform
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

VERSION = "0.2.1"
REPO = "ByteSizeData/apollo-home-stream"          # the ONLY place updates are ever fetched from
BRANCH = "main"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SHA_FILE = os.path.join(ROOT, ".apollo-sha")        # written by zip updates (git installs use .git)
APOLLO_WEB_UI_PORT = 47990                          # Apollo / Sunshine settings page
NET_TIMEOUT = 4
ALLOWED_HOSTS = ("github.com", "api.github.com", "codeload.github.com", "objects.githubusercontent.com")


# ----------------------------------------------------------------------------- bounded helpers
def _bounded(fn, timeout, default):
    """Run fn() in a daemon thread; give up after `timeout` s (DNS can hang far past socket timeouts)."""
    box = {}
    t = threading.Thread(target=lambda: box.__setitem__("v", fn()), daemon=True)
    t.start()
    t.join(timeout)
    return box.get("v", default)


def _tcp(host, port, timeout=NET_TIMEOUT):
    """True if ONE connection attempt to host:port succeeds within `timeout` — total, not per address."""
    def probe():
        try:
            infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
            if not infos:
                return False
            fam, typ, proto, _, addr = infos[0]
            s = socket.socket(fam, typ, proto)
            s.settimeout(timeout)
            try:
                s.connect(addr)
                return True
            finally:
                s.close()
        except OSError:
            return False
    return _bounded(probe, timeout + 0.5, False)


class _SameSiteRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to https on GitHub-owned hosts."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from urllib.parse import urlparse
        u = urlparse(newurl)
        if u.scheme != "https" or u.hostname not in ALLOWED_HOSTS:
            raise urllib.error.URLError("refusing redirect off GitHub: " + newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SameSiteRedirects())


def _get(url, timeout):
    if not url.startswith("https://") or url.split("/")[2] not in ALLOWED_HOSTS:
        raise ValueError("refusing to fetch from outside GitHub: " + url)
    return _opener.open(urllib.request.Request(url, headers={"User-Agent": "apollo-home-stream/" + VERSION, "Accept": "application/vnd.github+json"}), timeout=timeout)


# ----------------------------------------------------------------------------- checks
def _c(name, ok, detail, fail=True):
    return {"name": name, "status": "ok" if ok else ("fail" if fail else "warn"), "detail": detail}


def check_python():
    ok = sys.version_info >= (3, 8)
    return _c("python", ok, platform.python_version() + ("" if ok else " - need 3.8+"), fail=True)


def check_files():
    web = os.path.join(ROOT, "web")
    missing = [f for f in ("index.html", "pin.html") if not os.path.isfile(os.path.join(web, f))]
    return _c("web files", not missing, "web/index.html + pin.html present" if not missing else "missing: " + ", ".join(missing), fail=True)


def check_steam(bridge, allow_missing=False):
    root = getattr(bridge, "steam_root", None)
    if not root or not os.path.isdir(root):
        return _c("steam", False, 'Steam folder not found - pass --steam "C:\\Program Files (x86)\\Steam"', fail=not allow_missing)
    try:
        n = len(bridge.games())
    except Exception as e:  # noqa: BLE001
        return _c("steam", False, "found %s but couldn't read it: %s" % (root, e), fail=not allow_missing)
    return _c("steam", n > 0, "%d installed game%s in %s" % (n, "" if n == 1 else "s", root), fail=False)


def check_port(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)   # on Windows SO_REUSEADDR would bind over a live listener
    try:
        s.bind(("0.0.0.0", port))
        return _c("port %d" % port, True, "free")
    except OSError:
        return _c("port %d" % port, False, "already in use - is another bridge running?", fail=False)
    finally:
        s.close()


def check_apollo():
    """Apollo/Sunshine serve their settings UI over https on :47990. Look for an HTTP answer, not just an open port."""
    def probe():
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE                         # it's a self-signed cert on localhost
        try:
            r = urllib.request.urlopen("https://127.0.0.1:%d/" % APOLLO_WEB_UI_PORT, timeout=2, context=ctx)
            return ("http", r.status)
        except urllib.error.HTTPError as e:
            return ("http", e.code)                             # 401/403 etc. still means Apollo answered
        except Exception:  # noqa: BLE001
            return ("tcp", None) if _tcp("127.0.0.1", APOLLO_WEB_UI_PORT, timeout=1.5) else ("none", None)
    kind, code = _bounded(probe, 3, ("none", None))
    if kind == "http":
        return _c("apollo host", True, "answering on :%d (HTTP %s)" % (APOLLO_WEB_UI_PORT, code))
    if kind == "tcp":
        return _c("apollo host", False, "something is on :%d but it isn't speaking HTTPS - is that Apollo?" % APOLLO_WEB_UI_PORT, fail=False)
    return _c("apollo host", False, "nothing on :%d - Apollo isn't running on this PC (install it from the Install tab)" % APOLLO_WEB_UI_PORT, fail=False)


def check_tailscale():
    exe = shutil.which("tailscale") or (r"C:\Program Files\Tailscale\tailscale.exe" if os.name == "nt" else "/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    if exe and os.path.exists(exe):
        try:
            out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=5).stdout
            st = json.loads(out or "{}")
            state = st.get("BackendState", "?")
            me = st.get("Self") or {}
            ips = me.get("TailscaleIPs") or []
            dns = (me.get("DNSName") or "").rstrip(".")
            if state == "Running":
                return _c("tailscale", True, " / ".join(x for x in (ips[0] if ips else "", dns) if x) or "running")
            return _c("tailscale", False, "installed but %s - open the Tailscale app and sign in" % state, fail=False)
        except subprocess.TimeoutExpired:
            return _c("tailscale", False, "installed but not responding", fail=False)
        except Exception as e:  # noqa: BLE001
            return _c("tailscale", False, "installed, status unreadable (%s)" % type(e).__name__, fail=False)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            a, b = (int(x) for x in ip.split(".")[:2])
            if a == 100 and 64 <= b <= 127:
                return _c("tailscale", False, "%s looks like a tailnet address but the Tailscale CLI wasn't found" % ip, fail=False)
    except OSError:
        pass
    return _c("tailscale", False, "not detected - only needed for playing away from home", fail=False)


def check_internet():
    results = {}
    def probe(key, host):
        results[key] = _tcp(host, 443)
    ts = [threading.Thread(target=probe, args=("steam", "cdn.cloudflare.steamstatic.com"), daemon=True),
          threading.Thread(target=probe, args=("gh", "api.github.com"), daemon=True)]
    for t in ts: t.start()
    for t in ts: t.join(NET_TIMEOUT + 1)
    steam, gh = results.get("steam", False), results.get("gh", False)
    return [
        _c("steam cdn", steam, "reachable - cover art will load" if steam else "unreachable - covers fall back to generated art", fail=False),
        _c("github", gh, "reachable - updates can be checked" if gh else "unreachable - self-update unavailable", fail=False),
    ]


def health(bridge, port=8777, network=True, allow_missing_steam=False, serving=False):
    port_check = _c("port %d" % port, True, "serving") if serving else check_port(port)
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
    try:
        r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", tests], capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"name": "unit tests", "status": "fail", "detail": "tests did not finish within 5 minutes"}
    tail = (r.stderr or r.stdout).strip().splitlines()
    summary = next((l for l in reversed(tail) if l.startswith(("OK", "FAILED", "Ran "))), "no output")
    ran = next((l for l in tail if l.startswith("Ran ")), "")
    return {"name": "unit tests", "status": "ok" if r.returncode == 0 else "fail", "detail": (ran + " - " + summary).strip(" -")}


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
def _git(*args, timeout=120):
    return subprocess.run(["git", "-C", ROOT] + list(args), capture_output=True, text=True, timeout=timeout)


def is_git_checkout():
    return os.path.isdir(os.path.join(ROOT, ".git")) and shutil.which("git") is not None


def local_sha():
    if is_git_checkout():
        try:
            return _git("rev-parse", "HEAD", timeout=10).stdout.strip() or None
        except Exception:  # noqa: BLE001
            return None
    try:
        with open(SHA_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def remote_sha(timeout=NET_TIMEOUT):
    with _get("https://api.github.com/repos/%s/commits/%s" % (REPO, BRANCH), timeout) as r:
        return json.loads(r.read())["sha"]


def update_status(timeout=NET_TIMEOUT):
    """{'available': bool|None, 'local': sha, 'remote': sha, 'error': str|None} - never raises."""
    loc = local_sha()
    try:
        rem = _bounded(lambda: remote_sha(timeout), timeout + 1, None)
        if rem is None:
            raise TimeoutError("timed out")
    except Exception as e:  # noqa: BLE001
        return {"available": None, "local": loc, "remote": None, "error": "couldn't reach GitHub (%s)" % (type(e).__name__)}
    if not loc:
        return {"available": True, "local": None, "remote": rem, "error": None}
    return {"available": loc != rem, "local": loc, "remote": rem, "error": None}


def _safe_members(zf, prefix):
    for m in zf.infolist():
        name = m.filename
        parts = name.split("/")
        if not name.startswith(prefix) or ".." in parts or name.startswith("/") or "\\" in name:
            raise ValueError("refusing suspicious zip entry: " + name)
        if (m.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError("refusing symlink in zip: " + name)
    return True


def _git_update(log):
    before = local_sha()
    try:
        r = _git("fetch", "https://github.com/%s.git" % REPO, BRANCH)      # always the pinned repo, never `origin`
        if r.returncode != 0:
            return False, "git fetch failed: " + (r.stderr or r.stdout).strip()[-300:], before, before
        r = _git("merge", "--ff-only", "FETCH_HEAD")
        if r.returncode != 0:
            return False, ("this copy has local commits that aren't on GitHub, so it can't fast-forward - "
                           "commit them upstream or run `git -C %s reset --hard FETCH_HEAD` if you don't need them" % ROOT), before, before
    except subprocess.TimeoutExpired:
        return False, "git timed out (network drop or a credential prompt?) - nothing was changed", before, before
    after = local_sha()
    if after == before:
        return True, "already up to date (%s)" % (after or "?")[:7], before, after
    t = run_unit_tests()
    if t["status"] == "fail":
        _git("reset", "--hard", before)
        return False, "updated to %s but the tests failed (%s) - rolled back to %s" % (after[:7], t["detail"], before[:7]), before, local_sha()
    return True, "updated via git %s -> %s . %s" % ((before or "?")[:7], after[:7], t["detail"]), before, after


def _zip_update(log):
    before = local_sha()
    target = remote_sha()                                                    # decide the exact commit FIRST...
    url = "https://codeload.github.com/%s/zip/%s" % (REPO, target)           # ...then download exactly that commit
    log("downloading " + url)
    tmp = tempfile.mkdtemp(prefix="apollo-update-")
    zpath = os.path.join(tmp, "update.zip")
    backup = None
    touched = False
    try:
        with _get(url, 60) as r, open(zpath, "wb") as f:
            shutil.copyfileobj(r, f, 1024 * 1024)
        if os.path.getsize(zpath) > 200 * 1024 * 1024:
            return False, "download is implausibly large - not touching anything", before, before
        with zipfile.ZipFile(zpath) as zf:
            names = zf.namelist()
            top = names[0].split("/")[0] + "/"
            _safe_members(zf, top)
            if sum(i.file_size for i in zf.infolist()) > 500 * 1024 * 1024:
                return False, "archive expands too large - not touching anything", before, before
            for must in ("bridge/apollo_bridge.py", "bridge/selfcare.py", "web/index.html", "web/pin.html"):
                if top + must not in names:
                    return False, "download doesn't look like this project (missing %s) - not touching anything" % must, before, before
            zf.extractall(tmp)
        src = os.path.join(tmp, top.rstrip("/"))
        backup = os.path.join(ROOT, ".backup-" + time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(backup)
        for d in ("bridge", "web", "tests"):
            cur = os.path.join(ROOT, d)
            if os.path.isdir(cur):
                shutil.copytree(cur, os.path.join(backup, d))
        old_sha_text = None
        if os.path.exists(SHA_FILE):
            with open(SHA_FILE) as f:
                old_sha_text = f.read()
        touched = True
        for d in ("bridge", "web", "tests"):
            new = os.path.join(src, d)
            if not os.path.isdir(new):
                continue
            cur = os.path.join(ROOT, d)
            if os.path.isdir(cur):
                shutil.rmtree(cur)
            shutil.copytree(new, cur)
        t = run_unit_tests()
        if t["status"] == "fail":
            raise RuntimeError("tests failed after update: " + t["detail"])
        with open(SHA_FILE, "w") as f:                                        # record only once it's proven good
            f.write(target + "\n")
        shutil.rmtree(backup, ignore_errors=True)
        return True, "updated to %s . %s" % (target[:7], t["detail"]), before, target
    except Exception as e:  # noqa: BLE001
        if not touched:
            return False, "update failed before touching anything: %s" % e, before, before
        try:
            for d in ("bridge", "web", "tests"):
                cur = os.path.join(ROOT, d)
                old = os.path.join(backup, d)
                if os.path.isdir(cur):
                    shutil.rmtree(cur)
                if os.path.isdir(old):
                    shutil.copytree(old, cur)
            if old_sha_text is None:
                if os.path.exists(SHA_FILE):
                    os.remove(SHA_FILE)
            else:
                with open(SHA_FILE, "w") as f:
                    f.write(old_sha_text)
            return False, "update rolled back: %s (previous files restored)" % e, before, before
        except Exception as e2:  # noqa: BLE001
            return False, ("UPDATE FAILED AND ROLLBACK FAILED (%s / %s). Your previous files are in %s - copy bridge/, web/ and tests/ back by hand."
                           % (e, e2, backup)), before, local_sha()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def apply_update(log=print):
    """Returns (ok, message, sha_before, sha_after). sha_after == sha_before means nothing changed."""
    try:
        return _git_update(log) if is_git_checkout() else _zip_update(log)
    except Exception as e:  # noqa: BLE001
        return False, "update failed: %s" % e, local_sha(), local_sha()
