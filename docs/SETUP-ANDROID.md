# Using it from Android

There are two ways. The web UI is the one worth using.

## Option A — the web UI (recommended)

The tool ships a small mobile-friendly web app. Run the server on the machine
that has your rules (Windows or WSL), open it on your phone, and install it to
the home screen. No Play Store, no APK, no second codebase.

### Start the server

```powershell
# Windows
.\scripts\organizer.ps1 serve --host 0.0.0.0 -r rules.yaml
```

```bash
# Linux / WSL
immich-organizer serve --host 0.0.0.0 -r rules.yaml
```

`--host 0.0.0.0` is what makes it reachable from the phone; the default binds
to localhost only. It prints something like:

```
Immich Organizer UI -> http://192.168.1.42:8777/?t=xK3p9vQ2mN7wR5tY8bL1
```

### Open it on the phone

1. Both devices need to be on the same network.
2. Open that whole URL — **including the `?t=...` part** — in Chrome.
3. Menu (⋮) → **Add to Home screen**.

It installs as a standalone app. The token is stored on first open and scrubbed
from the address bar, so the shortcut keeps working without the token sitting
in your history.

### What you can do in it

- **Describe it** — type `person in a mountain`, get a grid of matches.
- **Like this photo** — paste an asset ID to find everything similar.
- Filters, plus *must also match* / *must not match* refinement.
- Tap thumbnails to include or exclude. Everything starts selected, because
  deselecting a bad tail is faster than picking winners.
- Type an album name (existing ones autocomplete) and file the selection.
  Optionally archive or favorite them at the same time.
- **Rules** tab: preview or apply your saved rules, with thumbnail strips.

### Windows Firewall

The first time you bind to `0.0.0.0`, Windows will ask whether to allow Python
through the firewall. Allow it on **private networks only**. If you dismissed
that prompt, add the rule manually:

```powershell
New-NetFirewallRule -DisplayName "Immich Organizer" -Direction Inbound `
  -Protocol TCP -LocalPort 8777 -Action Allow -Profile Private
```

### Security, plainly

Binding to `0.0.0.0` exposes the server to everything on your network. So:

- **Every data route requires the token** in that URL. Treat the link like a
  password — anyone on your network who has it can browse and file your photos.
- **Your Immich API key never leaves the machine.** The phone talks only to
  this local server, which signs the Immich calls itself and proxies
  thumbnails.
- Traffic is plain HTTP on your LAN. Do not port-forward this to the internet.
  If you need remote access, use a VPN or Tailscale and reach it that way.
- Pin the token across restarts so your home-screen shortcut keeps working:

  ```powershell
  $env:IMMICH_ORGANIZER_TOKEN = "some-long-random-string"
  .\scripts\organizer.ps1 serve --host 0.0.0.0 -r rules.yaml
  ```

  Otherwise a fresh token is generated each start, and you will need to reopen
  the new link.

### Keeping it running

The server only needs to be up while you are using the phone. If you want it
always available, run it as a scheduled task at logon, or in a `tmux`/`screen`
session inside WSL.

## Option B — Termux, on the phone itself

If you would rather run the CLI directly on Android:

```bash
pkg update && pkg install python git
git clone https://github.com/KuroViolet57/ImmichPersonal.git
cd ImmichPersonal
pip install -e ".[yaml]"

immich-organizer setup      # point it at your server's LAN address
immich-organizer doctor
```

Everything works the same. The catch is that the CLI's preview is a list of
filenames — for a visual task on a phone, the web UI is much better. Use
`--html out.html` and open the file in a browser if you go this route.

This is worth it mainly if you want the phone to run rules on a schedule
(with Termux:Boot and cron) independently of your PC.

## Why not a native APK?

It would mean a second codebase in Kotlin or Flutter, a build toolchain,
signing, and sideloading — to wrap the same API calls. The PWA gets you a
home-screen icon, a standalone window, and offline app-shell caching, and it
stays in step with the CLI automatically.

The one thing a native app would genuinely add is background sync on the phone
without a PC involved. If that is what you want, the rules engine already runs
headless — putting it on the server as a cron job is a simpler way to get it.
