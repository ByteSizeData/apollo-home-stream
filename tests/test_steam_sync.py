"""The scheduled Steam sync: id resolution, library shape, encryption round-trip, graceful no-op without secrets."""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sync"))
import steam_sync as ss  # noqa: E402

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False
if not HAVE_CRYPTO and os.environ.get("APOLLO_REQUIRE_CRYPTO"):   # set by the CI test matrix; never on someone's PC, where the bridge's self-test runs these too
    raise RuntimeError("cryptography is missing - the encryption tests must not be skipped in the CI matrix")

SID = "76561198012345678"


def fake_get(path, params, key, timeout=20):
    assert key == "KEY"
    if "ResolveVanityURL" in path:
        return {"response": {"success": 1, "steamid": SID}} if params["vanityurl"] == "bytesizedrift" else {"response": {"success": 42}}
    if "GetPlayerSummaries" in path:
        return {"response": {"players": [{"personaname": "ByteSizeDrift", "avatarfull": "https://a/x.jpg"}]}}
    if "GetRecentlyPlayedGames" in path:
        return {"response": {"games": [{"appid": 1091500, "playtime_2weeks": 340}]}}
    return {"response": {"games": [
        {"appid": 413150, "name": "Stardew Valley", "playtime_forever": 4440, "rtime_last_played": 1788100000},
        {"appid": 1091500, "name": "Cyberpunk 2077", "playtime_forever": 12730, "rtime_last_played": 1788900000},
        {"appid": 620, "name": "Portal 2", "playtime_forever": 1200, "rtime_last_played": 0},
    ]}}


class Resolve(unittest.TestCase):
    def test_accepts_id_urls_and_vanity(self):
        with mock.patch.object(ss, "_get", fake_get):
            for ident in (SID, "https://steamcommunity.com/profiles/%s" % SID, "https://steamcommunity.com/profiles/%s/" % SID,
                          "https://steamcommunity.com/id/bytesizedrift", "bytesizedrift"):
                self.assertEqual(ss.resolve_steamid("KEY", ident), SID, ident)
            with self.assertRaises(SystemExit):
                ss.resolve_steamid("KEY", "nobody-here")


class Fetch(unittest.TestCase):
    def test_library_shape_and_order(self):
        with mock.patch.object(ss, "_get", fake_get), mock.patch.object(ss.time, "time", lambda: 1789000000):
            d = ss.fetch("KEY", SID)
        self.assertEqual((d["persona"], d["avatar"], d["updated"]), ("ByteSizeDrift", "https://a/x.jpg", 1789000000))
        self.assertEqual([g["title"] for g in d["games"]], ["Cyberpunk 2077", "Stardew Valley", "Portal 2"])   # last played first
        self.assertEqual(d["games"][0]["minutes_2w"], 340)
        self.assertEqual(d["totals"], {"games": 3, "hours": 306.2, "hours_2w": 5.7})
        self.assertTrue(d["games"][0]["cover"].endswith("/1091500/library_600x900.jpg"))
        self.assertNotIn("KEY", json.dumps(d))


@unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed here (CI installs it)")
class Crypto(unittest.TestCase):
    def test_round_trip_and_wrong_pin(self):
        obj = {"updated": 1, "persona": "SecretPersonaName", "games": [{"appid": 1}]}
        blob = ss.encrypt(obj, "2550")
        self.assertEqual(set(blob), {"v", "enc", "kdf", "iter", "salt", "iv", "data", "updated"})     # the persona stays inside the encryption
        self.assertNotIn("SecretPersonaName", json.dumps(blob))
        self.assertEqual(ss.decrypt(blob, "2550"), obj)
        self.assertNotIn("games", json.dumps(blob))                        # the library itself is not readable
        with self.assertRaises(Exception):
            ss.decrypt(blob, "0000")
        self.assertNotEqual(ss.encrypt(obj, "2550")["data"], blob["data"])   # fresh salt/iv every time


class Main(unittest.TestCase):
    def test_no_secrets_is_a_quiet_no_op(self):
        with mock.patch.dict(os.environ, {"STEAM_API_KEY": "", "STEAM_ID": ""}, clear=False), tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "steam.json")
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = ss.main(["--out", out])
            self.assertEqual(rc, 0); self.assertFalse(os.path.exists(out)); self.assertIn("nothing to sync", buf.getvalue())

    def test_plain_output_when_asked(self):
        with mock.patch.dict(os.environ, {"STEAM_API_KEY": "KEY", "STEAM_ID": "bytesizedrift"}), mock.patch.object(ss, "_get", fake_get), tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "steam.json")
            with mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(ss.main(["--plain", "--out", out]), 0)
            j = json.load(open(out))
            self.assertEqual(j["persona"], "ByteSizeDrift"); self.assertEqual(len(j["games"]), 3)

    @unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed here (CI installs it)")
    def test_default_output_is_encrypted_with_the_pin(self):
        with mock.patch.dict(os.environ, {"STEAM_API_KEY": "KEY", "STEAM_ID": SID, "SITE_PASSPHRASE": ""}), mock.patch.object(ss, "_get", fake_get), tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "steam.json")
            with mock.patch("sys.stdout", io.StringIO()):
                ss.main(["--out", out])
            blob = json.load(open(out))
            self.assertIn("data", blob); self.assertNotIn("Cyberpunk", open(out).read())
            self.assertEqual(ss.decrypt(blob, "2550")["persona"], "ByteSizeDrift")


if __name__ == "__main__":
    unittest.main()


class BridgeUrlAndAlerts(unittest.TestCase):
    def test_only_a_plain_origin_may_ride_along(self):
        ok = {"gaming-station.tail1a2b.ts.net": "http://gaming-station.tail1a2b.ts.net:8777",
              "http://192.168.1.20:8777/": "http://192.168.1.20:8777",
              "https://gaming-station.tail1a2b.ts.net": "https://gaming-station.tail1a2b.ts.net",
              "HTTP://PC:9000": "http://pc:9000"}
        for raw, want in ok.items():
            self.assertEqual(ss.clean_bridge_url(raw), want, raw)
        for bad in ("", "javascript:alert(1)", "ftp://pc", "http://user:pw@pc:8777", "http://pc:8777/?play=1", "http://pc:8777/x", "http://pc:8777#f", "data:text/html,x"):
            self.assertEqual(ss.clean_bridge_url(bad), "", bad)

    def test_bridge_url_is_inside_the_data_not_beside_it(self):
        env = {"STEAM_API_KEY": "KEY", "STEAM_ID": SID, "BRIDGE_URL": "gaming-station.tail1a2b.ts.net"}
        with mock.patch.dict(os.environ, env), mock.patch.object(ss, "_get", fake_get), tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "steam.json")
            with mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(ss.main(["--plain", "--out", out]), 0)
            self.assertEqual(json.load(open(out))["bridge"], "http://gaming-station.tail1a2b.ts.net:8777")

    def test_steam_refusing_the_key_exits_3_and_says_why(self):
        import urllib.error
        def refuse(*a, **k): raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        with mock.patch.dict(os.environ, {"STEAM_API_KEY": "KEY", "STEAM_ID": SID}), mock.patch.object(ss, "_get", refuse), tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = ss.main(["--out", os.path.join(d, "steam.json")])
            self.assertEqual(rc, 3); self.assertIn("::error", buf.getvalue()); self.assertIn("revoked", buf.getvalue())
            self.assertNotIn("KEY", buf.getvalue().replace("STEAM_API_KEY", ""))


class Resilience(unittest.TestCase):
    def test_transient_faults_are_retried_then_succeed(self):
        import urllib.error
        calls = {"n": 0}
        class R:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return b'{"response": {"ok": 1}}'
        def flaky(req, timeout=20):
            calls["n"] += 1
            if calls["n"] == 1: raise urllib.error.HTTPError("u", 503, "maintenance", {}, None)
            if calls["n"] == 2: raise urllib.error.URLError("dns")
            return R()
        with mock.patch.object(ss.urllib.request, "urlopen", flaky), mock.patch.object(ss.time, "sleep", lambda s: None):
            self.assertEqual(ss._get("X/", {}, "KEY"), {"response": {"ok": 1}})
        self.assertEqual(calls["n"], 3)

    def test_a_rejected_key_is_not_retried(self):
        import urllib.error
        calls = {"n": 0}
        def refuse(req, timeout=20):
            calls["n"] += 1; raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        with mock.patch.object(ss.urllib.request, "urlopen", refuse), mock.patch.object(ss.time, "sleep", lambda s: None):
            with self.assertRaises(urllib.error.HTTPError):
                ss._get("X/", {}, "KEY")
        self.assertEqual(calls["n"], 1)

    def test_empty_library_is_a_failure_and_status_says_why(self):
        def empty(path, params, key, timeout=20):
            if "GetPlayerSummaries" in path: return {"response": {"players": [{"personaname": "x"}]}}
            return {"response": {}}
        with mock.patch.dict(os.environ, {"STEAM_API_KEY": "KEY", "STEAM_ID": SID}), mock.patch.object(ss, "_get", empty), tempfile.TemporaryDirectory() as d:
            out, st = os.path.join(d, "steam.json"), os.path.join(d, "status.json")
            with mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(ss.main(["--plain", "--out", out, "--status", st]), 3)
            self.assertFalse(os.path.exists(out))
            self.assertEqual(json.load(open(st))["reason"], "empty-library")

    def test_status_file_on_success_and_on_a_rejected_key(self):
        import urllib.error
        with mock.patch.dict(os.environ, {"STEAM_API_KEY": "KEY", "STEAM_ID": SID}), tempfile.TemporaryDirectory() as d:
            st = os.path.join(d, "status.json")
            with mock.patch.object(ss, "_get", fake_get), mock.patch("sys.stdout", io.StringIO()):
                ss.main(["--plain", "--out", os.path.join(d, "a.json"), "--status", st])
            self.assertEqual(json.load(open(st))["ok"], True)
            def refuse(*a, **k): raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
            with mock.patch.object(ss, "_get", refuse), mock.patch("sys.stdout", io.StringIO()):
                ss.main(["--plain", "--out", os.path.join(d, "b.json"), "--status", st])
            j = json.load(open(st)); self.assertEqual((j["ok"], j["reason"]), (False, "key-rejected"))
            self.assertNotIn("KEY", open(st).read())
