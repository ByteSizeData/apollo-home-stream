"""Your Steam account, two ways.

Tier 1 - local, private, offline: Steam already stores who is signed in and how long you've played
each game, on this PC. We read that. Nothing is sent anywhere.
Tier 2 - optional Web API key: adds games you own but haven't installed, and your avatar. The key
stays on this PC and is never sent to the page. Works with a private profile because it's YOUR key.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

STEAMID64_BASE = 76561197960265728
WEB_TIMEOUT = 6
CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps/%d/library_600x900.jpg"
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache")


def parse_vdf(text):
    from apollo_bridge import parse_vdf as _p  # same parser the library scanner uses
    return _p(text)


def _ci(d, *keys):
    """Case-insensitive nested lookup: _ci(d, 'Software', 'Valve') -> d['software']['valve'] whatever the case."""
    for k in keys:
        if not isinstance(d, dict):
            return None
        hit = next((v for kk, v in d.items() if kk.lower() == k.lower()), None)
        if hit is None:
            return None
        d = hit
    return d


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ----------------------------------------------------------------------------- the key, stored on the PC
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".apollo-home-stream", "config.json")


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_config(**values):
    d = load_config()
    d.update({k: v for k, v in values.items() if v is not None})
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)               # your eyes only (no-op on Windows)
    except OSError:
        pass
    return CONFIG_PATH


def looks_like_key(k):
    return isinstance(k, str) and len(k) == 32 and all(c in "0123456789ABCDEFabcdef" for c in k)


# ----------------------------------------------------------------------------- tier 1: local
def login_users(steam_root):
    """Accounts that have signed in on this PC, most recent first."""
    path = os.path.join(steam_root or "", "config", "loginusers.vdf")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = parse_vdf(f.read(4 * 1024 * 1024))
    except Exception:  # noqa: BLE001 - missing, unreadable, absurdly nested: all mean "no account", never a crash
        return []
    users = _ci(data, "users")
    if not isinstance(users, dict):
        return []
    out = []
    for sid, info in users.items():
        if not str(sid).isdigit() or not isinstance(info, dict):
            continue
        out.append({
            "steamid": str(sid),
            "account": _ci(info, "AccountName") or "",
            "persona": _ci(info, "PersonaName") or _ci(info, "AccountName") or "",
            "most_recent": _ci(info, "MostRecent") == "1",
            "timestamp": _int(_ci(info, "Timestamp")),
        })
    out.sort(key=lambda u: (not u["most_recent"], -u["timestamp"]))
    return out


def current_user(steam_root):
    users = login_users(steam_root)
    return users[0] if users else None


def account_id(steamid64):
    return int(steamid64) - STEAMID64_BASE


def local_playtime(steam_root, steamid64):
    """{appid: {'minutes': int, 'last_played': int}} from this user's localconfig.vdf."""
    try:
        path = os.path.join(steam_root or "", "userdata", str(account_id(steamid64)), "config", "localconfig.vdf")
        with open(path, encoding="utf-8", errors="replace") as f:
            data = parse_vdf(f.read(64 * 1024 * 1024))
    except Exception:  # noqa: BLE001
        return {}
    apps = _ci(data, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps")
    if not isinstance(apps, dict):
        return {}
    out = {}
    for appid, info in apps.items():
        if not str(appid).isdigit() or not isinstance(info, dict):
            continue
        out[int(appid)] = {"minutes": _int(_ci(info, "Playtime")), "last_played": _int(_ci(info, "LastPlayed"))}
    return out


# ----------------------------------------------------------------------------- tier 2: web api (optional)
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "refusing redirect (the key is in the URL)", headers, fp)


_opener = urllib.request.build_opener(_NoRedirect())


def _bounded(fn, timeout):
    """Wall-clock bound that covers DNS too (a socket timeout doesn't)."""
    box = {}
    def run():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["e"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if "e" in box:
        raise box["e"]
    if "v" not in box:
        raise TimeoutError("Steam Web API didn't answer within %ss" % timeout)
    return box["v"]


def _web(path, params, key, timeout=WEB_TIMEOUT):
    q = dict(params, key=key, format="json")
    url = "https://api.steampowered.com/%s?%s" % (path, urllib.parse.urlencode(q))
    req = urllib.request.Request(url, headers={"User-Agent": "apollo-home-stream"})
    def go():
        with _opener.open(req, timeout=timeout) as r:
            return json.loads(r.read(8 * 1024 * 1024))
    return _bounded(go, timeout + 1)


def web_summary(key, steamid64):
    d = _web("ISteamUser/GetPlayerSummaries/v2/", {"steamids": steamid64}, key)
    players = (d.get("response") or {}).get("players") or []
    if not players:
        return None
    p = players[0]
    return {"persona": p.get("personaname", ""), "avatar": p.get("avatarfull") or p.get("avatarmedium") or "", "profile": p.get("profileurl", "")}


def web_owned(key, steamid64):
    d = _web("IPlayerService/GetOwnedGames/v1/", {"steamid": steamid64, "include_appinfo": 1, "include_played_free_games": 1}, key)
    games = (d.get("response") or {}).get("games") or []
    return [{"appid": g["appid"], "title": g.get("name", "app %s" % g["appid"]), "minutes": _int(g.get("playtime_forever")),
             "last_played": _int(g.get("rtime_last_played")), "cover": CDN % g["appid"]} for g in games if "appid" in g]


def web_recent(key, steamid64):
    d = _web("IPlayerService/GetRecentlyPlayedGames/v1/", {"steamid": steamid64}, key)
    games = (d.get("response") or {}).get("games") or []
    return {g["appid"]: _int(g.get("playtime_2weeks")) for g in games if "appid" in g}


# ----------------------------------------------------------------------------- app names (store lookups, cached forever)
# Valve retired the keyless "all apps" list, so names come one app at a time from the public store
# endpoint - no key, no account - and are cached on disk permanently (names don't change).
STORE_URL = "https://store.steampowered.com/api/appdetails?appids=%d&filters=basic&l=english"
_names_lock = threading.Lock()
_names_mem = {}          # appid -> name ("" = looked up, unknown)
_names_loaded = False
_names_inflight = set()


def _names_path():
    return os.path.join(CACHE_DIR, "appnames.json")


def _names_load():
    global _names_loaded
    if _names_loaded:
        return
    _names_loaded = True
    try:
        with open(_names_path(), encoding="utf-8") as f:
            for k, v in json.load(f).items():
                _names_mem[int(k)] = v or ""
    except Exception:  # noqa: BLE001
        pass


def _names_save():
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = _names_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in _names_mem.items()}, f)
        os.replace(tmp, _names_path())
    except Exception:  # noqa: BLE001
        pass


def _store_name(appid):
    """Name of one app from the public store, or None if it can't be had right now."""
    req = urllib.request.Request(STORE_URL % appid, headers={"User-Agent": "apollo-home-stream"})
    def go():
        with _opener.open(req, timeout=WEB_TIMEOUT) as r:
            return json.loads(r.read(1024 * 1024))
    try:
        d = _bounded(go, WEB_TIMEOUT + 1)
        entry = d.get(str(appid)) or {}
        if not entry.get("success"):
            return ""                                  # delisted / unknown: remember that too
        return (entry.get("data") or {}).get("name") or ""
    except Exception:  # noqa: BLE001
        return None


def _fill_names(ids):
    for a in ids:
        name = _store_name(a)
        with _names_lock:
            if name is not None:
                _names_mem[a] = name
            _names_inflight.discard(a)
        time.sleep(0.25)                               # be polite to the store (its limit is ~200 calls / 5 min)
    with _names_lock:
        _names_save()


def app_names(appids, offline=False, wait=False):
    """{appid: name} for the ids we know. Unknown ids are fetched from the store in the background
    (or synchronously when wait=True) and cached forever. Never raises; never blocks unless asked."""
    want = sorted({int(a) for a in appids})
    if not want:
        return {}
    with _names_lock:
        _names_load()
        missing = [a for a in want if a not in _names_mem and a not in _names_inflight]
        if missing and not offline:
            _names_inflight.update(missing)
    if missing and not offline:
        if wait:
            _fill_names(missing)
        else:
            threading.Thread(target=_fill_names, args=(missing,), daemon=True).start()
    with _names_lock:
        return {a: _names_mem[a] for a in want if _names_mem.get(a)}


# ----------------------------------------------------------------------------- the account object
class SteamAccount:
    """Cached view of the signed-in account. `key` is optional and never leaves this process."""

    def __init__(self, steam_root, key="", offline=False):
        self.steam_root = steam_root
        self.key = key or ""
        self.offline = offline                      # --no-network: the web tier stays off entirely
        self._lock = threading.Lock()
        self._local = (None, 0.0)
        self._web = (None, 0.0)
        self._refreshing = False
        self.web_error = None

    def user(self):
        with self._lock:
            u, when = self._local
            if u is None or time.time() - when > 60:
                u = current_user(self.steam_root) or {}
                self._local = (u, time.time())
            return u

    def playtime(self):
        u = self.user()
        if not u:
            return {}
        return local_playtime(self.steam_root, u["steamid"])

    def _refresh(self, steamid):
        try:
            w = {"summary": web_summary(self.key, steamid), "owned": web_owned(self.key, steamid), "recent": {}}
            try:
                w["recent"] = web_recent(self.key, steamid)
            except Exception:  # noqa: BLE001 - optional detail
                pass
            self.web_error = None
        except Exception as e:  # noqa: BLE001
            self.web_error = "%s: %s" % (type(e).__name__, str(e)[:120])
            w = {"summary": None, "owned": []}
        with self._lock:
            self._web = (w, time.time())
            self._refreshing = False
        return w

    REFRESH_EVERY = 300                                  # the page is never more than five minutes behind Steam

    def start_auto_refresh(self):
        """Keep the web tier current in the background - whether or not anyone opens the page."""
        if not self.key or self.offline or getattr(self, "_auto", False):
            return
        self._auto = True
        def loop():
            while True:
                time.sleep(self.REFRESH_EVERY)
                try:
                    self.web(max_age=self.REFRESH_EVERY - 5, wait=True)
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=loop, daemon=True).start()

    def web(self, max_age=None, wait=False):
        if max_age is None:
            max_age = self.REFRESH_EVERY
        """{'summary': {...}|None, 'owned': [...]} or None when there's no key / offline.
        Never blocks the caller on the network unless wait=True (and even then only for a bounded time):
        a stale or missing value triggers a background refresh and the last known value is returned."""
        if not self.key or self.offline:
            return None
        u = self.user()
        if not u:
            return None
        with self._lock:
            w, when = self._web
            fresh = w is not None and time.time() - when < max_age
            if fresh:
                return w
            if self._refreshing:
                return w                                     # someone else is already fetching; use what we have
            self._refreshing = True
        if wait and w is None:
            return self._refresh(u["steamid"])                # first ever call from /api/account: bounded (~7 s max)
        threading.Thread(target=self._refresh, args=(u["steamid"],), daemon=True).start()
        return w

    def info(self):
        """What the page is allowed to see. Never includes the key."""
        u = self.user()
        out = {"connected": bool(u), "persona": u.get("persona", "") if u else "", "account": u.get("account", "") if u else "",
               "steamid": u.get("steamid", "") if u else "", "avatar": "", "source": "local" if u else "none", "web": bool(self.key),
               "names_pending": bool(_names_inflight)}
        w = self.web(wait=True)
        if w and w.get("summary"):
            out["persona"] = w["summary"].get("persona") or out["persona"]
            out["avatar"] = w["summary"].get("avatar", "")
            out["source"] = "web"
        if self.key and self.web_error:
            out["web_error"] = self.web_error
        with self._lock:
            out["web_updated"] = int(self._web[1]) if self._web[0] is not None else 0
        return out

    def enrich(self, games):
        """Hours, last played (and the last two weeks) on the installed-games list.
        The Web API - which sees every device you play on - wins when it's available; the local record fills gaps."""
        pt = self.playtime()
        w = self.web() or {}
        owned = {g["appid"]: g for g in w.get("owned") or []}
        recent = w.get("recent") or {}
        for g in games:
            local = pt.get(g["appid"]) or {}
            web = owned.get(g["appid"]) or {}
            minutes = max(local.get("minutes", 0), web.get("minutes", 0))
            g["hours"] = round(minutes / 60.0, 1) if minutes else 0
            g["hours_2w"] = round(recent.get(g["appid"], 0) / 60.0, 1) if recent.get(g["appid"]) else 0
            g["last_played"] = max(g.get("last_played", 0), local.get("last_played", 0), web.get("last_played", 0))
            g["stats_from"] = "steam" if web else ("pc" if local else "none")
        games.sort(key=lambda g: (-g["last_played"], g["title"].lower()))
        return games

    def played_before(self, installed_appids, wait=False):
        """Games with playtime in Steam's local record that aren't installed right now - no key needed."""
        pt = self.playtime()
        ids = [a for a, v in pt.items() if a not in installed_appids and (v["minutes"] > 0 or v["last_played"] > 0)]
        names = app_names(ids, offline=self.offline, wait=wait)
        return [{"appid": a, "title": names.get(a) or "App %d" % a, "minutes": pt[a]["minutes"], "last_played": pt[a]["last_played"],
                 "cover": CDN % a, "source": "local"} for a in ids]

    def owned_not_installed(self, installed_appids, limit=24, wait=False):
        """Past-played games (local record) plus, with a key, everything else you own. Most recent first."""
        by_id = {g["appid"]: g for g in self.played_before(installed_appids, wait=wait)}
        w = self.web(wait=True)
        for g in (w.get("owned") if w else None) or []:
            if g["appid"] in installed_appids:
                continue
            cur = by_id.get(g["appid"])
            if cur is None:
                by_id[g["appid"]] = dict(g, source="web")
            else:                                                     # keep the better of the two records
                cur["title"] = g["title"] or cur["title"]
                cur["minutes"] = max(cur["minutes"], g["minutes"]); cur["last_played"] = max(cur["last_played"], g["last_played"])
        rest = list(by_id.values())
        rest.sort(key=lambda g: (-g["last_played"], -g["minutes"], g["title"].lower()))
        for g in rest:
            g["hours"] = round(g["minutes"] / 60.0, 1) if g["minutes"] else 0
        return rest[:limit]

    def owns(self, appid):
        """Installable from this page: in your web library, or something Steam's local record says you've played."""
        if appid in self.playtime():
            return True
        w = self.web()
        return bool(w) and any(g["appid"] == appid for g in w.get("owned") or [])
