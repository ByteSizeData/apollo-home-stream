"""Health checks, self-test, and the self-updater — with GitHub and the network faked out."""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bridge"))
import selfcare  # noqa: E402


class FakeBridge:
    def __init__(self, root=None, games=None, fail=False):
        self.steam_root = root
        self._games = games or []
        self._fail = fail

    def games(self):
        if self._fail:
            raise OSError("disk on fire")
        return self._games


class Health(unittest.TestCase):
    def test_missing_steam_is_a_failure_unless_allowed(self):
        self.assertEqual(selfcare.check_steam(FakeBridge(None))["status"], "fail")
        self.assertEqual(selfcare.check_steam(FakeBridge(None), allow_missing=True)["status"], "warn")

    def test_steam_with_games_is_ok_and_counts_them(self):
        with tempfile.TemporaryDirectory() as d:
            c = selfcare.check_steam(FakeBridge(d, [1, 2, 3]))
            self.assertEqual(c["status"], "ok")
            self.assertIn("3 installed games", c["detail"])
            self.assertEqual(selfcare.check_steam(FakeBridge(d, []))["status"], "warn")   # folder but no games
            self.assertEqual(selfcare.check_steam(FakeBridge(d, fail=True))["status"], "fail")

    def test_port_check_sees_a_busy_port(self):
        import socket
        s = socket.socket(); s.bind(("0.0.0.0", 0)); s.listen(1)
        try:
            busy = selfcare.check_port(s.getsockname()[1])
            self.assertEqual(busy["status"], "warn")
        finally:
            s.close()

    def test_health_rolls_up_worst_status(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(selfcare, "check_apollo", lambda: selfcare._c("apollo host", False, "x", fail=False)), \
             mock.patch.object(selfcare, "check_tailscale", lambda: selfcare._c("tailscale", True, "x")):
            h = selfcare.health(FakeBridge(d, [1]), port=0, network=False)
            self.assertEqual(h["status"], "warn")
            names = [c["name"] for c in h["checks"]]
            self.assertEqual(names, ["python", "web files", "steam", "port 0", "apollo host", "tailscale"])   # no internet rows
            h2 = selfcare.health(FakeBridge(None), port=0, network=False)
            self.assertEqual(h2["status"], "fail")

    def test_running_bridge_does_not_warn_about_its_own_port(self):
        import socket
        s = socket.socket(); s.bind(("0.0.0.0", 0)); s.listen(1)
        try:
            with tempfile.TemporaryDirectory() as d, mock.patch.object(selfcare, "check_apollo", lambda: selfcare._c("apollo host", True, "x")), \
                 mock.patch.object(selfcare, "check_tailscale", lambda: selfcare._c("tailscale", True, "x")):
                port = s.getsockname()[1]
                self.assertEqual(selfcare.health(FakeBridge(d, [1]), port=port, network=False)["status"], "warn")            # a stranger holds it
                self.assertEqual(selfcare.health(FakeBridge(d, [1]), port=port, network=False, serving=True)["status"], "ok")  # we hold it
        finally:
            s.close()

    def test_report_prints_every_row(self):
        h = {"version": "9.9", "sha": "abc", "status": "ok", "checks": [{"name": "a", "status": "ok", "detail": "fine"}, {"name": "b", "status": "warn", "detail": "meh"}]}
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            selfcare.print_report(h)
        out = buf.getvalue()
        self.assertIn("[OK  ] a", out); self.assertIn("[warn] b", out); self.assertIn("everything works", out)


class UpdateStatus(unittest.TestCase):
    def test_offline_is_reported_not_raised(self):
        with mock.patch.object(selfcare, "remote_sha", side_effect=OSError("no net")), mock.patch.object(selfcare, "local_sha", return_value="aaa"):
            u = selfcare.update_status()
            self.assertIsNone(u["available"]); self.assertIn("couldn't reach GitHub", u["error"])

    def test_same_and_different_shas(self):
        with mock.patch.object(selfcare, "remote_sha", return_value="aaa"), mock.patch.object(selfcare, "local_sha", return_value="aaa"):
            self.assertFalse(selfcare.update_status()["available"])
        with mock.patch.object(selfcare, "remote_sha", return_value="bbb"), mock.patch.object(selfcare, "local_sha", return_value="aaa"):
            self.assertTrue(selfcare.update_status()["available"])
        with mock.patch.object(selfcare, "remote_sha", return_value="bbb"), mock.patch.object(selfcare, "local_sha", return_value=None):
            self.assertTrue(selfcare.update_status()["available"])   # unknown local → offer update

    def test_remote_sha_only_ever_asks_the_pinned_repo(self):
        seen = {}
        class R:
            def __init__(self, req, timeout=None): seen["url"] = req.full_url
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return json.dumps({"sha": "c" * 40}).encode()
        with mock.patch.object(selfcare.urllib.request, "urlopen", R):
            self.assertEqual(selfcare.remote_sha(), "c" * 40)
        self.assertEqual(seen["url"], "https://api.github.com/repos/ByteSizeData/apollo-home-stream/commits/main")


def make_zip(path, top="apollo-home-stream-main/", extra=None, omit=()):
    files = {
        "bridge/apollo_bridge.py": "print('new bridge')\n",
        "bridge/selfcare.py": "VERSION='9.9.9'\n",
        "web/index.html": "<title>new</title>\n",
        "web/pin.html": "<title>pin</title>\n",
        "tests/test_ok.py": "import unittest\nclass T(unittest.TestCase):\n    def test_x(self): pass\n",
    }
    files.update(extra or {})
    with zipfile.ZipFile(path, "w") as z:
        for name, body in files.items():
            if name in omit:
                continue
            z.writestr(top + name, body)


class ApplyUpdateZip(unittest.TestCase):
    """Exercise the zip path against a throwaway ROOT, with the download faked."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "root")
        for d, f, body in (("bridge", "apollo_bridge.py", "old"), ("bridge", "selfcare.py", "old"), ("web", "index.html", "old"), ("web", "pin.html", "old")):
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
            with open(os.path.join(self.root, d, f), "w") as fh:
                fh.write(body)
        self.zip = os.path.join(self.tmp, "dl.zip")
        self.patches = [
            mock.patch.object(selfcare, "ROOT", self.root),
            mock.patch.object(selfcare, "SHA_FILE", os.path.join(self.root, ".apollo-sha")),
            mock.patch.object(selfcare, "remote_sha", return_value="d" * 40),
            mock.patch.object(selfcare.shutil, "which", return_value=None),          # no git → zip path
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_download(self):
        zpath = self.zip
        class R:
            def __init__(self, req, timeout=None):
                assert req.full_url == "https://codeload.github.com/ByteSizeData/apollo-home-stream/zip/refs/heads/main", req.full_url
                self.f = open(zpath, "rb")
            def __enter__(self): return self.f
            def __exit__(self, *a): self.f.close()
        return mock.patch.object(selfcare.urllib.request, "urlopen", R)

    def test_good_update_swaps_files_and_records_sha(self):
        make_zip(self.zip)
        with self.fake_download(), mock.patch.object(selfcare, "run_unit_tests", return_value={"name": "unit tests", "status": "ok", "detail": "Ran 1 test — OK"}):
            ok, msg = selfcare.apply_update(log=lambda *a: None)
        self.assertTrue(ok, msg)
        self.assertEqual(open(os.path.join(self.root, "web", "index.html")).read(), "<title>new</title>\n")
        self.assertEqual(open(os.path.join(self.root, ".apollo-sha")).read().strip(), "d" * 40)
        self.assertFalse([d for d in os.listdir(self.root) if d.startswith(".backup-")])   # cleaned up

    def test_failed_tests_roll_back(self):
        make_zip(self.zip)
        with self.fake_download(), mock.patch.object(selfcare, "run_unit_tests", return_value={"name": "unit tests", "status": "fail", "detail": "FAILED"}):
            ok, msg = selfcare.apply_update(log=lambda *a: None)
        self.assertFalse(ok); self.assertIn("rolled back", msg)
        self.assertEqual(open(os.path.join(self.root, "web", "index.html")).read(), "old")   # original restored

    def test_wrong_project_zip_is_refused_before_touching_anything(self):
        make_zip(self.zip, omit=("web/pin.html",))
        with self.fake_download():
            ok, msg = selfcare.apply_update(log=lambda *a: None)
        self.assertFalse(ok); self.assertIn("doesn't look like this project", msg)
        self.assertEqual(open(os.path.join(self.root, "web", "index.html")).read(), "old")

    def test_zip_slip_entries_are_refused(self):
        make_zip(self.zip, extra={"../../evil.py": "boom"})
        with self.fake_download():
            ok, msg = selfcare.apply_update(log=lambda *a: None)
        self.assertFalse(ok); self.assertIn("suspicious", msg)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.py")))


if __name__ == "__main__":
    unittest.main()
