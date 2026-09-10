# Apollo Home Stream

[![tests](https://github.com/ByteSizeData/apollo-home-stream/actions/workflows/ci.yml/badge.svg)](https://github.com/ByteSizeData/apollo-home-stream/actions/workflows/ci.yml)

Your own GeForce NOW — except the games are already on your PC.

A small web page and a tiny bridge service for [Apollo](https://github.com/ClassicOldSong/Apollo)
(the self-hosted stream host) and [Artemis](https://github.com/ClassicOldSong/moonlight-android) /
[Moonlight](https://moonlight-stream.org/) clients. Three tabs:

- **Play** — your real Steam library, most-recently-played first, with a Launch button that
  starts the game on the gaming PC. Then you connect with Artemis or Moonlight to see it.
- **Install** — three installs: Apollo on the PC, the right client for each device, and
  **Tailscale on both** (that's what makes it work from the road), plus pairing and its permissions trap.
- **Sleep & wake** — set the PC to sleep-and-wake-on-demand or always-awake, with the exact
  Windows and Mac commands.

The Install and Sleep & wake pages are plain documentation. The Play page becomes real when
the bridge is running.

**Live site:** https://bytesizedata.github.io/apollo-home-stream/ — the guide plus a sample library.
Your real games only appear when you open the page *from the bridge on your gaming PC* (below),
because the bridge is what reads your Steam folder.

## Run it

On the **gaming PC** (the machine with Steam and Apollo on it):

```bash
python bridge/apollo_bridge.py
```

Then from any screen on your network — or your tailnet — open:

```
http://<your-pc-name>:8777
```

The bridge prints that address when it starts - and, if Tailscale is running, the Tailscale
address too. **Use the Tailscale address on phones and laptops**: it works at home and on the
road, so each screen is set up once.

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
--set-steam-key KEY    store your Steam Web API key on this PC once (then omit it; see Your Steam account)
--steam-key KEY        use a key for this run only; --forget-steam-key removes the stored one
--pin 4821             change the PIN (default 2550); --pin "" turns the gate off entirely
--token secret         additionally require an X-Apollo-Token header to launch (see Security)
--dry-run              print what it found and exit — try this first
--self-test            check Steam, Apollo, ports, network, then run the tests, and exit
--check-update         say whether a newer version is on GitHub
--update               fetch the newest version (git pull, or a verified zip), run the tests, roll back on failure
--auto-update          apply updates at startup and restart (or APOLLO_AUTO_UPDATE=1)
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

## Your Steam account

**Sync with Steam (recommended).** Give the bridge your Steam Web API key once and the page
stays in step with Steam itself: what you played last - on *any* device, Deck, laptop, anywhere -
your total hours, the last two weeks, your avatar, and every game you own but haven't installed
on this PC (tap a cover to start installing it from the couch). It refreshes every five minutes
in the background, whether or not you're using Apollo.

1. Get a key at https://steamcommunity.com/dev/apikey (sign in; any domain name is fine, e.g. `localhost`).
2. On the gaming PC, once:

```
python bridge/apollo_bridge.py --set-steam-key YOURKEY
```

3. Start the bridge normally. The banner says `Steam sync: on`.

The key is stored in `~/.apollo-home-stream/config.json` on the PC, readable only by you. It is
never sent to the page or anywhere except Steam. Because it's *your* key, syncing works even if
your Steam profile is private. `--forget-steam-key` removes it.

**Without a key** the bridge still works, from Steam's own record on this PC: who's signed in,
hours and last-played for installed games, and games you've played here before but since
uninstalled. That record only knows about this PC, so play on another device won't show up.

Both tiers sit behind the PIN like everything else.

## Show your account on the website

The public site can't see your PC. It *can* carry a copy of your Steam library that GitHub
refreshes from Steam **every 30 minutes** - what you played last on any device, hours, the last
two weeks, your avatar - encrypted with your PIN so only you can read it. Then every screen in
the house opens the same link, enters the PIN, and gets a **Stream** button on every game.

1. Get a Web API key at https://steamcommunity.com/dev/apikey and find your Steam ID
   (your profile URL: the 17-digit number, or the custom name after `/id/`).
2. In the GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `STEAM_API_KEY` - the key
   - `STEAM_ID` - the 17-digit id, the profile URL, or the custom name
   - `SITE_PASSPHRASE` - optional. Leave it out to unlock with the PIN (2550). A longer
     passphrase is much stronger; the site is public, and a 4-digit PIN only keeps casual eyes out.
3. **Actions → publish site → Run workflow** once. From then on it refreshes itself.

Then on each screen: open the site, enter the PIN, and - the first time - paste the bridge
address the PC prints when it starts (`http://your-pc-name:8777`). After that, **Stream** on any
game hands it to the PC, which launches it and tells you which client to open.

## Keeping it working

**Self-test.** Run this after installing, and any time something feels off:

```
python bridge/apollo_bridge.py --self-test
```

It checks Python, the web files, your Steam folder (and how many games it sees), whether
port 8777 is free, whether Apollo is answering on this PC, whether Tailscale is up, whether
Steam's cover-art CDN and GitHub are reachable — then runs the whole test suite. `OK` rows
work, `warn` rows are optional or explained, `FAIL` rows need fixing. The Play page shows
the same connections as a row of dots under the Continue card.

**Self-update.** The bridge checks GitHub at startup and prints a one-liner if there's a
newer version. `--update` applies it: a git checkout gets `git pull`, a plain download gets
the newest zip of this repo — verified to be this project, backed up first, tests run
afterwards, and rolled back automatically if they fail. `--auto-update` does that on every
start and restarts itself. Updates only ever come from `github.com/ByteSizeData/apollo-home-stream`.

**Continuous tests.** Every push runs the test suite on Linux and Windows across two Python
versions (see the *tests* badge / Actions tab), and every change to `web/` republishes the
live site automatically. Nothing to copy by hand.

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
