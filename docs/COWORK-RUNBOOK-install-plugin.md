# Runbook: install the Smart Album plugin into Immich (WSL2 + Docker)

**Audience: a Claude Cowork session (or any agent) with shell access to the
machine running Immich.** A human-facing version of the same procedure is in
[`INSTALL-PLUGIN-WSL2.md`](INSTALL-PLUGIN-WSL2.md); this one is written to be
executed, with explicit gates and failure handling.

You can also drop this file in as a skill at
`.claude/skills/install-immich-plugin/SKILL.md` if you want it loadable by name.

---

## What you are doing

Installing `immich-smart-album`, a plugin that adds a **"Filter by smart
search"** step to Immich's native Workflows, so a workflow can match photos by
image content rather than only by metadata.

You will make exactly four changes:

| # | Change | Where |
|---|---|---|
| 1 | Add `manifest.json` + `plugin.wasm` | `<immich>/plugins/immich-smart-album/` |
| 2 | Add two settings | `<immich>/.env` |
| 3 | Add one volume line | `<immich>/docker-compose.yml` |
| 4 | Restart the stack | `docker compose up -d` |

Everything else — creating the API key and building the workflow — is the
human's job, in the Immich web UI. See [Hand-off](#phase-6--hand-off).

## Hard rules

Violating any of these is a failure, not a shortcut.

1. **Never ask for, accept, store, echo, log, or write the user's Immich API
   key.** You do not need it. It is typed by the human directly into the Immich
   web UI in Phase 6. If you think you need it, you have gone off-script — stop
   and re-read.
2. **Never edit `docker-compose.yml` without first copying it to a backup**,
   and never leave an edit in place that fails `docker compose config`. Revert
   and stop instead.
3. **Never touch the library or database folders** — the paths in
   `UPLOAD_LOCATION` and `DB_DATA_LOCATION`. This procedure does not go near a
   single photo.
4. **Never run `docker compose down -v`.** The `-v` destroys the database
   volume. Use `docker compose up -d`, which recreates only what changed.
5. **Never upgrade Immich** (`docker compose pull`, editing `IMMICH_VERSION`)
   unless the human explicitly asks. If Phase 0 finds the version too old,
   report it and stop.
6. **Stop at any failed GATE.** Do not improvise around it, do not try a
   different approach, do not proceed "to see if it works anyway". Report what
   you saw and ask.
7. **Report every file you changed and where its backup is.**

## Establish the shell first

Immich runs inside WSL2. Every command below must run *there*, not in
PowerShell.

Work out which case you are in:

```bash
uname -a && docker version --format '{{.Server.Version}}'
```

- **It works** → you are already inside WSL2 (or Linux). Run commands directly.
- **It fails / you are on Windows** → prefix every command:

  ```powershell
  wsl -e bash -lc '<command>'
  ```

For anything multi-line, do **not** fight the quoting. Write a script inside
WSL and run it:

```powershell
wsl -e bash -lc 'cat > /tmp/step.sh <<"EOF"
...commands...
EOF
bash /tmp/step.sh'
```

Confirm Docker is reachable before going further:

```bash
docker compose version
python3 --version    # used for validation and the compose edit; fallbacks are noted inline
```

> **GATE A** — if Docker is not reachable from the shell you have chosen, stop.
> Ask the human whether Immich runs under Docker Desktop with WSL integration
> or under a Docker daemon inside WSL, and which distro.

---

## Phase 0 — Preflight: does this Immich support plugins?

**Goal:** confirm the feature exists before changing anything.

Workflows and plugins are a recent Immich feature. `GET /api/plugins` is
auth-guarded, so an unauthenticated request distinguishes the two cases without
needing any credential:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:2283/api/plugins
```

| Result | Meaning | Action |
|---|---|---|
| `401` | Endpoint exists, you are just not logged in → **feature supported** | Continue |
| `404` | Route does not exist → **Immich too old** | **Stop.** Report that plugins need a newer Immich, and that upgrading is their call (rule 5). |
| `000` / connection refused | Immich is not running, or not on this port | **Stop.** Ask for the correct URL/port. |

> **GATE B** — only `401` proceeds.

Also record the running version for your final report:

```bash
docker ps --format '{{.Names}}\t{{.Image}}' | grep -i immich
```

---

## Phase 1 — Locate the deployment

**Goal:** find the folder holding `docker-compose.yml` and `.env`.

```bash
ls -la ~/immich-app/docker-compose.yml 2>/dev/null \
  || find ~ /opt /srv -maxdepth 4 -name docker-compose.yml 2>/dev/null \
     | xargs -r grep -l immich-server
```

Set `IMMICH_DIR` to what you find. Verify both files exist:

```bash
IMMICH_DIR=~/immich-app          # adjust to what you found
ls -la "$IMMICH_DIR/docker-compose.yml" "$IMMICH_DIR/.env"
```

> **GATE C** — if you find zero candidates, or more than one and cannot tell
> which is live, **stop and ask**. Do not guess. You can narrow it with
> `docker inspect immich_server --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'`,
> which reports the directory Compose actually started the container from —
> prefer that answer over a filesystem guess.

---

## Phase 2 — Install the plugin files

**Goal:** two files in `<IMMICH_DIR>/plugins/immich-smart-album/`.

```bash
mkdir -p "$IMMICH_DIR/plugins/immich-smart-album"
cd "$IMMICH_DIR/plugins/immich-smart-album"

BASE=https://raw.githubusercontent.com/KuroViolet57/ImmichPersonal/claude/immich-album-organization-2sj7f8/plugins/immich-smart-album
curl -fsSL -o manifest.json "$BASE/manifest.json"
curl -fsSL -o plugin.wasm   "$BASE/dist/plugin.wasm"
ls -la
```

**Expect:** `manifest.json` a few KB, `plugin.wasm` ≈ 2.4 MB.

Validate rather than eyeballing. This checks all three things at once: the
manifest parses, the file it names exists, and that file is really WebAssembly.

```bash
python3 - <<'EOF'
import json, pathlib
m = json.load(open('manifest.json'))
w = pathlib.Path(m['wasmPath'])
ok = w.exists() and w.read_bytes()[:4] == b'\x00asm'
print(f"plugin : {m['name']} {m['version']}")
print(f"wasm   : {w} exists={w.exists()} size={w.stat().st_size if w.exists() else 0} magic_ok={ok}")
EOF
```

**Expect:**

```
plugin : immich-smart-album 1.0.1
wasm   : plugin.wasm exists=True size=2414886 magic_ok=True
```

Without `python3`, check the magic bytes with coreutils — `od -An -tx1 -N4
plugin.wasm` must print ` 00 61 73 6d`.

> **GATE D** — `magic_ok` must be `True` and the size a couple of megabytes. A
> few hundred bytes means you downloaded an error page, not the plugin. If
> `wasmPath` in the manifest names a different filename, rename the downloaded
> file to match it — Immich loads the filename the manifest declares, not the
> one you chose.

**Why this layout:** Immich scans the install folder, treats each
subdirectory as one plugin, reads `<dir>/manifest.json`, then loads the file
its `wasmPath` names.

---

## Phase 3 — Enable external plugins

**Goal:** two settings in `.env`. Immich's compose file has no `environment:`
block for `immich-server` — it reads `env_file: .env` — so this needs no YAML
editing.

Check first; this step must be idempotent:

```bash
grep -E '^\s*IMMICH_(ALLOW_EXTERNAL_PLUGINS|PLUGINS_INSTALL_FOLDER)=' "$IMMICH_DIR/.env"
```

If either is missing, back up and append:

```bash
cp "$IMMICH_DIR/.env" "$IMMICH_DIR/.env.bak.$(date +%Y%m%d%H%M%S)"
cat >> "$IMMICH_DIR/.env" <<'EOF'

# Smart Album plugin
IMMICH_ALLOW_EXTERNAL_PLUGINS=true
IMMICH_PLUGINS_INSTALL_FOLDER=/plugins
EOF
```

If a variable is present but set to something else (e.g. a different install
folder), **do not overwrite it** — that folder may hold other plugins. Report
it and ask.

`IMMICH_PLUGINS_INSTALL_FOLDER` is validated as an absolute path inside the
container. `/plugins` is correct; Phase 4 maps the host folder onto it.

---

## Phase 4 — Mount the folder (the only compose edit)

**Goal:** add `- ./plugins:/plugins` to the `immich-server` service's
`volumes:` list.

This is the one risky edit. Follow the sequence exactly: **check → back up →
edit → validate → revert on failure.**

**4a. Already done?**

```bash
grep -nE '^\s*-\s*\./plugins:/plugins' "$IMMICH_DIR/docker-compose.yml"
```

Any match → skip to Phase 5.

**4b. Find a safe anchor.** In Immich's stock compose file, this line appears
exactly once, inside `immich-server`:

```bash
grep -c '/etc/localtime:/etc/localtime:ro' "$IMMICH_DIR/docker-compose.yml"
```

> **GATE E** — the count must be exactly `1`. If it is `0` or `>1`, the file
> has been customised. **Stop**, show the human the `immich-server` service
> block, and ask them to add the line themselves.

**4c. Back up, then edit:**

```bash
cp "$IMMICH_DIR/docker-compose.yml" "$IMMICH_DIR/docker-compose.yml.bak.$(date +%Y%m%d%H%M%S)"

python3 - "$IMMICH_DIR/docker-compose.yml" <<'EOF'
import re, sys
from pathlib import Path
p = Path(sys.argv[1]); text = p.read_text()
anchor = re.search(r'^([ \t]*)-[ \t]*/etc/localtime:/etc/localtime:ro[ \t]*$', text, re.M)
assert anchor, "anchor line not found"
indent = anchor.group(1)                      # reuse the neighbour's exact indent
line = f"{indent}- ./plugins:/plugins\n"
p.write_text(text[:anchor.end()] + "\n" + line + text[anchor.end()+1:])
print("inserted with indent", repr(indent))
EOF
```

Without `python3`, GNU `sed` does the same thing — `\0` re-emits the anchor and
`\1` reuses its indentation, so the new line lands with matching indent:

```bash
sed -i 's|^\([ \t]*\)- /etc/localtime:/etc/localtime:ro[ \t]*$|\0\n\1- ./plugins:/plugins|' \
  "$IMMICH_DIR/docker-compose.yml"
```

**4d. Validate — mandatory:**

```bash
cd "$IMMICH_DIR" && docker compose config >/dev/null && echo VALID || echo INVALID
```

**GATE F** — on `INVALID`, immediately restore the backup and stop. Report the
error from `docker compose config` verbatim, and do not retry with a different
editing method.

```bash
cp "$IMMICH_DIR"/docker-compose.yml.bak.* "$IMMICH_DIR/docker-compose.yml"
cd "$IMMICH_DIR" && docker compose config >/dev/null && echo "restored cleanly"
```

Confirm the mount resolved as intended:

```bash
docker compose config | grep -A 2 'plugins'
```

---

## Phase 5 — Restart and verify

**Goal:** Immich imports the plugin on boot.

```bash
cd "$IMMICH_DIR"
docker compose up -d
```

Plugin import runs in the Microservices worker during bootstrap, so give it a
few seconds, then:

```bash
docker compose logs immich-server 2>&1 | grep -i plugin | tail -20
```

**Success looks like:**

```
Imported plugin immich-smart-album@1.0.1 (1 methods) from /plugins/immich-smart-album
```

or, on a re-run:

```
Plugin up to date (name=immich-smart-album@1.0.1, hash=...)
```

| Log line | Diagnosis | Action |
|---|---|---|
| `Invalid plugin manifest at ...` | Corrupt/truncated `manifest.json`; the message names the failing fields | Re-download it (Phase 2) |
| `Failed to import plugin from /plugins/...` | Immich logs **no detail** here | Check the container can see the files: `docker compose exec immich-server ls -la /plugins/immich-smart-album` |
| No plugin lines at all | Settings not picked up | `docker compose config \| grep IMMICH_ALLOW` then `docker compose up -d --force-recreate` |

Confirm the container actually sees the mount:

```bash
docker compose exec immich-server ls -la /plugins/immich-smart-album
```

> **GATE G** — do not report success without the `Imported plugin` (or
> `Plugin up to date`) line. "The command ran without error" is not
> verification.

**Known trap for later:** Immich hashes `manifest.json` to decide whether to
re-import. Replacing `plugin.wasm` alone changes nothing. Any future update
must bump `version` in the manifest.

---

## Phase 6 — Hand-off

These steps are the **human's**, in the Immich web UI. Do not attempt them, and
do not ask for the API key.

Tell them, in this order:

1. **Create a read-only API key.** Account Settings → API Keys → New API Key.
   The permission list starts empty — tick only **`asset.read`** and nothing
   else. That is the sole permission `POST /api/search/smart` requires. Filing
   into the album is done by the next workflow step through Immich's internal
   plugin functions, not by this key.
2. **The key must belong to the account that owns the photos.** The filter asks
   "is this asset among the closest matches", and the search only sees
   libraries that key can read. A key from another account matches nothing, and
   fails silently.
3. **Build the workflow:** Workflows → New, or the **Smart album** template.
   - **Trigger: Asset tagged** — this matters, see below.
   - Step 1, *Filter by smart search*: a description (e.g. `person in a
     mountain`), match depth `200`, and the API key.
   - Step 2, *Add to Album(s)*: the target album.

### Why the trigger must be "Asset tagged"

Explain this; it is the single most likely source of "it doesn't work".

A just-uploaded photo has **no CLIP embedding yet**, so smart search cannot
find it and the filter correctly rejects it. Immich queues the embedding job
only after thumbnail generation, while the *Asset created* and *Asset metadata
extraction* triggers both fire earlier. There is no "indexing finished"
trigger.

So: **Asset tagged** works; the upload triggers do not. For new uploads and for
photos already in the library, the `immich-organizer` CLI in this repository
sweeps on a schedule instead.

---

## Failure playbook

| Symptom | Likely cause | Response |
|---|---|---|
| `curl` to `/api/plugins` returns `404` | Immich predates plugins | Stop; upgrading is the human's call |
| Two candidate Immich folders | Multiple deployments | Ask `docker inspect` which is live (Phase 1) |
| `docker compose config` fails after edit | Indentation wrong | Restore backup, stop (GATE F) |
| Plugin files present, no log line | Settings not applied | `--force-recreate`; check `docker compose config` |
| `Host function failed` at workflow runtime | Network call blocked | `allowedHosts` in `manifest.json` permits `localhost`, `127.0.0.1`, `immich-server`, `immich_server`. If the step's *Immich URL (internal)* uses another hostname, add it and restart |
| Workflow runs, never matches | Embedding timing, or wrong account's key | Check the workflow log for "no smart-search embedding yet"; confirm trigger is *Asset tagged* |

## Rollback

Complete and safe — nothing here touches photos, albums or the database.

```bash
cd "$IMMICH_DIR"
cp docker-compose.yml.bak.<timestamp> docker-compose.yml   # or delete the volume line
cp .env.bak.<timestamp> .env                                # or delete the two settings
rm -rf plugins/immich-smart-album
docker compose up -d
```

The plugin row stays in Immich's database but is inert once the files are gone.

## Report template

Close with this, filled in:

```
Immich:        <version>, at <IMMICH_DIR>
Plugin:        immich-smart-album <version>  [installed | already present | FAILED]
Verified by:   <the exact log line you saw>

Changed:
  <IMMICH_DIR>/plugins/immich-smart-album/{manifest.json,plugin.wasm}   (new)
  <IMMICH_DIR>/.env                     (+2 lines, backup: .env.bak.<ts>)
  <IMMICH_DIR>/docker-compose.yml       (+1 line,  backup: docker-compose.yml.bak.<ts>)

Restarted:     docker compose up -d
Not done:      API key + workflow — yours to do in the Immich UI (Phase 6)

Next for you:
  1. Create an API key with ONLY asset.read, on the account that owns the photos
  2. Workflows → New → trigger "Asset tagged"
  3. Filter by smart search (description, match depth 200, the key)
     → Add to Album(s)
```

If any gate stopped you, say which one, what you saw, and what you did **not**
change — a partial install the human doesn't know about is worse than a clean
failure.
