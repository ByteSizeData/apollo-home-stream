# Apollo Home Stream

Your own GeForce NOW — except the games are already on your PC.

A small web page and a tiny bridge service for [Apollo](https://github.com/ClassicOldSong/Apollo)
(the self-hosted stream host) and [Artemis](https://github.com/ClassicOldSong/moonlight-android) /
[Moonlight](https://moonlight-stream.org/) clients. Three tabs:

- **Play** — your real Steam library, most-recently-played first, with a Launch button that
  starts the game on the gaming PC. Then you connect with Artemis or Moonlight to see it.
- **Install** — Apollo on the PC, the right client for each device, pairing (and the
  permissions trap), and Tailscale for playing from the road.
- **Sleep & wake** — set the PC to sleep-and-wake-on-demand or always-awake, with the exact
  Windows and Mac commands.

The Install and Sleep & wake pages are plain documentation. The Play page becomes real when
the bridge is running.

## Run it

On the **gaming PC** (the machine with Steam and Apollo on it):

```bash
python bridge/apollo_bridge.py
```

Then from any screen on your network — or your tailnet — open:

```
http://<your-pc-name>:8777
```

You'll be asked for a PIN — **2550** out of the box. Enter it once per device and that screen
stays unlocked for 30 days (or until you press *Lock this screen*).

Press a game. It starts on the PC. Open Artemis or Moonlight, pick the PC, and it's on screen.

Needs Python 3.8+ and nothing else — no `pip install`. On Windows, get Python from
python.org (tick *Add to PATH*), or `winget install Python.Python.3.12`.

### Options

```
--port 9000            serve somewhere other than 8777
--steam "D:\Steam"     if it can't find your Steam folder
--name "Gaming station"  how the PC is shown in the page
--pin 4821             change the PIN (default 2550); --pin "" turns the gate off entirely
--token secret         additionally require an X-Apollo-Token header to launch (see Security)
--dry-run              print what it found and exit — try this first
```

### What the bridge does

- Reads `steamapps/libraryfolders.vdf` and every `appmanifest_*.acf` across all your Steam
  libraries. Only fully-installed games are listed; Proton, runtimes and redistributables
  are skipped.
- Sorts by Steam's own `LastPlayed`, so "recently played" is real.
- Cover art comes straight from Steam's CDN by app id.
- Launch runs `steam://rungameid/<appid>` on the host. It will only launch app ids it found
  in your libraries.
- Serves the `web/` folder. No database, no accounts, no external services.

### Security

The bridge binds to all interfaces so your other devices can reach it, and everything —
the page and the API — sits behind the PIN:

- Wrong guesses are counted per device. After 5 you wait 30 s, then 60, 120… up to 15 min.
  A 4-digit PIN is 10,000 combinations; at those rates a brute-force takes weeks, and you'd
  see the lockout messages. Change it from the default with `--pin` or `APOLLO_PIN=`.
- Unlocking sets an `HttpOnly`, `SameSite=Strict` session cookie with a random 256-bit
  token, valid 30 days. *Lock this screen* in the footer ends it.
- The PIN is compared in constant time and never appears in the page or the logs.
- Lockout counts live in memory and are per device address, so restarting the bridge
  resets them and a determined peer could rotate addresses. That's fine for the
  people-in-your-house threat this is built for; it is not a bank vault.

This is designed for a home network or a tailnet — to keep housemates and guests from
launching things on your PC. It is **not** hardened for the open internet: the bridge
speaks plain HTTP, so on an untrusted network the PIN and cookie travel in the clear.
Do **not** port-forward 8777. Use Tailscale (the Install tab explains), which encrypts
the whole path.

If you want a second factor for launching specifically, run with `--token` and send the
matching `X-Apollo-Token` header.

Do **not** port-forward 8777 to the internet. Use Tailscale (the Install tab explains).

## Development

```bash
python3 -m unittest discover -s tests -v     # Steam file parsing tests
python3 bridge/apollo_bridge.py --dry-run     # see what it finds on this machine
```

`web/index.html` is a single self-contained file — no build step. Open it directly in a
browser and it shows a sample library; served by the bridge it shows yours.

## Status

The Steam parsing is unit-tested against real-format fixtures and the API is
integration-tested against a fake library. It has **not** yet been run against a real
Windows gaming PC with Apollo installed — expect to tweak paths or the launch call on first
run. `--dry-run` is the place to start.

## Not affiliated

Not affiliated with Valve, NVIDIA, Tailscale, or the Apollo / Moonlight projects.
"GeForce NOW" is NVIDIA's; this just aims for the same feel on your own hardware.
