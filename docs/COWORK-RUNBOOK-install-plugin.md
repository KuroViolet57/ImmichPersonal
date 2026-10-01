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

You will make exactly four changes to install it:

| # | Change | Where |
|---|---|---|
| 1 | Add `manifest.json` + `plugin.wasm` | `<immich>/plugins/immich-smart-album/` |
| 2 | Add two settings | `<immich>/.env` |
| 3 | Add one volume line | `<immich>/docker-compose.yml` |
| 4 | Restart the stack | `docker compose up -d` |

Then one of two endings, depending on whether you were given an API key:

- **No key** → stop and hand the workflow over to the human.
  See [Phase 6](#phase-6--hand-off-no-api-key).
- **Key in `$IMMICH_PLUGIN_API_KEY`** → create the workflow yourself.
  See [Phase 7](#phase-7--create-the-workflow-api-key-available).

Creating the API key itself is always the human's job — you cannot make one,
and you must never guess at one.

## Hard rules

Violating any of these is a failure, not a shortcut.

1. **Handle the API key only in memory and only from the environment.** Phase 7
   can create the workflow for the user, which needs a key. When it does:
   - Read it from `$IMMICH_PLUGIN_API_KEY`. Never pass it as a command-line
     argument — arguments land in shell history and in `ps` output.
   - **Never write it into any file inside this repository.** The repository is
     **public**. A credential pushed there is scraped within minutes and stays
     in the git history after it is rotated.
   - Never echo it, never include it in your report, never paste it back into
     the conversation. `immich-organizer` redacts it for you; do not
     re-introduce it by hand.
   - If the user pastes a key in chat, use it for the run but tell them, once,
     that it is now in the transcript and should be rotated afterwards.
   - If you have no key, do Phases 0–5 and hand Phase 6 to the human. Do not
     invent, guess, or reuse a key from anywhere else.
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

## Shortcut for Phases 2–5

If you have a shell on the machine, the repository's own installer does Phases
2 through 5 with the same checks this runbook specifies — backup before every
edit, `docker compose config` validation with automatic revert, and a log check
after restart:

```bash
git clone -b claude/immich-album-organization-2sj7f8 \
  https://github.com/KuroViolet57/ImmichPersonal.git ~/.immich-organizer
cd ~/.immich-organizer && bash scripts/install-plugin.sh --all --yes "$IMMICH_DIR"
```

It is idempotent, so a second run is a no-op. Read its summary, confirm the
`Imported plugin` line (GATE G still applies), then continue at Phase 6 or 7.

Do the phases by hand instead when the installer stops with a message about a
customised compose file — it deliberately refuses to guess, and so should you.

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

## Phase 6 — Hand-off (no API key)

Take this path when `$IMMICH_PLUGIN_API_KEY` is unset. These steps are the
**human's**, in the Immich web UI.

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

## Phase 7 — Create the workflow (API key available)

Take this path instead of Phase 6 when the user has given you a key. Re-read
[hard rule 1](#hard-rules) before you start.

### 7a. Which key, and how it reaches you

Two different keys are involved. Keeping them separate is the point.

| Key | Used by | Needs | Lifetime |
|---|---|---|---|
| **Step key** | The plugin, on every workflow run | `asset.read` | Long-lived; stored in Immich's database inside the workflow's config |
| **Operator key** | You, once, to create the workflow | `workflow.create`, `workflow.read`, `plugin.read` | Disposable — the user can delete it the moment you are done |

The step key must belong to **the account that owns the photos**. The filter
asks "is this asset among the closest matches", and the search only sees
libraries that key can read; a key from another account matches nothing and
fails silently.

The user supplies them as environment variables, so they never reach argv:

```bash
export IMMICH_PLUGIN_API_KEY='<step key: asset.read>'
export IMMICH_API_KEY='<operator key: workflow.create, workflow.read, plugin.read>'
export IMMICH_URL='http://localhost:2283'
```

One key with all four permissions also works and is simpler; it is just
longer-lived than it needs to be, because it ends up embedded in the workflow.

> **GATE H** — if `$IMMICH_PLUGIN_API_KEY` is empty, go to Phase 6 instead.
> Do not prompt for a key on the command line, and do not proceed with the
> tool's own configured key without saying so.

### 7b. Get the tool

`immich-organizer` builds and posts the workflow JSON for you. Hand-writing it
with `curl` is not an improvement: the key would land in your shell history,
and a workflow naming a method that does not exist is accepted silently and
simply never runs.

```bash
git clone -b claude/immich-album-organization-2sj7f8 \
  https://github.com/KuroViolet57/ImmichPersonal.git /tmp/immich-organizer
cd /tmp/immich-organizer && pip install -e ".[yaml]"
immich-organizer doctor
```

**Expect** `doctor` to report the content-matching filter as installed. If it
does not, Phase 5 did not actually succeed — go back, do not continue here.

### 7c. Preview, then create

Dry run first. The key is redacted in all output:

```bash
immich-organizer workflow create \
  --query "person in a mountain" \
  --album "Mountains" \
  --limit 200 \
  --show-payload
```

**Expect** a summary naming the match, album, trigger (`AssetTagged`), depth,
and `key from: $IMMICH_PLUGIN_API_KEY`. The printed JSON must show
`"apiKey": "***redacted***"`.

> **GATE I** — if the output contains the real key anywhere, stop and report a
> bug. Do not continue and do not paste that output anywhere.

Then create it:

```bash
immich-organizer workflow create \
  --query "person in a mountain" --album "Mountains" --limit 200 --apply

immich-organizer workflow list
```

**Expect** `Created workflow 'Mountains' (id ...)`, and the workflow listed with
its two steps.

Use `--like <asset-id>` in place of `--query` to match against a reference
photo instead of a description. `--trigger` accepts the other triggers, but the
command warns you about the embedding timing and the default is the one that
works.

### 7d. Verify it actually fires

Ask the user to tag one photo that should match, then:

```bash
immich-organizer workflow list
```

and have them open **Workflows → the workflow → logs** in Immich.

| What the log shows | Meaning |
|---|---|
| The photo added to the album | Working |
| `no smart-search embedding yet` | Not indexed. Confirm the trigger is `AssetTagged`; a just-uploaded photo will do this |
| `search failed with 401` | Step key is wrong or lacks `asset.read` |
| `Host function failed` | `allowedHosts` does not cover the hostname in `--server-url` |
| Nothing at all | The trigger did not fire — check the workflow is enabled |

> **GATE J** — do not report the job complete on "the workflow was created".
> Created is not working. Either confirm a photo was filed, or say plainly
> that creation succeeded and end-to-end behaviour is unverified.

### 7e. Clean up

Tell the user to delete the **operator** key now — it is not needed again. The
**step** key must stay; the plugin uses it on every run.

If they pasted either key into the chat, say once that the transcript holds it
and it should be rotated.

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

If Phase 7 created a workflow, delete it too — in **Workflows** in the UI, or
via the API. A workflow whose plugin is gone does nothing, but it still holds
the step key in its config, so removing it removes that copy of the key.

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

Workflow:      [not created — Phase 6 | created: "<name>" id <id> | FAILED]
End-to-end:    [confirmed: <photo> filed into <album> | NOT verified]
```

Then, whichever ending applied:

**Phase 6 (no key):**

```
Next for you:
  1. Create an API key with ONLY asset.read, on the account that owns the photos
  2. Workflows → New → trigger "Asset tagged"
  3. Filter by smart search (description, match depth 200, the key)
     → Add to Album(s)
```

**Phase 7 (key supplied):**

```
Next for you:
  1. Tag a photo that should match, and check Workflows → logs
  2. Delete the operator key — it is not needed again
  3. The step key stays; the plugin uses it on every run
  4. Rotate anything you pasted into the chat
```

Never put a key, or any fragment of one, in the report.

If any gate stopped you, say which one, what you saw, and what you did **not**
change — a partial install the human doesn't know about is worse than a clean
failure.
