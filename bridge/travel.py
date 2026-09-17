"""Will this PC still be reachable next week? The "ready to travel" check, and the real Always-awake switch.

Stdlib only, read-only, never raises. Everything that can leave the PC unreachable while its owner is away
is looked at here: sleep, Tailscale (signed in, runs unattended, sign-in about to expire), a Windows restart
waiting to happen, the startup task, Apollo's service, Steam's saved sign-in.
"""
import calendar
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time

import selfcare
import steamaccount

TASK_NAME = "Apollo Home Stream bridge"
TS_ADMIN = "https://login.tailscale.com/admin/machines"
NO_WINDOW = selfcare.NO_WINDOW                                             # never flash a console under pythonw.exe
KEY_MARGIN_DAYS = 28            # a Tailscale sign-in that lapses within a trip's length (+ slack) is a failure, not a warning
IS_WIN = os.name == "nt"


def _run(cmd, timeout=6):
    """(exit_code, stdout). A missing program, a hang, a permission error: all come back as (None, "").
    stdout only: `tailscale netcheck --format=json` prints a warning on stderr that would break the JSON."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace", **NO_WINDOW)
        return r.returncode, (r.stdout or "")
    except Exception:  # noqa: BLE001
        return None, ""


def _c(name, ok, detail, fail=True, fix=""):
    row = selfcare._c(name, ok, detail, fail=fail)
    if fix and not ok:
        row["fix"] = fix
    return row


def tailscale_exe():
    exe = shutil.which("tailscale") or (r"C:\Program Files\Tailscale\tailscale.exe" if IS_WIN else "/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    return exe if exe and os.path.exists(exe) else None


# ----------------------------------------------------------------------------- sleep
def parse_powercfg_ac(text):
    """Seconds until the setting kicks in on mains power, from `powercfg /q ...` output; None if unreadable.
    Labels are translated on non-English Windows, the numbers are not: the last two hex values are AC then DC."""
    vals = re.findall(r"0x([0-9a-fA-F]{8})\b", text or "")
    if len(vals) < 2:
        return None
    return int(vals[-2], 16)


def sleep_policy():
    """{"standby": seconds|None, "hibernate": seconds|None}  - 0 means never."""
    if platform.system() == "Darwin":
        _, text = _run(["pmset", "-g"])
        m = re.search(r"^\s*sleep\s+(\d+)", text, re.M)
        return {"standby": int(m.group(1)) * 60 if m else None, "hibernate": None}
    if not IS_WIN:
        return {"standby": None, "hibernate": None}
    out = {}
    for key, alias in (("standby", "STANDBYIDLE"), ("hibernate", "HIBERNATEIDLE")):
        code, text = _run(["powercfg", "/q", "SCHEME_CURRENT", "SUB_SLEEP", alias])
        out[key] = parse_powercfg_ac(text) if code == 0 else None
    return out


class AwakeHold:
    """Keeps the PC from sleeping while it is on. On Windows the request belongs to the thread that made it,
    so one long-lived thread owns it; it needs no admin rights and shows up in `powercfg /requests`.
    On a Mac the same job is done by `caffeinate -s`."""
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

    def __init__(self):
        self._want = False
        self._held = False
        self._cond = threading.Condition()
        self._thread = None
        self._proc = None

    @property
    def held(self):
        return self._held

    def set(self, on):
        on = bool(on)
        with self._cond:
            self._want = on
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, daemon=True, name="awake-hold")
                self._thread.start()
            self._cond.notify_all()
        for _ in range(40):                                  # let the owner thread apply it before we report back
            if self._held == on or not self.supported():
                break
            time.sleep(0.05)
        return self._held

    @staticmethod
    def supported():
        return IS_WIN or platform.system() == "Darwin"

    def _apply(self, on):
        try:
            if IS_WIN:
                import ctypes
                flags = self.ES_CONTINUOUS | (self.ES_SYSTEM_REQUIRED if on else 0)
                self._held = bool(ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))) and on
            elif platform.system() == "Darwin":
                if on and not self._proc:
                    self._proc = subprocess.Popen(["caffeinate", "-s", "-w", str(os.getpid())])
                elif not on and self._proc:
                    self._proc.terminate(); self._proc = None
                self._held = bool(self._proc)
        except Exception:  # noqa: BLE001
            self._held = False

    def _loop(self):
        applied = None
        while True:
            with self._cond:
                while self._want == applied:
                    self._cond.wait(60)
                want = self._want
            self._apply(want)
            applied = want


HOLD = AwakeHold()


def awake_mode():
    m = steamaccount.load_config().get("awake")
    return "awake" if m == "awake" else "sleep"


def awake_state():
    """What the page shows: the saved choice, whether the bridge is holding the PC awake, and Windows' own timer."""
    pol = sleep_policy()
    never = pol["standby"] == 0 and pol["hibernate"] in (0, None)
    timers = [t for t in (pol["standby"], pol["hibernate"]) if t]           # whichever fires first is when it drops off
    return {"mode": awake_mode(), "holding": HOLD.held, "never_sleeps": bool(never or HOLD.held), "windows_never": bool(never),
            "sleep_after_min": None if pol["standby"] is None else (min(timers) // 60 if timers else 0), "supported": HOLD.supported()}


def set_awake(mode, apply_now=True):
    """Save the choice and make it true: hold the PC awake (and set Windows' own timers to Never), or let it sleep again."""
    mode = "awake" if mode == "awake" else "sleep"
    steamaccount.save_config(awake=mode)
    if apply_now:
        HOLD.set(mode == "awake")
        if IS_WIN:
            if mode == "awake":
                _run(["powercfg", "/change", "standby-timeout-ac", "0"])
                _run(["powercfg", "/change", "hibernate-timeout-ac", "0"])
            elif sleep_policy().get("standby") == 0:
                _run(["powercfg", "/change", "standby-timeout-ac", "30"])      # "Sleep when idle" has to mean it
    return awake_state()


def resume_awake():
    """Called when the bridge starts: pick the saved choice back up - including Windows' own timers, which a
    vendor 'gaming mode' tool may have switched to another power plan since."""
    if awake_mode() == "awake":
        HOLD.set(True)
        if IS_WIN:
            pol = sleep_policy()
            if pol["standby"] or pol["hibernate"]:
                _run(["powercfg", "/change", "standby-timeout-ac", "0"])
                _run(["powercfg", "/change", "hibernate-timeout-ac", "0"])


def check_sleep():
    st = awake_state()
    if st["windows_never"]:
        return _c("stays awake", True, "Windows is set to never sleep" + (", and the bridge holds it awake too" if st["holding"] else ""))
    if st["holding"]:                                        # the hold only exists while someone is signed in and the bridge runs
        return _c("stays awake", False, "kept awake only while you're signed in - Windows' own timer is still %s min, so it would sleep at the sign-in screen after a restart"
                  % st["sleep_after_min"], fail=False, fix="Run the one-line installer again - it sets Windows itself to never sleep.")
    if st["sleep_after_min"] is None:
        return _c("stays awake", False, "couldn't read this system's sleep setting", fail=False)
    return _c("stays awake", False, "this PC goes to sleep after %d min idle - and a sleeping PC can't be woken from outside your home" % st["sleep_after_min"],
              fix="Press 'Keep it always awake' on this page before you leave.")


# ----------------------------------------------------------------------------- tailscale
def _days_left(iso, now=None):
    """Whole days until an RFC 3339 time; None if absent/unparseable. Tailscale uses 0001-01-01 for "never"."""
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)", str(iso or ""))
    if not m or int(m.group(1)) < 1971:
        return None
    t = calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0))
    return int((t - (now or time.time())) // 86400)


def evaluate_tailscale(status, prefs, margin_days=KEY_MARGIN_DAYS, now=None):
    """Rows from `tailscale status --json` (+ `debug prefs`). Pure, so it can be tested with saved output."""
    rows = []
    state = (status or {}).get("BackendState")
    me = (status or {}).get("Self") or {}
    if state != "Running":
        return [_c("tailscale", False, "Tailscale isn't connected (%s) - away from home nothing can reach this PC" % (state or "no answer"),
                   fix="Open Tailscale on this PC and sign in.")]
    rows.append(_c("tailscale", True, "connected as %s" % ((me.get("DNSName") or "").rstrip(".") or "this PC")))
    if prefs is not None:
        rows.append(_c("tailscale unattended", bool(prefs.get("ForceDaemon")),
                       "stays connected when nobody is signed in to Windows" if prefs.get("ForceDaemon")
                       else "disconnects whenever Windows is at the sign-in screen (after a restart or power cut)",
                       fix="Tailscale tray icon > Preferences > Run unattended - or run the installer again."))
    days = _days_left(me.get("KeyExpiry"), now)
    if days is None:
        rows.append(_c("tailscale sign-in", True, "never expires"))
    elif days < margin_days:
        rows.append(_c("tailscale sign-in", False, "this PC gets signed out of Tailscale in %d day%s" % (max(days, 0), "" if days == 1 else "s"),
                       fix="On %s open the ... menu next to this PC and choose Disable key expiry." % TS_ADMIN))
    else:
        rows.append(_c("tailscale sign-in", False, "expires in %d days - fine for now, but it will sign this PC out one day" % days, fail=False,
                       fix="On %s open the ... menu next to this PC and choose Disable key expiry." % TS_ADMIN))
    soon = []
    for p in ((status or {}).get("Peer") or {}).values():
        if not isinstance(p, dict) or p.get("ShareeNode"):
            continue
        d = _days_left(p.get("KeyExpiry"), now)
        if p.get("Expired") or (d is not None and d < margin_days):
            soon.append("%s (%s)" % ((p.get("HostName") or "a device"), "expired" if p.get("Expired") or d < 0 else "%d days" % d))
    if soon:
        rows.append(_c("your other devices", False, "Tailscale sign-in running out on: " + ", ".join(sorted(soon)[:6]), fail=False,
                       fix="Open Tailscale on that device and sign in again before you leave."))
    return rows


def check_tailscale_travel():
    exe = tailscale_exe()
    if not exe:
        return [_c("tailscale", False, "Tailscale isn't installed - away from home nothing can reach this PC", fix="Install Tailscale from the Install tab.")]
    code, text = _run([exe, "status", "--json"])
    try:
        status = json.loads(text) if code == 0 else {}
    except ValueError:
        status = {}
    prefs = None
    if IS_WIN:                                               # "unattended" only exists on Windows
        code, text = _run([exe, "debug", "prefs"])
        try:
            prefs = json.loads(text) if code == 0 else None
        except ValueError:
            prefs = None
    return evaluate_tailscale(status, prefs)


def evaluate_netcheck(rep):
    """One row from `tailscale netcheck --format=json`: can outside devices reach this house directly?"""
    if not isinstance(rep, dict) or not rep:
        return None
    if not rep.get("UDP"):
        return _c("direct connection", False, "this network blocks UDP, so streams from outside go through a slow relay", fail=False,
                  fix="Check the router isn't blocking UDP; allow UDP 41641 to this PC.")
    if rep.get("MappingVariesByDestIP") and not (rep.get("UPnP") or rep.get("PMP") or rep.get("PCP")):
        return _c("direct connection", False, "your router makes direct connections hard - from some hotels the stream will be relayed and blurry", fail=False,
                  fix="Turn on UPnP (or NAT-PMP) in the router, or forward UDP 41641 to this PC.")
    return _c("direct connection", True, "outside devices can connect straight to this PC")


def check_netcheck():
    exe = tailscale_exe()
    if not exe:
        return None
    code, text = _run([exe, "netcheck", "--format=json"], timeout=12)
    try:
        return evaluate_netcheck(json.loads(text)) if code == 0 else None
    except ValueError:
        return None


# ----------------------------------------------------------------------------- windows
def _reg(root, path, name=None):
    """A registry value; with name=None, whether the key exists. None when absent / not Windows."""
    if not IS_WIN:
        return None
    try:
        import winreg
        with winreg.OpenKey(getattr(winreg, root), path) as k:
            return True if name is None else winreg.QueryValueEx(k, name)[0]
    except Exception:  # noqa: BLE001
        return None


def check_reboot_pending():
    pending = _reg("HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired") or \
              _reg("HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending")
    if pending:
        return _c("windows restart", False, "Windows is waiting to restart for an update", fail=False,
                  fix="Restart the PC now, while you're still next to it - not by itself next week.")
    return _c("windows restart", True, "no restart waiting")


def check_autologon():
    on = str(_reg("HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon", "AutoAdminLogon") or "") == "1"
    if on:
        return _c("after a restart", True, "signs itself back in")
    return _c("after a restart", False, "after a power cut (and on most PCs after a Windows update too) it waits at the sign-in screen - you can still get in: open Moonlight/Artemis, choose Desktop, type your password", fail=False,
              fix="Optional: run the installer with -AutoLogon. Also set the BIOS to power on when mains power returns.")


def check_task():
    code, _ = _run(["schtasks", "/query", "/tn", TASK_NAME])
    if code == 0:
        return _c("starts by itself", True, "startup task is in place (re-checked every minute)")
    return _c("starts by itself", False, "this bridge was started by hand - it won't come back after a restart",
              fix="Run the one-line installer from the Install tab.")


def parse_sc_services(text):
    """Service names from `sc query`: the first "label: value" line of each blank-line-separated block. The labels
    are translated on non-English Windows (DIENSTNAME, NOM_SERVICE...); the layout and the value tokens are not."""
    names = []
    for block in re.split(r"\r?\n[ \t]*\r?\n", text or ""):
        first = next((l for l in block.splitlines() if l.strip()), "")
        if ":" in first:
            names.append(first.split(":", 1)[1].strip())
    return [n for n in names if n]


def check_apollo_service():
    code, text = _run(["sc", "query", "type=", "service", "state=", "all"], timeout=10)
    names = [n for n in parse_sc_services(text) if "apollo" in n.lower() or "sunshine" in n.lower()]
    if code != 0 or not names:
        return _c("apollo service", False, "Apollo's Windows service wasn't found", fail=False, fix="Install Apollo from the Install tab.")
    _, qc = _run(["sc", "qc", names[0]])
    _, q = _run(["sc", "query", names[0]])
    auto = bool(re.search(r":\s*2\s+AUTO_START", qc))
    running = bool(re.search(r":\s*4\s+RUNNING", q))
    if auto and running:
        return _c("apollo service", True, "running, starts with Windows")
    return _c("apollo service", False, "%s, %s" % ("running" if running else "NOT running", "starts with Windows" if auto else "does NOT start with Windows"),
              fail=not running, fix="Run the installer again - it sets Apollo to start and restart itself.")


def steam_autologin(steam_root):
    """True/False from Steam's own record of the last account; None when unknown."""
    path = os.path.join(steam_root or "", "config", "loginusers.vdf")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            users = steamaccount._ci(steamaccount.parse_vdf(f.read(4 * 1024 * 1024)), "users")
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(users, dict):
        return None
    for info in users.values():
        if isinstance(info, dict) and steamaccount._ci(info, "MostRecent") == "1":
            return steamaccount._ci(info, "AllowAutoLogin") == "1" and steamaccount._ci(info, "RememberPassword") == "1"
    return None


def check_steam_login(steam_root):
    auto = steam_autologin(steam_root)
    if auto is None:
        return _c("steam sign-in", False, "no saved Steam sign-in found on this PC", fail=False, fix="Open Steam and sign in with 'Remember me' ticked.")
    if not auto:
        return _c("steam sign-in", False, "Steam will ask for a password after a restart - nothing can be launched until someone types it",
                  fix="Sign in to Steam once more with 'Remember me' ticked.")
    if not IS_WIN:
        return _c("steam sign-in", True, "remembered")
    starts = _reg("HKEY_CURRENT_USER", r"Software\Microsoft\Windows\CurrentVersion\Run", "Steam")
    return _c("steam sign-in", True, "remembered" + (", Steam starts with Windows" if starts else " (Steam starts when you launch a game)"))


# ----------------------------------------------------------------------------- the verdict
def preflight(bridge=None, network=True):
    steam_root = getattr(bridge, "steam_root", None)
    jobs = [check_sleep, check_tailscale_travel, lambda: check_steam_login(steam_root)]
    if IS_WIN:
        jobs += [check_task, check_apollo_service, check_reboot_pending, check_autologon]
    if network:
        jobs.append(check_netcheck)
    results = [None] * len(jobs)

    def work(i, fn):
        try:
            results[i] = fn()
        except Exception as e:  # noqa: BLE001 - a broken check is a warning, never a crash
            results[i] = _c(getattr(fn, "__name__", "check"), False, "couldn't be checked (%s)" % type(e).__name__, fail=False)
    threads = [threading.Thread(target=work, args=(i, fn), daemon=True) for i, fn in enumerate(jobs)]
    for t in threads: t.start()
    for t in threads: t.join(15)
    checks = []
    for r in results:
        if isinstance(r, list):
            checks += r
        elif r:
            checks.append(r)
    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    if fails:
        headline = "Not ready: " + fails[0]["detail"]
    elif warns:
        headline = "Ready - with %d thing%s worth a look" % (len(warns), "" if len(warns) == 1 else "s")
    else:
        headline = "Ready to travel"
    return {"ready": not fails, "status": selfcare._rollup(checks), "headline": headline, "checks": checks,
            "awake": awake_state(), "checked_at": int(time.time())}


def print_preflight(rep):
    marks = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}
    print(rep["headline"])
    for c in rep["checks"]:
        print("  [%s] %-22s %s" % (marks.get(c["status"], "?"), c["name"], c["detail"]))
        if c.get("fix"):
            print("         %-22s -> %s" % ("", c["fix"]))
