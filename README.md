# Immich Organizer

Organize an [Immich](https://immich.app) library into albums using the server's
own smart-search AI — from Windows PowerShell, from a phone, or on a schedule.

Give it a description (`person in a mountain`) or a reference photo
(`everything that looks like this`), and it files the matches into an album.

```
immich-organizer search --query "person in a mountain" --limit 40 --album "Mountains"
immich-organizer plan  -r rules.yaml --html plan.html --open
immich-organizer apply -r rules.yaml
```

---

## What it actually does

Immich exposes two things this tool is built on, both through its documented
REST API:

| Feature you know from the web UI | API call it maps to |
|---|---|
| The search bar ("person in a mountain") | `POST /api/search/smart` with `query` |
| Three-dots → *Search similar* | `POST /api/search/smart` with `queryAssetId` |
| Adding photos to an album | `PUT /api/albums/{id}/assets` |

So "find everything that looks like this photo and put it in album X" is a
search call and an album call. No extra machine learning, no database access,
no re-encoding — your Immich server does the hard part and this drives it.

See [`docs/API-NOTES.md`](docs/API-NOTES.md) for the full endpoint mapping.

## The one caveat worth reading

**Smart search returns no similarity score.** Immich ranks the entire library
against your query and hands back the top N. It never says "nothing matched".
Ask for 500 photos of a dog in a library with 12 dogs, and you get 488
non-dogs.

Everything in this tool is shaped around that:

- **`limit` is the real control**, not a confidence threshold. Start at 50–100.
- **`plan` is the default workflow.** It changes nothing and writes an HTML
  page of thumbnails so you can see the tail go bad.
- **`refine`** lets a rule require matches to also appear in other searches
  (`all_of`) or be absent from them (`none_of`). Intersecting two independent
  searches is the most effective precision lever available.
- **Everything is reversible.** Each run is journalled; `undo` removes exactly
  what it added.

## Install

Needs Python 3.9+. The tool itself uses only the standard library; PyYAML is
needed only if you write rules in YAML instead of JSON.

**Windows** (see [`docs/SETUP-WINDOWS.md`](docs/SETUP-WINDOWS.md) for detail):

```powershell
git clone https://github.com/KuroViolet57/ImmichPersonal.git
cd ImmichPersonal
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
.\scripts\organizer.ps1 setup
```

**Linux / macOS / WSL / Termux:**

```bash
git clone https://github.com/KuroViolet57/ImmichPersonal.git
cd ImmichPersonal
pip install -e ".[yaml]"
immich-organizer setup
```

`setup` asks for your server URL and an API key (Immich → your avatar →
*Account Settings* → *API Keys* → *New API Key*). Then:

```bash
immich-organizer doctor
```

`doctor` verifies the key, checks that smart search returns results — which
tells you whether the Smart Search job has actually built embeddings — and
confirms that similar-asset search works on your server version.

## Use it

### One-off searches

```bash
# Look before you leap: nothing is filed without --apply.
immich-organizer search --query "sunset over water" --limit 30

# See the thumbnails rather than a list of filenames.
immich-organizer search --query "sunset over water" --limit 30 --html out.html --open

# Everything that looks like a specific photo.
immich-organizer search --like 7f3e1a2b-... --limit 50

# File them.
immich-organizer search --query "sunset over water" --limit 30 --album "Sunsets" --apply
```

Useful flags: `--type IMAGE|VIDEO`, `--taken-after 2023-01-01` (or `-6m`),
`--unfiled` (only photos not in any album yet), `--city Lisbon`, `--favorite`.

### Rules

Copy `rules.example.yaml` to `rules.yaml` and edit:

```yaml
version: 1
defaults:
  limit: 150
  filters: { type: IMAGE }

rules:
  - name: Mountains
    album: Mountains
    query: person in a mountain
    limit: 200

  - name: Photos of Bailey
    album: Bailey
    like_asset: 7f3e1a2b-....     # a reference photo's asset ID

  - name: Beach days
    album: Beach
    query: beach
    refine:
      all_of: [sand and ocean]
      none_of: [swimming pool]
```

```bash
immich-organizer validate -r rules.yaml           # syntax only, no server needed
immich-organizer plan     -r rules.yaml --html plan.html --open
immich-organizer apply    -r rules.yaml
immich-organizer undo                             # reverse the last apply
immich-organizer history
```

Rules are **idempotent**: photos already in the target album are skipped, so
re-running a rules file only picks up what you have uploaded since. That makes
it safe to put on a schedule — see
[`Register-ScheduledTask.ps1`](scripts/Register-ScheduledTask.ps1) on Windows
or cron elsewhere.

Full reference: [`docs/RULES.md`](docs/RULES.md).

### On your phone

```bash
immich-organizer serve --host 0.0.0.0 -r rules.yaml
```

Open the printed URL on your phone (same network), then *Add to Home Screen*.
It installs as a standalone app: search, tap thumbnails to include or exclude,
file the rest into an album, and preview or apply your saved rules.

The phone never sees your Immich API key — the local server proxies
thumbnails and calls. Because binding to `0.0.0.0` exposes it to your whole
network, every data route requires the token embedded in that URL, so treat
the link like a password. Details in
[`docs/SETUP-ANDROID.md`](docs/SETUP-ANDROID.md).

## Immich's own Workflows, and the plugin in this repository

Recent Immich versions ship a native **Workflows** feature: a UI at
`/workflows`, event triggers, and WASM plugins providing filters and actions.
It is worth knowing how it relates to this tool, because the two cover
different halves of the problem.

Immich's built-in plugin filters on **metadata only** — filename, date,
geolocation, EXIF, tags, asset type. There is no filter for *what a photo looks
like*. And every trigger is an asset event, so workflows act on new uploads and
cannot sweep a library that already exists.

|  | Immich Workflows | This tool |
|---|---|---|
| Runs on the **existing** library | no — new assets only | yes |
| Automatic on new uploads | yes | via a scheduled re-run |
| Matches on image content | not out of the box | yes |
| Interface | built into Immich | CLI + the mobile web UI |

`plugins/immich-smart-album/` closes the content gap: it is an Immich plugin
adding a **"Filter by smart search"** step, so a native workflow can say
*"photos that look like a mountain → Mountains"*. It is written against
Immich's plugin SDK interface, compiled to WASM, and ships prebuilt.

Install guide for Docker on WSL2: [`docs/INSTALL-PLUGIN-WSL2.md`](docs/INSTALL-PLUGIN-WSL2.md), or run
`./scripts/install-plugin.sh ~/immich-app` from a checkout inside WSL2.

One caveat it cannot engineer away: a freshly uploaded photo has no CLIP
embedding when the upload triggers fire, so the plugin is reliable on the
`AssetTagged` trigger and not on `AssetCreate`. Full explanation in
[`plugins/immich-smart-album/README.md`](plugins/immich-smart-album/README.md).

`immich-organizer doctor` reports whether your server has Workflows, which
plugin methods it exposes, and whether a content-matching filter is installed.

## Commands

| Command | What it does |
|---|---|
| `setup` | Store the server URL and API key |
| `doctor` | Check connectivity, auth, and that smart search is ready |
| `search` | One-off search; optionally file the results |
| `albums` | List albums with their IDs |
| `validate` | Check a rules file without contacting the server |
| `plan` | Preview what the rules would do — changes nothing |
| `apply` | Carry the rules out, after confirmation |
| `undo` | Remove the assets a previous run added |
| `history` | Show previous runs |
| `serve` | Start the mobile web UI |

Configuration lives in `%APPDATA%\immich-organizer\config.json` on Windows and
`~/.config/immich-organizer/config.json` elsewhere; `IMMICH_URL` and
`IMMICH_API_KEY` override it, which is handy for scheduled runs.

## Safety

- `plan` is read-only, and `apply` confirms before writing.
- `search --album` needs `--apply`; without it, it is a dry run.
- Albums in Immich are labels, not folders — filing a photo does not move or
  copy the file, and removing it from an album does not delete it.
- `actions: { archive: true }` hides filed photos from the main timeline. It
  does **not** delete them; they stay in the album and in the archive.
- Every apply is journalled to the state directory so `undo` can reverse it.
  `undo` empties what it added but never deletes an album, since the album may
  have gained other photos since.

## Development

```bash
python3 -m unittest discover -s tests -t .      # the CLI and web UI
cd plugins/immich-smart-album && npm test        # the compiled plugin
```

The Python suite runs against an in-process fake Immich server that mirrors the
real API's request and response shapes, so the client, engine, CLI, and web UI
are all covered without a live server.

The plugin suite loads the compiled `plugin.wasm` into the real Extism runtime
using the same host-function contract the Immich server uses, against a fake
Immich whose search results the test controls.

## Licence

See [LICENSE](LICENSE).
