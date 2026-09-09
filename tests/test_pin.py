"""PIN gate tests — the lockout math, session handling, and the HTTP surface.
Run:  python3 -m unittest discover -s tests -v"""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bridge"))
import apollo_bridge as ab  # noqa: E402


class TryPin(unittest.TestCase):
    def setUp(self):
        self.b = ab.Bridge(None, "host", "", pin="2550")

    def test_correct_pin_returns_session(self):
        status, tok = self.b.try_pin("10.0.0.2", "2550")
        self.assertEqual(status, "ok")
        self.assertTrue(self.b.session_ok(tok))

    def test_wrong_pin_counts_down_then_locks(self):
        lefts = []
        for _ in range(ab.FREE_TRIES - 1):
            status, left = self.b.try_pin("10.0.0.2", "0000")
            self.assertEqual(status, "bad")
            lefts.append(left)
        self.assertEqual(lefts, list(range(ab.FREE_TRIES - 1, 0, -1)))
        status, wait = self.b.try_pin("10.0.0.2", "0000")
        self.assertEqual(status, "locked")
        self.assertEqual(wait, ab.LOCK_BASE_SECONDS)
        # even the RIGHT pin is refused while locked
        status, wait = self.b.try_pin("10.0.0.2", "2550")
        self.assertEqual(status, "locked")
        self.assertGreater(wait, 0)

    def test_lockout_is_per_ip(self):
        for _ in range(ab.FREE_TRIES):
            self.b.try_pin("10.0.0.2", "0000")
        self.assertEqual(self.b.try_pin("10.0.0.2", "2550")[0], "locked")
        self.assertEqual(self.b.try_pin("10.0.0.3", "2550")[0], "ok")

    def test_lockout_grows_and_caps(self):
        b = self.b
        for _ in range(ab.FREE_TRIES):
            b.try_pin("ip", "0000")
        # expire the lock artificially, guess wrong again, expect the wait to double
        count, _ = b._fails["ip"]
        b._fails["ip"] = (count, 0.0)
        _, wait2 = b.try_pin("ip", "0000")
        self.assertEqual(wait2, ab.LOCK_BASE_SECONDS * 2)
        for _ in range(20):
            count, _ = b._fails["ip"]
            b._fails["ip"] = (count, 0.0)
            _, w = b.try_pin("ip", "0000")
        self.assertEqual(w, ab.LOCK_MAX_SECONDS)

    def test_success_resets_failures(self):
        self.b.try_pin("ip", "0000")
        self.b.try_pin("ip", "0000")
        self.assertEqual(self.b.try_pin("ip", "2550")[0], "ok")
        self.assertNotIn("ip", self.b._fails)

    def test_non_string_and_empty_input(self):
        self.assertEqual(self.b.try_pin("ip", None)[0], "bad")
        self.assertEqual(self.b.try_pin("ip", "")[0], "bad")
        self.assertEqual(self.b.try_pin("ip", 2550)[0], "ok")   # numeric JSON is fine

    def test_session_expiry_and_logout(self):
        _, tok = self.b.try_pin("ip", "2550")
        self.b._sessions[tok] = time.time() - 1
        self.assertFalse(self.b.session_ok(tok))
        _, tok2 = self.b.try_pin("ip", "2550")
        self.b.end_session(tok2)
        self.assertFalse(self.b.session_ok(tok2))
        self.assertFalse(self.b.session_ok("made-up"))
        self.assertFalse(self.b.session_ok(None))

    def test_ungated_bridge_accepts_everything(self):
        b = ab.Bridge(None, "host", "", pin="")
        self.assertFalse(b.gated)
        self.assertTrue(b.session_ok(None))


class HttpGate(unittest.TestCase):
    """Real HTTP requests against the handler, on a random port."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = cls.tmp.name
        os.makedirs(os.path.join(root, "steamapps"))
        with open(os.path.join(root, "steamapps", "appmanifest_413150.acf"), "w") as f:
            f.write('"AppState"\n{\n\t"appid"\t\t"413150"\n\t"name"\t\t"Stardew Valley"\n'
                    '\t"StateFlags"\t\t"4"\n\t"LastPlayed"\t\t"1000"\n\t"SizeOnDisk"\t\t"5"\n}\n')
        cls.bridge = ab.Bridge(root, "testhost", "", pin="2550")
        ab.launch_appid = lambda appid: None          # don't actually open steam:// in tests
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), ab.make_handler(cls.bridge))
        cls.port = cls.srv.server_address[1]
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def req(self, method, path, body=None, cookie=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = dict(headers or {})
        if body is not None:
            body = json.dumps(body)
            h["Content-Type"] = "application/json"
        if cookie:
            h["Cookie"] = "%s=%s" % (ab.SESSION_COOKIE, cookie)
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        return r.status, dict(r.getheaders()), data

    def unlock(self):
        st, hd, _ = self.req("POST", "/api/pin", {"pin": "2550"})
        self.assertEqual(st, 200)
        sc = hd.get("Set-Cookie", "")
        self.assertIn("HttpOnly", sc)
        self.assertIn("SameSite=Strict", sc)
        return sc.split(";")[0].split("=", 1)[1]

    def setUp(self):
        self.bridge._fails.clear()

    def test_everything_is_gated_without_a_session(self):
        st, hd, body = self.req("GET", "/")
        self.assertEqual(st, 302)                            # pages bounce to the PIN screen
        self.assertEqual(hd.get("Location"), "/pin")
        self.assertEqual(body, b"")
        st, hd, _ = self.req("GET", "/index.html")
        self.assertEqual((st, hd.get("Location")), (302, "/pin?next=/index.html"))
        st, hd, _ = self.req("GET", "//evil.example/x")
        self.assertEqual(st, 302)                                    # never build an open redirect:
        self.assertTrue(hd.get("Location", "").startswith("/pin"))   # stays on this origin…
        self.assertNotIn("//", hd.get("Location", ""))               # …and can't smuggle a host in
        self.assertEqual(self.req("GET", "/api/games")[0], 401)   # APIs answer 401, not a redirect
        self.assertEqual(self.req("GET", "/api/host")[0], 401)
        st, hd, _ = self.req("HEAD", "/index.html")
        self.assertEqual((st, hd.get("Location")), (302, "/pin"))
        self.assertEqual(self.req("POST", "/api/launch", {"appid": 413150})[0], 401)
        self.assertEqual(self.req("GET", "/pin")[0], 200)     # the one page you're allowed to see

    def test_bad_cookie_values_are_rejected(self):
        self.assertEqual(self.req("GET", "/api/games", cookie="nope")[0], 401)
        st, _, _ = self.req("GET", "/api/games", headers={"Cookie": "garbage;;=;apollo_session"})
        self.assertEqual(st, 401)

    def test_pin_unlocks_and_cookie_grants_access(self):
        tok = self.unlock()
        st, _, body = self.req("GET", "/api/games", cookie=tok)
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["games"][0]["title"], "Stardew Valley")
        st, _, body = self.req("GET", "/", cookie=tok)
        self.assertEqual(st, 200)
        self.assertNotIn(b"Enter your PIN", body)
        st, _, body = self.req("POST", "/api/launch", {"appid": 413150}, cookie=tok)
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_wrong_pin_then_lockout_over_http(self):
        for i in range(ab.FREE_TRIES - 1):
            st, _, body = self.req("POST", "/api/pin", {"pin": "1111"})
            self.assertEqual(st, 401)
            self.assertEqual(json.loads(body)["left"], ab.FREE_TRIES - 1 - i)
        st, hd, body = self.req("POST", "/api/pin", {"pin": "1111"})
        self.assertEqual(st, 429)
        self.assertIn("Retry-After", hd)
        self.assertEqual(json.loads(body)["retry_after"], ab.LOCK_BASE_SECONDS)
        # correct pin is refused during the lock
        self.assertEqual(self.req("POST", "/api/pin", {"pin": "2550"})[0], 429)

    def test_malformed_pin_bodies(self):
        self.assertEqual(self.req("POST", "/api/pin", {"pin": ["2550"]})[0], 400)
        self.assertEqual(self.req("POST", "/api/pin", {})[0], 400)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/api/pin", body="not json", headers={"Content-Type": "application/json"})
        self.assertEqual(c.getresponse().status, 400)
        # malformed bodies must not count as tries
        self.assertNotIn("127.0.0.1", self.bridge._fails)

    def test_logout_ends_the_session(self):
        tok = self.unlock()
        self.assertEqual(self.req("GET", "/api/games", cookie=tok)[0], 200)
        st, hd, _ = self.req("POST", "/api/logout", cookie=tok)
        self.assertEqual(st, 200)
        self.assertIn("Max-Age=0", hd.get("Set-Cookie", ""))
        self.assertEqual(self.req("GET", "/api/games", cookie=tok)[0], 401)

    def test_pin_page_never_leaks_the_pin(self):
        _, _, body = self.req("GET", "/pin")
        self.assertNotIn(b"2550", body)


if __name__ == "__main__":
    unittest.main()
