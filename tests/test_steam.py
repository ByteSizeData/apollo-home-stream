"""Tests for the Steam file parsing — the part of the bridge most likely to break
on a real machine. Run:  python3 -m unittest discover tests"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bridge"))
import apollo_bridge as ab  # noqa: E402

LIBRARYFOLDERS = r'''
"libraryfolders"
{
	"0"
	{
		"path"		"C:\\Program Files (x86)\\Steam"
		"label"		""
		"contentid"		"12345"
		"apps"
		{
			"228980"		"265386178"
		}
	}
	"1"
	{
		"path"		"D:\\SteamLibrary"
		"label"		"Big drive"
		"apps"
		{
			"1245620"		"58100000000"
		}
	}
}
'''

MANIFEST_GAME = r'''
"AppState"
{
	"appid"		"1245620"
	"Universe"		"1"
	"name"		"ELDEN RING"
	"StateFlags"		"4"
	"installdir"		"ELDEN RING"
	"LastUpdated"		"1700000000"
	"LastPlayed"		"1757300000"
	"SizeOnDisk"		"58100000000"
	"UserConfig"
	{
		"language"		"english"
	}
}
'''

MANIFEST_REDIST = r'''
"AppState"
{
	"appid"		"228980"
	"name"		"Steamworks Common Redistributables"
	"StateFlags"		"4"
	"LastPlayed"		"0"
	"SizeOnDisk"		"265386178"
}
'''

MANIFEST_PARTIAL = r'''
"AppState"
{
	"appid"		"570"
	"name"		"Dota 2"
	"StateFlags"		"1026"
	"LastPlayed"		"1757000000"
	"SizeOnDisk"		"1000"
}
'''

MANIFEST_QUOTED = r'''
"AppState"
{
	"appid"		"9999"
	"name"		"Say \"Hello\" \\ Goodbye"
	"StateFlags"		"4"
	"LastPlayed"		"10"
	"SizeOnDisk"		"5"
}
'''


class ParseVdf(unittest.TestCase):
    def test_nested_and_scalar(self):
        d = ab.parse_vdf(LIBRARYFOLDERS)
        self.assertIn("libraryfolders", d)
        self.assertEqual(d["libraryfolders"]["1"]["path"], r"D:\SteamLibrary")
        self.assertEqual(d["libraryfolders"]["0"]["apps"]["228980"], "265386178")

    def test_escapes(self):
        d = ab.parse_vdf(MANIFEST_QUOTED)
        self.assertEqual(d["AppState"]["name"], 'Say "Hello" \\ Goodbye')

    def test_tolerates_garbage(self):
        self.assertEqual(ab.parse_vdf(""), {})
        self.assertEqual(ab.parse_vdf('"a" "b" }'), {"a": "b"})


class Manifests(unittest.TestCase):
    def _write(self, d, name, text):
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_game_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            g = ab.read_manifest(self._write(d, "appmanifest_1245620.acf", MANIFEST_GAME))
        self.assertEqual(g["appid"], 1245620)
        self.assertEqual(g["title"], "ELDEN RING")
        self.assertEqual(g["last_played"], 1757300000)
        self.assertEqual(g["size_bytes"], 58100000000)
        self.assertTrue(g["cover"].endswith("/1245620/library_600x900.jpg"))

    def test_redistributables_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(ab.read_manifest(self._write(d, "appmanifest_228980.acf", MANIFEST_REDIST)))

    def test_partial_download_is_skipped(self):
        # StateFlags 1026 = updating / not fully installed (bit 4 not set)
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(ab.read_manifest(self._write(d, "appmanifest_570.acf", MANIFEST_PARTIAL)))


class WholeLibrary(unittest.TestCase):
    def test_scan_walks_every_library_and_sorts_by_recency(self):
        with tempfile.TemporaryDirectory() as root:
            second = os.path.join(root, "second")
            for lib in (root, second):
                os.makedirs(os.path.join(lib, "steamapps"), exist_ok=True)
            lf = LIBRARYFOLDERS.replace(r"C:\\Program Files (x86)\\Steam", root.replace("\\", "\\\\")) \
                               .replace(r"D:\\SteamLibrary", second.replace("\\", "\\\\"))
            with open(os.path.join(root, "steamapps", "libraryfolders.vdf"), "w", encoding="utf-8") as f:
                f.write(lf)
            with open(os.path.join(root, "steamapps", "appmanifest_228980.acf"), "w", encoding="utf-8") as f:
                f.write(MANIFEST_REDIST)
            with open(os.path.join(root, "steamapps", "appmanifest_9999.acf"), "w", encoding="utf-8") as f:
                f.write(MANIFEST_QUOTED)  # last_played 10 — oldest
            with open(os.path.join(second, "steamapps", "appmanifest_1245620.acf"), "w", encoding="utf-8") as f:
                f.write(MANIFEST_GAME)   # last_played 1757300000 — newest
            with open(os.path.join(second, "steamapps", "appmanifest_570.acf"), "w", encoding="utf-8") as f:
                f.write(MANIFEST_PARTIAL)

            libs = ab.library_folders(root)
            self.assertEqual(libs, [root, second])
            games = ab.scan_games(root)
        self.assertEqual([g["appid"] for g in games], [1245620, 9999])
        self.assertEqual(games[0]["library"], second)


if __name__ == "__main__":
    unittest.main()
