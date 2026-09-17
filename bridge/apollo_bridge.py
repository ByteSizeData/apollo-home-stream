#!/usr/bin/env python3
"""
Apollo Home Stream — bridge.

Runs on the gaming PC (the Apollo host). Reads the Steam libraries already on
this machine, serves the web page, and starts games on the host when you press
Launch from any device. Standard library only — no pip installs.

    python bridge/apollo_bridge.py                 # auto-detect Steam, serve on :8777
    python bridge/apollo_bridge.py --port 9000
    python bridge/apollo_bridge.py --steam "D:\\Steam"
    python bridge/apollo_bridge.py --pin 2550        # PIN gate (default 2550; --pin "" turns it off)
    python bridge/apollo_bridge.py --token mysecret  # additionally require X-Apollo-Token on launch
    python bridge/apollo_bridge.py --dry-run         # print what it found, don't serve

Then open  http://<this-pc-name>:8777  from any screen on your network or tailnet.
Launch starts the game here on the host; you then connect with Artemis/Moonlight
to see it. The game never leaves this machine.
"""
import argparse
import glob
import hashlib
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import selfcare  # noqa: E402  (health checks, self-test, self-update)
import steamaccount  # noqa: E402  (who's signed in, hours played, optional Web API)
import travel  # noqa: E402  (ready-to-travel check, the Always-awake switch)
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

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


def steam_path_from_registry():
    """Where Steam says it lives - gaming PCs often keep it on D: or E:, which no fixed list can guess."""
    found = []
    try:
        import winreg
    except ImportError:
        return found
    for root, path, name in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")):
        try:
            with winreg.OpenKey(root, path) as k:
                v = winreg.QueryValueEx(k, name)[0]
            if isinstance(v, str) and v:
                found.append(os.path.normpath(v))
        except OSError:
            continue
    return found


def default_steam_paths():
    sysname = platform.system()
    home = os.path.expanduser("~")
    if sysname == "Windows":
        cands = [
            os.environ.get("STEAM_PATH", ""),
        ] + steam_path_from_registry() + [
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


def parse_appid(v):
    """A Steam app id is a positive integer below 2^31; anything else is 0."""
    try:
        if isinstance(v, bool):
            return 0
        if isinstance(v, float):
            if v != v or v in (float("inf"), float("-inf")) or v != int(v):
                return 0
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        return 0
    return n if 0 < n < 2 ** 31 else 0


def launch_url(url):
    """Open a steam:// URL on this PC (install, rungameid, ...)."""
    if platform.system() == "Windows":
        os.startfile(url)  # noqa: S606 - a steam:// URL, handled by Steam
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", url])
    else:
        subprocess.Popen(["xdg-open", url])


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
    def __init__(self, steam_root, host_name, token, pin=DEFAULT_PIN, steam_key=""):
        self.steam_root = steam_root
        self.account = steamaccount.SteamAccount(steam_root, steam_key)
        self.host_name = host_name
        self.token = token
        self.pin = str(pin or "")
        self.port = 8777
        self.network = True
        self._care_lock = threading.Lock()
        self._travel_lock = threading.Lock()      # the travel check shells out for many seconds: never make health/host wait for it
        self.auto_update = False                  # set by main(): does this run update itself?
        self._cache = ([], 0.0)
        self._sessions = {}          # sha256(token) -> expiry (unix seconds); the token itself is never kept
        self.sessions_path = None    # set by main(): sessions then survive restarts and self-updates
        self.last_launch = 0.0
        self._fails = {}             # client ip -> (wrong_count, locked_until)
        self._lock = threading.Lock()

    # ---- PIN gate -------------------------------------------------------- #
    @property
    def gated(self):
        return bool(self.pin)

    def _prune(self, now):
        """Drop expired sessions and lockouts that have long since lapsed (called under the lock)."""
        for tok in [t for t, exp in self._sessions.items() if exp < now]:
            del self._sessions[tok]
        for ip in [ip for ip, (_, until) in self._fails.items() if until and until < now - 3600]:
            del self._fails[ip]

    @staticmethod
    def _key(token):
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    def _pin_tag(self):
        return hashlib.sha256(("pin:" + self.pin).encode("utf-8")).hexdigest()[:16]

    def load_sessions(self, path):
        """Pick up the sessions an earlier run saved - unless the PIN has changed since, which signs everyone out."""
        self.sessions_path = path
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("pin") == self._pin_tag() and isinstance(d.get("sessions"), dict):
                now = time.time()
                with self._lock:
                    self._sessions.update({k: float(v) for k, v in d["sessions"].items()
                                           if isinstance(k, str) and len(k) == 64 and isinstance(v, (int, float)) and v > now})
        except Exception:  # noqa: BLE001 - no file, junk in it: everyone just enters the PIN again
            pass

    def _save_sessions(self):
        """Called under the lock. Hashes only, in the owner's profile, 0600."""
        if not self.sessions_path:
            return
        try:
            os.makedirs(os.path.dirname(self.sessions_path), exist_ok=True)
            tmp = self.sessions_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"pin": self._pin_tag(), "sessions": self._sessions}, f)
            os.replace(tmp, self.sessions_path)
            try:
                os.chmod(self.sessions_path, 0o600)
            except OSError:
                pass
        except OSError:
            pass

    def session_ok(self, token):
        if not self.gated:
            return True
        if not token:
            return False
        key = self._key(token)
        with self._lock:
            self._prune(time.time())
            exp = self._sessions.get(key)
            if exp is None:
                return False
            if exp < time.time():
                del self._sessions[key]
                return False
            return True

    def try_pin(self, ip, given):
        """Returns ("ok", session_token) | ("bad", tries_left) | ("locked", seconds)."""
        now = time.time()
        with self._lock:
            self._prune(now)
            count, until = self._fails.get(ip, (0, 0.0))
            if now < until:
                return "locked", int(until - now) + 1
            if hmac.compare_digest(str(given or "").encode("utf-8"), self.pin.encode("utf-8")):
                self._fails.pop(ip, None)
                tok = secrets.token_urlsafe(32)
                self._sessions[self._key(tok)] = now + SESSION_DAYS * 86400
                self._save_sessions()
                return "ok", tok
            count += 1
            if count >= FREE_TRIES:
                wait = min(LOCK_MAX_SECONDS, LOCK_BASE_SECONDS * (2 ** (count - FREE_TRIES)))
                self._fails[ip] = (count, now + wait)
                return "locked", wait
            self._fails[ip] = (count, 0.0)
            return "bad", FREE_TRIES - count

    def health(self):
        with self._care_lock:
            h = getattr(self, "_health", None)
            if not h or time.time() - h[1] > 30:
                data = selfcare.health(self, port=self.port, network=self.network, serving=True)
                try:
                    data["checks"][3:3] = self.extra_checks()          # right after the steam row
                    data["status"] = selfcare._rollup(data["checks"])
                except Exception:  # noqa: BLE001
                    pass
                h = (data, time.time())                        # stamped when it FINISHED, so a slow check isn't instantly stale
                self._health = h
            return h[0]

    def update_info(self):
        with self._care_lock:
            u = getattr(self, "_update", None)
            if not u or time.time() - u[1] > 3600:
                data = selfcare.update_status() if self.network else {"available": None, "local": selfcare.local_sha(), "remote": None, "error": "network checks off"}
                u = (data, time.time())
                self._update = u
            return u[0]

    def end_session(self, token):
        with self._lock:
            self._sessions.pop(self._key(token), None)
            self._save_sessions()

    def games(self, max_age=15.0):
        cached, when = self._cache
        if time.time() - when > max_age:
            cached = scan_games(self.steam_root) if self.steam_root else []
            try:
                self.account.enrich(cached)
            except Exception as e:  # noqa: BLE001 - account data is a bonus, never a blocker
                print("steam account: couldn't read playtime (%s)" % e)
            self._cache = (cached, time.time())
        return cached

    def safe_account_info(self):
        try:
            return self.account.info()
        except Exception as e:  # noqa: BLE001
            print("steam account: couldn't read the sign-in (%s)" % e)
            return {"connected": False, "persona": "", "account": "", "steamid": "", "avatar": "", "source": "none", "web": bool(self.account.key)}

    def account_view(self):
        info = self.account.info()
        installed = {g["appid"] for g in self.games()}
        info["also_owned"] = self.account.owned_not_installed(installed)      # past-played games need no key
        return info

    def extra_checks(self):
        info = self.safe_account_info()
        checks = [selfcare._c("steam account", info["connected"], ("signed in as %s" % info["persona"]) if info["connected"] else "no Steam login found on this PC (open Steam and sign in once)", fail=False)]
        if self.account.key:
            self.account.web()
            err = self.account.web_error
            checks.append(selfcare._c("steam web api", not err, "library and avatar fetched" if not err else "key set but the call failed: " + err, fail=False))
        return checks

    def awake(self, fresh=False):
        a = getattr(self, "_awake", None)                    # no lock: two powercfg reads at worst, and nothing queues behind a slow check
        if fresh or not a or time.time() - a[1] > 30:
            a = (travel.awake_state(), time.time())
            self._awake = a
        return a[0]

    def set_awake(self, mode):
        state = travel.set_awake(mode)
        self._awake = (state, time.time())
        self._preflight = None                               # plain assignments: never wait behind a running travel check
        return state

    def preflight(self, fresh=False):
        with self._travel_lock:
            p = getattr(self, "_preflight", None)
            if not p or time.time() - p[1] > (5 if fresh else 60):       # "fresh" still can't be hammered: one real run per 5 s
                p = (travel.preflight(self, network=self.network), time.time())
                self._preflight = p
            return p[0]

    def host(self):
        awake = self.awake()
        return {
            "name": self.host_name,
            "platform": platform.system(),
            "steam_path": self.steam_root,
            "libraries": library_folders(self.steam_root) if self.steam_root else [],
            "games": len(self.games()),
            "awake": awake["never_sleeps"],
            "awake_state": awake,
            "time": int(time.time()),
        }


def make_handler(bridge):
    class Handler(SimpleHTTPRequestHandler):
        timeout = 30          # a phone that dropped off mid-connection must not hold a socket (and a thread) forever

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
            except ValueError:
                return None
            if n < 0 or n > 4096:
                return None
            try:
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
                return self._json(200, {"games": bridge.games(), "host": bridge.host_name, "account": bridge.safe_account_info()})
            if path == "/api/account":
                try:
                    return self._json(200, bridge.account_view())
                except Exception as e:  # noqa: BLE001
                    self.log_message("account view failed: %s", e)
                    return self._json(200, dict(bridge.safe_account_info(), also_owned=[]))
            if path == "/api/host":
                return self._json(200, bridge.host())
            if path == "/api/health":
                return self._json(200, bridge.health())
            if path == "/api/preflight":
                fresh = parse_qs(urlparse(self.path).query).get("fresh", [""])[0] == "1"
                return self._json(200, bridge.preflight(fresh=fresh))
            if path == "/api/version":
                return self._json(200, {"version": selfcare.VERSION, "sha": (selfcare.local_sha() or "")[:7], "update": bridge.update_info(), "auto": bool(bridge.auto_update)})
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
            appid = parse_appid(parse_qs(urlparse(self.path).query).get("play", [""])[0])       # same validator as /api/launch: never raises
            loc = "/pin" + ("?play=%d" % appid if appid else "")                                    # digits only, nothing else rides along
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
            if path == "/api/awake":
                body = self._read_json()
                mode = body.get("mode") if isinstance(body, dict) else None
                if mode not in ("awake", "sleep"):
                    return self._json(400, {"error": "mode must be awake or sleep"})
                state = bridge.set_awake(mode)
                self.log_message("sleep mode set to %s (holding awake: %s)", mode, state["holding"])
                return self._json(200, dict(state, ok=True))
            if path != "/api/launch":
                return self._json(404, {"error": "not found"})
            if bridge.token and not hmac.compare_digest(self.headers.get("X-Apollo-Token", ""), bridge.token):
                return self._json(403, {"error": "bad token"})
            body = self._read_json()
            appid = parse_appid(body.get("appid") if isinstance(body, dict) else None)
            if not appid:
                return self._json(400, {"error": "appid required"})
            action = (body.get("action") if isinstance(body, dict) else None) or "run"
            if action == "install":
                if not bridge.account.owns(appid):
                    return self._json(403, {"error": "that isn't in your Steam library"})
                try:
                    launch_url("steam://install/%d" % appid)
                except Exception as e:  # noqa: BLE001
                    self.log_message("install of %d failed: %s", appid, e)
                    return self._json(500, {"error": "the host couldn't start the install - check the bridge window"})
                return self._json(200, {"ok": True, "appid": appid, "action": "install"})
            known = {g["appid"]: g for g in bridge.games()}
            if appid not in known:  # only launch what's actually installed here
                return self._json(404, {"error": "not installed on this host"})
            try:
                launch_appid(appid)
                bridge.last_launch = time.time()
            except Exception as e:  # noqa: BLE001
                self.log_message("launch of %d failed: %s", appid, e)
                return self._json(500, {"error": "the host couldn't start it — check the bridge window"})
            return self._json(200, {"ok": True, "appid": appid, "title": known[appid]["title"]})

    return Handler


class Server(ThreadingHTTPServer):
    """Refuses to start on a port another bridge holds - on Windows too, where the stdlib default would bind over it."""
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        selfcare.exclusive_bind_options(self.socket)
        # Not HTTPServer.server_bind(): it calls socket.getfqdn(), a reverse-DNS lookup that stalls for ~45 s on
        # some Windows machines - the bridge would be deaf for most of a minute after every boot.
        import socketserver
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host or "localhost"
        self.server_port = port

    def handle_error(self, request, client_address):
        """A screen that gave up waiting (closed tab, timeout, Wi-Fi drop) is not an error worth a traceback."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError, BrokenPipeError)):
            try:
                print("%s  client %s went away (%s)" % (time.strftime("%H:%M:%S"), client_address[0], type(exc).__name__))
            except Exception:  # noqa: BLE001
                pass
            return
        super().handle_error(request, client_address)


def bridge_addresses(port):
    """Every way a device on the network can reach this bridge - hostname first, then the LAN IP."""
    out = []
    host = socket.gethostname()
    if host:
        out.append("http://%s:%d" % (host if "." in host or platform.system() != "Darwin" else host + ".local", port))
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))            # no packets sent; just picks the outbound interface
        ip = s.getsockname()[0]; s.close()
        if ip and not ip.startswith("127."):
            out.append("http://%s:%d" % (ip, port))
    except OSError:
        pass
    return out or ["http://localhost:%d" % port]


def restart(new_sha, supervised=False):
    """Run the new code. Under the installer's startup task (--supervised) simply exit: the task's every-minute
    watchdog starts a fresh process, and nothing of the old one lingers. Otherwise re-run with the same arguments;
    on Windows os.execv mangles quoting, so spawn - and make this process an inert parent first."""
    if supervised:
        print("exiting - the startup task brings the new version up within a minute")
        sys.stdout.flush()
        sys.exit(0)
    env = dict(os.environ, APOLLO_RESTARTED_FOR=new_sha or "")   # one restart per version, never a loop
    argv = [sys.executable] + sys.argv
    if os.name == "nt":
        travel.HOLD.set(False)                                   # the child takes its own; ours would outlive "let it sleep"
        for stream in (sys.stdout, sys.stderr):                  # let go of the log so the child can rotate it
            try:
                if getattr(stream, "name", "") not in ("<stdout>", "<stderr>"):
                    stream.close()
            except Exception:  # noqa: BLE001
                pass
        sys.stdout = sys.stderr = open(os.devnull, "w")
        sys.exit(subprocess.call(argv, env=env))
    os.execve(sys.executable, argv, env)


UPDATE_EVERY = 6 * 3600             # while running, look for a new version this often (startup checks too)
QUIET_AFTER_LAUNCH = 30 * 60        # never restart under someone who just pressed Stream


def auto_update_once(bridge):
    """Apply an available update if nobody launched a game recently. Returns the new commit to restart into, else None."""
    if time.time() - bridge.last_launch < QUIET_AFTER_LAUNCH:
        return None
    u = selfcare.update_status()
    if not u.get("available") or os.environ.get("APOLLO_RESTARTED_FOR") == (u.get("remote") or ""):
        return None
    print("%s  update available - applying..." % time.strftime("%H:%M:%S"))
    ok, msg, before, after = selfcare.apply_update()
    print(msg)
    return after if ok and after and after != before else None


def auto_update_loop(bridge, srv, state, every=UPDATE_EVERY):
    """The startup check only helps a PC that restarts. This one keeps a PC that runs for months current."""
    while True:
        time.sleep(every)
        try:
            new = auto_update_once(bridge)
        except Exception as e:  # noqa: BLE001 - an update problem must never take the bridge down
            print("auto-update: %s" % e)
            continue
        if new:
            state["restart_to"] = new
            srv.shutdown()                                   # main thread closes the port, then restarts into the new code
            return


def setup_logging(path):
    """Send everything the bridge prints to a file. Essential under pythonw.exe (the hidden logon task), where
    sys.stdout/sys.stderr are None and http.server would crash the first time it tried to log a request."""
    if path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            try:
                if os.path.exists(path) and os.path.getsize(path) > 5 * 1024 * 1024:  # keep one previous log
                    os.replace(path, path + ".1")
            except OSError:
                pass                                                                    # someone has it open: keep appending, never go silent
            f = open(path, "a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stderr = f
            print("---- bridge started %s ----" % time.strftime("%Y-%m-%d %H:%M:%S"))
            return
        except OSError:
            pass
    if sys.stdout is None or sys.stderr is None:                                        # pythonw with no --log: never crash on print
        sink = open(os.devnull, "w")
        sys.stdout = sys.stdout or sink
        sys.stderr = sys.stderr or sink


def _console_safe():
    """Windows consoles/redirects may be cp1252; never let a stray character kill the bridge."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main():
    _console_safe()
    ap = argparse.ArgumentParser(description="Apollo Home Stream bridge")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--bind", default="0.0.0.0", help="0.0.0.0 = reachable from other devices")
    ap.add_argument("--steam", help="Steam install folder (auto-detected if omitted)")
    ap.add_argument("--name", default=socket.gethostname(), help="how this PC is shown in the page")
    ap.add_argument("--self-test", action="store_true", help="check Steam, Apollo, ports, network and run the unit tests, then exit")
    ap.add_argument("--no-network", action="store_true", help="skip internet checks and the update check")
    ap.add_argument("--allow-missing-steam", action="store_true", help="self-test: a missing Steam folder is a warning, not a failure")
    ap.add_argument("--check-update", action="store_true", help="say whether a newer version is on GitHub, then exit")
    ap.add_argument("--update", action="store_true", help="update this copy from GitHub (git pull, or a verified zip), run the tests, then exit")
    ap.add_argument("--auto-update", action="store_true", default=os.environ.get("APOLLO_AUTO_UPDATE") == "1",
                    help="at startup, apply any available update and restart (or APOLLO_AUTO_UPDATE=1)")
    ap.add_argument("--preflight", action="store_true", help="ready to travel? check sleep, Tailscale, restarts, Steam sign-in; exit 1 if not ready")
    ap.add_argument("--set-awake", choices=["on", "off"], default=None,
                    help="on = the bridge keeps this PC from sleeping whenever it runs; off = let it sleep. Saved, then exit")
    ap.add_argument("--supervised", action="store_true",
                    help="something restarts this bridge whenever it exits (the installer's startup task): after an update, just exit")
    ap.add_argument("--log", default="", help="write output to this file (used by the Windows logon task, which runs hidden)")
    ap.add_argument("--steam-key", default="",
                    help="Steam Web API key for this run only (steamcommunity.com/dev/apikey). To keep it: --set-steam-key")
    ap.add_argument("--set-steam-key", metavar="KEY", default=None,
                    help="store your Steam Web API key on this PC (~/.apollo-home-stream/config.json) and exit; the bridge uses it from then on")
    ap.add_argument("--forget-steam-key", action="store_true", help="remove the stored Steam Web API key and exit")
    ap.add_argument("--pin", default=os.environ.get("APOLLO_PIN", DEFAULT_PIN),
                    help='4-digit (or longer) PIN needed to open the page and launch games; --pin "" disables')
    ap.add_argument("--token", default=os.environ.get("APOLLO_TOKEN", ""), help="additionally require this X-Apollo-Token header to launch")
    ap.add_argument("--dry-run", action="store_true", help="scan and print, then exit")
    args = ap.parse_args()
    setup_logging(args.log)
    if args.set_steam_key is not None:
        key = args.set_steam_key.strip()
        if not steamaccount.looks_like_key(key):
            print("that doesn't look like a Steam Web API key (32 hex characters) - get one at https://steamcommunity.com/dev/apikey")
            sys.exit(2)
        path = steamaccount.save_config(steam_key=key)
        print("Steam Web API key saved to %s - start the bridge normally and your library stays in sync with Steam." % path)
        sys.exit(0)
    if args.forget_steam_key:
        steamaccount.save_config(steam_key="")
        print("Steam Web API key removed."); sys.exit(0)
    if args.set_awake is not None:
        travel.set_awake("awake" if args.set_awake == "on" else "sleep", apply_now=False)
        print("Saved: the bridge will %s." % ("keep this PC awake whenever it runs" if args.set_awake == "on" else "let this PC sleep"))
        sys.exit(0)


    steam = args.steam or (default_steam_paths() or [None])[0]
    if not steam:
        print("Couldn't find Steam. Pass --steam \"C:\\Path\\To\\Steam\".", file=sys.stderr)
        if not args.dry_run:
            print("Serving the page anyway with an empty library.", file=sys.stderr)
    steam_key = args.steam_key or os.environ.get("APOLLO_STEAM_KEY", "") or steamaccount.load_config().get("steam_key", "")
    bridge = Bridge(steam, args.name, args.token, pin=args.pin, steam_key=steam_key)
    bridge.account.offline = args.no_network
    if steam_key and not args.no_network:
        bridge.account.start_auto_refresh()
    bridge.port = args.port
    bridge.network = not args.no_network

    if args.self_test:
        rep = selfcare.self_test(bridge, port=args.port, network=not args.no_network, allow_missing_steam=args.allow_missing_steam)
        try:
            rep["checks"][3:3] = bridge.extra_checks()
        except Exception as e:  # noqa: BLE001
            rep["checks"].insert(3, selfcare._c("steam account", False, "couldn't read the sign-in (%s)" % e, fail=False))
        rep["status"] = selfcare._rollup(rep["checks"])
        selfcare.print_report(rep)
        sys.exit(0 if rep["status"] != "fail" else 1)
    if args.preflight:
        rep = travel.preflight(bridge, network=not args.no_network)
        travel.print_preflight(rep)
        sys.exit(0 if rep["ready"] else 1)
    if args.check_update:
        if args.no_network:
            print("--no-network given; not checking"); sys.exit(0)
        u = selfcare.update_status()
        if u["error"]:
            print(u["error"]); sys.exit(2)
        print("update available: %s -> %s  (run with --update)" % ((u["local"] or "?")[:7], u["remote"][:7]) if u["available"]
              else "up to date (%s)" % (u["local"] or "?")[:7])
        sys.exit(0)
    if args.update:
        ok, msg, _, _ = selfcare.apply_update()
        print(msg)
        sys.exit(0 if ok else 1)
    if not args.dry_run:
        busy = selfcare.check_port(args.port)                # first: a second copy must not check for updates, let alone apply one
        if busy["status"] != "ok":
            print("port %d is %s" % (args.port, busy["detail"]))
            print("stop the other one, or start this one with --port 8778")
            sys.exit(1)
    if not args.no_network:
        u = selfcare.update_status(timeout=3)
        if u["available"]:
            if args.auto_update and os.environ.get("APOLLO_RESTARTED_FOR") != (u["remote"] or ""):
                print("update available - applying (auto-update on)...")
                ok, msg, before, after = selfcare.apply_update()
                print(msg)
                if ok and after and after != before:
                    print("restarting with the new version...")
                    restart(after, supervised=args.supervised)  # never re-exec unless the code actually changed
            elif u["available"]:
                print("update available: %s -> %s  - run  python bridge/apollo_bridge.py --update" % ((u["local"] or "?")[:7], u["remote"][:7]))

    if args.dry_run:
        print("Steam:", steam)
        try:
            u = bridge.account.user()
        except Exception:  # noqa: BLE001
            u = None
        print("Account:", ("%s (%s)" % (u["persona"], u["account"])) if u else "nobody signed in on this PC",
              "- Web API key set" if args.steam_key else "- no Web API key (optional)")
        for lib in bridge.host()["libraries"]:
            print("Library:", lib)
        for g in bridge.games():
            when = time.strftime("%Y-%m-%d", time.localtime(g["last_played"])) if g["last_played"] else "never"
            print("%8d  %-40s %6.1f h  last played %s  %.1f GB" % (g["appid"], g["title"][:40], g.get("hours", 0), when, g["size_bytes"] / 1e9))
        print("%d games." % len(bridge.games()))
        return

    if not os.path.isdir(WEB_DIR):
        print("web/ folder not found next to bridge/ — page won't load.", file=sys.stderr)

    bridge.load_sessions(os.path.join(os.path.dirname(steamaccount.CONFIG_PATH), "sessions.json"))   # a restart doesn't sign every screen out
    travel.resume_awake()
    srv = Server((args.bind, args.port), make_handler(bridge))
    for addr in bridge_addresses(args.port):
        print("Apollo bridge on %s" % addr)
    print("Shown as \"%s\"  (Steam: %s, %d games)" % (args.name, steam, len(bridge.games())))
    print("PIN gate: %s" % ("on" if bridge.gated else "OFF - anyone who can reach this port can launch games"))
    print("Steam sync: %s" % ("on - library and stats refresh from Steam every %d min" % (steamaccount.SteamAccount.REFRESH_EVERY // 60)
                              if steam_key and not args.no_network else "off - showing this PC's own record (run --set-steam-key KEY to sync)"))
    try:
        ts = selfcare.check_tailscale()
        if ts["status"] == "ok":
            name = ts["detail"].split(" / ")[-1].strip() or ts["detail"].split(" / ")[0].strip()
            print("Away from home (Tailscale): http://%s:%d   <- use THIS address on phones and laptops; it works at home too" % (name, args.port))
        else:
            print("Tailscale: %s" % ts["detail"])
    except Exception:  # noqa: BLE001 - purely informational
        pass
    print("Sleep: %s" % ("the bridge keeps this PC awake" if travel.HOLD.held else "this PC follows its own sleep settings (Sleep & wake tab to change)"))
    print("Open that address from any device on your network or tailnet. Ctrl-C to stop.")
    state = {}
    bridge.auto_update = bool(args.auto_update and not args.no_network)
    if bridge.auto_update:
        threading.Thread(target=auto_update_loop, args=(bridge, srv, state), daemon=True, name="auto-update").start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    srv.server_close()
    if state.get("restart_to"):
        print("restarting with the new version...")
        restart(state["restart_to"], supervised=args.supervised)


if __name__ == "__main__":
    main()
