"""Steam account: local sign-in + playtime (offline), optional Web API (mocked), and what the page may see."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bridge"))
import steamaccount as sa  # noqa: E402
import apollo_bridge as ab  # noqa: E402

SID = "76561198012345678"
ACC = 52079950

LOGINUSERS = '''"users"
{
	"%s"
	{
		"AccountName"		"austin_g"
		"PersonaName"		"ByteSizeDrift"
		"MostRecent"		"1"
		"Timestamp"		"1788800000"
	}
	"76561198000000001"
	{
		"AccountName"		"old"
		"PersonaName"		"Old Account"
		"MostRecent"		"0"
		"Timestamp"		"1700000000"
	}
}
''' % SID

LOCALCONFIG = '''"UserLocalConfigStore"
{
	"Software" { "Valve" { "Steam" { "Apps"
	{
		"413150" { "LastPlayed" "1788100000"  "Playtime" "4440" }
		"1091500" { "lastplayed" "1788900000"  "playtime" "12730" }
		"junk" { "Playtime" "1" }
	} } } }
}
'''


def make_root(tmp, users=LOGINUSERS, local=LOCALCONFIG):
    os.makedirs(os.path.join(tmp, "config"))
    with open(os.path.join(tmp, "config", "loginusers.vdf"), "w") as f:
        f.write(users)
    if local is not None:
        d = os.path.join(tmp, "userdata", str(ACC), "config")
        os.makedirs(d)
        with open(os.path.join(d, "localconfig.vdf"), "w") as f:
            f.write(local)
    return tmp


class Local(unittest.TestCase):
    def test_current_user_is_the_most_recent(self):
        with tempfile.TemporaryDirectory() as d:
            make_root(d)
            u = sa.current_user(d)
            self.assertEqual((u["steamid"], u["account"], u["persona"], u["most_recent"]), (SID, "austin_g", "ByteSizeDrift", True))
            self.assertEqual(len(sa.login_users(d)), 2)

    def test_falls_back_to_newest_timestamp_when_nothing_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            make_root(d, users=LOGINUSERS.replace('"MostRecent"\t\t"1"', '"MostRecent"\t\t"0"'))
            self.assertEqual(sa.current_user(d)["steamid"], SID)     # newer Timestamp wins

    def test_no_steam_login_means_no_user_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(sa.current_user(d))
            self.assertEqual(sa.local_playtime(d, SID), {})

    def test_account_id_math(self):
        self.assertEqual(sa.account_id(SID), ACC)

    def test_playtime_is_read_case_insensitively_and_junk_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            make_root(d)
            pt = sa.local_playtime(d, SID)
            self.assertEqual(pt[413150], {"minutes": 4440, "last_played": 1788100000})
            self.assertEqual(pt[1091500], {"minutes": 12730, "last_played": 1788900000})
            self.assertNotIn("junk", pt); self.assertEqual(len(pt), 2)

    def test_enrich_adds_hours_and_reorders_by_true_last_played(self):
        with tempfile.TemporaryDirectory() as d:
            make_root(d)
            acct = sa.SteamAccount(d)
            games = [{"appid": 413150, "title": "Stardew", "last_played": 0}, {"appid": 1091500, "title": "Cyberpunk", "last_played": 100}, {"appid": 999, "title": "Unknown", "last_played": 50}]
            acct.enrich(games)
            self.assertEqual([g["title"] for g in games], ["Cyberpunk", "Stardew", "Unknown"])
            self.assertEqual(games[0]["hours"], 212.2); self.assertEqual(games[1]["hours"], 74.0); self.assertEqual(games[2]["hours"], 0)
            self.assertEqual(games[1]["last_played"], 1788100000)     # localconfig beats a 0 in the manifest

    def test_info_without_a_key_is_local_and_never_mentions_a_key(self):
        with tempfile.TemporaryDirectory() as d:
            make_root(d)
            info = sa.SteamAccount(d).info()
            self.assertEqual((info["connected"], info["persona"], info["source"], info["web"], info["avatar"]), (True, "ByteSizeDrift", "local", False, ""))
            self.assertNotIn("key", json.dumps(info).lower())


class WebTier(unittest.TestCase):
    def fake_web(self, summary=True, owned=True, fail=False):
        def _web(path, params, key, timeout=6):
            self.assertEqual(key, "SECRETKEY")
            if fail:
                raise OSError("no net")
            if "GetPlayerSummaries" in path:
                return {"response": {"players": [{"personaname": "Drift (web)", "avatarfull": "https://avatars.example/a.jpg"}] if summary else []}}
            return {"response": {"games": [
                {"appid": 413150, "name": "Stardew Valley", "playtime_forever": 4440, "rtime_last_played": 1788100000},
                {"appid": 570, "name": "Dota 2", "playtime_forever": 90, "rtime_last_played": 1600000000},
                {"appid": 620, "name": "Portal 2", "playtime_forever": 0, "rtime_last_played": 0},
            ] if owned else []}}
        return mock.patch.object(sa, "_web", _web)

    def test_web_adds_avatar_owned_games_and_install_permission(self):
        with tempfile.TemporaryDirectory() as d, self.fake_web():
            make_root(d)
            acct = sa.SteamAccount(d, key="SECRETKEY")
            info = acct.info()
            self.assertEqual((info["source"], info["persona"], info["avatar"], info["web"]), ("web", "Drift (web)", "https://avatars.example/a.jpg", True))
            self.assertNotIn("SECRETKEY", json.dumps(info))
            rest = acct.owned_not_installed({413150})
            self.assertEqual([g["title"] for g in rest], ["Dota 2", "Portal 2"])
            self.assertEqual(rest[0]["hours"], 1.5)
            self.assertTrue(acct.owns(570)); self.assertFalse(acct.owns(413150 + 1))

    def test_web_failure_is_remembered_not_raised_and_local_still_works(self):
        with tempfile.TemporaryDirectory() as d, self.fake_web(fail=True):
            make_root(d)
            acct = sa.SteamAccount(d, key="SECRETKEY")
            info = acct.info()
            self.assertEqual((info["source"], info["persona"]), ("local", "ByteSizeDrift"))
            self.assertIn("OSError", info["web_error"])
            self.assertEqual(acct.owned_not_installed(set()), [])
            self.assertFalse(acct.owns(570))


class ThroughTheBridge(unittest.TestCase):
    """The HTTP surface: hours on /api/games, /api/account behind the PIN, install only for owned games."""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer
        cls.tmp = tempfile.TemporaryDirectory(); root = cls.tmp.name
        make_root(root)
        os.makedirs(os.path.join(root, "steamapps"))
        with open(os.path.join(root, "steamapps", "appmanifest_413150.acf"), "w") as f:
            f.write('"AppState"\n{\n\t"appid"\t\t"413150"\n\t"name"\t\t"Stardew Valley"\n\t"StateFlags"\t\t"4"\n\t"LastPlayed"\t\t"1000"\n\t"SizeOnDisk"\t\t"5"\n}\n')
        cls.bridge = ab.Bridge(root, "testhost", "", pin="2550", steam_key="SECRETKEY")
        cls.launched = []
        ab.launch_appid = lambda appid: cls.launched.append(("run", appid))
        ab.launch_url = lambda url: cls.launched.append(("url", url))
        def _web(path, params, key, timeout=6):
            if "GetPlayerSummaries" in path:
                return {"response": {"players": [{"personaname": "Drift", "avatarfull": "https://a/x.jpg"}]}}
            return {"response": {"games": [{"appid": 413150, "name": "Stardew Valley", "playtime_forever": 4440, "rtime_last_played": 1788100000},
                                            {"appid": 570, "name": "Dota 2", "playtime_forever": 90, "rtime_last_played": 1600000000}]}}
        cls.webpatch = mock.patch.object(sa, "_web", _web); cls.webpatch.start()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), ab.make_handler(cls.bridge)); cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close(); cls.webpatch.stop(); cls.tmp.cleanup()

    def req(self, method, path, body=None, cookie=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {}
        if body is not None:
            body = json.dumps(body); h["Content-Type"] = "application/json"
        if cookie:
            h["Cookie"] = "%s=%s" % (ab.SESSION_COOKIE, cookie)
        c.request(method, path, body=body, headers=h); r = c.getresponse(); return r.status, json.loads(r.read() or b"{}"), dict(r.getheaders())

    def unlock(self):
        st, _, hd = self.req("POST", "/api/pin", {"pin": "2550"})
        return hd["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def test_account_is_behind_the_pin(self):
        self.assertEqual(self.req("GET", "/api/account")[0], 401)

    def test_games_carry_hours_and_account_and_never_the_key(self):
        tok = self.unlock()
        st, d, _ = self.req("GET", "/api/games", cookie=tok)
        self.assertEqual(st, 200)
        self.assertEqual(d["games"][0]["hours"], 74.0)
        self.assertEqual(d["games"][0]["last_played"], 1788100000)
        self.assertEqual(d["account"]["persona"], "Drift")
        self.assertNotIn("SECRETKEY", json.dumps(d))
        st, a, _ = self.req("GET", "/api/account", cookie=tok)
        self.assertEqual([g["title"] for g in a["also_owned"]], ["Dota 2"])
        self.assertNotIn("SECRETKEY", json.dumps(a))

    def test_install_only_for_owned_games(self):
        tok = self.unlock()
        st, d, _ = self.req("POST", "/api/launch", {"appid": 570, "action": "install"}, cookie=tok)
        self.assertEqual(st, 200); self.assertIn(("url", "steam://install/570"), self.launched)
        st, d, _ = self.req("POST", "/api/launch", {"appid": 99999, "action": "install"}, cookie=tok)
        self.assertEqual(st, 403)
        st, d, _ = self.req("POST", "/api/launch", {"appid": 413150}, cookie=tok)
        self.assertEqual(st, 200); self.assertIn(("run", 413150), self.launched)

    def test_health_shows_the_account_rows(self):
        h = self.bridge.health()
        names = [c["name"] for c in h["checks"]]
        self.assertIn("steam account", names); self.assertIn("steam web api", names)
        self.assertEqual(next(c for c in h["checks"] if c["name"] == "steam account")["detail"], "signed in as Drift")


if __name__ == "__main__":
    unittest.main()
