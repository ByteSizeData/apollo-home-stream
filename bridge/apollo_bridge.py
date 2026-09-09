#!/usr/bin/env python3
"""
Apollo Home Stream — bridge.

Runs on the gaming PC (the Apollo host). Reads the Steam libraries already on
this machine, serves the web page, and starts games on the host when you press
Launch from any device. Standard library only — no pip installs.

    python apollo_bridge.py                 # auto-detect Steam, serve on :8777
    python apollo_bridge.py --port 9000
    python apollo_bridge.py --steam "D:\\Steam"
    python apollo_bridge.py --pin 2550        # PIN gate (default 2550; --pin "" turns it off)
    python apollo_bridge.py --token mysecret  # additionally require X-Apollo-Token on launch
    python apollo_bridge.py --dry-run         # print what it found, don't serve

Then open  http://<this-pc-name>:8777  from any screen on your network or tailnet.
Launch starts the game here on the host; you then connect with Artemis/Moonlight
to see it. The game never leaves this machine.
"""
import argparse
import glob
import hmac
import json
import os
import platform
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(os.path.dirname(HERE), "web")

# Things that show up as "apps" in Steam but aren't games you'd launch.
NOT_GAMES = re.compile(
    r"(steamworks common redistributables|proton|steam linux runtime|"
    r"steamvr|directx|vc redist|\.net framework|dedicated server)",
    re.I,
)
STATE_FULLY_INSTALLED = 4

DEFAULT_PIN = "2550"
SESSION_COOKIE = "apollo_session"
SESSION_DAYS = 30
FREE_TRIES = 5            # wrong guesses before the first timeout
LOCK_BASE_SECONDS = 30    # first timeout; doubles each further wrong guess, capped below
LOCK_MAX_SECONDS = 900


# --------------------------------------------------------------------------- #
# Steam's KeyValues (VDF) text format:  "key" "value"   or   "key" { ... }
# --------------------------------------------------------------------------- #
def parse_vdf(text):
    """Parse a VDF/ACF document into nested dicts. Values stay strings."""
    tokens = re.findall(r'"((?:\\.|[^"\\])*)"|(\{)|(\})', text)
    pos = 0

    def parse_block():
        nonlocal pos
        out = {}
        while pos < len(tokens):
            s, open_b, close_b = tokens[pos]
            pos += 1
            if close_b:
                return out
            if open_b:
                continue  # stray brace; tolerate
            key = s.replace('\\"', '"').replace("\\\\", "\\")
            if pos >= len(tokens):
                break
            s2, open2, close2 = tokens[pos]
            pos += 1
            if open2:
                out[key] = parse_block()
            elif close2:
                out[key] = ""
                return out
            else:
                out[key] = s2.replace('\\"', '"').replace("\\\\", "\\")
        return out

    return parse_block()


def default_steam_paths():
    sysname = platform.system()
    home = os.path.expanduser("~")
    if sysname == "Windows":
        cands = [
            os.environ.get("STEAM_PATH", ""),
            os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Steam"),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Steam"),
            r"C:\Steam", r"D:\Steam", r"D:\SteamLibrary", r"E:\SteamLibrary",
        ]
    elif sysname == "Darwin":
        cands = [os.path.join(home, "Library", "Application Support", "Steam")]
    else:
        cands = [
            os.path.join(home, ".steam", "steam"),
            os.path.join(home, ".local", "share", "Steam"),
            os.path.join(home, ".var", "app", "com.valvesoftware.Steam", ".local", "share", "Steam"),
        ]
    return [c for c in cands if c and os.path.isdir(c)]


def library_folders(steam_root):
    """Every Steam library on this machine, starting with the root install."""
    libs = [steam_root]
    vdf = os.path.join(steam_root, "steamapps", "libraryfolders.vdf")
    if os.path.isfile(vdf):
        try:
            with open(vdf, encoding="utf-8", errors="replace") as f:
                data = parse_vdf(f.read())
        except OSError:
            data = {}
        folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
        for _, entry in sorted(folders.items()):
            path = entry.get("path") if isinstance(entry, dict) else entry
            if path and os.path.isdir(path) and path not in libs:
                libs.append(path)
    return libs


def read_manifest(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = parse_vdf(f.read())
    except OSError:
        return None
    app = data.get("AppState") or {}
    try:
        appid = int(app.get("appid", 0))
    except ValueError:
        return None
    name = app.get("name", "").strip()
    if not appid or not name or NOT_GAMES.search(name):
        return None
    try:
        state = int(app.get("StateFlags", 0))
    except ValueError:
        state = 0
    if not state & STATE_FULLY_INSTALLED:
        return None

    def as_int(k):
        try:
            return int(app.get(k, 0))
        except ValueError:
            return 0

    return {
        "appid": appid,
        "title": name,
        "last_played": as_int("LastPlayed"),          # unix seconds, 0 = never
        "size_bytes": as_int("SizeOnDisk"),
        "install_dir": app.get("installdir", ""),
        "cover": "https://cdn.cloudflare.steamstatic.com/steam/apps/%d/library_600x900.jpg" % appid,
        "header": "https://cdn.cloudflare.steamstatic.com/steam/apps/%d/header.jpg" % appid,
    }


def scan_games(steam_root):
    games, seen = [], set()
    for lib in library_folders(steam_root):
        for manifest in glob.glob(os.path.join(lib, "steamapps", "appmanifest_*.acf")):
            g = read_manifest(manifest)
            if g and g["appid"] not in seen:
                seen.add(g["appid"])
                g["library"] = lib
                games.append(g)
    games.sort(key=lambda g: (-g["last_played"], g["title"].lower()))
    return games


def launch_appid(appid):
    """Ask Steam on this machine to start the game."""
    url = "steam://rungameid/%d" % appid
    sysname = platform.system()
    if sysname == "Windows":
        os.startfile(url)  # type: ignore[attr-defined]
    elif sysname == "Darwin":
        subprocess.Popen(["open", url])
    else:
        subprocess.Popen(["xdg-open", url])


# --------------------------------------------------------------------------- #
class Bridge:
    def __init__(self, steam_root, host_name, token, pin=DEFAULT_PIN):
        self.steam_root = steam_root
        self.host_name = host_name
        self.token = token
        self.pin = str(pin or "")
        self._cache = ([], 0.0)
        self._sessions = {}          # token -> expiry (unix seconds)
        self._fails = {}             # client ip -> (wrong_count, locked_until)
        self._lock = threading.Lock()

    # ---- PIN gate -------------------------------------------------------- #
    @property
    def gated(self):
        return bool(self.pin)

    def session_ok(self, token):
        if not self.gated:
            return True
        if not token:
            return False
        with self._lock:
            exp = self._sessions.get(token)
            if exp is None:
                return False
            if exp < time.time():
                del self._sessions[token]
                return False
            return True

    def try_pin(self, ip, given):
        """Returns ("ok", session_token) | ("bad", tries_left) | ("locked", seconds)."""
        now = time.time()
        with self._lock:
            count, until = self._fails.get(ip, (0, 0.0))
            if now < until:
                return "locked", int(until - now) + 1
            if hmac.compare_digest(str(given or ""), self.pin):
                self._fails.pop(ip, None)
                tok = secrets.token_urlsafe(32)
                self._sessions[tok] = now + SESSION_DAYS * 86400
                return "ok", tok
            count += 1
            if count >= FREE_TRIES:
                wait = min(LOCK_MAX_SECONDS, LOCK_BASE_SECONDS * (2 ** (count - FREE_TRIES)))
                self._fails[ip] = (count, now + wait)
                return "locked", wait
            self._fails[ip] = (count, 0.0)
            return "bad", FREE_TRIES - count

    def end_session(self, token):
        with self._lock:
            self._sessions.pop(token, None)

    def games(self, max_age=15.0):
        cached, when = self._cache
        if time.time() - when > max_age:
            cached = scan_games(self.steam_root) if self.steam_root else []
            self._cache = (cached, time.time())
        return cached

    def host(self):
        return {
            "name": self.host_name,
            "platform": platform.system(),
            "steam_path": self.steam_root,
            "libraries": library_folders(self.steam_root) if self.steam_root else [],
            "games": len(self.games()),
            "awake": True,
            "time": int(time.time()),
        }


def make_handler(bridge):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=WEB_DIR, **kw)

        def log_message(self, fmt, *args):
            sys.stdout.write("%s  %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

        def _json(self, code, obj, extra_headers=()):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            for k, v in extra_headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _client_ip(self):
            return self.client_address[0]

        def _session_token(self):
            raw = self.headers.get("Cookie", "")
            if not raw:
                return None
            c = SimpleCookie()
            try:
                c.load(raw)
            except Exception:  # noqa: BLE001 - malformed cookie header
                return None
            m = c.get(SESSION_COOKIE)
            return m.value if m else None

        def _authed(self):
            return bridge.session_ok(self._session_token())

        def _send_file(self, name, code=200):
            path = os.path.join(WEB_DIR, name)
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                return self._json(500, {"error": "%s missing from web/" % name})
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n > 4096:
                    return None
                return json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return None

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/pin", "/pin.html"):
                return self._send_file("pin.html", 200)
            if not self._authed():
                if path.startswith("/api/"):
                    return self._json(401, {"error": "pin required"})
                return self._redirect_to_pin(path)        # any page: go to the PIN screen
            if path == "/api/games":
                return self._json(200, {"games": bridge.games(), "host": bridge.host_name})
            if path == "/api/host":
                return self._json(200, bridge.host())
            if path == "/":
                self.path = "/index.html"
            return super().do_GET()

        def do_HEAD(self):
            # SimpleHTTPRequestHandler would happily HEAD any file; keep it behind the gate.
            if not self._authed():
                self.send_response(302)
                self.send_header("Location", "/pin")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            return super().do_HEAD()

        def _redirect_to_pin(self, path):
            nxt = path if (path.startswith("/") and not path.startswith("//") and path not in ("/pin", "/pin.html")) else "/"
            loc = "/pin" if nxt == "/" else "/pin?next=" + quote(nxt, safe="/")
            self.send_response(302)
            self.send_header("Location", loc)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            path = urlparse(self.path).path

            if path == "/api/pin":
                if not bridge.gated:
                    return self._json(200, {"ok": True, "gated": False})
                body = self._read_json()
                given = body.get("pin") if isinstance(body, dict) else None
                if not isinstance(given, (str, int)):
                    return self._json(400, {"error": "pin required"})
                status, val = bridge.try_pin(self._client_ip(), given)
                if status == "ok":
                    cookie = "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict" % (
                        SESSION_COOKIE, val, SESSION_DAYS * 86400)
                    return self._json(200, {"ok": True}, [("Set-Cookie", cookie)])
                if status == "locked":
                    return self._json(429, {"error": "too many tries", "retry_after": val},
                                      [("Retry-After", str(val))])
                return self._json(401, {"error": "wrong pin", "left": val})

            if path == "/api/logout":
                bridge.end_session(self._session_token())
                cookie = "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict" % SESSION_COOKIE
                return self._json(200, {"ok": True}, [("Set-Cookie", cookie)])

            if not self._authed():
                return self._json(401, {"error": "pin required"})
            if path != "/api/launch":
                return self._json(404, {"error": "not found"})
            if bridge.token and not hmac.compare_digest(self.headers.get("X-Apollo-Token", ""), bridge.token):
                return self._json(403, {"error": "bad token"})
            body = self._read_json()
            try:
                appid = int(body.get("appid", 0)) if isinstance(body, dict) else 0
            except (TypeError, ValueError):
                appid = 0
            if not appid:
                return self._json(400, {"error": "appid required"})
            known = {g["appid"]: g for g in bridge.games()}
            if appid not in known:  # only launch what's actually installed here
                return self._json(404, {"error": "not installed on this host"})
            try:
                launch_appid(appid)
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": str(e)})
            return self._json(200, {"ok": True, "appid": appid, "title": known[appid]["title"]})

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Apollo Home Stream bridge")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--bind", default="0.0.0.0", help="0.0.0.0 = reachable from other devices")
    ap.add_argument("--steam", help="Steam install folder (auto-detected if omitted)")
    ap.add_argument("--name", default=socket.gethostname(), help="how this PC is shown in the page")
    ap.add_argument("--pin", default=os.environ.get("APOLLO_PIN", DEFAULT_PIN),
                    help='4-digit (or longer) PIN needed to open the page and launch games; --pin "" disables')
    ap.add_argument("--token", default=os.environ.get("APOLLO_TOKEN", ""), help="additionally require this X-Apollo-Token header to launch")
    ap.add_argument("--dry-run", action="store_true", help="scan and print, then exit")
    args = ap.parse_args()

    steam = args.steam or (default_steam_paths() or [None])[0]
    if not steam:
        print("Couldn't find Steam. Pass --steam \"C:\\Path\\To\\Steam\".", file=sys.stderr)
        if not args.dry_run:
            print("Serving the page anyway with an empty library.", file=sys.stderr)
    bridge = Bridge(steam, args.name, args.token, pin=args.pin)

    if args.dry_run:
        print("Steam:", steam)
        for lib in bridge.host()["libraries"]:
            print("Library:", lib)
        for g in bridge.games():
            when = time.strftime("%Y-%m-%d", time.localtime(g["last_played"])) if g["last_played"] else "never"
            print("%8d  %-40s last played %s  %.1f GB" % (g["appid"], g["title"][:40], when, g["size_bytes"] / 1e9))
        print("%d games." % len(bridge.games()))
        return

    if not os.path.isdir(WEB_DIR):
        print("web/ folder not found next to bridge/ — page won't load.", file=sys.stderr)

    srv = ThreadingHTTPServer((args.bind, args.port), make_handler(bridge))
    print("Apollo bridge on http://%s:%d  (Steam: %s, %d games)" % (args.name, args.port, steam, len(bridge.games())))
    print("PIN gate: %s" % ("on" if bridge.gated else "OFF — anyone who can reach this port can launch games"))
    print("Open that address from any device on your network or tailnet. Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
