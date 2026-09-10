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
        obj = {"updated": 1, "persona": "x", "games": [{"appid": 1}]}
        blob = ss.encrypt(obj, "2550")
        self.assertEqual(set(blob), {"v", "enc", "kdf", "iter", "salt", "iv", "data", "updated", "persona"})
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
