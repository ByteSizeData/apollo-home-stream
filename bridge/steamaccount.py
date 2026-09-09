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
import urllib.parse
import urllib.request

STEAMID64_BASE = 76561197960265728
WEB_TIMEOUT = 6
CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps/%d/library_600x900.jpg"


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


# ----------------------------------------------------------------------------- tier 1: local
def login_users(steam_root):
    """Accounts that have signed in on this PC, most recent first."""
    path = os.path.join(steam_root or "", "config", "loginusers.vdf")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = parse_vdf(f.read())
    except OSError:
        return []
    users = _ci(data, "users") or {}
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
    path = os.path.join(steam_root or "", "userdata", str(account_id(steamid64)), "config", "localconfig.vdf")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = parse_vdf(f.read())
    except (OSError, ValueError):
        return {}
    apps = _ci(data, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps") or {}
    out = {}
    for appid, info in apps.items():
        if not str(appid).isdigit() or not isinstance(info, dict):
            continue
        out[int(appid)] = {"minutes": _int(_ci(info, "Playtime")), "last_played": _int(_ci(info, "LastPlayed"))}
    return out


# ----------------------------------------------------------------------------- tier 2: web api (optional)
def _web(path, params, key, timeout=WEB_TIMEOUT):
    q = dict(params, key=key, format="json")
    url = "https://api.steampowered.com/%s?%s" % (path, urllib.parse.urlencode(q))
    req = urllib.request.Request(url, headers={"User-Agent": "apollo-home-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


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


# ----------------------------------------------------------------------------- the account object
class SteamAccount:
    """Cached view of the signed-in account. `key` is optional and never leaves this process."""

    def __init__(self, steam_root, key=""):
        self.steam_root = steam_root
        self.key = key or ""
        self._lock = threading.Lock()
        self._local = (None, 0.0)
        self._web = (None, 0.0)
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

    def web(self, max_age=1800):
        """{'summary': {...}|None, 'owned': [...]} or None when there's no key; errors are remembered, not raised."""
        if not self.key:
            return None
        u = self.user()
        if not u:
            return None
        with self._lock:
            w, when = self._web
            if w is not None and time.time() - when < max_age:
                return w
        try:
            w = {"summary": web_summary(self.key, u["steamid"]), "owned": web_owned(self.key, u["steamid"])}
            self.web_error = None
        except Exception as e:  # noqa: BLE001
            self.web_error = "%s: %s" % (type(e).__name__, str(e)[:120])
            w = {"summary": None, "owned": []}
        with self._lock:
            self._web = (w, time.time())
        return w

    def info(self):
        """What the page is allowed to see. Never includes the key."""
        u = self.user()
        out = {"connected": bool(u), "persona": u.get("persona", "") if u else "", "account": u.get("account", "") if u else "",
               "steamid": u.get("steamid", "") if u else "", "avatar": "", "source": "local" if u else "none", "web": bool(self.key)}
        w = self.web()
        if w and w.get("summary"):
            out["persona"] = w["summary"].get("persona") or out["persona"]
            out["avatar"] = w["summary"].get("avatar", "")
            out["source"] = "web"
        if self.key and self.web_error:
            out["web_error"] = self.web_error
        return out

    def enrich(self, games):
        """Add hours (and a better last_played) to the installed-games list, from local data first, web second."""
        pt = self.playtime()
        w = self.web() or {}
        owned = {g["appid"]: g for g in w.get("owned") or []}
        for g in games:
            local = pt.get(g["appid"])
            minutes = local["minutes"] if local else owned.get(g["appid"], {}).get("minutes", 0)
            g["hours"] = round(minutes / 60.0, 1) if minutes else 0
            lp = max(g.get("last_played", 0), local["last_played"] if local else 0, owned.get(g["appid"], {}).get("last_played", 0))
            g["last_played"] = lp
        games.sort(key=lambda g: (-g["last_played"], g["title"].lower()))
        return games

    def owned_not_installed(self, installed_appids, limit=24):
        w = self.web()
        if not w:
            return []
        rest = [g for g in w.get("owned") or [] if g["appid"] not in installed_appids]
        rest.sort(key=lambda g: (-g["last_played"], -g["minutes"], g["title"].lower()))
        for g in rest:
            g["hours"] = round(g["minutes"] / 60.0, 1) if g["minutes"] else 0
        return rest[:limit]

    def owns(self, appid):
        w = self.web()
        return bool(w) and any(g["appid"] == appid for g in w.get("owned") or [])
