# Installing the Smart Album plugin (Immich in Docker on WSL2)

For the common Windows setup: Immich running under Docker inside WSL2, reached
from your Windows browser at `http://localhost:2283`.

Everything here happens **inside WSL2**, not in PowerShell. Open your WSL
terminal (`wsl` from PowerShell, or the Ubuntu app).

There is a script that does steps 2–4 for you — see [the shortcut](#shortcut)
at the end — but the manual steps are worth reading once so you know what
changed.

---

## Step 0 — Check your Immich supports plugins

Workflows and plugins are a recent Immich feature. Before changing anything,
open this in your browser:

```
http://localhost:2283/workflows
```

- **A Workflows page appears** → you're good.
- **404 / redirect to the timeline** → your Immich is too old. Update it first
  (bump `IMMICH_VERSION` in `.env`, then `docker compose pull && docker compose up -d`).

## Step 1 — Find your Immich folder

The folder holding `docker-compose.yml` and `.env`. If you followed the
official install it is probably `~/immich-app`:

```bash
ls ~/immich-app
```

If that's not it, search:

```bash
find ~ /opt /srv -maxdepth 4 -name docker-compose.yml 2>/dev/null | xargs grep -l immich-server
```

The rest of this guide calls that folder `~/immich-app`. Adjust if yours differs.

## Step 2 — Put the plugin files in place

The plugin is two files: a manifest and a compiled WASM module.

```bash
mkdir -p ~/immich-app/plugins/immich-smart-album
cd ~/immich-app/plugins/immich-smart-album

BASE=https://raw.githubusercontent.com/KuroViolet57/ImmichPersonal/claude/immich-album-organization-2sj7f8/plugins/immich-smart-album
curl -fLO "$BASE/manifest.json"
curl -fL -o plugin.wasm "$BASE/dist/plugin.wasm"
```

Check both arrived (the wasm should be roughly 2.4 MB — if it's a few hundred
bytes you downloaded an error page):

```bash
ls -la
```

> The folder layout matters. Immich reads `<install folder>/<any name>/manifest.json`
> and then the file named by that manifest's `wasmPath`, which is `plugin.wasm`.

## Step 3 — Turn on external plugins

Immich only loads third-party plugins when you opt in. Its compose file reads
settings from `.env`, so that's where these go — no YAML indentation to worry
about:

```bash
cd ~/immich-app
cp .env .env.backup
cat >> .env <<'EOF'

# Smart Album plugin
IMMICH_ALLOW_EXTERNAL_PLUGINS=true
IMMICH_PLUGINS_INSTALL_FOLDER=/plugins
EOF
```

`/plugins` is the path **inside the container** and must be absolute. Step 4
maps your folder onto it.

## Step 4 — Mount the folder into the container

This is the only change to `docker-compose.yml`. Open it:

```bash
nano ~/immich-app/docker-compose.yml
```

Find the `immich-server` service and add one line to its `volumes:` list:

```yaml
services:
  immich-server:
    container_name: immich_server
    image: ghcr.io/immich-app/immich-server:${IMMICH_VERSION:-release}
    volumes:
      - ${UPLOAD_LOCATION}:/data
      - /etc/localtime:/etc/localtime:ro
      - ./plugins:/plugins          # <-- add this line
```

Indentation must match the lines above it exactly — YAML is strict, and a
mis-indented line gives a confusing parse error rather than a helpful one.

Save with `Ctrl+O`, `Enter`, then `Ctrl+X`.

Confirm the file still parses:

```bash
cd ~/immich-app && docker compose config >/dev/null && echo "compose file is valid"
```

## Step 5 — Restart and confirm it loaded

```bash
cd ~/immich-app
docker compose up -d
docker compose logs immich-server | grep -i plugin
```

You want this line:

```
Imported plugin immich-smart-album@1.0.0 (1 methods) from /plugins/immich-smart-album
```

Other things you might see, and what they mean:

| Log line | Cause |
|---|---|
| `Plugin up to date (name=immich-smart-album@1.0.0, hash=...)` | Already installed, nothing to do. |
| `Invalid plugin manifest at ...` | `manifest.json` is truncated or corrupt — re-download it. The message lists which fields failed. |
| `Failed to import plugin from /plugins/...` | Something else went wrong. Immich logs no detail here, so check the obvious causes: `plugin.wasm` missing or truncated, or the folder not readable inside the container. Verify with `docker compose exec immich-server ls -la /plugins/immich-smart-album`. |
| nothing at all about plugins | `IMMICH_ALLOW_EXTERNAL_PLUGINS` was not picked up. Check with `docker compose config \| grep IMMICH_ALLOW`, then recreate: `docker compose up -d --force-recreate`. |

You can also verify from this repository:

```bash
immich-organizer doctor
```

which should now report a content-matching filter installed.

## Step 6 — Build a workflow

In Immich: **Workflows → New**, or pick the **Smart album** template.

1. **Trigger: Asset tagged.** This matters — see below.
2. **Step 1 — Filter by smart search:**
   - *Description*: `person in a mountain`
   - *Match depth*: `200`
   - *API key*: create one under Account Settings → API Keys and paste it.
3. **Step 2 — Add to Album(s):** choose or name the album.

Save and enable it. Now tagging a photo files it if it matches.

---

## Why the trigger has to be "Asset tagged"

A photo uploaded a moment ago has **no CLIP embedding yet**, so smart search
cannot find it and the filter will reject it.

Immich queues the job that builds the embedding only after thumbnail
generation, while the `Asset created` and `Asset metadata extraction` triggers
both fire earlier in the pipeline. There is no "indexing finished" trigger to
hook instead.

So:

- **Asset tagged** — reliable. By the time you tag something, it's indexed.
- **Asset created / metadata extraction** — will not work for fresh uploads.
- For uploads, and for the photos already in your library, use the CLI on a
  schedule: `immich-organizer apply -r rules.yaml`. That's what it's for.

The filter notices this case and writes an explanatory line to the workflow log
rather than silently dropping the photo. Leave *Diagnose non-matches* on until
you trust it.

## Updating the plugin later

Immich hashes `manifest.json` to decide whether to re-import. **Replacing
`plugin.wasm` alone changes nothing** — the server sees the same manifest hash
and skips it. Bump `version` in `manifest.json` (or change any character in it)
before restarting.

## Shortcut

From a checkout of this repository inside WSL2:

```bash
git clone -b claude/immich-album-organization-2sj7f8 \
  https://github.com/KuroViolet57/ImmichPersonal.git
cd ImmichPersonal
./scripts/install-plugin.sh ~/immich-app
```

It does steps 2 and 3 (backing up `.env` first), tells you the exact line to add
for step 4, and prints the verification commands. Re-running it is safe — it
skips anything already done. It deliberately does not edit
`docker-compose.yml` for you.

## Troubleshooting

**The filter never matches anything.** Almost always the embedding timing.
Check the workflow log for the "no smart-search embedding yet" line. Switch the
trigger to *Asset tagged*.

**"Host function failed" in the log.** The plugin's network call was blocked.
`allowedHosts` in `manifest.json` permits `localhost`, `127.0.0.1`,
`immich-server` and `immich_server`. If your *Immich URL (internal)* setting
uses a different hostname, add it to `allowedHosts` and restart.

**Matches are junk.** *Match depth* is too high — it has reached the tail of
the ranking, which is the least similar results. Preview a query first:

```bash
immich-organizer search --query "person in a mountain" --limit 200 --html out.html
```

and see where the results stop being relevant.

**Genuine matches are skipped.** *Match depth* is too low. The photo has to be
in the global top-N for your whole library, so raise it.

**Rolling it back.** Remove the volume line, restore `.env` from
`.env.backup`, delete `~/immich-app/plugins`, and `docker compose up -d`.
Nothing about your photos or albums is touched by any of this.
