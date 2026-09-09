"""Health checks, self-test, and the self-updater - with GitHub, git, and the network faked out."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bridge"))
import selfcare  # noqa: E402

SHA_A = "a" * 40
SHA_B = "b" * 40


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
            self.assertEqual(c["status"], "ok"); self.assertIn("3 installed games", c["detail"])
            self.assertEqual(selfcare.check_steam(FakeBridge(d, []))["status"], "warn")
            self.assertEqual(selfcare.check_steam(FakeBridge(d, fail=True))["status"], "fail")

    def test_port_check_sees_a_busy_port(self):
        import socket
        s = socket.socket(); s.bind(("0.0.0.0", 0)); s.listen(1)
        try:
            self.assertEqual(selfcare.check_port(s.getsockname()[1])["status"], "warn")
        finally:
            s.close()

    def test_running_bridge_does_not_warn_about_its_own_port(self):
        import socket
        s = socket.socket(); s.bind(("0.0.0.0", 0)); s.listen(1)
        try:
            with tempfile.TemporaryDirectory() as d, mock.patch.object(selfcare, "check_apollo", lambda: selfcare._c("apollo host", True, "x")), \
                 mock.patch.object(selfcare, "check_tailscale", lambda: selfcare._c("tailscale", True, "x")):
                port = s.getsockname()[1]
                self.assertEqual(selfcare.health(FakeBridge(d, [1]), port=port, network=False)["status"], "warn")
                self.assertEqual(selfcare.health(FakeBridge(d, [1]), port=port, network=False, serving=True)["status"], "ok")
        finally:
            s.close()

    def test_health_rolls_up_worst_status(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(selfcare, "check_apollo", lambda: selfcare._c("apollo host", False, "x", fail=False)), \
             mock.patch.object(selfcare, "check_tailscale", lambda: selfcare._c("tailscale", True, "x")):
            h = selfcare.health(FakeBridge(d, [1]), port=0, network=False)
            self.assertEqual(h["status"], "warn")
            self.assertEqual([c["name"] for c in h["checks"]], ["python", "web files", "steam", "port 0", "apollo host", "tailscale"])
            self.assertEqual(selfcare.health(FakeBridge(None), port=0, network=False)["status"], "fail")

    def test_tcp_probe_is_bounded_even_when_dns_hangs(self):
        import time
        def hang(*a, **k):
            time.sleep(5); return []
        with mock.patch.object(selfcare.socket, "getaddrinfo", hang):
            t = time.time(); ok = selfcare._tcp("nowhere.invalid", 443, timeout=0.3)
            self.assertFalse(ok); self.assertLess(time.time() - t, 2)

    def test_tailscale_needs_login_is_explained_not_a_crash(self):
        fake = mock.Mock(stdout=json.dumps({"BackendState": "NeedsLogin", "Self": None}))
        with mock.patch.object(selfcare.shutil, "which", return_value="/usr/bin/tailscale"), mock.patch.object(selfcare.os.path, "exists", return_value=True), \
             mock.patch.object(selfcare.subprocess, "run", return_value=fake):
            c = selfcare.check_tailscale()
            self.assertEqual(c["status"], "warn"); self.assertIn("NeedsLogin", c["detail"]); self.assertIn("sign in", c["detail"])

    def test_report_prints_every_row(self):
        h = {"version": "9.9", "sha": "abc", "status": "ok", "checks": [{"name": "a", "status": "ok", "detail": "fine"}, {"name": "b", "status": "warn", "detail": "meh"}]}
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            selfcare.print_report(h)
        out = buf.getvalue()
        self.assertIn("[OK  ] a", out); self.assertIn("[warn] b", out); self.assertIn("everything works", out)
        out.encode("cp1252")   # every character survives a Windows console


class Fetching(unittest.TestCase):
    def test_only_github_hosts_are_ever_fetched(self):
        with self.assertRaises(ValueError):
            selfcare._get("https://evil.example/x", 1)
        with self.assertRaises(ValueError):
            selfcare._get("http://api.github.com/x", 1)          # plain http refused too

    def test_redirects_off_github_are_refused(self):
        h = selfcare._SameSiteRedirects()
        req = urllib_req("https://codeload.github.com/a/b/zip/main")
        with self.assertRaises(selfcare.urllib.error.URLError):
            h.redirect_request(req, None, 302, "Found", {}, "https://evil.example/payload.zip")
        with self.assertRaises(selfcare.urllib.error.URLError):
            h.redirect_request(req, None, 302, "Found", {}, "http://codeload.github.com/a")   # downgrade refused

    def test_remote_sha_asks_the_pinned_repo(self):
        seen = {}
        class R:
            def __init__(self, req, timeout=None): seen["url"] = req.full_url
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return json.dumps({"sha": SHA_A}).encode()
        with mock.patch.object(selfcare._opener, "open", lambda req, timeout=None: R(req, timeout)):
            self.assertEqual(selfcare.remote_sha(), SHA_A)
        self.assertEqual(seen["url"], "https://api.github.com/repos/ByteSizeData/apollo-home-stream/commits/main")


def urllib_req(url):
    return selfcare.urllib.request.Request(url)


class UpdateStatus(unittest.TestCase):
    def test_offline_is_reported_not_raised(self):
        with mock.patch.object(selfcare, "remote_sha", side_effect=OSError("no net")), mock.patch.object(selfcare, "local_sha", return_value=SHA_A):
            u = selfcare.update_status()
            self.assertIsNone(u["available"]); self.assertIn("couldn't reach GitHub", u["error"])

    def test_same_and_different_shas(self):
        with mock.patch.object(selfcare, "remote_sha", return_value=SHA_A), mock.patch.object(selfcare, "local_sha", return_value=SHA_A):
            self.assertFalse(selfcare.update_status()["available"])
        with mock.patch.object(selfcare, "remote_sha", return_value=SHA_B), mock.patch.object(selfcare, "local_sha", return_value=SHA_A):
            self.assertTrue(selfcare.update_status()["available"])
        with mock.patch.object(selfcare, "remote_sha", return_value=SHA_B), mock.patch.object(selfcare, "local_sha", return_value=None):
            self.assertTrue(selfcare.update_status()["available"])


class GitUpdate(unittest.TestCase):
    """The git path, with git itself faked: it must fetch the PINNED repo, and roll back on failing tests."""

    def run_git(self, calls, shas, fetch_rc=0, merge_rc=0, tests_ok=True):
        it = iter(shas)
        def fake_git(*args, timeout=120):
            calls.append(args)
            if args[0] == "rev-parse":
                return mock.Mock(returncode=0, stdout=next(it) + "\n", stderr="")
            if args[0] == "fetch":
                return mock.Mock(returncode=fetch_rc, stdout="", stderr="boom" if fetch_rc else "")
            if args[0] == "merge":
                return mock.Mock(returncode=merge_rc, stdout="", stderr="not ff" if merge_rc else "")
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(selfcare, "is_git_checkout", return_value=True), mock.patch.object(selfcare, "_git", fake_git), \
             mock.patch.object(selfcare, "run_unit_tests", return_value={"name": "unit tests", "status": "ok" if tests_ok else "fail", "detail": "Ran 1 test - OK" if tests_ok else "FAILED"}):
            return selfcare.apply_update(log=lambda *a: None)

    def test_fetches_the_pinned_repo_not_origin(self):
        calls = []
        ok, msg, before, after = self.run_git(calls, [SHA_A, SHA_B])
        fetch = next(c for c in calls if c[0] == "fetch")
        self.assertEqual(fetch[1], "https://github.com/ByteSizeData/apollo-home-stream.git")
        self.assertTrue(ok); self.assertEqual((before, after), (SHA_A, SHA_B)); self.assertIn("a" * 7 + " -> " + "b" * 7, msg)

    def test_nothing_new_means_before_equals_after(self):
        ok, msg, before, after = self.run_git([], [SHA_A, SHA_A])
        self.assertTrue(ok); self.assertEqual(before, after); self.assertIn("already up to date", msg)

    def test_local_commits_are_explained_not_looped(self):
        ok, msg, before, after = self.run_git([], [SHA_A], merge_rc=1)
        self.assertFalse(ok); self.assertEqual(before, after); self.assertIn("local commits", msg)

    def test_failing_tests_reset_to_the_previous_commit(self):
        calls = []
        ok, msg, before, after = self.run_git(calls, [SHA_A, SHA_B, SHA_A], tests_ok=False)
        self.assertFalse(ok); self.assertIn("rolled back", msg)
        self.assertIn(("reset", "--hard", SHA_A), calls)
        self.assertEqual(after, SHA_A)

    def test_git_timeouts_do_not_raise(self):
        def hang(*a, **k): raise subprocess.TimeoutExpired("git", 1)
        with mock.patch.object(selfcare, "is_git_checkout", return_value=True), mock.patch.object(selfcare, "local_sha", return_value=SHA_A), mock.patch.object(selfcare, "_git", hang):
            ok, msg, before, after = selfcare.apply_update(log=lambda *a: None)
        self.assertFalse(ok); self.assertIn("timed out", msg); self.assertEqual(before, after)


def make_zip(path, top, extra=None, omit=()):
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


class ZipUpdate(unittest.TestCase):
    """The zip path against a throwaway ROOT: exact-commit download, verified shape, backup, tests, rollback."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "root")
        for d, f in (("bridge", "apollo_bridge.py"), ("bridge", "selfcare.py"), ("web", "index.html"), ("web", "pin.html")):
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
            with open(os.path.join(self.root, d, f), "w") as fh:
                fh.write("old")
        self.sha_file = os.path.join(self.root, ".apollo-sha")
        with open(self.sha_file, "w") as fh:
            fh.write(SHA_A + "\n")
        self.zip = os.path.join(self.tmp, "dl.zip")
        self.seen = {}
        self.patches = [
            mock.patch.object(selfcare, "ROOT", self.root),
            mock.patch.object(selfcare, "SHA_FILE", self.sha_file),
            mock.patch.object(selfcare, "remote_sha", return_value=SHA_B),
            mock.patch.object(selfcare, "is_git_checkout", return_value=False),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_download(self):
        zpath, seen = self.zip, self.seen
        class R:
            def __init__(self, url, timeout=None):
                seen["url"] = url; self.f = open(zpath, "rb")
            def __enter__(self): return self.f
            def __exit__(self, *a): self.f.close()
        return mock.patch.object(selfcare, "_get", lambda url, timeout: R(url, timeout))

    def read(self, *parts):
        with open(os.path.join(self.root, *parts)) as f:
            return f.read()

    def test_downloads_exactly_the_commit_it_will_record(self):
        make_zip(self.zip, "apollo-home-stream-" + SHA_B + "/")
        with self.fake_download(), mock.patch.object(selfcare, "run_unit_tests", return_value={"name": "unit tests", "status": "ok", "detail": "Ran 1 test - OK"}):
            ok, msg, before, after = selfcare.apply_update(log=lambda *a: None)
        self.assertTrue(ok, msg)
        self.assertEqual(self.seen["url"], "https://codeload.github.com/ByteSizeData/apollo-home-stream/zip/" + SHA_B)
        self.assertEqual(self.read("web", "index.html"), "<title>new</title>\n")
        self.assertEqual(self.read(".apollo-sha").strip(), SHA_B)
        self.assertEqual((before, after), (SHA_A, SHA_B))
        self.assertFalse([d for d in os.listdir(self.root) if d.startswith(".backup-")])

    def test_failed_tests_roll_back_files_and_sha(self):
        make_zip(self.zip, "x/")
        with self.fake_download(), mock.patch.object(selfcare, "run_unit_tests", return_value={"name": "unit tests", "status": "fail", "detail": "FAILED"}):
            ok, msg, before, after = selfcare.apply_update(log=lambda *a: None)
        self.assertFalse(ok); self.assertIn("rolled back", msg)
        self.assertEqual(self.read("web", "index.html"), "old")
        self.assertEqual(self.read(".apollo-sha").strip(), SHA_A)          # the OLD sha is back, so it won't claim to be up to date
        self.assertEqual((before, after), (SHA_A, SHA_A))

    def test_wrong_project_zip_is_refused_before_touching_anything(self):
        make_zip(self.zip, "x/", omit=("web/pin.html",))
        with self.fake_download():
            ok, msg, before, after = selfcare.apply_update(log=lambda *a: None)
        self.assertFalse(ok); self.assertIn("doesn't look like this project", msg); self.assertEqual(self.read("web", "index.html"), "old")

    def test_zip_slip_and_backslash_entries_are_refused(self):
        for bad in ("../../evil.py", "x/..\\evil.py"):
            make_zip(self.zip, "x/", extra={bad: "boom"})
            with self.fake_download():
                ok, msg, _, _ = selfcare.apply_update(log=lambda *a: None)
            self.assertFalse(ok); self.assertIn("suspicious", msg, bad)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.py")))

    def test_rollback_failure_is_loud_and_names_the_backup(self):
        make_zip(self.zip, "x/")
        real_copytree = shutil.copytree
        state = {"n": 0}
        def flaky_copytree(src, dst, *a, **k):
            state["n"] += 1
            if state["n"] > 4:                       # let the backup (3 dirs) + first install copy succeed, then fail during rollback
                raise OSError("disk full")
            return real_copytree(src, dst, *a, **k)
        with self.fake_download(), mock.patch.object(selfcare, "run_unit_tests", return_value={"name": "unit tests", "status": "fail", "detail": "FAILED"}), \
             mock.patch.object(selfcare.shutil, "copytree", flaky_copytree):
            ok, msg, _, _ = selfcare.apply_update(log=lambda *a: None)
        self.assertFalse(ok); self.assertIn("ROLLBACK FAILED", msg); self.assertIn(".backup-", msg)


if __name__ == "__main__":
    unittest.main()
