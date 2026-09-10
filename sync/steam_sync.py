"""Fetch your Steam account + library from the Steam Web API and write it for the website.

Runs on GitHub Actions on a schedule (see .github/workflows/pages.yml). Your key never leaves the
Action; the output is encrypted with your PIN / passphrase so the public site can host it.

    STEAM_API_KEY     your Web API key (steamcommunity.com/dev/apikey)
    STEAM_ID          your 64-bit Steam ID, or a profile URL / vanity name
    SITE_PASSPHRASE   what unlocks the data in the browser (default: the PIN, 2550)

    python sync/steam_sync.py --out web/steam.json
    python sync/steam_sync.py --plain --out /tmp/steam.json      # unencrypted, for a look
"""
import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

API = "https://api.steampowered.com/"
CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps/%d/library_600x900.jpg"
PBKDF2_ITERATIONS = 310000
DEFAULT_PASSPHRASE = "2550"


def _get(path, params, key, timeout=20):
    q = dict(params, key=key, format="json")
    req = urllib.request.Request(API + path + "?" + urllib.parse.urlencode(q), headers={"User-Agent": "apollo-home-stream-sync"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def resolve_steamid(key, ident):
    """Accepts a 17-digit id, a /profiles/<id> URL, a /id/<vanity> URL, or a bare vanity name."""
    ident = (ident or "").strip().rstrip("/")
    m = re.search(r"(\d{17})$", ident)
    if m:
        return m.group(1)
    vanity = ident.rsplit("/", 1)[-1]
    d = _get("ISteamUser/ResolveVanityURL/v1/", {"vanityurl": vanity}, key)
    r = d.get("response") or {}
    if r.get("success") != 1:
        raise SystemExit("Couldn't resolve Steam ID from %r - use the 17-digit id from your profile URL" % ident)
    return r["steamid"]


def fetch(key, steamid):
    summary = ((_get("ISteamUser/GetPlayerSummaries/v2/", {"steamids": steamid}, key).get("response") or {}).get("players") or [{}])[0]
    owned = (_get("IPlayerService/GetOwnedGames/v1/", {"steamid": steamid, "include_appinfo": 1, "include_played_free_games": 1}, key)
             .get("response") or {}).get("games") or []
    recent = {g["appid"]: int(g.get("playtime_2weeks") or 0)
              for g in ((_get("IPlayerService/GetRecentlyPlayedGames/v1/", {"steamid": steamid}, key).get("response") or {}).get("games") or [])}
    games = [{"appid": g["appid"], "title": g.get("name") or "App %d" % g["appid"], "minutes": int(g.get("playtime_forever") or 0),
              "minutes_2w": recent.get(g["appid"], 0), "last_played": int(g.get("rtime_last_played") or 0), "cover": CDN % g["appid"]}
             for g in owned if "appid" in g]
    games.sort(key=lambda g: (-g["last_played"], -g["minutes"], g["title"].lower()))
    return {
        "v": 1,
        "updated": int(time.time()),
        "persona": summary.get("personaname", ""),
        "avatar": summary.get("avatarfull") or summary.get("avatarmedium") or "",
        "games": games,
        "totals": {"games": len(games), "hours": round(sum(g["minutes"] for g in games) / 60.0, 1),
                   "hours_2w": round(sum(recent.values()) / 60.0, 1)},
    }


def encrypt(obj, passphrase):
    """AES-256-GCM, key from PBKDF2-HMAC-SHA256 - decryptable with WebCrypto in the browser."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    dk = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, PBKDF2_ITERATIONS, dklen=32)
    ct = AESGCM(dk).encrypt(iv, json.dumps(obj, separators=(",", ":")).encode("utf-8"), None)
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return {"v": 1, "enc": "aes-256-gcm", "kdf": "pbkdf2-sha256", "iter": PBKDF2_ITERATIONS,
            "salt": b64(salt), "iv": b64(iv), "data": b64(ct), "updated": obj["updated"], "persona": obj.get("persona", "")}


def decrypt(blob, passphrase):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    dk = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), base64.b64decode(blob["salt"]), int(blob["iter"]), dklen=32)
    return json.loads(AESGCM(dk).decrypt(base64.b64decode(blob["iv"]), base64.b64decode(blob["data"]), None))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="web/steam.json")
    ap.add_argument("--plain", action="store_true", help="write unencrypted JSON (for a look; never publish this)")
    args = ap.parse_args(argv)
    key = os.environ.get("STEAM_API_KEY", "").strip()
    ident = os.environ.get("STEAM_ID", "").strip()
    passphrase = os.environ.get("SITE_PASSPHRASE", "").strip() or DEFAULT_PASSPHRASE
    if not key or not ident:
        print("STEAM_API_KEY and STEAM_ID are not both set - nothing to sync (that's fine until you add them).")
        return 0
    steamid = resolve_steamid(key, ident)
    data = fetch(key, steamid)
    out = data if args.plain else encrypt(data, passphrase)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    print("synced %s: %d games, %.1f h total, %.1f h last two weeks -> %s%s" % (
        data["persona"] or steamid, data["totals"]["games"], data["totals"]["hours"], data["totals"]["hours_2w"], args.out,
        "" if args.plain else " (encrypted; unlock with your %s)" % ("PIN" if passphrase == DEFAULT_PASSPHRASE else "passphrase")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
