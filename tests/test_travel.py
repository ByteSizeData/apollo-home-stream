"""Ready-to-travel check, the Always-awake switch, sessions that survive a restart, and the running self-update.
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
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bridge"))
import apollo_bridge as ab  # noqa: E402
import steamaccount  # noqa: E402
import travel  # noqa: E402

NOW = 1789600000            # 2026-09-16
DAY = 86400


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def status_of(rows, name):
    return next(r["status"] for r in rows if r["name"] == name)


class Powercfg(unittest.TestCase):
    ENGLISH = """Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)
  Subgroup GUID: 238c9fa8-0aad-41ed-83f4-97be242c8f20  (Sleep)
    Power Setting GUID: 29f6c1db-86da-48c5-9fdb-f2b67b1f44da  (Sleep after)
      Minimum Possible Setting: 0x00000000
      Maximum Possible Setting: 0xffffffff
      Possible Settings increment: 0x00000001
      Possible Settings units: Seconds
    Current AC Power Setting Index: 0x00000708
    Current DC Power Setting Index: 0x00000384
"""
    GERMAN = ENGLISH.replace("Current AC Power Setting Index", "Index der aktuellen Wechselstromeinstellung") \
                    .replace("Current DC Power Setting Index", "Index der aktuellen Gleichstromeinstellung")

    def test_reads_the_mains_value_in_any_language(self):
        self.assertEqual(travel.parse_powercfg_ac(self.ENGLISH), 1800)
        self.assertEqual(travel.parse_powercfg_ac(self.GERMAN), 1800)
        self.assertEqual(travel.parse_powercfg_ac(self.ENGLISH.replace("0x00000708", "0x00000000")), 0)

    def test_junk_is_unknown_not_a_crash(self):
        for junk in ("", None, "Access denied", "0x00000000"):
            self.assertIsNone(travel.parse_powercfg_ac(junk))

    def test_a_sleeping_pc_is_a_failure_and_a_held_one_is_not(self):
        with mock.patch.object(travel, "sleep_policy", return_value={"standby": 1800, "hibernate": 0}):
            row = travel.check_sleep()
            self.assertEqual(row["status"], "fail")
            self.assertIn("30 min", row["detail"])
            self.assertIn("fix", row)
            with mock.patch.object(travel.HOLD, "_held", True):
                held = travel.check_sleep()                                    # the hold dies with the sign-in: worth a warning, not a green light
                self.assertEqual(held["status"], "warn"); self.assertIn("30 min", held["detail"])
        with mock.patch.object(travel, "sleep_policy", return_value={"standby": 0, "hibernate": 0}):
            self.assertEqual(travel.check_sleep()["status"], "ok")
        with mock.patch.object(travel, "sleep_policy", return_value={"standby": 0, "hibernate": 3600}):
            row = travel.check_sleep()
            self.assertEqual(row["status"], "fail")                            # hibernating is just as unreachable
            self.assertIn("60 min", row["detail"])                             # ...and never reported as "after 0 min"
        with mock.patch.object(travel, "sleep_policy", return_value={"standby": 7200, "hibernate": 1800}):
            self.assertEqual(travel.awake_state()["sleep_after_min"], 30)      # whichever timer fires first

    def test_only_stdout_is_parsed(self):
        code, text = travel._run([sys.executable, "-c", "import sys; print('{\"UDP\": true}'); print('# Warning: not a stable interface', file=sys.stderr)"])
        self.assertEqual((code, json.loads(text)), (0, {"UDP": True}))         # tailscale netcheck warns on stderr; that must not break the JSON
        self.assertEqual(travel._run(["definitely-not-a-program-xyz"]), (None, ""))

    def test_resume_puts_windows_timers_back(self):
        calls = []
        with mock.patch.object(travel, "awake_mode", return_value="awake"), mock.patch.object(travel.HOLD, "set"), \
             mock.patch.object(travel, "IS_WIN", True), mock.patch.object(travel, "sleep_policy", return_value={"standby": 1800, "hibernate": 0}), \
             mock.patch.object(travel, "_run", side_effect=lambda cmd, timeout=6: calls.append(cmd) or (0, "")):
            travel.resume_awake()
        self.assertEqual([c[2:] for c in calls], [["standby-timeout-ac", "0"], ["hibernate-timeout-ac", "0"]])
        with mock.patch.object(travel, "sleep_policy", return_value={"standby": None, "hibernate": None}):
            self.assertEqual(travel.check_sleep()["status"], "warn")


class Tailscale(unittest.TestCase):
    def st(self, **me):
        return {"BackendState": "Running", "Self": dict({"DNSName": "gaming-pc.tail1234.ts.net."}, **me), "Peer": {}}

    def test_not_running_is_the_only_row_and_it_fails(self):
        rows = travel.evaluate_tailscale({"BackendState": "NeedsLogin"}, None, now=NOW)
        self.assertEqual([r["status"] for r in rows], ["fail"])
        self.assertEqual(travel.evaluate_tailscale({}, None, now=NOW)[0]["status"], "fail")
        self.assertEqual(travel.evaluate_tailscale(None, None, now=NOW)[0]["status"], "fail")

    def test_key_expiry(self):
        never = travel.evaluate_tailscale(self.st(), {"ForceDaemon": True}, now=NOW)
        self.assertEqual(status_of(never, "tailscale sign-in"), "ok")
        zero = travel.evaluate_tailscale(self.st(KeyExpiry="0001-01-01T00:00:00Z"), {"ForceDaemon": True}, now=NOW)
        self.assertEqual(status_of(zero, "tailscale sign-in"), "ok")                         # Go's zero time means "never"
        soon = travel.evaluate_tailscale(self.st(KeyExpiry=iso(NOW + 10 * DAY)), {"ForceDaemon": True}, now=NOW)
        self.assertEqual(status_of(soon, "tailscale sign-in"), "fail")
        row = next(r for r in soon if r["name"] == "tailscale sign-in")
        self.assertIn("10 days", row["detail"])
        self.assertIn("Disable key expiry", row["fix"])
        later = travel.evaluate_tailscale(self.st(KeyExpiry=iso(NOW + 150 * DAY)), {"ForceDaemon": True}, now=NOW)
        self.assertEqual(status_of(later, "tailscale sign-in"), "warn")                      # not urgent, never silent
        gone = travel.evaluate_tailscale(self.st(KeyExpiry=iso(NOW - 3 * DAY)), {"ForceDaemon": True}, now=NOW)
        self.assertEqual(status_of(gone, "tailscale sign-in"), "fail")

    def test_unattended(self):
        off = travel.evaluate_tailscale(self.st(), {"ForceDaemon": False}, now=NOW)
        self.assertEqual(status_of(off, "tailscale unattended"), "fail")
        on = travel.evaluate_tailscale(self.st(), {"ForceDaemon": True}, now=NOW)
        self.assertEqual(status_of(on, "tailscale unattended"), "ok")
        mac = travel.evaluate_tailscale(self.st(), None, now=NOW)                             # not a Windows concept: no row at all
        self.assertNotIn("tailscale unattended", [r["name"] for r in mac])

    def test_a_phone_about_to_be_signed_out_is_mentioned(self):
        s = self.st()
        s["Peer"] = {"a": {"HostName": "iphone", "KeyExpiry": iso(NOW + 5 * DAY)},
                     "b": {"HostName": "steamdeck", "Expired": True, "KeyExpiry": iso(NOW - DAY)},
                     "c": {"HostName": "laptop", "KeyExpiry": iso(NOW + 170 * DAY)},
                     "d": {"HostName": "friend", "ShareeNode": True, "KeyExpiry": iso(NOW + DAY)},
                     "e": "junk"}
        rows = travel.evaluate_tailscale(s, {"ForceDaemon": True}, now=NOW)
        row = next(r for r in rows if r["name"] == "your other devices")
        self.assertEqual(row["status"], "warn")
        self.assertIn("iphone", row["detail"]); self.assertIn("steamdeck (expired)", row["detail"])
        self.assertNotIn("laptop", row["detail"]); self.assertNotIn("friend", row["detail"])

    def test_netcheck(self):
        self.assertIsNone(travel.evaluate_netcheck({}))
        self.assertIsNone(travel.evaluate_netcheck("nope"))
        self.assertEqual(travel.evaluate_netcheck({"UDP": True, "MappingVariesByDestIP": False})["status"], "ok")
        self.assertEqual(travel.evaluate_netcheck({"UDP": True, "MappingVariesByDestIP": True, "UPnP": True})["status"], "ok")
        hard = travel.evaluate_netcheck({"UDP": True, "MappingVariesByDestIP": True, "UPnP": False, "PMP": False, "PCP": False})
        self.assertEqual(hard["status"], "warn"); self.assertIn("UPnP", hard["fix"])
        self.assertEqual(travel.evaluate_netcheck({"UDP": False})["status"], "warn")


class SteamSignIn(unittest.TestCase):
    def root(self, body):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "config"))
        with open(os.path.join(d, "config", "loginusers.vdf"), "w") as f:
            f.write(body)
        return d

    def test_remember_me(self):
        yes = self.root('"users"\n{\n "76561198000000001"\n {\n  "AccountName" "a"\n  "RememberPassword" "1"\n  "AllowAutoLogin" "1"\n  "MostRecent" "1"\n }\n}\n')
        no = self.root('"users"\n{\n "76561198000000001"\n {\n  "AccountName" "a"\n  "RememberPassword" "0"\n  "AllowAutoLogin" "0"\n  "MostRecent" "1"\n }\n}\n')
        self.assertTrue(travel.steam_autologin(yes))
        self.assertFalse(travel.steam_autologin(no))
        self.assertIsNone(travel.steam_autologin(None))
        self.assertIsNone(travel.steam_autologin(self.root("not vdf {{{{")))
        self.assertEqual(travel.check_steam_login(no)["status"], "fail")
        self.assertEqual(travel.check_steam_login(None)["status"], "warn")
        self.assertEqual(travel.check_steam_login(yes)["status"], "ok")

    SC = ("\r\nSERVICE_NAME: ApolloService\r\nDISPLAY_NAME: Apollo Service\r\n        TYPE               : 10  WIN32_OWN_PROCESS\r\n"
          "        STATE              : 4  RUNNING\r\n                                (STOPPABLE, NOT_PAUSABLE)\r\n\r\n"
          "SERVICE_NAME: Tailscale\r\nDISPLAY_NAME: Tailscale\r\n        STATE              : 4  RUNNING\r\n")

    def test_sc_service_names(self):
        self.assertEqual(travel.parse_sc_services(self.SC), ["ApolloService", "Tailscale"])
        german = self.SC.replace("SERVICE_NAME", "DIENSTNAME").replace("DISPLAY_NAME", "ANZEIGENAME").replace("STATE ", "STATUS")
        self.assertEqual(travel.parse_sc_services(german), ["ApolloService", "Tailscale"])        # labels are translated, the layout is not
        self.assertEqual(travel.parse_sc_services(None), [])

    def test_apollo_service_row_in_any_language(self):
        def fake(cmd, timeout=6):
            if cmd[1] == "qc":
                return 0, "DIENSTNAME: ApolloService\r\n        STARTTYP           : 2   AUTO_START\r\n"
            if len(cmd) == 3:
                return 0, "DIENSTNAME: ApolloService\r\n        STATUS             : 4  RUNNING\r\n"
            return 0, self.SC.replace("SERVICE_NAME", "DIENSTNAME")
        with mock.patch.object(travel, "_run", side_effect=fake):
            self.assertEqual(travel.check_apollo_service()["status"], "ok")
        def stopped(cmd, timeout=6):
            if cmd[1] == "qc":
                return 0, "SERVICE_NAME: ApolloService\r\n        START_TYPE         : 3   DEMAND_START\r\n"
            if len(cmd) == 3:
                return 0, "SERVICE_NAME: ApolloService\r\n        STATE              : 1  STOPPED\r\n"
            return 0, self.SC
        with mock.patch.object(travel, "_run", side_effect=stopped):
            row = travel.check_apollo_service()
            self.assertEqual(row["status"], "fail"); self.assertIn("NOT running", row["detail"])


class Verdict(unittest.TestCase):
    def setUp(self):                                                        # the Windows-only rows read the real machine: keep them out of these
        p = mock.patch.object(travel, "IS_WIN", False); p.start(); self.addCleanup(p.stop)

    def test_a_broken_check_never_breaks_the_report(self):
        def boom():
            raise RuntimeError("x")
        with mock.patch.object(travel, "check_sleep", boom), \
             mock.patch.object(travel, "check_tailscale_travel", return_value=[travel._c("tailscale", True, "connected")]), \
             mock.patch.object(travel, "awake_state", return_value={"mode": "sleep", "holding": False, "never_sleeps": False, "sleep_after_min": 30, "supported": True}):
            rep = travel.preflight(None, network=False)
        self.assertTrue(rep["ready"])                                      # a check that couldn't run is a warning
        self.assertIn("warn", [c["status"] for c in rep["checks"]])
        self.assertIn("checked_at", rep)

    def test_first_failure_is_the_headline(self):
        with mock.patch.object(travel, "check_sleep", return_value=travel._c("stays awake", False, "goes to sleep", fix="do x")), \
             mock.patch.object(travel, "check_tailscale_travel", return_value=[travel._c("tailscale", True, "connected")]), \
             mock.patch.object(travel, "awake_state", return_value={}):
            rep = travel.preflight(None, network=False)
        self.assertFalse(rep["ready"])
        self.assertEqual(rep["headline"], "Not ready: goes to sleep")
        self.assertEqual(rep["status"], "fail")


class AwakeSwitch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = mock.patch.object(steamaccount, "CONFIG_PATH", os.path.join(self.tmp.name, "config.json"))
        self.cfg.start()

    def tearDown(self):
        self.cfg.stop()
        self.tmp.cleanup()

    def test_the_choice_is_saved_and_applied(self):
        calls = []
        with mock.patch.object(travel.HOLD, "set", side_effect=lambda on: calls.append(on) or on), \
             mock.patch.object(travel, "_run", return_value=(0, "")), \
             mock.patch.object(travel, "sleep_policy", return_value={"standby": 0, "hibernate": 0}):
            self.assertEqual(travel.awake_mode(), "sleep")                 # nothing saved yet: the PC's own settings rule
            st = travel.set_awake("awake")
            self.assertEqual((st["mode"], travel.awake_mode(), calls), ("awake", "awake", [True]))
            travel.set_awake("sleep")
            self.assertEqual((travel.awake_mode(), calls), ("sleep", [True, False]))
            travel.set_awake("anything else")
            self.assertEqual(travel.awake_mode(), "sleep")
            travel.set_awake("awake", apply_now=False)                     # the installer's --set-awake: save only
            self.assertEqual((travel.awake_mode(), calls), ("awake", [True, False, False]))
        with open(steamaccount.CONFIG_PATH) as f:
            self.assertEqual(json.load(f)["awake"], "awake")

    def test_the_steam_key_is_untouched_by_the_switch(self):
        steamaccount.save_config(steam_key="A" * 32)
        with mock.patch.object(travel.HOLD, "set"), mock.patch.object(travel, "_run", return_value=(0, "")), \
             mock.patch.object(travel, "sleep_policy", return_value={"standby": 0, "hibernate": 0}):
            travel.set_awake("awake")
        self.assertEqual(steamaccount.load_config()["steam_key"], "A" * 32)

    def test_hold_thread_applies_and_releases(self):
        hold = travel.AwakeHold()
        seen = []
        def apply(on):
            seen.append(on); hold._held = on
        with mock.patch.object(hold, "_apply", side_effect=apply), mock.patch.object(travel.AwakeHold, "supported", return_value=True):
            self.assertTrue(hold.set(True))
            self.assertTrue(hold.set(True))                                # asking twice doesn't re-apply
            self.assertFalse(hold.set(False))
        self.assertEqual(seen, [True, False])


class Sessions(unittest.TestCase):
    def test_sessions_survive_a_restart_but_not_a_pin_change(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "sessions.json")
            a = ab.Bridge(None, "host", "", pin="2550")
            a.load_sessions(path)
            _, tok = a.try_pin("10.0.0.2", "2550")
            with open(path) as f:
                saved = f.read()
            self.assertNotIn(tok, saved)                                   # only a hash of the cookie is ever written down
            b = ab.Bridge(None, "host", "", pin="2550")
            b.load_sessions(path)
            self.assertTrue(b.session_ok(tok))                             # same PIN, new process: still signed in
            c = ab.Bridge(None, "host", "", pin="9999")
            c.load_sessions(path)
            self.assertFalse(c.session_ok(tok))                            # PIN changed: everyone out
            b.end_session(tok)
            d2 = ab.Bridge(None, "host", "", pin="2550")
            d2.load_sessions(path)
            self.assertFalse(d2.session_ok(tok))                           # Lock means locked, across restarts too

    def test_junk_on_disk_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sessions.json")
            for junk in ("", "[]", '{"pin": 5, "sessions": 7}', '{"sessions": {"short": 99999999999}}'):
                with open(path, "w") as f:
                    f.write(junk)
                b = ab.Bridge(None, "host", "", pin="2550")
                b.load_sessions(path)
                self.assertEqual(b._sessions, {})
            self.assertEqual(b.try_pin("ip", "2550")[0], "ok")

    def test_nothing_is_written_unless_asked(self):
        b = ab.Bridge(None, "host", "", pin="2550")
        with mock.patch("builtins.open", side_effect=AssertionError("no disk writes by default")):
            self.assertEqual(b.try_pin("ip", "2550")[0], "ok")


class RunningUpdate(unittest.TestCase):
    def setUp(self):
        self.b = ab.Bridge(None, "host", "", pin="2550")

    def test_no_update_no_restart(self):
        with mock.patch.object(ab.selfcare, "update_status", return_value={"available": False, "remote": "b" * 40}), \
             mock.patch.object(ab.selfcare, "apply_update") as ap:
            self.assertIsNone(ab.auto_update_once(self.b))
            ap.assert_not_called()

    def test_never_under_someone_who_just_pressed_stream(self):
        self.b.last_launch = time.time() - 60
        with mock.patch.object(ab.selfcare, "update_status") as us:
            self.assertIsNone(ab.auto_update_once(self.b))
            us.assert_not_called()

    def test_applies_and_reports_the_new_commit(self):
        with mock.patch.object(ab.selfcare, "update_status", return_value={"available": True, "remote": "b" * 40}), \
             mock.patch.object(ab.selfcare, "apply_update", return_value=(True, "updated", "a" * 40, "b" * 40)):
            self.assertEqual(ab.auto_update_once(self.b), "b" * 40)

    def test_a_failed_or_empty_update_never_restarts(self):
        with mock.patch.object(ab.selfcare, "update_status", return_value={"available": True, "remote": "b" * 40}):
            with mock.patch.object(ab.selfcare, "apply_update", return_value=(False, "tests failed - rolled back", "a" * 40, "a" * 40)):
                self.assertIsNone(ab.auto_update_once(self.b))
            with mock.patch.object(ab.selfcare, "apply_update", return_value=(True, "already current", "a" * 40, "a" * 40)):
                self.assertIsNone(ab.auto_update_once(self.b))

    def test_one_restart_per_version(self):
        with mock.patch.dict(os.environ, {"APOLLO_RESTARTED_FOR": "b" * 40}), \
             mock.patch.object(ab.selfcare, "update_status", return_value={"available": True, "remote": "b" * 40}), \
             mock.patch.object(ab.selfcare, "apply_update") as ap:
            self.assertIsNone(ab.auto_update_once(self.b))
            ap.assert_not_called()

    def test_under_the_startup_task_a_restart_is_just_an_exit(self):
        with mock.patch.object(ab.subprocess, "call") as spawn, mock.patch.object(ab.os, "execve") as execve:
            with self.assertRaises(SystemExit) as e:
                ab.restart("c" * 40, supervised=True)                  # the every-minute watchdog starts the new code; no parked parent
            self.assertEqual(e.exception.code, 0)
            spawn.assert_not_called(); execve.assert_not_called()

    def test_the_loop_stops_the_server_and_names_the_commit(self):
        class Srv:
            stopped = False
            def shutdown(self):
                self.stopped = True
        srv, state = Srv(), {}
        with mock.patch.object(ab, "auto_update_once", side_effect=[RuntimeError("offline"), None, "c" * 40]), \
             mock.patch.object(ab.time, "sleep"):
            ab.auto_update_loop(self.b, srv, state, every=0)
        self.assertTrue(srv.stopped)
        self.assertEqual(state, {"restart_to": "c" * 40})


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = ab.Bridge(None, "testhost", "", pin="2550")
        cls.bridge.network = False
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), ab.make_handler(cls.bridge))
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, method, path, body=None, cookie=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {}
        if body is not None:
            body = json.dumps(body); h["Content-Type"] = "application/json"
        if cookie:
            h["Cookie"] = "%s=%s" % (ab.SESSION_COOKIE, cookie)
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")

    def cookie(self):
        _, tok = self.bridge.try_pin("127.0.0.9", "2550")
        return tok

    def test_both_are_behind_the_pin(self):
        self.assertEqual(self.req("GET", "/api/preflight")[0], 401)
        self.assertEqual(self.req("POST", "/api/awake", {"mode": "awake"})[0], 401)

    def test_preflight_and_host_report_the_real_state(self):
        fake = {"mode": "awake", "holding": True, "never_sleeps": True, "sleep_after_min": 0, "supported": True}
        with mock.patch.object(travel, "IS_WIN", False), mock.patch.object(travel, "awake_state", return_value=fake), \
             mock.patch.object(travel, "check_tailscale_travel", return_value=[travel._c("tailscale", True, "connected")]), \
             mock.patch.object(travel, "check_sleep", return_value=travel._c("stays awake", True, "held")):
            st, rep = self.req("GET", "/api/preflight?fresh=1", cookie=self.cookie())
            self.assertEqual(st, 200)
            self.assertIn("headline", rep); self.assertIn("checks", rep)
            self.bridge._awake = None
            st, host = self.req("GET", "/api/host", cookie=self.cookie())
            self.assertEqual((st, host["awake"], host["awake_state"]["mode"]), (200, True, "awake"))

    def test_check_again_cannot_be_hammered(self):
        tok = self.cookie()
        with mock.patch.object(travel, "preflight", return_value={"ready": True, "status": "ok", "headline": "Ready to travel", "checks": [], "awake": {}, "checked_at": 1}) as pf:
            self.bridge._preflight = None
            for _ in range(4):
                self.assertEqual(self.req("GET", "/api/preflight?fresh=1", cookie=tok)[0], 200)
            self.assertEqual(pf.call_count, 1)                             # one real run per few seconds, however often the button is pressed

    def test_version_says_whether_this_bridge_updates_itself(self):
        tok = self.cookie()
        self.assertIs(self.req("GET", "/api/version", cookie=tok)[1]["auto"], False)
        self.bridge.auto_update = True
        try:
            self.assertIs(self.req("GET", "/api/version", cookie=tok)[1]["auto"], True)
        finally:
            self.bridge.auto_update = False

    def test_the_switch(self):
        tok = self.cookie()
        with mock.patch.object(travel, "set_awake", return_value={"mode": "awake", "holding": True, "never_sleeps": True, "sleep_after_min": 0, "supported": True}) as sa:
            st, out = self.req("POST", "/api/awake", {"mode": "awake"}, cookie=tok)
            self.assertEqual((st, out["ok"], out["holding"]), (200, True, True))
            sa.assert_called_once_with("awake")
            for bad in ({"mode": "on"}, {"mode": 1}, {}, [1], "awake"):
                self.assertEqual(self.req("POST", "/api/awake", bad, cookie=tok)[0], 400, bad)
            self.assertEqual(sa.call_count, 1)


if __name__ == "__main__":
    unittest.main()
