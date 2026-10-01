# AI Tagger — design and contracts

A panel tab that tags every photo and video with two image taggers, lets a vision-language model (VLM) refine
the tags and write a short description following the user's own instructions, and writes the result into the
asset's Immich description. It runs in the background like Search+ and keeps new uploads up to date.

This file is the contract between the parts. Change it when an interface changes.

## Decisions

| Topic | Decision |
|---|---|
| Tagger 1 | `SmilingWolf/wd-eva02-large-tagger-v3` (ONNX, Apache-2.0): 10,861 Danbooru tags — illustration/anime, people, clothing, pose, characters, rating |
| Tagger 2 | `xinyu1205/recognize-anything-plus-model` (RAM++, Apache-2.0): 4,585 plain-English tags — real-world objects, scenes, activities |
| VLM | `Qwen/Qwen3.5-9B` (Apache-2.0, 2026-02) served by vLLM, FP8, thinking off, several frames per request |
| Captures | photo: 1 · animated image / video: `video_frames` (2 or 6; 1–8 allowed) taken from 8 equal segments, skipping the first and last segment |
| Where results go | a managed block inside the Immich description; the user's own text is never changed. Optional: native Immich tags under `AI/` |
| GPU | taggers + VLM share a `vram_gb` budget. Search+ and the tagger never run on the GPU at the same time |
| Reprocessing | raw tagger scores and the VLM answer are stored, so most setting changes re-apply without the GPU |

## Components and ownership

| Path | What |
|---|---|
| `immich_organizer/tagger_service.py` | standalone model server (no package imports), runs in container `immich_aitagger` |
| `deploy/aitagger/` | `Dockerfile` (tagger service), `docker-compose.yml` (project `immich-aitagger`: `immich_aitagger` + `immich_aitagger_vlm`), `.env.example` |
| `immich_organizer/aitagger.py` | panel side: settings, store, catalog, frames, indexer, aggregation, rules, VLM client, write-back, services |
| `immich_organizer/web/server.py` | `/api/aitagger*` routes |
| `immich_organizer/client.py` | `update_asset`, tag helpers |
| `immich_organizer/web/static/*` | the "AI Tagger" tab |
| `tests/test_aitagger.py`, `tests/test_web.py` | tests with fakes; no GPU needed |

## 1. Tagger service (`tagger_service.py`)

Container `immich_aitagger`, published on `127.0.0.1:11440` (container port 8080). It mirrors `embed_service.py`:
the HTTP server answers at once, the models load in a background thread, and every route except `/health` answers 503
`{"error", "status"}` until they are loaded. It runs a single GPU worker thread fed by a queue, and a bad image fails
only its own slot.

Environment: `AITAGGER_VRAM_GB` (memory cap for this process, default 5), `AITAGGER_BATCH` (GPU micro-batch,
default 16), `IDLE_EXIT_MINUTES` (default 20; **must not exit while a request is in flight**), `WD_MODEL`, `RAM_MODEL`,
`WD_PRECISION` (default `fp16`: the ONNX model is converted once and cached; `fp32` keeps the original). Model files live
under `/cache` (`/cache/hub` is a Hugging Face cache). Built and measured: WD fp16 + RAM++ fp16 loads in about 6 s and
holds about 4 GB of VRAM under the 5 GB cap; about 40 pictures/s with both models (WD alone 70/s, RAM++ alone 85/s).
WD is run in micro-batches of at most 8 (bigger gains nothing); a CUDA out-of-memory answer halves the micro-batch of
that model for the rest of the process (`effectiveBatch` in `/health`).

RAM++ is **not** the official `ram` package (its pins, `timm==0.4.12`/`fairscale`/an old `transformers`, no longer
install). The image build fetches one pinned commit of `xinyu1205/recognize-anything` and keeps only the Swin-L backbone
source, the tag list with per-class thresholds and the licence files (`/opt/ram`); `tagger_service.py` implements the small
tagging head itself. Checked against the official code on real pictures: identical logits in fp32, one tag in 167 flips in fp16.

### `GET /health`
```json
{"status": "loading|ok|error", "error": null, "device": "cuda", "vramCapGb": 5, "batch": 16,
 "models": [{"name": "wd-eva02-large-tagger-v3", "kind": "wd", "tags": 10861, "precision": "fp16", "loadedIn": 4.1},
            {"name": "ram_plus_swin_large_14m", "kind": "ram", "tags": 4585, "precision": "fp16", "loadedIn": 9.8}],
 "idleExitMinutes": 20, "idleSeconds": 12, "busy": 0}
```

### `POST /tag`
Request: `{"images": ["<base64 jpeg/png>", ...], "floor": 0.05}` (maximum 64 images). Optional: `"models": ["wd"]` or
`["ram"]` runs only that model (default both; the other key is then absent), so the panel can skip a disabled tagger.
`/health` also reports `effectiveBatch: {wd, ram}`. Any failure of one picture on one model makes that slot an error
(`results[i]` null, `errors[i]` says which model).

Response:
```json
{"results": [{"wd": {"general": {"long_hair": 0.93}, "character": {"hatsune_miku": 0.81},
                     "rating": {"general": 0.02, "sensitive": 0.71, "questionable": 0.2, "explicit": 0.07}},
              "ram": {"dog": 0.88, "beach": 0.64}}, null],
 "errors": [null, "cannot identify image file"], "tookMs": 412}
```
Scores are **calibrated**, so 0.5 is the model's own recommended threshold for that tag:
`s' = sigmoid(logit(s) - logit(t))`. Here `t` is 0.35 for WD general tags, 0.75 for WD character tags, and RAM++'s
shipped per-class threshold for RAM++ tags. Only tags with `s' >= floor` are returned. WD `rating` is the model's raw
probability for each rating. Tag names are model-native (WD keeps its underscores); the panel normalises them.

**Errors.** 503 while loading (above). A bad picture fails only its own slot (`results[i]` is `null`, `errors[i]` says
why). **Out of graphics memory** is reported as HTTP 500 `{"error": "... CUDA out of memory ..."}`, or as such a text in
a slot's `errors[i]`; the panel looks for the words `out of memory` (any case), halves its `batch_size` for the session
and sends the assets again in smaller requests. Any other 5xx counts as "the service fell over" (the assets stay
untagged and are tried again); a 4xx is a bug in the request.

## 2. VLM (`immich_aitagger_vlm`)

`vllm/vllm-openai` serving `Qwen/Qwen3.5-9B` as model name `tagger-vlm`, on `127.0.0.1:11441` (container port 8000).
Flags: `--quantization fp8`, `--gpu-memory-utilization ${AITAGGER_VLM_UTIL}`, `--max-model-len 8192`,
`--max-num-seqs ${AITAGGER_VLM_SEQS}`, `--limit-mm-per-prompt {"image":8}`, plus (added when building it, see below)
`--max-num-batched-tokens 8192`, `--mm-processor-kwargs {"max_pixels":589824}` and
`--default-chat-template-kwargs {"enable_thinking":false}`. Image `vllm/vllm-openai:v0.30.0`. The HF cache is a volume under
`~/vlm/models/aitagger/hf` (the torch.compile cache is another, `.../vllm`). Ready means `GET /health` answers 200
(about 2 minutes after `up`, once the weights are cached).

**Memory (measured, RTX 4090, vLLM 0.30.0).** The FP8 weights take 10.8 GiB (embeddings, lm_head and the vision tower stay
bf16), plus about 1 GiB of activations, plus the KV cache. `--gpu-memory-utilization 0.45` (11 GiB) therefore **cannot
start**: "No available memory for the cache blocks". 0.51 is the bare minimum, 0.58 (13.9 GiB, 38k tokens of KV cache)
runs 8 concurrent requests with 6 pictures each; the real footprint is about 0.8 GiB more than the fraction (CUDA
context): 14.6 GiB at 0.58. With the formula in section 3 this needs `vram_gb >= 19` (0.58); the settings default is
therefore 20 (0.62) with limits 18–21. Measured with everything running: Immich ML 2.2 GB + tagger 4.0 GB +
VLM 14.6 GB = 21 GB of 24. Speed: one request with 6 pictures (about 2k prompt tokens, 200 answer tokens) takes
about 3 s; 8 in flight give about 1.7-2 requests/s (100-125 per minute); the worst case of 8 pictures of 768x768 px each
(5k prompt tokens) about 1 request/s. Thinking text never appears. A prompt that does not limit the tag lists made 4 of
16 answers run into `max_tokens` (cut-off, invalid JSON); with `"maxItems": 12` on `add_tags` and `remove_tags` in the
schema, 0 of 16 (vLLM enforces it), so the panel should add `maxItems` to its schema and ask for "at most 8" tags.
Restarting the stopped container takes about 70 s; first start downloads 18 GB.

The panel calls `POST /v1/chat/completions` with:
- `model: "tagger-vlm"`, `temperature: 0.2`, `max_tokens: 400`
- `chat_template_kwargs: {"enable_thinking": false}`
- `response_format: {"type": "json_schema", ...}` (the schema below)

Content: one system message, then one user message with the text part followed by 1–8 images as
`{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}`. Each image is at most 768 px on its long side.

The answer must match `{"description": str, "add_tags": [str], "remove_tags": [str]}`.

The system prompt says this is a private personal archive, that every picture is described neutrally and factually
including adult content, and that the model never refuses. The user text lists the detected tags with scores, the
rating, the user's `instructions` and `vocabulary`, and asks for 1–2 sentences in `language`.

A refusal or an invalid JSON answer is retried once, then stored as `description: ""` and noted in that asset's
status. It is **not** an asset failure.

## 3. Panel module (`aitagger.py`)

Storage lives in `state_dir()/aitagger/`: `settings.json` and `tagger.sqlite` (WAL, RLock, `check_same_thread=False`).

### Settings

`DEFAULTS` (validate every type explicitly — never `bool("false")`):

| key | default | limits / type | content? |
|---|---|---|---|
| indexing | false | bool | |
| keep_updated | true | bool | |
| video_frames | 6 | int 1–8 (UI offers 2 / 6) | yes |
| batch_size | 8 | int 1–64 (assets per round) | |
| vlm_parallel | 8 | int 1–32 (concurrent VLM requests, also `--max-num-seqs`) | |
| vram_gb | 20 | int 18–21 (taggers + VLM together; below 18 the VLM cannot start, above 21 the card runs out next to Immich ML) | |
| describe | true | bool | yes |
| use_wd, use_ram | true | bool | yes |
| wd_strictness, ram_strictness | 0.5 | float 0.05–0.95 (calibrated threshold) | yes |
| character_tags | true | bool | yes |
| rating_tag | true | bool | yes |
| max_tags | 30 | int 5–100 | yes |
| instructions | "" | str ≤ 4000 | yes |
| vocabulary | "" | str ≤ 4000: one entry per line, `old -> new` renames a tag, any other line is a preferred term passed to the VLM | yes |
| blocked | [] | list of str (never output) | yes |
| rules | [] | list of rules (below) | yes |
| write_tags | false | bool (also attach native Immich tags `AI/<tag>`) | yes |
| language | "English" | str ≤ 40 | yes |

`settings_version` is stored in `meta` and goes up by one whenever any "content" setting changes. Every result records
the version it was made with ("outdated" means a smaller version). How much of each GPU the two containers get is
derived from `vram_gb`:
- tagger: `AITAGGER_VRAM_GB = 5`
- VLM: `AITAGGER_VLM_UTIL = round((vram_gb - 5) / total_gpu_gb, 2)`

Changing `vram_gb` or `vlm_parallel` recreates the containers the next time they start.

### Rules

```json
{"if_all": ["girl", "beach"], "if_any": [], "unless": ["night"], "add": ["summer"], "remove": []}
```
A rule fires when all of `if_all` are present, at least one of `if_any` is present (if that list is given), and none of
`unless` are present. At least one of `if_all` / `if_any` and at least one of `add` / `remove` is required. Rules run
in order and repeat until nothing changes (at most 5 passes). Tags are compared after normalisation.

### Pipeline per asset

1. **Captures.** A photo uses its Immich preview JPEG, shrunk to 1024 px (reuse `searchplus.fetch_catalog`, `_picture`
   and `media.shrink_image`). For videos and animated images: split the length into 8 equal segments, take the midpoint
   of segments 2–7 (six candidates), then pick `video_frames` of them evenly (2 → segments 3 and 6). Frames come from
   ffmpeg on the original (reuse `searchplus.video_frames` logic with explicit timestamps) or from
   `media.animation_frames`.
2. **Tag.** Send all captures to `/tag` (batched across assets). Store per capture and per model the calibrated scores
   ≥ 0.05, in `raw`.
3. **Aggregate per tag across captures.** Missing means 0. `combined = 0.5 * median + 0.5 * max`. Keep the tag if
   `combined >= strictness` of its model. Rating: the mean of the per-capture probabilities, then argmax, giving the tag
   `rating: <name>` if `rating_tag` is on. WD character tags are kept only if `character_tags` is on.
4. **Normalise.** Lowercase, `_` becomes a space, `name_(qualifier)` becomes `name (qualifier)`. Apply the vocabulary
   renames, merge duplicates across models keeping the highest score, and drop `blocked` tags.
5. **VLM** (if `describe`). Apply its `add_tags` (score 1.0) and `remove_tags`, then normalise, rename and block again.
6. **Rules**, then `blocked` again. Cap at `max_tags` by score; rule- and VLM-added tags score 1.0.
7. **Write.** Compose the block and write the description with `PUT /api/assets/{id}`. Read it back and compare. Keep
   the previous description in `history`. If `write_tags` is on, upsert `AI/<tag>` tags (`PUT /api/tags`), attach them
   (`PUT /api/tags/assets`), and detach `AI/` tags that are no longer present.

Block format (the markers are how the panel finds its own text; everything outside them belongs to the user):
```
<user text, unchanged>

[AI Tagger]
Tags: girl, beach, summer, rating: general
Description: A woman walks along a sunny beach at low tide.
[/AI Tagger]
```

### Reprocess modes

| mode | taggers | VLM | when |
|---|---|---|---|
| `retag` | stored scores | stored answer | strictness, rules, blocked, renames, max_tags, write_tags changed |
| `describe` | stored scores | run again | instructions, vocabulary, language, describe changed |
| `full` | run again | run again | video_frames, use_wd/use_ram changed, or asked |

`retag` needs no GPU and runs even while the models are unloaded. The `queue` table holds `(id, mode, at)`; a stronger
mode replaces a weaker one for the same id.

Scopes: `ids` (list), `tag` (has that tag), `outdated` (version older than current), `all` (every processed asset).

### Tables

- `assets` (same columns as Search+)
- `raw(id pk, captures, scores_json, rating_json, tagged_at, models)`
- `results(id pk, tags_json [{tag, score, source: wd|ram|vlm|rule}], vlm_json, description, block, settings_version, processed_at, written_at, note)`
- `history(id, at, old_description, new_description)`
- `failed(id pk, error, attempts, at)` (same retry rules as Search+)
- `queue(id pk, mode, at)`
- `excluded(id pk, at)`
- `meta(key pk, value)`: `settings_version`, `catalog_at`, `native_tags`, `services_env`
- `asset_tags(id, tag)` (extra): one row per tag of a result, for the tag filter and the top-tags list

### Indexer

Same shape as Search+:
- A daemon thread, `instance()`, and `autostart()` (when `settings.indexing`) called from `serve()`.
- `CATALOG_EVERY = 600`.
- 6 prep threads; at most 2 tagger requests in flight; `vlm_parallel` VLM requests in flight.
- Work order: `queue` first, then unprocessed assets (newest first), skipping `excluded`.
- "Service down" is not the asset's fault.
- A CUDA OOM answer halves `batch_size` for the session and retries.
- Status: `{state: stopped|starting|running|done|error, detail, error, ratePerMin, etaMinutes}`.

### Services and GPU

- `Service`-like wrappers for both containers, through `docker compose -p immich-aitagger -f deploy/aitagger/docker-compose.yml up -d <svc>` with the env derived from the settings. Use `--force-recreate` when the derived env changed.
- **Stop Search+ before starting the tagger** (`searchplus.Service().stop()`).
- While the tagger VLM container runs, `searchplus.Service.start()` raises `GpuBusy` (a subclass of `ServiceDown`) with "The GPU is in use by the AI Tagger — pause it to use Search+". The Search+ indexer waits on `GpuBusy` without counting it as a drop.
- Unload stops both containers. The panel also stops the VLM container when the indexer has been idle for `IDLE_EXIT_MINUTES`.

### Implementation notes (what `aitagger.py` does where this file leaves room)

**Settings.** Types are checked strictly: a bool must be a JSON bool (`"false"`, `0`, `null` are refused), an int a whole
number (`6.0` is accepted, `"6"` and `true` are not), a float a finite number. `language` is stripped and not empty;
text keeps its text, with `\r\n` as `\n`. `blocked` and every list inside a rule are stored **normalised** (see step 4,
duplicates and empties dropped), so the UI shows the canonical form. Rule checks: at most 100 rules, 50 tags per list,
no unknown field, a tag cannot be in both `add` and `remove`; the error says "Rule N: ...". A change is all or
nothing. A bad value in a hand-edited `settings.json` falls back to the default. The file is written first and
`settings_version` bumped after; readers read the version first, so a race at worst makes a fresh result look
"outdated". `meta.native_tags = "1"` is set once `write_tags` has ever been on: from then on every write also keeps the
`AI/` tags in step (detaching them all when `write_tags` is off). Without that flag the panel never touches tags.

**Vocabulary.** `old -> new` (or `old → new`); both sides are normalised. A line with an arrow and an empty side is
ignored. The `new` side is also passed to the VLM as a preferred term. The VLM sees the already renamed tags.

**Normalising.** Besides lowercase and `_` → space: `,` and `/` become spaces (a tag cannot split the `Tags:` line or
the native tag path), `[`/`]` become `(`/`)` (a tag cannot forge the block markers), a trailing `.` goes, at most
60 characters. `blocked` is checked against a tag's name both before and after the renames.

**Step 3, 6.** `combined` is compared with the strictness with a 1e-9 tolerance. A tag a capture does not list counts
as 0 for that capture; a capture the tagger could not read is not a capture. **The rating tag is exempt from the cap:**
the cap keeps the best `max_tags - 1` other tags plus `rating: <name>`, and `rating:` tags are listed last. Equal
scores sort by name. `video_frames` above 6 behaves as 6 (there are six candidate segments).

**Block.** The description inside the block is one line; `[AI Tagger]` / `[/AI Tagger]` inside it are removed. A block
is found by looking for `[/AI Tagger]` and taking the nearest `[AI Tagger]` before it, so a stray marker in the owner's
text is the owner's. A new block is appended after `\n\n`. An existing block is replaced where it stands, so text
before and after it stays byte for byte. Removing a block removes the `\n\n` the panel added before it when the block
was last (so the owner's text is exactly what it was), or the line break after it otherwise. A second block is leftover
and removed. When there is nothing to say (no tags, no description) the block is "" and an old block is removed.

**Pipeline and failures.** Raw scores are stored as soon as the tagger answers, the result before it is written
(`written_at` is null until Immich holds it and the read-back matched). An asset whose result is stored but not written
counts as *pending* and is finished later **without the GPU** (as a `retag`): Immich being down loses no GPU work. The
read-back compares after turning `\r\n` into `\n` and trimming the ends. Failure bookkeeping is Search+'s (3 attempts,
retried after 900 s, a `ValueError` is final); `failed` rows also leave the queue when final. Not the asset's fault, so
no failure row: a model server or Immich that does not answer (5xx, unreachable), a refused API key (the indexer stops
with an error). Immich saying 404 for an asset is a final failure ("not found").

**Queue.** `enqueue` makes a stronger mode replace a weaker one (`full` > `describe` > `retag`); asking for an asset
also forgets its earlier failures and its "exclude". A queue row leaves the queue when the work done is at least what
was asked; a `describe` asked while `describe` is off is done by a retag and counts. A `retag` or `describe` for an
asset with no stored scores becomes `full`. Excluding an asset takes it off the queue.

**Indexer.** Pipelined: pictures are prepared (6 threads), tagged (≤ 2 requests in flight, ≤ `batch_size` assets and ≤ 64
pictures each), then described and written (`vlm_parallel` at a time) while the next assets are already being
prepared. 3 servers going away in a row, with nothing finished in between, end the run with an error. A CUDA
out-of-memory answer sets a session cap on the batch size (half of the failed batch) that is dropped when the owner
changes `batch_size`; a single picture that does not fit fails that asset only.

**Services.** Compose services are `aitagger` and `vlm` in `deploy/aitagger/docker-compose.yml` (project
`immich-aitagger`); the command is `docker compose -p immich-aitagger -f <file> up -d [--force-recreate] <service>`
with `AITAGGER_VRAM_GB`, `AITAGGER_VLM_UTIL`, `AITAGGER_VLM_SEQS` in its environment. The env each container was last
started with is remembered in `meta.services_env`; a stopped container whose env differs (or is unknown) is recreated.
`AITAGGER_VLM_UTIL = min(round((vram_gb - 5) / total_gpu_gb, 2), 0.95)`, `total_gpu_gb` from
`nvidia-smi --query-gpu=memory.total` (MiB / 1024), 24 if that fails. Container states are remembered for 5 s. Starting
any container first stops Search+. One idle clock (`last_used`: tagging, describing, Test, load) is checked by the
indexer while it waits and by a small panel thread every minute, so the VLM container is also stopped after 20 idle
minutes while tagging is paused. `GpuBusy` is raised by `searchplus.Service.start()` only when Search+ would have to be
started; a Search+ that is already running is left alone.

## 4. Panel API

Every route needs the panel token. Errors are `{"error": "..."}`: 400 for invalid input, 503 for models not ready or GPU busy, 404 for unknown.

| Route | Body | Answer |
|---|---|---|
| `GET /api/aitagger` | — | status (below) |
| `POST /api/aitagger/settings` | `{changes: {...}, reprocess?: "none"\|"retag"\|"describe"\|"full", scope?: "outdated"\|"all"}` | status + `queued` |
| `POST /api/aitagger/index` | `{action: start\|pause\|retry\|clear}` | status |
| `POST /api/aitagger/load` | — | status (starts both containers; no tagging) |
| `POST /api/aitagger/unload` | — | status (pauses, stops both containers) |
| `POST /api/aitagger/preview` | `{id}` | preview (below), writes nothing |
| `POST /api/aitagger/apply` | `{id}` | preview + `written: true` (process and write now) |
| `POST /api/aitagger/reprocess` | `{scope, ids?, tag?, mode}` | status + `queued` |
| `POST /api/aitagger/remove` | `{ids, exclude: bool}` | `{removed, excluded}` (strip the block, forget results) |
| `GET /api/aitagger/assets` | `?tag=&q=&outdated=1&page=1&size=60` | `{items: [{id, name, type, taken, tags: [str], description, settingsVersion, processedAt}], total, page, tags: [{tag, count}]}` (top 200 tags) |
| `GET /api/aitagger/sample` | `?type=IMAGE\|VIDEO` | `{id, name, type}` (a random catalog asset) |

Status:
```json
{"settings": {...}, "limits": {"video_frames": [1, 8], "batch_size": [1, 64], "vlm_parallel": [1, 32], "vram_gb": [6, 22],
 "wd_strictness": [0.05, 0.95], "ram_strictness": [0.05, 0.95], "max_tags": [5, 100]},
 "settingsVersion": 3,
 "counts": {"assets": 0, "images": 0, "videos": 0, "processed": 0, "pending": 0, "queued": 0, "outdated": 0, "failed": 0, "excluded": 0},
 "indexer": {"state": "stopped", "detail": "", "error": null, "ratePerMin": null, "etaMinutes": null},
 "service": {"tagger": {"container": "running|stopped|missing|unknown", "status": "ok|loading|error|down", "error": null},
             "vlm": {"container": "...", "status": "ok|loading|down", "error": null},
             "gpu": {"totalGb": 24, "usedGb": 7.1}, "searchplusRunning": false},
 "models": {"wd": "wd-eva02-large-tagger-v3", "ram": "RAM++ (swin-large)", "vlm": "Qwen3.5-9B (FP8)"},
 "failures": [{"id", "name", "error", "attempts", "at"}]}
```

Preview:
```json
{"id", "name", "type", "captures": 6, "frames": ["data:image/jpeg;base64,... (256 px)"],
 "models": {"wd": [{"tag", "score"}], "ram": [{"tag", "score"}], "rating": {"general": 0.1, ...}},
 "vlm": {"description", "add_tags", "remove_tags", "note"},
 "rules": [{"rule": 0, "added": [], "removed": []}],
 "tags": [{"tag", "score", "source"}], "description": "...", "block": "...",
 "currentDescription": "...", "newDescription": "...", "written": false}
```

**Details and extras of the routes** (all additions are optional for the UI):
- Status: `counts` also has `retrying`, `cleared` (failures hidden with "clear") and `catalogAt`; `indexer` also has
  `running`; each failure has `name` and `at`; `reprocessKeys` is `{retag: [...], describe: [...], full: [...]}`, the
  settings keys that suggest each mode (the strongest of the changed keys wins). `counts.processed` counts assets whose
  result is written to Immich; `pending` are assets with no written result that have not failed; `queued` is the queue.
- `settings` answers the status plus `queued`, `changed` (the content keys that really changed) and `suggest`
  (`none|retag|describe|full`). `indexing` inside `changes` is ignored (use `index`). `reprocess` defaults to `none`,
  `scope` to `outdated`; the check of `reprocess` / `scope` happens before anything is saved. When tagging is on and
  something was queued the indexer is woken (or started).
- `reprocess` takes `ids` (asset ids) or `tag`; `queued` is the number of assets matched. `remove` answers
  `{removed, excluded, failed: [{id, error}]}`: results are kept for the ones that failed; `exclude` is applied to all.
- `preview` / `apply` read the library list from Immich first when the panel has none yet (the Test card works before the
  first start). In `models.wd` / `models.ram` every entry also has `kept` (passed that model's strictness); entries are
  the scores from 0.2 up, at most 80, best first.
- `GET /api/aitagger/assets`: `size` is capped at 200; `q` matches the file name, the description and the tags (plain
  text, `%` and `_` are not wildcards); `tags` is the top 200 over all tagged assets (not only the filtered ones).
  Only assets whose result is written are listed. `sample` answers 404 when the library list has no such asset.
- Errors: 400 invalid input (including a photo Immich has no file for), 404 unknown asset / route, 503 the models are not
  ready ("... Try again in a minute.") or `GpuBusy` (its own message, also from the Search+ routes), 502 Immich does not
  answer. `preview` / `apply` wait at most 90 s for the models (starting them if needed), then answer 503.

## 5. Web tab

The tab button is "AI Tagger" (`data-panel="tagger"`, section `panel-tagger`). It reuses the Search+ conventions:
`.card`, `.seg`, `.meter`, `.stats`, `busy()`, `toast()`, 5 s status polling while visible, and `CACHE` bumped in
`sw.js`.

Cards:
1. **Model and progress.** State line, meter, and stats (processed / pending / queued / outdated / failed). Buttons:
   Load models, Start / Pause tagging, Stop & free GPU. A note that Search+ is paused while the tagger uses the GPU.
2. **How to tag.**
   - Instructions (textarea) and Vocabulary (textarea, with a help line).
   - Blocked tags.
   - Language.
   - Checkboxes: describe, character tags, rating tag, write native Immich tags.
   - Strictness sliders for WD and RAM++, and max tags.
3. **Rules.** Editable rows: IF all of [tags] / any of [tags], UNLESS [tags], THEN add [tags] / remove [tags]. Add or
   delete a row, and Save.
4. **Speed and memory.** Captures per video (2 / 6), assets per round, parallel descriptions, GPU memory (GB), keep up
   to date.
5. **Test.** An asset id field plus Random photo / Random video, then Preview. It shows the captures, the tags per model
   with scores, the rating, what the VLM added and removed, which rules fired, the final tags, the description, and the
   description before and after. A "Write this" button (`apply`).
6. **Tagged assets.** Top-tag chips, search, an "outdated only" filter, and a list with thumbnails, tags and
   description. Selection actions: Re-tag, Re-describe, Full re-process, Remove AI text (with "and don't tag again").

Saving "How to tag" or "Rules" opens a choice:
- **New assets only** (default)
- **Also update the N already-tagged assets**, with the suggested mode preselected from the keys that changed (`retag`
  or `describe`).
