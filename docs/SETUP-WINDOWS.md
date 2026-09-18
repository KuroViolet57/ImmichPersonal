# Windows setup

This runs as a normal Windows program talking to your Immich server over HTTP.
It does not need to run inside WSL — WSL is where Immich lives, and the API is
reachable from Windows either way.

## 1. Python

Needs Python 3.9 or newer.

```powershell
py --version
```

If that fails, install from <https://www.python.org/downloads/> and tick **Add
python.exe to PATH** during setup. (If `python` opens the Microsoft Store, that
is the disabled stub — install the real thing, or use `py` instead.)

## 2. Install

```powershell
git clone https://github.com/KuroViolet57/ImmichPersonal.git
cd ImmichPersonal
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

This creates `.venv` in the repository and installs the package into it, so
nothing touches your system Python.

Afterwards, run everything through the wrapper:

```powershell
.\scripts\organizer.ps1 <command>
```

Or activate the environment once per session and use the short name:

```powershell
.\.venv\Scripts\Activate.ps1
immich-organizer <command>
```

If PowerShell refuses to run the scripts, allow local ones for your user:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## 3. Point it at your server

```powershell
.\scripts\organizer.ps1 setup
```

It asks for two things:

**Server URL.** Immich in WSL2 is reachable from Windows at
`http://localhost:2283` — WSL2 forwards localhost ports to Windows
automatically. If that does not work, get the WSL IP and use it:

```powershell
wsl hostname -I
# then use http://<that-address>:2283
```

**API key.** In Immich: your avatar → *Account Settings* → *API Keys* → *New
API Key*. It is shown once, so copy it straight away.

Then confirm everything works:

```powershell
.\scripts\organizer.ps1 doctor
```

This checks reachability, the key, and — importantly — whether smart search
actually returns results. If it reports no results on a library that is not
empty, the Smart Search job has not run: go to *Administration → Jobs → Smart
Search* in Immich and run it. Embeddings are what makes any of this work.

## 4. Try it

```powershell
# Look, do not touch.
.\scripts\organizer.ps1 search --query "person in a mountain" --limit 25

# See the thumbnails instead of filenames.
.\scripts\organizer.ps1 search --query "person in a mountain" --limit 25 --html plan.html --open

# File them.
.\scripts\organizer.ps1 search --query "person in a mountain" --limit 25 --album "Mountains" --apply
```

## 5. Rules

```powershell
Copy-Item rules.example.yaml rules.yaml
notepad rules.yaml

.\scripts\organizer.ps1 validate -r rules.yaml
.\scripts\organizer.ps1 plan     -r rules.yaml --html plan.html --open
.\scripts\organizer.ps1 apply    -r rules.yaml
```

## 6. Run it on a schedule

Rules skip anything already filed, so a recurring run only catches up on new
uploads.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Register-ScheduledTask.ps1 -Schedule Weekly -Time 03:00
```

This validates the rules file first, then registers a Task Scheduler entry that
runs `apply --yes` and appends output to
`%LOCALAPPDATA%\immich-organizer\scheduled-run.log`.

Review your rules with `plan` before automating them — a scheduled run does not
prompt.

Remove it later with:

```powershell
Unregister-ScheduledTask -TaskName "Immich Organizer"
```

## Where things live

| What | Path |
|---|---|
| Config (URL + API key) | `%APPDATA%\immich-organizer\config.json` |
| Run journal (for `undo`) | `%LOCALAPPDATA%\immich-organizer\state\journal.jsonl` |
| Scheduled run log | `%LOCALAPPDATA%\immich-organizer\scheduled-run.log` |

`IMMICH_URL` and `IMMICH_API_KEY` environment variables override the config
file, which is often easier for scheduled tasks.

## Troubleshooting

**"Could not reach Immich"** — check the URL in a browser first. If
`http://localhost:2283` works in a browser but not here, you are probably
pointing at the wrong port or a stale WSL address.

**"Immich rejected the API key"** — the key was revoked, mistyped, or belongs
to a different user. Create a new one and re-run `setup`.

**Smart search returns nothing** — the Smart Search job has not built
embeddings yet. On a large library the first run takes a while.

**Self-signed certificate** — if you front Immich with HTTPS using your own
certificate, run `setup --insecure` to skip verification. Only do that on a
network you trust.
