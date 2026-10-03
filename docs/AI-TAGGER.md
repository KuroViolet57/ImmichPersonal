# AI Tagger — design and contracts

A panel tab that tags every photo and video with two image taggers, lets a vision-language model (VLM) refine
the tags and write a short description following the user's own instructions, and writes the result into the
asset's Immich description. It runs in the background like Search+ and keeps new uploads up to date.

This file is the contract between the parts. Change it when an interface changes.

## As deployed (v3.1, 2026-10-02) — read this first; older numbers further down are history

| | |
|---|---|
| Taggers (`TAGGERS`) | `wd` WD EVA02-L v3 · `pixai` PixAI v1.0 · `ram` RAM++ (no rating, noise words) · `e621` Hydra 3.5 (general/species/character/copyright, no rating, noise `mammal`) |
| Explicit-tag check | on a general/sensitive rating a tag in `EXPLICIT_TAGS` needs ≥ 2 enabled taggers (wd, pixai, e621 can confirm; ram shares only "condom") |
| `FLOOR` / strictness | the panel asks for scores ≥ **0.2** (at 0.05 RAM++ alone sent ~4,400 tags per picture, all stored); `<key>_strictness` limits **0.2–0.95**, default 0.5 = each model's own threshold |
| `vram_gb` | default **6**, limits **5–8** (four taggers: 5.77 GB at 6, ~11 pictures/s; under ~5 GB they don't load) |
| Description block | `[AI Tagger]\nTags: …\n[/AI Tagger]` — no describer |
| Vocabulary box | "Renames and combinations": `a -> b`, `a + b -> c`, `a \| b -> c`, `a + !b -> c`, `-tag` removes (see the section at the end of v3) |
| Unloading | the panel **stops the container right after the work**: nothing left to tag and `/health` `idleSeconds` ≥ `unload_after` minutes (default **2**, limits **1–60**), never while the indexer works or a request is in flight (`busy`); after a Test-card use the models keep their place for the server's own 20 minutes first. The server's 20-minute exit stays as the backstop. Status `unload` object, countdown in the web tab and the app (see "Unloading the models and picking up new uploads") |
| New uploads | a cheap change probe every `check_every` minutes (default **1**, limits **1–60**) instead of a full library read every 10 minutes; the full read only when the probe changed, and at least hourly. A new asset waits for Immich's preview file (up to 30 minutes) |
| Indexer | pipelined across rounds: ≤ 2 tagger requests in flight (`Indexer.TAG_REQUESTS`) while the next round's pictures are prepared, so the GPU is not idle at round boundaries (see "Indexer: pipelined across rounds" in v3, panel side) |
| Finding tags | `GET /api/aitagger/tags?q=` finds any of the (21,110) tags; the "Tagged assets" card shows the 200 commonest as "Most common tags" with "Show more"; the Search and Search+ tabs filter by `tags` / `tagMode` / `description` (see "Search by tags and description") |

## Unloading the models and picking up new uploads (v3.2, 2026-10-03)

The owner: "I do not want the models to take space even when they are not indexing or being used." One new photo used to
keep the models in the graphics card for 20 minutes (the model server's own idle exit, `IDLE_EXIT_MINUTES`), and a new
upload waited up to 10 minutes to be noticed. The AI Tagger and Search+ now work the same way; the shared code is in
`searchplus.py` (`unload_status`, `IdleLoop`, `CatalogWatch`, `fetch_probe`). `tagger_service.py`, `embed_service.py` and
the deploy files are unchanged.

**Settings** (both features; whole minutes; not content settings, so no "outdated" bump and no reprocess):

| Key | Default | Limits | Meaning |
|---|---|---|---|
| `unload_after` | 2 | 1–60 | the panel stops the model container once nothing is left to do and the server has been idle this long |
| `check_every` | 1 | 1–60 | how often the panel asks Immich's database whether anything changed |

**Unload rules.** The check runs from the indexer's waiting loop (it wakes at least every minute, `IDLE_POLL`; a loop that
is working never checks). The panel stops the container when all of these hold: the indexer has nothing left to do (it is
waiting for new photos, or `keep_updated` is off and it is winding down), `/health` is `ok` and its `idleSeconds` ≥
`unload_after` × 60, no request is in flight (the tagger's `/health` `busy` is 0 *and* the panel has none out; the Search+
server has no `busy`, so the panel counts its own requests), and no interactive grace is on.
- **Interactive grace.** After a person used a model the short rule waits until the server's own idle time (its
  `idleExitMinutes`, 20) has passed since that use. Interactive use is: the AI Tagger's Test card (`Indexer.test`:
  preview and apply; `Services.mark_interactive`) and, for Search+, every text embedding (`Service.embed_text`: the Search+
  search, the search engine paths of `/api/search`, smart albums and themes; a "like this photo" search needs no model).
  The panel remembers the time (`interactive_age`); the indexer's own requests never count.
- **Paused indexer.** With no indexer thread there is no waiting loop to run the check; if the models were loaded
  interactively only the server's 20-minute exit applies.
- **`keep_updated` off.** The thread still ends when everything is done, but first it waits out the short rule (the status
  stays "up to date") and stops the container; with an interactive grace on it ends at once and the server's exit does it.
  Start / Try again wakes it for new work.
- A failing `docker stop`, or a health look that fails, never ends the indexer; the server's own exit is the net.

**The `unload` object** in `GET /api/aitagger` (and `GET /api/searchplus`), computed from the (remembered, ≤ 5 s) `/health`
and container state, so the polling stays cheap:
```json
"unload": {"loaded": true, "idleSeconds": 40, "unloadInSeconds": 80, "rule": "after-work", "busy": false}
```
`loaded`: the container runs (false: `idleSeconds` / `unloadInSeconds` / `rule` are null). `idleSeconds`: the server's, null while
it loads. `unloadInSeconds`: when the container will be gone if nothing happens, null while busy. `rule` says which rule
does it: `after-work` (the short rule), `interactive` (the grace, then the short rule) or `server` (the server's own exit:
the indexer is paused, or `unload_after` is longer than the server waits). `busy`: a request is in flight or the indexer is
working (no countdown then). The web tab shows "Models loaded · unloads in ~1 min 40 s if nothing new" or "Not loaded · GPU
memory free" (Search+: "Model"); the app shows the same line, and both have the two settings (the web tab in "Speed and
memory" and next to "keep it up to date" on the Search+ tab).

**Picking up new uploads.** The 10-minute full read of the library list (`CATALOG_EVERY`) is gone. Every `check_every`
minutes the indexer runs one small query through the same `docker exec immich_postgres psql` path (`fetch_probe`, about 0.2 s):
```sql
select (select count(*) from asset a where <live>),                                  -- live = not deleted, timeline or archive, image or video
       (select coalesce(max(a."createdAt")::text,'') from asset a where <live>),      -- newest upload
       (select coalesce(max(a."deletedAt")::text,'') from asset a),                   -- newest trashing
       (select count(*) from asset_file f where f.type = 'preview'),                  -- previews made
       (select coalesce(max(f."createdAt")::text,'') from asset_file f where f.type = 'preview')
```
The columns were checked against the live schema (`\d asset`, `\d asset_file`). `asset."updatedAt"` / `updateId` are *not*
used: they move whenever anything is written to an asset, and the AI Tagger writes every description. The full library
read (`fetch_catalog`) happens only when the probe's answer differs from the one at the last full read (new, deleted,
trashed, archived or hidden assets, a new preview), when there has not been one yet, and at least every
`SAFETY_SYNC` = 60 minutes. A failing probe changes nothing (the hourly read is the net); Start / Try again looks at
once.

**Not before Immich has finished.** The catalogue rows carry `added` (`asset."createdAt"`, epoch seconds; 0 = unknown, kept
in the `assets` table of both stores, which add the column when they are opened). An asset with no preview file
(`asset_file` of type `preview`; videos too) that was added less than `PREVIEW_GRACE` = 30 minutes ago is left out of the
work list (`searchplus.READY`), not failed; the status line says how many wait. The probe notices the preview and the next
read brings its path. After 30 minutes without one it is processed as before (the fallback to the original).

## Search by tags and description (v3.3, 2026-10-03)

The owner thought tags were missing: the "Tagged assets" card listed only the 200 commonest of the library's 21,110
distinct tags (the 200th is on 4,300 assets; 3,812 tags are on exactly one asset), so a tag seen on an asset was
often not in it. Now every tag can be found, and the Search and Search+ tabs can filter by AI tags and by description
text. Immich can do neither: `SmartSearchDto` has no `description` filter, and the `tagIds` of both search bodies are
Immich's *native* tags, not ours (ours live in the description block and in `asset_tags`). `MetadataSearchDto`
(`POST /search/metadata`) does have `description` ("the description contains this text", a case- and accent-blind
substring match; marked deprecated in 3.2.0, present in 3.2.4). Code: `tagsearch.py` (the filter, the candidates, the
`filters` object), `aitagger.Store` (tags), `searchplus.search(only=)` and `engine.evaluate_rule(only_ids=)` (the ranking).

**`GET /api/aitagger/tags?q=&limit=50&offset=0`** → `{"tags": [{"tag", "count"}], "total"}`. Tags that contain `q`
(normalised with `norm_tag` like everywhere: lowercase, `_` as a space, ...; no `q`: every tag), most used first then by
name, one page; `total` is how many match. `limit` is 1-200, `offset` ≥ 0 (numbers or 400). The count is the number of live
assets (Immich still lists them) that have the tag. `GET /api/aitagger/assets` keeps its top-200 `tags` and has `tagsTotal`
(how many tags there are); the screens label the 200 "Most common tags" and page on through the route ("Show more").

**Store.** `asset_tags(id, tag)` already had the index `asset_tags_tag` and the primary key `(id, tag)`: asset to tags,
tag to assets, both by index. They are not enough for a *substring* search: on the live library `asset_tags` has 4.2
million rows, `like '%wolf%'` over the covering index takes 180 ms, and the old top-200 query (a join with `assets`) took 5 s.
So there is a summary, `tag_counts(tag primary key, n)`, one row per tag (21,110), kept by two SQL triggers on `asset_tags`
(insert +1, delete -1 and the row goes at 0), so every way of writing or deleting tags is counted (a row must never be
replaced with `insert or replace`, which does not fire delete triggers). A store made before is filled once when it is
opened (`meta.tag_counts`, one `group by`: about 0.2 s warm, a few seconds cold on the 5.5 GB live file); `rebuild_tag_counts()`
repairs it. `Store.tag_index()` reads it into memory (a list best first and a dict), minus the tags of assets that are
gone (`assets.gone`; a query that starts from the few gone assets), and keeps it until a result, a forget or a library read
changes something (`_changes`). `list_tags`, `top_tags` and the chip counts all come from it; after the first call a search
touches no table. Measured on a store of the live shape: see "Measured" below.

**Search requests.** Three optional fields for `POST /api/search` (both engines) and `POST /api/searchplus/search`:

| Field | Meaning |
|---|---|
| `tags: [str]` | at most 20 (400 above that; counted as sent). Each is normalised with `norm_tag` and matched **exactly** against `asset_tags` (renames are not applied: the stored tags are the renamed ones, the box suggests them). Blank entries are skipped; one that normalises to nothing is a 400. Repeats count once |
| `tagMode` | `"all"` (default) or `"any"`; 400 otherwise |
| `description: str` | at most 200 characters after trimming (400 above); "the description contains this text" through Immich's metadata search, newest first, paged, up to `tagsearch.DESCRIPTION_CAP` = 5,000 assets. The text is also in the `[AI Tagger]` block, so it finds tags too |

With none of them (or empty ones) a request is as before. Tags and description together: the assets in both. The
candidates are the live assets of the tagger store (tags) and the assets Immich answers (description). What happens next
depends on the rest of the request:

| Request | What is returned |
|---|---|
| no text, no reference photo (only filters) | the candidates, **newest first**, up to `limit` (with `total` when more). Dates and names come from the tagger store's library list, or, with a description, from Immich's answer. Either model; needs neither. With people, "in no album", `must also` words or "any of these people" it reads Immich's list of those newest first and keeps the candidates (the same page-by-page way as below) |
| Search+ model (Search+ tab or `engine: searchplus`) with text or like | **only the candidates are ranked**: the mask (live, indexed, candidates, then type, dates, people, skipped albums, must-also words) is made first and `view.rows_of(positions)` gives the matrix rows of what is left; only those rows are read from the memory-mapped file and scored (`best_scores(only=)`, `row_scores_of`). Nothing outside the candidates is read, scored or returned; the same order and scores as ranking everything and filtering, which a test checks on random sets. No candidates: no model start either |
| Immich smart search with text or like | Immich cannot rank just a set, so its ranking is read page by page (1,000 a page) and the candidates picked out until `limit` of them are found or `tagsearch.SCAN_CAP` = 10,000 results have been looked at (skipped assets count as looked at). It stops reading as soon as it has `limit` |
| `compare` on the Search+ tab | Immich's column is read the same way (its first 10,000 ranked results) and cut to the candidates |

The answer gets `filters` (only when a filter was asked for):
```json
"filters": {"tags": ["anthro", "wolf"], "tagMode": "all", "description": "", "candidates": 312, "capped": false,
            "ranking": "none|searchplus|immich", "ranked": 41, "scanned": 4210, "scanCapped": false,
            "text": "Filtered to 312 assets with all of: anthro, wolf. Ranked only those with the Search+ model (41 of them are in its index)."}
```
`candidates` is the size of the set (tags ∩ description) before anything else; `capped` that Immich had more than 5,000
assets with that description text (only the newest 5,000 were used); `ranked` (Search+) how many candidates are in the
index; `scanned` / `scanCapped` (Immich) how many results of its ranking were looked at and whether the cap stopped it
before `limit` was reached. `text` is the line the screens show over the results.

**Screens.** Web (Search and Search+ forms, in the filters area): an "AI tags" box with a drop-down from the route (the
commonest when it opens, narrowing as you type; counts beside each), chips, an "All of them / Any of them" switch from
two chips on, a "Description contains" box and `filters.text` over the results. A typed tag is checked against the route
before it becomes a chip. The AI Tagger tab: "Find a tag" (same drop-down; picking one filters the list exactly like a
chip), the chips labelled "Most common tags" and "Show more" (60 a time through the route). Android: the same in
`SearchScreen`, `SearchPlusScreen` (inside the filters) and `TaggerScreen` (Tagged), with `TagFilter.kt` shared.
"Save as smart album" leaves the two filters out and says so (a smart album cannot use them yet).

**Fix on the way.** `ImmichClient.iter_smart_search` asked for `limit - seen` on the last page, but Immich's offset is
`(page - 1) * size`, so a smaller last page landed inside the one before (repeats, skipped by the id check) and a limit one
over a whole page needed a request per result (10,001 would have been 10,000 requests). Every page now asks for the same
`size`.

**Measured** (2026-10-03). Read-only against the live `tagger.sqlite` (opened `mode=ro`); the query timings are of the
shipped code on a scratch store holding a copy of the live `assets` and `asset_tags` rows (4.2 million rows, 73,000
assets, 21,110 tags), on WSL's own disk like the live file (the same copy on a Windows drive was 20 times slower, which is
not how the panel runs).

| | |
|---|---|
| `tag_counts` kept by the triggers while 4.2 million rows were inserted | equal to a recount (`group by tag`, 150-190 ms warm) |
| `list_tags(q)` (the route's work), `q` = wolf, anthro, a, e, long hair, `(`, 1girl | 0.5-0.6 ms each (0.00 ms for the first page of everything) |
| the first one after a change: the in-memory index is built from `tag_counts` | 10 ms (39 ms with 2,000 assets gone) |
| for comparison: `like '%wolf%'` over `asset_tags`' covering index / the old top-200 query | 148-180 ms / 5 s |
| `ids_with_tags`: anthro AND wolf (228 assets) / anthro (8,480) / `solo` (38,705) / `solo` OR 1girl (56,919) | 0.7 / 1.7 / 8 / 27 ms |
| `ids_with_tags`, the 20 commonest tags, all of them / any of them | 37 / 157 ms |
| `newest` (the listing for a filters-only search), 228 assets | 0.3 ms |
| `list_assets(tag=...)` for the Tagged card: before (a correlated `exists`) / now (`a.id in (select id from asset_tags where tag=?)`) | 130 ms for any tag / 2-5 ms for a rare tag (534 assets), 46 ms for 8,480, 210 ms for 38,705 |

Plans: the "all" lookup is `SEARCH t0 USING COVERING INDEX asset_tags_tag (tag=?)` and, per row of the rarest tag,
`SEARCH t USING PRIMARY KEY (id=? AND tag=?)`; "any" is `SEARCH asset_tags USING COVERING INDEX asset_tags_tag`; the gone
assets come from the partial index `assets_gone` (`create index ... on assets(id) where gone = 1`, empty while nothing is
gone). Nothing scans the 4.2 million rows. The one-off migration of the live store (the `group by`) is the only
slow step and runs when the new panel first opens it.

The owner: the text describer "doesn't enhance or improve anything"; the description should hold **tags only**. Instead,
add a **third tagger** that is good on both real photos and illustration, or is bigger / more accurate.

1. **No describer.** The `vlm` service/container, the VLM client, the prompt and the guards that existed for it are
   removed. The settings `describe`, `instructions`, `language` and `vlm_parallel` are removed (old settings files
   holding them still load; sending them is refused like any unknown key). `vocabulary` holds renames and
   combinations (`a + b -> c`, see "Renames and combinations" below; before it held only `old -> new` renames and
   ignored other lines). Reprocess modes are `retag` and `full` (a stored or requested `describe` counts as `retag`).
2. **Block format:** `[AI Tagger]\nTags: a, b, c\n[/AI Tagger]`, with no Description line. Results made with a
   Description line become outdated (settings version bump), and a `retag` rewrites them without the line.
3. **Taggers are a registry.** There is an ordered list of tagger kinds (`wd`, `pixai`, `ram`: the third is RAM++,
   item 4; `e621`: the fourth, item 6). Each has `use_<key>` and `<key>_strictness` settings, a label, the categories it
   contributes (general / character / copyright…), and whether it reports a rating. Everything that was hard-wired
   to two taggers loops over the registry:
   - detection and merging (highest score wins, `source` = that tagger's key)
   - the rating (mean of the enabled taggers that report one)
   - the status `models` labels, the preview's per-model lists, and the settings validation
   - **the explicit-tag check:** on a general/sensitive rating, a tag in `EXPLICIT_TAGS` is kept only when **at
     least two enabled taggers** found it. With a single tagger on, it is dropped.
4. **The third tagger is RAM++ (Recognize Anything Plus Model), key `ram`**, in the same `tagger` container and
   `/tag` answer, with the same calibration idea (0.5 = its recommended threshold), `floor`, per-picture errors,
   VRAM cap, out-of-memory splitting and optional `models` field. It was chosen by testing JoyTag, RAM++ and the
   WD EVA02 2026 "canary" on 53 real library pictures next to WD and PixAI (see "Third tagger: research and test"). In
   short: it is the only candidate that adds many *correct* tags on real photographs and screenshots (objects, scenes,
   food, "screenshot", "selfie", "poster") with few made-up ones, it costs 0.7 GB of VRAM and about 4.5 ms a picture, and
   it names what the two Danbooru taggers have no word for. What it does **not** do: it adds nothing on illustration,
   it reports no rating, and it cannot vote in the explicit-tag check (it has no sexual vocabulary). The registry
   entry is `{"key": "ram", "label": "RAM++ (swin-large)", "categories": ["general"], "rating": false}`.
5. **GPU:** only the `immich_aitagger` container. `vram_gb` stays the taggers' cap (`AITAGGER_VRAM_GB`). Re-measured
   with three taggers: **default 5, limits 4-8** (it was 3-8: with three models a cap of 3 no longer loads, 3.5
   does). The process uses 4.2 GB at a cap of 5 and 15 pictures/s. See "Measured (v3)".
6. **A fourth tagger: Hydra 3.5, key `e621`** (2026-10-02, after the owner's furry / anthro art): an e621-vocabulary tagger
   (`anthro`, `feral`, `human on anthro`, species...), in the same container and `/tag` answer with `general`, `species`,
   `character`, `copyright`. With four taggers the **`vram_gb` default is 6, limits 5-8** (5.8 GB used, 11 pictures/s); the
   three-tagger numbers above are superseded. See "4th tagger: Hydra 3.5".

### Third tagger: RAM++ as built

| | |
|---|---|
| Key | `ram` |
| Label | panel: "RAM++ (swin-large)"; `/health` `models[].name`: `ram_plus_swin_large_14m` |
| Model | Recognize Anything Plus Model (Huang, Zhang et al.; paper arXiv 2310.15200), Swin-L image encoder at 384 px + a label-embedding re-weighting + two cross-attention layers; 329M parameters; file `ram_plus_swin_large_14m.pth` (3.0 GB, it still holds the optimizer state; 0.66 GB on the GPU in fp16) |
| Hugging Face | `xinyu1205/recognize-anything-plus-model`, revision `84d4aee3a0265c4e0df1f714f0572011d1bf2ec3` (2023-10-25, the repository's only revision), cached under `/cache/hub` like the others (`RAM_REVISION` overrides) |
| Source files | the official `ram` package is not installed (it pins `timm==0.4.12`, `fairscale` and an old `transformers`). The image build fetches one pinned commit of https://github.com/xinyu1205/recognize-anything, `7cb804a8609e9f4b1a50b7f31436d2df40bb9481`, and keeps `swin_transformer.py`, `ram_tag_list.txt`, `ram_tag_list_threshold.txt` and the licence texts in `/opt/ram` (each file checked against its SHA-256); `tagger_service.py` holds the small tagging head. It was checked against the official code on real pictures in v1 (identical logits in fp32; the network code is unchanged). New dependency: `scipy==1.18.1` (imported at the top of the Swin source, not used for inference) |
| Licence | Apache-2.0 (model card and repository; the Swin source inside is MIT, see its `NOTICE.txt`): free for personal self-hosted use |
| Vocabulary | 4,585 plain-English tags (objects, scenes, actions, "screenshot", "selfie", "anime", "birthday cake"...), unique, no commas or slashes. Names have spaces, some capitals (`3D CG rendering`); the panel's normalisation lowercases them |
| Categories | `general` only: no characters, no series, **no rating** |
| Calibration | the model's own per-tag thresholds (`ram_tag_list_threshold.txt`: 0.45-1.0, mean 0.69; 10 tags have 1.0 and are never returned): `s' = sigmoid(logit(s) - logit(t))`, so **0.5 = the model's threshold**; `floor` as for the others (default 0.05, so the panel's `ram_strictness` 0.5 is the model's own operating point) |
| Input | RGB (transparency flattened onto white), resized to 384 x 384 with bilinear interpolation, aspect ratio **not** kept (what the official transform does), ImageNet mean/std |
| Precision | fp16 (`RAM_PRECISION`; bf16 and fp32 work). Over 240 library pictures fp16 differs from fp32 in 6 of 1,723 decisions at 0.5 (0.35%, largest score difference 0.007); bf16 in 19 (1.1%) |
| Typical output | 7.2 tags >= 0.5 per picture over 240 library previews: 10.4 on the 70 that look like photographs, 5.9 on the 170 that do not (WD 25.9, PixAI 27.0) |

`POST /tag` answers one more key per picture, with the same nesting as the others and no `rating`:
```json
{"results": [{"wd": {...}, "pixai": {...},
              "ram": {"general": {"screenshot": 0.88, "text message": 0.62, "person": 0.55}}}, null],
 "errors": [null, "cannot identify image file"], "tookMs": 412}
```
`"models"` accepts `"wd"`, `"pixai"` and `"ram"` in any mix (any other name is a 400 listing the three); `/health` has
`effectiveBatch: {"wd": 8, "pixai": 4, "ram": 8}` and a third entry in `models`
(`{"name": "ram_plus_swin_large_14m", "kind": "ram", "tags": 4585, "precision": "fp16", "loadedIn": 2.1}`). Environment:
`RAM_MODEL`, `RAM_REVISION`, `RAM_PRECISION`, `RAM_CODE` (default `/opt/ram`).

What the panel should know about it:
- **Plain English next to Danbooru.** `man`, `woman`, `girl`, `person` say what `1boy`, `1girl`, `solo` say; the panel does
  not merge them (different words), the owner can with `old -> new` renames. The rest is new: `screenshot` (WD and PixAI
  only say `fake screenshot`), `selfie`, `text message`, `website`, `poster`, `qr code`, `calendar`, `pizza`, `paella`,
  `stove`, `liquor`...
- **Not in the explicit-tag check.** RAM++ has no sexual tag, so it can neither confirm nor remove one: the check stays
  "both WD and PixAI found it", whether RAM++ is on or off. A third voter that did name such tags (JoyTag) was tested
  and was worse, see below.
- **A little noise at 0.5.** Over 240 previews, 7 words make 4.3% of its output without describing anything:
  `image`, `catch`, `peak`, `miss`, `take`, `wear`, `label`. They are the registry entry's `noise`: the panel never
  keeps one of them as a tag and never lists it on the Test card, whatever its score (it is compared after the name is
  normalised; the same word from WD or PixAI is a tag like any other). The stored scores keep them, so the filter
  applies when the tags are made, and a change to the list reaches stored results with a `retag`. `stand`, `sit` and
  `lay` are true but vague, and are kept.
- **Old rows.** The v1 service stored RAM++ flat (`"ram": {tag: score}`, no category level) and WD's rating flat
  (`ratings[i]` = `{rating: probability}`, not under a tagger's key), and such rows may still be in the store. They have
  no `pixai` entry, so with `use_pixai` on a `retag` of them runs as `full` (the v2 rule) and the row is replaced by one
  in today's shape. With `use_pixai` off the stored scores are used, and they are read as `{"general": {...}}` and
  `{"wd": {...}}` (before this was fixed they were read as "no RAM++ tags" and "no rating", which also switched the
  explicit-tag check off; a RAM++ tag called `general` even crashed it). Nothing is rewritten in place. A v1 settings
  file loads again with its `use_ram` / `ram_strictness`; a stored `vram_gb` outside 4-8 (v1: 18-21, v2: 3) falls back to
  the default 5.
- It is by far the fastest of the three, so it never becomes the bottleneck.

Changes to the service that came with it:
- `MODEL_NAMES = ("wd", "pixai", "ram")`, in that order (also the order the models run in a request).
- **A bug fixed in the out-of-memory splitting.** The retry used to run inside the `except` block, where the exception's
  traceback still held the failed batch's tensors in GPU memory, so after one OOM even a single picture failed. It now
  retries after the block. With two models this showed only at the edge of the cap; with three, before the warm-up
  change below, it was the first thing that happened at a cap of 5 (every PixAI picture failed).
- **Warm-up after all weights are loaded.** PixAI and RAM++ share PyTorch's memory cap, so each finds its largest
  micro-batch only once both are in memory (before, PixAI warmed up first and its first real batch ran out of memory).
- A cap that is too small now fails the load with `... AITAGGER_VRAM_GB=3 is too small for the three models: raise it`
  in `/health` `error`.
- `deploy/aitagger`: the `Dockerfile` adds `scipy` and the pinned RAM++ source files; `docker-compose.yml` has only the
  `tagger` service (the `vlm` service and its environment, volumes and comments are gone); `.env.example` is updated;
  `deploy/aitagger/bench.py` is the benchmark used below. The old `hf` and `vllm` model caches on disk are not deleted.
  The image must be rebuilt (`docker compose ... build tagger`); the first start has nothing to download if
  `/cache/hub` already holds RAM++ from v1.
- `tests/test_tagger_service.py`: 20 tests that need no GPU (calibration, the answer keys, the request checks, the
  per-picture errors, the OOM splitting including the retry-outside-the-exception regression).

### Third tagger: research and test

The owner's wish: a tagger that is good on real photos *and* illustration, or bigger, newer, more accurate. The library
(~73k pictures, ~18k videos) is personal photos, screenshots and memes next to a lot of anime, MMD/3D and adult
content; WD and PixAI are Danbooru-vocabulary anime taggers (on photos WD adds art-medium tags such as
`traditional media` and `colored pencil (medium)`, and sexual tags on everyday pictures; PixAI has no photographic
words). Every claim below was checked on the model cards, the repositories or the Hugging Face API on 2026-10-02.

| Candidate | Vocabulary | Licence | Verdict |
|---|---|---|---|
| **RAM++**: https://huggingface.co/xinyu1205/recognize-anything-plus-model, https://github.com/xinyu1205/recognize-anything | 4,585 plain-English tags, trained on large-scale web image-text data (the paper: arXiv 2310.15200; +10.2 mAP over CLIP on OpenImages, +15.4 on ImageNet); per-tag thresholds shipped | Apache-2.0 | **Chosen.** Tested: 3.6 useful new tags per picture on the 36 non-illustration pictures, 16% of what it adds false; 232 pictures/s alone (model only); 1.0 GB loaded. Nothing for illustration; no rating |
| JoyTag: https://huggingface.co/fancyfeast/joytag, https://github.com/fpgaminer/joytag | 5,813 Danbooru tags (no categories, no rating); ViT-B/16 448 px, 91.5M parameters; F1 0.578 at 0.4; trained on Danbooru 2021 (0.3% photographs) plus a small hand-tagged set | Apache-2.0 | Tested, not chosen. Marks 56 of the 70 photo-like pictures as photographs (`photo (medium)`, `real life`, `selfie`), names characters about as well as WD, 133 pictures/s. But of the tags it adds on non-illustration pictures only 36% are useful, 29% vague and 35% false, and it hallucinated `penis`, `fellatio`, `oral` on everyday pictures (screenshots, an old black-and-white photo): as a third voter it would have confirmed WD's false sexual tags |
| WD EVA02 2026 canary: https://huggingface.co/ashen-sensored/wd-eva02-tagger-2026-canary | 16,473 Danbooru tags (WD v3's 10,861 + 5,999 new, data to 2026-05-18); EVA02-L 448 px, 0.3B; P=R threshold 0.6094, F1 0.5416 | Apache-2.0 | Tested, not chosen. Same vocabulary and the same photo habits as WD (`3d`, `realistic`; `implied nudity` on a meme); a better WD (no art-medium false tags on the 70 photo-like pictures, WD had 5), but only 0.5 new useful tags per picture. Worth swapping for WD later, not worth a third slot |
| `animetimm/caformer_b36.pexelsv0-full`: https://huggingface.co/animetimm/caformer_b36.pexelsv0-full | 18,440 tags in 15 categories, trained on a Pexels photo set; CAFormer-B36 384 px | **GPL-3.0 and gated** (manual approval, contact details required; files answer 401 without a Hugging Face login) | Not testable here; a gated GPL download is not something to build a service on. 0 downloads, 1 like |
| Camie Tagger v2: https://huggingface.co/Camais03/camie-tagger-v2 | 70,527 Danbooru-2024 tags, micro-F1 67.3%; ViT 143M | GPL-3.0 | Anime only (trained on Danbooru 2024): another WD, not a photo tagger |
| DINOv3 booru tagger `lodestones/taggerine`: https://huggingface.co/lodestones/taggerine | 74,625 e621 + Danbooru tags; ViT-H/16+ (~1.1B parameters, 5.3 GB file) | Apache-2.0 on the card (the DINOv3 backbone has its own licence) | No recommended thresholds, a "proto" checkpoint, evaluated only on booru and furry art according to its card, and 5.3 GB of weights is far over "a few GB" |
| `cella110n/cl_tagger_v2`: https://huggingface.co/cella110n/cl_tagger_v2 | Danbooru-style | "other", gated (auto-approval after login) | Needs a login and a custom licence; anime |
| `sorryhyun/anima-tagger`: https://huggingface.co/sorryhyun/anima-tagger | 2,532 anime tags | MIT, but it loads the gated GPL `animetimm` backbone | Anime only, gated backbone |

Nothing newer than RAM++ (2023) that tags photographs *and* illustration with per-tag scores, an open licence, an
ungated download and a loader that runs on torch 2.14 turned up in searches of the Hugging Face model API (tagger,
photo tagger, booru, recognize anything, ram, pexels, SigLIP / DINOv3 / OpenImages taggers, WD and JoyTag
derivatives) and of the web. SmilingWolf's last tagger is from July 2024, JoyTag's model from March 2024; the RAM++
repository's last push is 2025-02-18. The PixAI v1.0 already in the service (2026-09) is the newest anime tagger.

**How it was tested.** 240 random Immich preview JPEGs (read-only copies in a temp folder, deleted afterwards) went
through WD and PixAI (the service's own code), JoyTag (the repository's ONNX, fp32; its logits match the repository's
PyTorch model: 68 decisions of 308,089 differ), RAM++ (the code above) and the canary (timm, fp16, BGR input as WD),
all calibrated to their recommended thresholds. 53 of them were picked by looking: 21 real photographs (people, food,
places, objects), 9 screenshots/documents, 6 memes, 17 anime/illustration/3D. For the 36 that are not
illustration, every tag a candidate gave at >= 0.5 that neither WD nor PixAI gave was judged by eye as *useful* (right
and informative), *vague* (right but trivial or a repeat, such as `person` next to `1boy`) or *false*:

| added to what WD + PixAI already say, on the 36 | tags | useful | vague | false | useful per picture |
|---|---|---|---|---|---|
| **RAM++** | 282 | **131 (46%)** | 107 (38%) | 44 (16%) | **3.6** |
| JoyTag | 173 | 63 (36%) | 50 (29%) | 60 (35%) | 1.8 |
| WD canary | 64 | 17 (27%) | 21 (33%) | 26 (41%) | 0.5 |

(Hand-judged, one reader, so read it as a ranking, not as exact percentages.) On the 17 illustrations RAM++ says
`girl`, `anime`, `illustration` and little else; JoyTag and the canary tag them like WD and PixAI do (JoyTag names
characters and series: Uraraka Ochako, Hatsune Miku, Mankanshoku Mako, Taimanin), so neither adds much there.

Examples (calibrated >= 0.5, abbreviated):

| picture | WD / PixAI already | RAM++ adds | JoyTag |
|---|---|---|---|
| a pot of tomato stew being stirred on a stove (video frame) | kitchen, cooking pot, stove, tile wall, curry, spoon; WD also `chopsticks`, `soy sauce`, `1girl` | **stew, tomato sauce, tomato, stir, cook, kitchenware, kitchen counter, cooker, mixture** (one wrong: `blender`) | `1boy, 1girl, screencap, solo, male focus` |
| a plate of paella with prawns and a scallop | WD `shrimp tempura`, `fried chicken`; PixAI `lobster`, `fried rice`, `fate (series)` | **paella, seafood, scallop, silverware, dinning table** (the tag's own spelling), **platter** | `food, fork, plate`, and `ice cube` (wrong) |
| a Licor 43 poster (bottle, cocktail glass) | WD `traditional media`, `colored pencil (medium)`, `acrylic paint (medium)`, `painting (medium)` (all wrong: it is a photograph); both `alcohol`, `whiskey` | **liquor, wine glass, poster, martini** | `beer` (wrong), `photo (medium)` |
| a Reddit page and a tweet, as screenshots | `fake screenshot`, `fake phone screenshot`, `twitter`, `english text` | **screenshot, text message, website, app** | `phone screen`, `text-only page`; but also `touhou`, `cirno`, `1girl`, `grey hair` on the Reddit page |

**Would it have caught WD's false sexual tags?** Of the 240 pictures, 183 are rated general or sensitive by WD and
PixAI together (the pictures the explicit-tag check is for). On them WD raised 6 sexual tags, 3 of them wrong
(`penis` on a TikTok screenshot of a man with glasses; `condom`, `sex toy` on a TikTok feed screen; the owner's
earlier example, `oral, fellatio, loli` on an ordinary photo, is the same problem), PixAI 7, 3 of them wrong (`nude,
sex, hetero` on a meme of two silhouettes in the sea), the canary 1 (right). **JoyTag raised 10 on 5 pictures, 9 of
them wrong**: `penis, fellatio, oral, erection, hetero` on that same TikTok screenshot (so the rule would now have
*kept* WD's false `penis`), `censored, penis` on an Instagram profile screen, `penis` on a black-and-white photo of
three men. A third voter is only worth having if its yes is trustworthy; JoyTag's is not on photographs. RAM++
never votes yes, so it neither rescues nor spoils the check.

**Speed and memory of the candidates**, each alone, batch of 8 (model only, preprocessed pictures, GPU: used-memory
growth by `nvidia-smi`): RAM++ 232 pictures/s, 1.0 GB loaded, 1.9 GB at peak; JoyTag 133 pictures/s, 0.9 GB loaded, 2.4 GB
at peak (onnxruntime's default arena; the fp16 conversion of its ONNX file fails); canary 127 pictures/s, 1.0 GB
loaded, 1.4 GB at peak. For reference WD does 80-83 and PixAI 19-20 pictures/s on the same pictures.

**Why RAM++ again.** v2 dropped it only because the owner preferred PixAI as the second tagger, and for that job PixAI
is better (characters, series, the Danbooru words the owner searches by). As a *third* tagger the question is
different: what do WD and PixAI both miss? On the library that is real-world things (food, rooms, drinks, vehicles,
"screenshot", "poster"), and RAM++ is the only open candidate built for them. At 0.7 GB and 7% of the time it is cheap
enough to keep next to the other two.

### Measured (v3)

Measured on 2026-10-02 on the owner's RTX 4090 (24 GB, WSL2, Docker 29), with Immich ML (2.7 GB) and Search+ (loaded,
idle) in memory. torch 2.14.1+cu130, transformers 4.57.6, onnxruntime-gpu 1.30.0. 720 pictures (240 real library
previews shrunk to 1024 px, three times over) sent as base64 JPEG through the HTTP API with `deploy/aitagger/bench.py`
(decoding and transfer included), 8 pictures per request and 2 requests in flight (16 per request for micro-batch 16).
"GB" is nvidia-smi's MiB / 1024; "process VRAM" is the growth of the GPU's used memory from before the container
started to the peak while it worked.

**Pictures per second** (everything loaded, model-native thresholds)

| | micro-batch 8 | micro-batch 16 |
|---|---|---|
| RAM++ alone | 131 | not measured |
| PixAI alone | 20.0 | 20.0 (v2) |
| WD alone | 83.9 | (WD is held to 8) |
| WD + PixAI | 16.3 | 16.3 (v2) |
| **WD + PixAI + RAM++ (all three)** | **15.2** | **15.1** |

RAM++ costs about 4.5 ms a picture on top of WD + PixAI (61 -> 66 ms): 7%. PixAI is still the whole story (20
pictures/s, compute-bound), so the micro-batch buys nothing but memory.

**Memory by cap** (`AITAGGER_VRAM_GB`, micro-batch 8; "settles on" is `effectiveBatch` in `/health`: the largest micro-batch
each model fits in after all weights are loaded, PixAI / RAM++ / WD):

| cap | settles on | process VRAM | WD + PixAI + RAM++ |
|---|---|---|---|
| 3 | does not load (`... too small for the three models`) | - | - |
| 3.5 | 1 / 2 / 4 | 3.47 GB | 14.6 pictures/s |
| 4 | 2 / 4 / 8 | 3.79 GB | 15.1 |
| **5** | 4 / 8 / 8 | **4.20 GB** | 15.1 |
| 6 | 8 / 8 / 8 | 4.76 GB | 15.1 |
| 8 | 8 / 8 / 8 | 4.76 GB | 15.2 |
| 8, micro-batch 16 | 16 / 16 / 8 | 6.17 GB | 15.1 |

Loaded and idle the process holds 2.8 GB. Immich ML and Search+ (loaded, idle) held 7.7 GB together; with the tagger
busy at a cap of 6 or 8 the whole GPU used 12.6 GB, so Search+ and the tagger fit together with room to spare.
Start-up with the files cached: 8 s until `status: ok` (WD 1.3 s, PixAI 2.3 s, RAM++ 2.1 s to load, plus the
warm-ups). Down to a cap of 4 no speed is lost (a small cap only shrinks the micro-batches: PixAI is
compute-bound); at 3.5 it is 3% lower, because WD then runs at a micro-batch of 4. **Recommended: `vram_gb` default 5
(4.2 GB used, PixAI settles on 4) and `AITAGGER_BATCH` 8; limits 4-8** (6 and above use 4.8 GB; 3.5 loads but has no
room to spare; 3 does not load).

### 4th tagger: Hydra 3.5, the e621 tagger (key `e621`)

The owner's library has a lot of furry / anthro art. WD, PixAI (Danbooru vocabulary) and RAM++ (plain English) only say
`furry`, `furry male`, `furry female`, `furry with non-furry`, `animal ears`, `wolf`; the furry boards' own vocabulary
(e621) has `anthro`, `feral`, `human on anthro`, `anthro on anthro`, `duo`, species (`canid`, `wolf`, `lagomorph`...),
colours and body parts. So a fourth tagger, trained on e621. Every claim below was checked on the model cards, the
repositories or the Hugging Face API on 2026-10-02.

#### As built

| | |
|---|---|
| Key / label | `e621` / "Hydra 3.5 (e621)"; `/health` `models[].name`: `hydra-3.5` |
| Model | Hydra 3.5 by Project RedRocket (model file dated 2026-06-23): a SigLIP 2 So400m patch-16 NaFlex ViT (aspect ratio kept, at most 1,024 patches of 16 x 16 pixels, about 512 x 512) followed by a per-tag cross-attention head with one learned query per tag, about 0.5B parameters; trained on e621 images only |
| Hugging Face | `RedRocket/Hydra`, revision `cfa9b0a1ffcf2b8df8553be7673210fd60fba23b` (2026-08-23, the newest of the repository), file `models/hydra-3.5.safetensors` (1,064,526,448 bytes, bf16, sha256 `5e9337c21019c51f2bb5b33d053ea3e2c0412e5ca972b5ea3c4c21e6987452ca`), cached under `/cache/hub` like the others (`E621_REVISION` overrides). https://huggingface.co/RedRocket/Hydra |
| Source files | the repository's package imports pyvips and a Qt GUI, so it is not installed. The image build fetches five modules of the network (`siglip2.py`, `pool.py`, `head.py`, `glu.py`, `cufork.py`) from that one commit into `/opt/hydra`, each checked against its SHA-256 (the same arrangement as RAM++); `tagger_service.py` builds the model from them and does its own picture preparation. New dependency: `einops==0.8.2` |
| Licence | Apache-2.0: stated in the model card's front matter and in the model file's metadata (`modelspec.license`); the repository has no LICENSE file. Base model `google/siglip2-so400m-patch16-naflex`: Apache-2.0. Free for personal self-hosted use |
| Vocabulary | 8,886 e621 tags: general 6,262, species 1,426, character 1,036, meta 120 (art media, layout, `english_text`, the ratings `safe` / `questionable` / `explicit`), copyright 36, lore 6. Includes the combination tags (`anthro_on_anthro`, `human_on_anthro`, `male_feral`, `duo`, `female_anthro`...) and colours. The answer has `general`, `species`, `character`, `copyright`; meta, lore and ratings are not returned (artists are not in the model) |
| Names | model-native, underscores kept (`male_anthro`, `pokemon_(species)`); the panel normalises them to spaces |
| Calibration | per-tag threshold = the best F1 on the validation counts stored in the model file (99 thresholds x tp / fp / tn / fn per tag) among thresholds with at least 10% precision, rounded to bf16 in logit space: the repository's default calibration (`f1.0@0.1`). The thresholds are 0.36-0.92 (median 0.62); 77 of the 8,886 tags never reach 10% precision and are never returned. `s' = sigmoid(logit(s) - logit(t))`, so **0.5 = its threshold**, as for the others |
| Implications | the repository's default `inherit` mode: a tag scores at least as high as every tag that implies it, directly or through others (14,558 implied pairs in the model file), so a `wolf` brings `canis`, `canine`, `canid`, `mammal`; a tag is kept whenever one that implies it is |
| `floor` | `max(floor, 0.2)`. Its probabilities are squashed (the median tag of a picture sits at 0.2, thresholds are around 0.6), so after calibration 5,000-6,500 of the 8,886 tags of a picture are above 0.05, about 520 on average above 0.2, 15-50 above 0.5. At the panel's floor of 0.05 one answer would hold about 6,300 tags (130 KB of JSON); at 0.2 it holds about 520 (11 KB). 0.2 is also the panel's `DISPLAY_FLOOR`; an `e621_strictness` below 0.2 therefore acts as 0.2 |
| Input | RGB, transparency on white, resized to `get_image_size_for_seq` (ported; same result as the repository's on 3,018 sizes) with the **Magic Kernel Sharp 2013 in linear light** (the model file says `classifier.resize = mks2013-linear`) on the GPU as two matrix products (`_mks_matrix`), patches padded to the longest picture of the micro-batch and masked |
| Precision | bf16 as trained (`E621_PRECISION`; fp16 and fp32 work: fp16 changes 0.57% of the tag decisions) |
| Memory | 1.0 GB of weights; the head asks one query per tag of every picture (180 MB a picture in the widest layer), so it runs on 1,024 tags at a time (`E621_TAG_CHUNK`, same numbers up to one bf16 step): 0.54 GB extra at a micro-batch of 8 instead of 3.7 GB |
| Speed | 21-22 ms a picture alone (46 pictures/s at a micro-batch of 8; the tag head is 11 of the 22 ms, resize and backbone the other 11) |
| Typical output | 29 tags >= 0.5 per picture over the 239 test pictures: 58 on the 49 furry ones, 21 on everyday photos (see below) |

`POST /tag` answers one more key per picture, with no `rating`:
```json
{"results": [{"wd": {...}, "pixai": {...}, "ram": {...},
              "e621": {"general": {"anthro": 0.99, "female_anthro": 0.97, "fur": 0.95},
                       "species": {"canid": 0.98, "wolf": 0.91, "mammal": 0.98},
                       "character": {}, "copyright": {"christmas": 0.62}}}, null],
 "errors": [null, "cannot identify image file"], "tookMs": 760}
```
`"models"` accepts `"wd"`, `"pixai"`, `"ram"`, `"e621"` in any mix; `/health` has `effectiveBatch: {"wd": 8, "pixai": 8,
"ram": 8, "e621": 8}` and a fourth entry in `models` (`{"name": "hydra-3.5", "kind": "e621", "tags": 8886, "precision":
"bf16", "loadedIn": 1.9}`). Environment: `E621_MODEL`, `E621_REVISION`, `E621_PRECISION`, `E621_TAG_CHUNK`, `HYDRA_CODE`
(default `/opt/hydra`).

#### Research: the e621 taggers

| Candidate | Vocabulary | Licence (as stated) | Verdict |
|---|---|---|---|
| **Hydra 3.5** (RedRocket): https://huggingface.co/RedRocket/Hydra | 8,886 e621 tags with categories (species, character, copyright, general), combination tags, per-tag thresholds from validation counts; SigLIP 2 NaFlex 0.5B; 1.06 GB. The successor of JTP-3 Hydra (2025-11-10, 7,504 tags, in the same repository: `RedRocket/JTP-3` was renamed to `RedRocket/Hydra`). Tools built around it: https://github.com/Fenrir784/e621tagger (Hydra 3.5), https://github.com/renfald/tail-tagger (recommends JTP-3 Hydra) | Apache-2.0 | **Chosen** |
| JTP-1.1 PILOT2 (RedRocket): https://huggingface.co/RedRocket/JointTaggerProject (`JTP_PILOT2/`) | 9,083 e621 tags, no categories, no thresholds shipped (the card: 0.2 recommended); `vit_so400m_patch14_siglip_384` with a gated head, 2024-07 | Apache-2.0 | Tested: 51 tags a picture on furry art, fast (175 pictures/s, 1.0 GB), says `anthro` on pictures where Hydra says `humanoid` / `feral` / `human`; 32 of the 190 non-furry pictures got a sexual-vocabulary tag (Hydra 21). Older, no categories, no per-tag calibration |
| `eva02_large_E621_FULL_V1` (nzs234, mirror xdcx): https://huggingface.co/nzs234/eva02_large_E621_FULL_V1 | 8,783 tags (general 7,180, species 1,106, character 494, rating 3), EVA02-L 448 px, WD-style ONNX (1.25 GB fp32), per-category thresholds (0.60 / 0.65 / 0.60), 2026-02 | MIT on the card ("follow the upstream dataset and base-model licences"; the dataset `animetimm/e621-wdtagger-v1-w640-ws-full` is `other`, gated) | Tested: 35 tags a picture on furry art (Hydra 58), 39 pictures/s with onnxruntime's default arena (it grew to 4 GB), 21 sexual-vocabulary pictures like Hydra, no colour or combination tags, no `human_on_anthro`. A fair second choice |
| `Thouph/eva02-vit-large-448-8046`: https://huggingface.co/Thouph/eva02-vit-large-448-8046 | 8,041 tags + rating, EVA02-L 448 px, ONNX fp16 (0.64 GB), no recommended threshold (the author's example uses 0.3), 2023-12 | CC-BY-NC-4.0 (non-commercial: fine for personal use) | Tested: 34 tags a picture, `explicit` / `penis` on a photographed manga page, the rating pseudo-tags (`safe`...) mixed into the tag list; old |
| `toynya/Z3D-E621-Convnext` (and copies): https://huggingface.co/toynya/Z3D-E621-Convnext | ConvNeXt, Keras / ONNX, 2023-09 | `other`; the card says only that the source was the Z3D Discord channel | Not tested: no licence terms |
| `lodestones/taggerine`: https://huggingface.co/lodestones/taggerine | 74,625 e621 + Danbooru tags, DINOv3 ViT-H/16+ (~1.1B parameters), 5.3 GB file, no thresholds, "proto", 2026-04 | Apache-2.0 on the card; its DINOv3 backbone has its own gated licence | Not tested: 5.3 GB of weights does not fit next to the others |
| `animetimm/swinv2_base_window8_256.e621v1-full`: https://huggingface.co/animetimm/swinv2_base_window8_256.e621v1-full | e621, SwinV2-B 256 px, 2025-06 | GPL-3.0 | Not tested: older and smaller than the EVA02 version, GPL |

Searches: the Hugging Face model API for e621, furry, anthro, jtp, taggerine, hydra, eva02 e621, e621 classifier /
siglip / vit and the author RedRocket; GitHub's e621 topic; the web. Nothing newer than Hydra 3.5 turned up
(RedRocket's own successor chain: PILOT 2024-06, PILOT2 2024-07, JTP-3 Hydra 2025-11, Hydra 3.5 2026-06).

#### How it was tested

239 library previews (read-only copies in a temporary folder, deleted afterwards), through the service's own code for WD,
PixAI, RAM++ and Hydra and through standalone loaders for the other three (PILOT2: timm, fp16, the card's preprocessing;
the two EVA02: onnxruntime, the card's preprocessing), each at its operating point (Hydra 0.5 calibrated, PILOT2 0.2,
EVA02 e621 0.6 / 0.65, Thouph 0.3, WD and PixAI 0.5 calibrated):
- **49 furry pictures** from Immich's own smart search ("anthro furry character", "furry art", "anthropomorphic wolf",
  "two furry characters", "human and furry"; the most frequent hits plus the top 12 of the last query). They are not all
  anthro: there are fursuit photographs, kemonomimi / humanoid art, a feral wolf sketch and a face-swapped photograph.
- **20 everyday photographs** (family, food, street, document, landscape, selfie, phone screenshot, dog), **10 non-furry
  anime / manga / cartoon pictures**, and a stress set of **160 more everyday photographs** (beach, shirtless, swimsuit,
  baby, party, wedding, gym and mirror selfies, couples, bedroom, bathroom, pool, statues, paintings; 8 per search) for
  false sexual tags. This set also holds real adult pictures, where such tags are right; the doubtful cases were looked at.

| per picture, mean | WD | PixAI | RAM++ | **Hydra** | PILOT2 | EVA02 e621 | Thouph |
|---|---|---|---|---|---|---|---|
| tags on the 49 furry pictures | 48.8 | 41.2 | 5.7 | **58.4** | 51.1 | 35.4 | 33.7 |
| tags on everyday photos (180) | 28.6 | 30.0 | 10.8 | 21.3 | 28.2 | 16.4 | 18.0 |
| furry pictures with `anthro` (of 49) | 0 | 0 | 0 | 38 | 43 | 41 | 41 |
| furry pictures with `furry` | 39 | 39 | 0 | 0 | 0 | 0 | 0 |
| non-furry pictures (190) with a sexual-vocabulary tag (`EXPLICIT_TAGS`) / tags | 32 / 64 | 38 / 69 | 0 / 0 | **21 / 28** | 32 / 60 | 21 / 31 | 20 / 30 |

What the four say on the same pictures (>= their operating point, abbreviated):

| picture | WD / PixAI | RAM++ | Hydra (`e621`) |
|---|---|---|---|
| a fox in a red sweater, painted portrait | `furry`, `furry female`, `fox girl`, `fox ears`, `animal ears`, `snout`, `body fur`, `1girl`, `animal nose` | `fox`, `sweater`, `animal` | `anthro`, `female_anthro`, `canid`, `canine`, `fox`, `true_fox`, `red_fox`, `mammal`, `fur`, `tuft`, `cheek_tuft`, `inner_ear_fluff`, `sweater`, `topwear`, `black_nose`, `smiling_at_viewer` |
| a comic panel: a grey wolf in a white shirt on a chair | `wolf boy`, `furry`, `furry male`, `wolf ears`, `wolf tail`, `1boy`, `comic`, `speech bubble` | `person`, `wolf`, `chair` | `anthro`, `male_anthro`, `canid`, `canine`, `canis`, `wolf`, `grey_fur`, `text`, `dialogue`, `speech_bubble`, `sitting`, `on_chair`, `clothed_anthro`, `fully_clothed` |
| a photograph of fursuiters and a cosplayer | `1girl`, `cosplay`, `bikini armor`, `purple hair`, `crowd`, `realistic` | `costume`, `mascot`, `cosplay` | `fursuit`, `costume`, `cosplay`, `real`, `human`, `group`, `pokemon_(species)`, `mammal` (no `anthro`: they are people) |
| a girl, a werewolf and a bed, comic | `furry`, `furry female`, `multiple girls`, `1boy`, `werewolf`, `interspecies` | `person`, `cartoon`, `screenshot` | `anthro`, `human`, `canid`, `wolf`, `werewolf`, `werecanid`, `mythological_canine`, `trio`, `group`, `caught_in_the_act`, `nude_anthro`; `human_on_anthro` at 0.36 |

Where Hydra and the other e621 taggers disagree about `anthro` (5 of 49 pictures) Hydra is the one that separates the
kinds: a photograph of people in fursuits (`human`), a girl with animal ears next to a photograph of a maned wolf
(`humanoid`, `animal_humanoid`, `feral`), a nude figure with a feral wolf (`feral` 0.88), a face-swapped photograph
(`human`; PILOT2 says `anthro`), a pencil sketch of a cat-eared girl (`humanoid`). `human_on_anthro` is a rare tag: at 0.5
it fired on none of the 49, at 0.3 on exactly the three pictures with a human and an anthro together. The
species and the extra detail are what the owner asked for: species 12 `wolf`, 6 `fox`, 6 `domestic_cat`, 4 `rabbit`,
`lagomorph`, `scalie`, `pantherine`, `domestic_dog`...; characters 5 (`pulchra_fellini`, `loona_(helluva_boss)`,
`von_lycaon`...); copyright `christmas`, `vtuber`, `animal_crossing`.

**What it does wrong.** On the two near-black frames of the set it invents: `penis`, `erection`, `sex` on a dark close-up of
a shoulder (PILOT2, EVA02 e621 and Thouph also say `penis` there; PixAI says `nude`), `nude`, `anthro`, `rodent` on a
dark blurred bottle photo. On everyday photographs it adds `mammal` to every human (87% of the non-furry pictures) and
`human`, `not_furry`, `human_only`, `real`; that is correct and mostly redundant next to `1boy` / `man` (the registry
entry below lists `mammal` as noise). `loli` on a chibi child drawing (it is in `EXPLICIT_TAGS`; one voter on a general
picture is dropped by the panel).

**Would it have caught false sexual tags / is it a trustworthy voter?** Of the 190 non-furry pictures it raised a sexual
tag on 21, WD on 32 and PixAI on 38 (the three have real adult pictures in common). Everyday pictures I checked by eye
where it did *not* say what the others said: a beach selfie (PILOT2 `penis`, `erection`, `nude`; WD `nude`), a bedroom with
people sleeping (WD `hetero`, `nude`), two shirtless boys sitting outdoors (WD, PixAI, PILOT2, EVA02 e621 and Thouph all
`nude`), a shirtless selfie (PixAI `nude`), an empty bed (WD `nude`; PILOT2 `bdsm`). The panel's rule (a sexual tag on a
general / sensitive picture needs two taggers) can only get weaker when a voter is added: of the 204 pictures WD and PixAI
rate general or sensitive, 17 sexual tags are kept today (both found them) and Hydra as a third voter would
keep **1 more** (`nipples` on a muscular shirtless anthro: true); PILOT2 would keep 3 (one false), Thouph 2 (one false: `nude` on
a rabbit pencil sketch), EVA02 e621 1. So **yes, it should vote**: the one tag it would add is right, it said nothing sexual on the everyday
pictures above, and it knows the furry sexual vocabulary that WD and PixAI under-use. (Its weak spot is near-black frames, where all four
e621 taggers hallucinate; on a general / sensitive picture the panel's rule keeps a lone hallucination out unless another tagger says the same.)

**Fidelity.** Checked against the repository's own pipeline (pyvips + its code) on 79 pictures: 3,669 tag decisions at the
thresholds, 46 differ (1.25%, 0.6 a picture), which is the size of the noise from the batch composition (0.63%) or bf16
against fp16 (0.57%). The first version resized with torch's antialiased bicubic and differed on 6.3% (bilinear: 10.4%), which
is why the Magic Kernel Sharp is built here (the decoder is not the cause: official pipeline from the file vs from a
PIL-decoded array differ on 0.22%).

#### Changes to the service that came with it

- `MODEL_NAMES = ("wd", "pixai", "ram", "e621")`, in that order; `Tagger.batch` / `effectiveBatch` have `e621`; torch warm-up
  after all weights are loaded for pixai, ram, e621 (the e621 warm-up is the worst case: a full 1,024-patch picture).
- **Do not use the repository's `CuFork`.** It runs the position-embedding adds of each picture size on a side CUDA stream.
  With `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` (the compose file's setting) requests hung for good after a few
  calls inside torch 2.14.1's allocator (one thread at 90% CPU in the position embedding's `interpolate`; it happened in 4
  of the 5 request sequences tried, with the service and with the repository's own code, and in none of the 3 with the
  default allocator). The service replaces it with a stand-in that does nothing (everything on one stream, nothing
  slower: these are tiny kernels). No hang since, in 5 sequences and all the benchmarks.
- A picture with a one-pixel side failed (`Failed to find C compiler`: torch 2.14 sends a matrix product with an inner size
  of 1 to a compiler the image does not have); such a side is doubled first. 15 x 40, 16 x 4000, 6000 x 4000 JPEG, grey,
  RGBA and palette pictures were tried.
- The answer is capped at scores of 0.2 (see `floor` above).
- The image must be rebuilt (`docker compose -p immich-aitagger -f deploy/aitagger/docker-compose.yml build tagger`: einops and the
  Hydra source files are new); the model file (1.06 GB) is in the cache from the tests, otherwise the first start downloads it.
- `bench.py` knows `e621`. The Dockerfile adds einops and the five source files. Compose and the service default
  `AITAGGER_VRAM_GB` is now **6** (see below).
- Tests: `tests/test_tagger_service.py` has 30 tests (10 new, 6 changed) that need no GPU: the picture sizes against the
  repository's function, bf16 rounding, the metadata parser, the thresholds from hand-computed counts, implications (transitive,
  cycles, unknown tags), the answer rows and the minimum score, the resize kernel (if torch is installed), the `e621` key
  in the request checks, the answer keys, the per-picture errors and the out-of-memory splitting.

**RAM++ has the same problem at the floor of 0.05, which the panel uses.** Its answer holds on average 4,395 of its 4,585
tags per picture (86 KB of JSON; at a floor of 0.2: 572 tags and 10 KB; at 0.5: 9), and
`aitagger.raw_from_results` stores every returned score of every capture. A minimum score for RAM++ like Hydra's
(`RAM_MIN_SCORE = 0.2` in the service, nothing else changes: the Test card lists scores from 0.2 up) would cut the
stored data of that tagger by a factor of eight. Not done here (outside this task); the service side is a one-line change.

#### For the panel: the registry entry

```python
TaggerKind("e621", "Hydra 3.5 (e621)", ("general", "species", "character", "copyright"), ("character", "copyright"),
           has_rating=False, noise=frozenset({"mammal"}))
```
- `character_categories` are `character` and `copyright` (e621 copyright is franchises and events: `christmas`,
  `animal_crossing`, `vtuber`); species stay when `character_tags` is off.
- **It should vote in the explicit-tag check** (no change needed: the check counts the taggers that kept the tag, and its
  names are the same words: `penis`, `nipples`, `nude`, `sex`, `oral`, `anus`, `cum`...). It has no rating (`has_rating=False`):
  e621's `safe` / `questionable` / `explicit` are not the Danbooru ones and it was never trained on photographs.
- `noise`: `mammal` is optional (the owner can drop it from the list: on furry art it is true and redundant with `canid`
  etc.). Anything else the owner dislikes goes into `blocked` / `vocabulary` (`human_only -> ...`).
- `default_on`: True. At the default strictness of 0.5 (its own F1-optimal point per tag) it adds about 29 tags per picture;
  lower the strictness to 0.3 for combination tags such as `human on anthro`.

#### Measured (four taggers)

Same method as "Measured (v3)": 2026-10-02, RTX 4090, torch 2.14.1+cu130, Immich ML and Search+ (loaded, idle) on the GPU
(runs where another process changed the GPU's memory during the run were discarded and repeated), 720 pictures (239
library previews shrunk to 1024 px, three times over, cut at 720) as base64 JPEG through the HTTP API with
`deploy/aitagger/bench.py`, 8 per request, 2 in flight (16 for micro-batch 16). "Process VRAM" is the growth of the GPU's used
memory from before the container started to the peak.

| cap (`AITAGGER_VRAM_GB`), micro-batch 8 | settles on (PixAI / RAM++ / e621 / WD) | process VRAM | WD + PixAI + RAM++ + Hydra |
|---|---|---|---|
| 4, 4.5 | does not load (`... too small for the four models: raise it`) | - | - |
| 5 | 1 / 2 / 4 / 8 | 4.94 GB | 10.6 pictures/s |
| 5.5 | 4 / 8 / 8 / 8 | 5.11 GB | 10.9 |
| **6** | 8 / 8 / 8 / 8 | **5.77 GB** | **11.1** |
| 8 | 8 / 8 / 8 / 8 | 5.75 GB | 11.0 |
| 8, micro-batch 16 | 16 / 16 / 16 / 8 | 7.09 GB | 11.2 |

| (cap 6) | pictures/s |
|---|---|
| Hydra alone (`models: ["e621"]`) | 42.9 |
| WD + PixAI + RAM++ (Hydra loaded, not asked) | 14.8 (v3 container with three models: 15.2) |
| all four | 11.1 |

Hydra costs 22 ms a picture on top of the other three (68 -> 90 ms: +33%), 1.0 GB of weights and 0.5 GB while working at a
micro-batch of 8. Loaded and idle the process holds 3.8 GB (2.8 GB with three). PixAI is still the largest share (50 of the 90
ms). Start-up with the files cached: 10.5 s until `status: ok` (Hydra loads in 1.9 s); the first start downloads 1.06 GB.

**Recommended: `vram_gb` default 6, limits 5-8** (5.8 GB used, every micro-batch at 8; 5 loads and works, with PixAI at a
micro-batch of 1 and 4% slower; 4.5 and below do not load: PixAI + RAM++ + Hydra need 2.7 GB of weights under PyTorch's share of the
cap). With the three-tagger default of 5 the GPU would still not run out, but one of the four would sit at a micro-batch of 1-2.

### v3, panel side: as built (design details and where it differs from the list above)

**The registry.** `aitagger.TAGGERS` is an ordered list of `TaggerKind(key, label, categories, character_categories,
has_rating, default_on)`:
- `key` is lowercase letters and digits. It is the key of the tagger's answer in `/tag`, its name in the request's
  `models` list, and the stem of its settings `use_<key>` and `<key>_strictness`.
- `categories` are the tag categories it answers with, `general` included; `rating` is separate. `character_categories`
  are those of them that the `character_tags` setting switches off (WD: `character`; PixAI: `character`, `copyright`).
- `has_rating` says whether it answers with a rating; `default_on` is the default of `use_<key>`.
- Registered today: `wd`, `pixai`, `ram`, `e621` (see "As deployed" at the top).

`configure_taggers()` generates from the registry, in place: `DEFAULTS` (`use_<key>` = `default_on`, `<key>_strictness` =
0.5), `LIMITS` (`<key>_strictness` 0.05-0.95), `CONTENT`, and `REPROCESS` (`use_*` in `full`, `*_strictness` in `retag`).
It runs once at import; the tests call it to add a fake third tagger (`tests/test_aitagger.py`: `EXTRA`, `with_extra`).
Everything else loops over `TAGGERS` or `enabled_kinds(settings)`: `detect`, the rating (mean over the enabled taggers
with `has_rating`; a tie goes to the earlier entry), `raw_from_results`, `synthetic_raw`, the status `models`
(`{key: label}`, registry order), the preview's per-tagger lists (an empty list for a tagger that is off) and the `models`
sent to `/tag`.

**Adding the third tagger: the lines.**
1. `immich_organizer/aitagger.py`, in `TAGGERS`, at the marked line:
   `TaggerKind("<key>", "<model name>", ("general", "character"), ("character",))`, with `has_rating=False` if it gives no
   rating and `default_on=False` if it should start switched off.
2. `immich_organizer/tagger_service.py` answers `/tag` under `"<key>"` with those categories (and `rating`), and accepts
   `"<key>"` in `models` and in `/health`.

Nothing else changes: the settings and their validation, the status, the Test card, the web tab and the Android screen
follow from the registry. The first time a store is opened after a tagger that is on by default was added, its results
are "outdated" once (`meta.taggers` remembers the registry; see below), and an asset whose stored scores lack an enabled
tagger is redone as `full` by any reprocess.

**The explicit-tag check** counts the taggers that *kept* the tag (it passed that tagger's own strictness), not the ones
that merely listed it. The check applies when the combined rating is general or sensitive, and needs at least two such
taggers; with one tagger on, none can confirm.

**Stored scores.** `raw.scores[i]` is `{key: {category: {tag: score}} | null}` for every registered tagger (`null` when it
did not answer, for example because it was off when the asset was tagged) and `raw.ratings[i]` is
`{key: {rating: probability} | null}` (`null` for a tagger without a rating). The panel asks `/tag` only for the enabled
taggers (`models: [...]`), so a tagger that was off leaves no scores. A stored raw row lacking an enabled tagger (`missing_kinds`; this
generalises the v2 PixAI rule) makes a `retag` run as `full` for that asset.

**Reprocess modes.** `MODES = ("retag", "full")`. `normalize_mode()` turns `describe` into `retag`: in `enqueue` and
`reprocess` (so a request from an old app is accepted), in the `reprocess` route and the settings route's `reprocess`
field, and for a `describe` row already in the queue (rewritten to `retag` when the store opens, and still understood
by `dequeue` and the Indexer if one gets in some other way). `vocabulary` is a `retag` setting now (renames re-apply to the
stored scores); in v2 it was a `describe` one.

**Store.** `results` is `(id, tags_json, block, settings_version, processed_at, written_at)`. A store made by v2 keeps its
`vlm_json`, `description` and `note` columns (nullable, never written or read again). `/api/aitagger/assets` items have no
`description`, and `q` matches the file name and the tags. One-time "outdated" bumps when a store is opened, each
remembered in `meta` so none repeats, and all counted as one bump: `raw_format` (v1 scores, as in v2), `block_format` (a
stored block with a `Description:` line, which a retag rewrites without it) and `taggers` (a tagger that is on by default
is new since the store was last opened; a store without the key counts as having had `wd` and `pixai`).

**Services.** Only the `tagger` compose service is started, recreated or stopped; the only environment is
`AITAGGER_VRAM_GB` (`vram_gb`). `ensure_ready(need_tagger, ...)` and `load()` take no VLM argument, and `status()` has no
`vlm`. The panel has no idle clock any more: it only existed to stop the describer container, and the tagger container
exits by itself after `IDLE_EXIT_MINUTES` (`tagger_service.py`). Write-back parallelism is the constant
`Indexer.WRITERS = 8` (it was `vlm_parallel`).

**GPU sharing with Search+.** `searchplus.AITAGGER_CONTAINER` (environment `AITAGGER_CONTAINER`, default
`immich_aitagger`) replaces `AITAGGER_VLM_CONTAINER`; `GpuBusy` is raised for it, and only while `AITAGGER_EXCLUSIVE` is
true. `AITAGGER_EXCLUSIVE` stays `False`.

**Indexer: pipelined across rounds** (this replaces the "Indexer" description under "Implementation notes", which is the v1
one). One round is `batch_size` assets: `Store.work(batch_size, skip=<claimed>)`, then the pictures are read / cut by
`WORKERS` (6) threads and sent to the tagger in batches of at most `batch_size` assets and 64 pictures, the first as soon as
it is full and the last, partial one as soon as the round's pictures are ready. A round **returns when its last batch is
sent, not when it is answered**: the tagger requests in flight are state of the indexer (`_tag_futs`), not of the round, so
the next round's pictures are read and cut (previews, ffmpeg) while the GPU is still busy with the earlier ones. Before
this, each round ended by waiting for all its requests, the next round's preparation started only then, and the GPU was
idle at every round boundary (measured: ~95 % busy with regular 0 % gaps, 244 assets/min at `batch_size` 32 with four
taggers).
- **The limit.** `TAG_REQUESTS` (2) requests are in flight at most; `_send` waits (`_reap_tags`) only while that many are
  out. The pictures held in memory are therefore the ones of the requests in flight plus the round being prepared: at most
  `TAG_REQUESTS + 1` batches. `store.work` does not fetch further ahead: the round in hand is the look-ahead, and the GPU
  stays fed as long as one round's preparation (plus the wait for write-back, `_reap(WRITERS)`) takes less than
  `TAG_REQUESTS` batches on the GPU.
- **Claims.** An asset is claimed (`_inflight`, skipped by `Store.work`) from the start of its round until it is written,
  failed or given back, so it is never tagged twice at once, however many rounds overlap. A request that fails (or a pause)
  gives back the assets that did not get as far as the write-back stage.
- **Failures.** A request's exception (`ServiceDown`, anything else) is raised by the next `_send`, by the start of the next
  `_step`, or by the wait at the end of the work (`_step` ends the run only when no request is in flight and nothing is waiting
  to be written), so it reaches `_run`'s handling as before: `ServiceDown` is waited out (3 in a row with nothing finished
  end the run), other exceptions end it with an error. `_drain` waits for the requests in flight, then the write-back stage,
  and looks at no outcome. While a request is on the GPU, a round does not ask the server for its health again.
- **Pausing** stops sending (a batch waiting for a free slot goes back to the pool), then `_run` waits for the requests on the
  GPU and their write-back (`_drain`): nothing stays claimed and nothing is lost; the rest is worked on at the next start.
- **Out of memory** halves the batch as before; the cap is remembered together with the `batch_size` the batch was made under
  (`_tag_halving(batch, models, want)`), since rounds with different settings can now overlap.

**API and screens.** The status has no `vlm` under `service`, `models` is `{key: label}`, `limits` and `settings` have the
generated keys, and `reprocessKeys` is `{retag: [...], full: [...]}`. The preview has `models.<key>` per registered tagger,
`models.rating`, `models.dropped` when the explicit-tag check removed tags, and no `vlm` or `description`. The web tab and
the Android screen build their tagger switches and strictness sliders from `models` + `settings` + `limits` (the web tab
also takes the reprocess mode of each setting from `reprocessKeys`).

### Renames and combinations (the `vocabulary` text)

The owner: "the how-to-tag section only lets you rename one tag to another; there are no instructions on how to do a
combination of tags." The Rules card could (the web tab's "Rules", the Android screen's "Rules"), but sat apart and
nobody found it. Now the same combinations can be typed in the Vocabulary box, one entry per line, and the two places are
one feature: **form or text** (the card is titled "Combinations (form)", the box "Renames and combinations", and each says
so). Nothing new is stored: it is all the existing `vocabulary` string (so it is already a `retag` setting, no new
setting, nothing for `full`), whose limit is now 20,000 characters (was 4,000).

| Line | Meaning |
|---|---|
| `old -> new` | rename: a single tag on both sides, no sign (`→` works as the arrow) |
| `a + b -> c` | when the asset has a **and** b, add c |
| `a \| b -> c` | when it has a **or** b, add c |
| `a + !b -> c` | a and **not** b. `!` works on any tag of a `+` line (`!b + a`, `a + !b + !c`) |
| `a + b -> c, -d` | several results, comma separated: `-tag` removes, `+tag` or a bare tag adds |
| `a -> +b` | add b and **keep** a: one condition with an explicit sign is a rule, not a rename |
| `# note`, blank line | ignored (but counted in line numbers). A `#` only starts a note at the start of a line |

Examples: `1girl -> woman`, `furry with non-furry -> human on anthro` (WD's own tag, renamed), `furry + human -> human on
anthro`, `anthro | furry -> furry art`, `1girl + 1boy -> couple, -solo`, `furry + !human -> furry only`.

**Rules of the syntax.**
- Tags are normalised as everywhere (`norm_tag`: lowercase, `_` is a space, one line, 60 characters), on both sides.
  Write the tags as they are *after the renames* (renames happen first, see below).
- `+` and `|` are operators only with a space on each side: `a + b`, not `a+b` (which is refused, with a message, rather
  than read as a tag called `a+b`). Tags that contain the signs (`c++`, `+_+`, `^_^`) stay tags. A target that itself
  starts with `+` or `-` needs a sign in front (`++_+` adds the tag `+_+`).
- Mixing `+` and `|` on one left side is an error. `!` with `|` is an error too (`a | !b` cannot be said in the rule
  shape; use `a + !b`). A left side needs at least one tag that must be present (not only `!tags`) and cannot require and
  exclude the same tag.
- A line is a rename only when the left side is one bare tag and the right side is one bare tag. Anything else is a
  rule: `a -> b, c` adds b and c and keeps a; `a -> -b` removes b when a is there; `a -> +b` adds b. To replace a by two
  tags write `a -> b, c, -a`. `a -> a` changes nothing and is ignored. Two renames of one tag: the later line wins; a
  rename is one step (`a -> b` and `b -> c` do not chain).
- No `!` on the right, no inline comments, at most 50 tags on each side (`MAX_RULE_TAGS`), one `->` per line.

**How it runs.** A rename is applied while the tags are detected (before blocked, the explicit-tag check, the rating
and every rule). Each other line becomes a rule in exactly the Rules card's shape,
`{if_all, if_any, unless, add, remove}` (`+` fills `if_all`, `|` fills `if_any`, `!` fills `unless`), plus a `label`
(`"line 3"`). `finalize` runs `settings["rules"]` (the card) followed by the typed rules, in the order written, through
the one repeat-until-stable engine (`apply_rules`, at most `MAX_RULE_PASSES = 5` passes: a chain written backwards needs
a pass per link). Added tags score 1.0 with source `rule`; `blocked` and the `max_tags` cap apply afterwards, as for the
card.

**Errors.** `Vocabulary(text)` reads forgivingly: a line it cannot use is skipped and listed in `errors`
(`(line number, message)`), which keeps v2 vocabularies (preferred terms on lines without an arrow) working: loading
the saved file never drops them. **Saving is strict**: `validate_setting("vocabulary", ...)` refuses with HTTP 400 and a
message that names every bad line (the first five, then "N more lines have problems"), for example
`Line 3: use + or |, not both; Line 5: there is nothing after ->`. The messages (after `Line N: `):

| Message | When |
|---|---|
| `write it as "tags -> result", for example a + b -> c` | no arrow (a plain term, a typo like `>`) |
| `only one -> per line` | two arrows |
| `there is nothing before ->` / `there is nothing after ->` | an empty side (`a -> ,` counts as empty) |
| `use + or \|, not both` | `+` and `\|` on one left side |
| `! can't be used with \| (a + !b means 'a and not b')` | `!` on an `\|` line |
| `it needs at least one tag that must be present, not only !tags` | `!a -> b` |
| `a tag can't be both required and excluded` | `a + !a -> b` |
| `put a tag on each side of + or \|, with spaces around it (a + b)` | `a+b`, `a +b`, `a + ->` |
| `a tag is missing before or after + / \| / !` | `a + _ -> b`, `! -> b` |
| `"-" needs a tag after it` / `"+" needs a tag after it` | `a + b -> -` |
| `a tag is missing after ->` | a target that normalises to nothing |
| `! only works on the left of -> (a + !b -> c)` | `a + b -> !c` |
| `a tag can't be both added and removed` | `a + b -> c, -c` |
| `at most 50 tags before ->` / `at most 50 tags after ->` | too long a side |

**Preview.** The Test card's `rules` trace names a Rules-card rule by its number (the existing 0-based `rule` integer; the
apps show "Rule N") and a typed rule by its line, `{"rule": "line 3", "added": [...], "removed": [...]}`. The apps show
"Line 3 (the saved text of that line): added ..., removed ...". Card rules come first in the trace, then typed lines in the
order written; a rule that changed nothing is not listed.

**Screens.** Web: the "Vocabulary" textarea is "Renames and combinations", with a collapsible "Syntax and examples" under
it (open by default); the Rules card is "Combinations (form)", directly after the "How to tag" card, each pointing at the
other. Android: the same label and cheat-sheet in the "Tags" card, and the "Combinations (form)" card right after it
(before "Taggers"). The web service worker `CACHE` was bumped for it.

## v2 (2026-10-02) — superseded by v3 where they differ

The owner found the 9B vision model too heavy for what it is needed for: stringing the tags into a short
description. v2 changes:

1. **Tagger 2 is PixAI Tagger v1.0** (`pixai-labs/pixai-tagger-v1.0`, Apache-2.0, 486M params, input 1008×1008 with
   aspect kept by resize + pad, loaded with transformers `trust_remote_code=True`), replacing RAM++. It has
   30,877 tags; the categories used are `general`, `character`, `copyright` and `rating`. `style` (artists) and `meta`
   are ignored. The card's thresholds — general 0.17, character 0.27, copyright 0.24 — are the calibration points
   (`s' = sigmoid(logit(s) - logit(t))`, so 0.5 = the card's threshold), exactly as for WD. Like WD it is
   Danbooru-style, so the two vocabularies merge naturally (same normalisation, highest score wins).
2. **The describer is text-only and small: `Qwen/Qwen3.5-2B`** in the same vLLM container (`immich_aitagger_vlm`,
   port 11441, served name `tagger-vlm`, `--limit-mm-per-prompt {"image":0}`, thinking off). It receives **no
   images**: only the final tag list with scores, the rating, and the user's instructions, vocabulary and language. It
   answers the same JSON `{description, add_tags, remove_tags}` (`maxItems` 12). With no image to look at, its tag
   edits can only follow the instructions or the tags themselves (e.g. "beach + swimsuit → summer"). The guards stay:
   a tag in both lists is ignored, it cannot remove a tag scored ≥ 0.9 or the rating, and a tag only it added scores 0.7.
3. **`/tag` response:** key `ram` is replaced by
   `"pixai": {"general": {...}, "character": {...}, "copyright": {...}, "rating": {...}}`
   (calibrated, `rating` raw). The optional request field `"models": ["wd", "pixai"]` stays. `/health` lists kinds
   `wd` and `pixai`.
4. **Settings:**
   - `use_ram` becomes `use_pixai` and `ram_strictness` becomes `pixai_strictness`, with the same defaults, types and
     reprocess modes.
   - `character_tags` now covers WD characters, PixAI characters and PixAI copyright (series) tags.
   - The rating is the mean of WD's and PixAI's rating probabilities, each first averaged over the captures, then the
     argmax.
   - `vram_gb` is now **the taggers' memory cap** (`AITAGGER_VRAM_GB = vram_gb`). Its default and limits come from
     the measurements below ("Measured (v2)": default 5, limits 3-8; `AITAGGER_VLM_UTIL` 0.22).
   - The describer gets a fixed small share (`AITAGGER_VLM_UTIL`, a constant from the measurements) and no longer
     derives from `vram_gb`. `vlm_parallel` still sets `--max-num-seqs`.
   - Old settings files holding `use_ram` / `ram_strictness` are read without error (the unknown keys are ignored).
   - Stored raw scores without a `pixai` entry are treated as needing a `full` reprocess.
5. **Labels:** `models` in the status is `{"wd": "wd-eva02-large-tagger-v3", "pixai": "pixai-tagger-v1.0", "vlm":
   "Qwen3.5-2B (text)"}`. In the preview, `models.pixai` replaces `models.ram`. The preview still returns the capture
   thumbnails.
6. **GPU sharing with Search+:** if the measurements show that taggers + describer + Immich ML + Search+ (PE-Core,
   about 7 GB) fit in 22 GB, the mutual exclusion (`GpuBusy`) is dropped. Otherwise it stays. **Measured: they fit** (17.3
   GB with all four busy, Search+ itself 5.9 GB), see "Measured (v2)".

### Measured (v2)

Measured on 2026-10-02 on the owner's RTX 4090 (24 GB, WSL2, Docker 29) with Immich ML (2.2 GB) loaded. Software: torch
2.14.1+cu130, transformers 4.57.6, onnxruntime-gpu 1.30.0, vLLM 0.30.0. Pictures are real library previews shrunk to 1024 px
and sent as base64 JPEG through the HTTP API (decoding and transfer included), 8 pictures per request, 2 requests in flight,
unless stated. "GB" is nvidia-smi's MiB / 1024, as everywhere in this file.

**Taggers: pictures per second** (everything loaded, model-native thresholds)

| | micro-batch 8 | micro-batch 16 |
|---|---|---|
| PixAI alone | 20.1 | 20.0 |
| WD + PixAI together | 16.4 | 16.3 |
| WD alone | 82.6 | (WD is held to 8) |

PixAI is compute-bound: on its own (no HTTP) it does 20.4 pictures/s at batch 8 and 20.5 at 16, in bf16 and fp16 alike
(batch 1: 20.8, batch 32: 20.6; fp32 about 5.5); matrix multiplies are 56% of the GPU time (about 170 TFLOPs, close to the
card's limit), flash attention 12%. So the micro-batch buys nothing but memory: PixAI needs 0.9 GB for the weights plus 0.17 GB per picture in
the batch (peak 1.1 / 1.6 / 2.3 / 3.6 / 6.2 GB at batch 1 / 4 / 8 / 16 / 32). Loading takes 1.2 s (WD) + 2.7 s (PixAI) with
the files cached; the first start downloads the 1.9 GB PixAI weights.

**Taggers: memory.** The whole process (CUDA context included), measured as the growth of the GPU's used memory, by cap:

| `AITAGGER_VRAM_GB` | micro-batches it settles on (PixAI / WD) | process VRAM | WD + PixAI |
|---|---|---|---|
| 3 | 2 / 2 | 2.8 GB | 16.4 pictures/s |
| 3.5 | 4 / 4 | 3.3 GB | 16.2 |
| 4 | 4 / 8 | 3.8 GB | 16.3 |
| **5** | 8 / 8 | 4.1 GB | 16.4 |
| 6 | 16 / 8 | 5.5 GB | 16.3 |
| 8 | 16 / 8 | 5.5 GB | 16.4 |

The service fits itself to the cap (warm-up at load for PixAI, out-of-memory splitting at run time for WD), so a small cap
costs no speed down to 3 GB (WD loses 10% at a micro-batch of 2); a cap of 2.5 or less was not tried. Asking for micro-batch 16
under a cap of 5 settles on 8 and uses 4.5 GB. **Recommended: `AITAGGER_VRAM_GB` 5 (4.1 GB used, 0.9 GB of room) and
`AITAGGER_BATCH` 8; limits for the panel's `vram_gb` 3-8** (above 5.5 GB nothing more is used).

**Describer** (Qwen3.5-2B, text only, 4096 tokens, `--language-model-only`; prompts of about 520 tokens, answers of about
180; panel-style request: JSON schema with `maxItems` 12, temperature 0.2, no thinking)

| | FP8 (chosen) | bf16 |
|---|---|---|
| weights in VRAM | 2.4 GB | 3.6 GB |
| smallest `AITAGGER_VLM_UTIL` that starts | 0.17 (0.18 tried with 8 and with 32 seqs) | 0.19 (0.17: "no memory for the cache") |
| VRAM at 0.22 (nvidia-smi) | 4.9 GB | 4.8 GB |
| KV cache at 0.22 with 32 seqs | 63,780 tokens | 14,336 tokens |
| one request | 0.94 s (190 tokens/s) | 1.15 s (150 tokens/s) |
| 8 requests in flight | **6.4 requests/s** (382/min) | 4.8 (290/min) |
| 16 in flight (`--max-num-seqs 32`) | 10.8 requests/s | 4.9 (KV-starved at 0.22) |
| 32 in flight (`--max-num-seqs 32`) | 13.6 requests/s (815/min), 2,450 tokens/s | 5.0 at 0.22; 13.7 at 0.30 (6.8 GB) |

FP8 gives the same quality (same shape of answers, no invalid JSON in either) with a third less weight memory, 30% more
speed at 8 in flight and a 4.5 times larger KV cache for the same share, so it is the default. Startup: 43 s with the weights
and the compile cache present (3.5 s of it is loading); the first start ever, with the 4.3 GB download and the compile, took
143 s; a new FP8 / `--max-num-seqs` combination compiles once more (about 115 s), then it is cached. **Recommended:
`AITAGGER_VLM_UTIL` 0.22** (4.9 GB used, KV room for 32 requests in flight at about 3 times the typical length; 0.18 works
and saves 0.4 GB). With `vlm_parallel` 8 the describer does 6.4 requests/s, so describing is the slower stage next to tagging
(16.4 pictures/s); at 16 it is 10.8 and at 32 13.6, so a higher `vlm_parallel` pays for it.

Quality of the answers (48 real tag lists, 30 tags each, merged from WD + PixAI; plus 18 explicit lists: 11 real library
lists rated questionable/explicit and 7 Danbooru lists written for the test; each with two prompts):
- Valid JSON every time, `finish_reason` always `stop`, no thinking text, **no refusals** (more than 2,500 answers in all,
  36 of them on explicit lists; the descriptions of those were neutral and factual).
- Descriptions are fluent and follow the tags but invent what the tags do not say (setting, light, mood, "three girls"
  where the tags say two). Examples (panel prompt, tags shortened):
  1. *hat, 1boy, male focus, facial hair, baseball cap, earphones, beard, outdoors, real life, tiktok, ...* ->
     "A close-up portrait of a young man with brown hair and facial hair, wearing a grey baseball cap and a black shirt,
     looking directly at the camera with a closed mouth. He is outdoors in daylight, and the image appears slightly blurry."
     (faithful; "young" and "daylight" are guesses). Its `add_tags` were "sunglasses" eight times: made up.
  2. *sunna (zenless zone zero), zenless zone zero, 2girls, umbrella, green hair, backpack, pink bag, striped socks, ...* ->
     "A screenshot from the game Zenless Zone Zero showing two female characters with green hair and green eyes standing
     outdoors in a park-like setting, holding umbrellas. One character wears a white shirt and striped socks, while the
     other wears a pink bag and pink footwear; the scene appears to be a beach or park on a sunny day." (recognises the
     game, garbles who wears what, invents the place)
  3. *chess piece, chessboard, board game, computer, bishop (chess), laptop, keyboard (computer), monitor, ...* ->
     "A close-up view of a chessboard with several pieces arranged on it, including a bishop and a knight, viewed from
     above. The scene is lit by natural light, creating soft shadows and highlighting the texture of the wood and the
     metallic finish of the chess pieces." (right subject, invented light and material)
- Tag edits are weak. Over 48 answers with the panel prompt (FP8): 66% of `add_tags` are not among the prompt's tags
  (generic words such as "depth", "texture", "background", or made up), 5 answers repeated entries inside a list, 7 named a
  tag in both lists, and 29% of the tags in `remove_tags` (122 of 415) were scored >= 0.9 by the taggers (the panel's guards
  block those). With `presence_penalty: 1.0` plus a stricter ask (use only the tags; `add_tags` only what the owner's
  instructions or preferred terms call for; `remove_tags` only for a direct contradiction) the same 48 gave 0 repeats, 0
  both-lists, empty `remove_tags` and `add_tags` that are 90% echoes of existing tags; 7.2 requests/s at 8 in flight. The
  descriptions were no more accurate with the stricter prompt. **Treat the describer as "tags to prose" and its tag edits as
  low-trust.**

**Everything at once** (GPU memory in GB; "busy" = all four working at the same time)

| | VRAM |
|---|---|
| Immich ML | 2.2 |
| tagger (cap 5, micro-batch 8) | 4.1 |
| describer (FP8, 0.22, 8 seqs; 5.1 after load) | 4.9 |
| Search+ (PE-Core-bigG bf16, batches of 16 pictures; a second copy of its image, measured loaded and busy) | 5.9 |
| **total, measured with all four busy** | **17.3 of 24** (peak 17,735 MiB) |

**Does Search+ fit next to the taggers and the describer within 22 GB? Yes**: 17.3 GB with all four working at once, so 4.7 GB
of the 22 GB budget (6.7 GB of the card) stay free, and Immich ML would need to grow to about 7 GB to break it. Memory is not a reason
for `GpuBusy`. Compute is shared, though: with all four busy at the same time the taggers did 6.1 pictures/s (alone 16.4),
the describer 5.5 requests/s (alone 6.4) and Search+ 8.3 pictures/s (alone 26), so running them together finishes the sum of
the work in about the time it takes one after the other, not faster. The panel can therefore drop the mutual exclusion; whether
to keep Search+ paused while the taggers index is a speed preference, not a memory one.

**Tag quality** on real library previews (anime/illustration, real photos, screenshots, memes; 48 previews tagged and
looked at, 240 for the counts). Calibrated scores >= 0.5, abbreviated:

| picture | WD | PixAI |
|---|---|---|
| photo: man with a cap and beard (TikTok selfie) | 1boy, male focus, facial hair, solo, hat, earphones, baseball cap, beard | hat, 1boy, male focus, solo, facial hair, baseball cap, earphones, short hair, outdoors; real_life 0.77, tiktok 0.74 |
| photo: white sneaker in a shop | photorealistic, shoes, no humans, sneakers, nike (company), **traditional media, colored pencil (medium)** | shoes, no humans, sneakers, white shoes, socks, black socks, shoelaces, nike (company) |
| photo: chess set in the dark | no humans, **keyboard, monitor**, still life, screen | **chess piece, chessboard, chess, board game, bishop, rook**, computer, laptop |
| photo: bus interior with passengers | sitting, male focus, **car interior**, photo background, motor vehicle | photo background, sitting, multiple boys, **airplane interior 0.84, train interior 0.83** |
| screenshot: map app | fake screenshot, english text, map, balloon, chat log | map 1.00, no humans, fake screenshot, english text, fake phone screenshot, user interface |
| screenshot of a social post with anime art | pointy ears, elf, 3girls, santa hat, bikini, ... (no character) | same kind of tags plus **character rory_mercury 0.94, series "gate - jieitai ka no chi nite..." 1.00**, twitter 0.93 |
| game screenshot (two girls, umbrellas) | umbrella, 2girls, backpack, bag, ... (no character) | 2girls, umbrella, green hair, ...; **character sunna_(zenless_zone_zero) 1.00**, series zenless_zone_zero 1.00 |
| anime meme (girl with a halo and a flower) | 1girl, halo, flower, hair ornament, black hair, ... (no character) | hair ornament, flower, halo, 1girl; **character hatsune_miku 0.99**, vocaloid 0.97 |
| line-art swordsman, hat and scarf | 1boy, monochrome, greyscale, sword, weapon, rice hat, katana | weapon, greyscale, monochrome, hat, sword, 1boy; series one_piece 0.97 (unchecked) |
| screenshot: man in a tie, app UI | english text, 1boy, fake screenshot, necktie, blue necktie, open mouth | necktie, fake screenshot, english text, collared shirt, open mouth; social_network 0.89, instagram 0.92, youtube 0.87 |

Over 240 random previews PixAI names a character in 59 pictures (WD: 30) and a series/platform in 102, and it adds `3d`
(46 pictures), `social_network`, `spanish_text`, `pixel_art`; WD adds `asian`, `black_footwear`, `blurry_foreground`, `pixiv_id`.
On the 32 pictures PixAI marks `real_life` the two taggers return about the same number of general tags (27 each, 16 in
common). **PixAI on real photos:** better than expected for an anime model. It names the objects (chess set, sneakers,
laptop, map), clothing and people counts about as well as WD, marks photos with `real_life` (0.77-0.91) and reads app
chrome (`twitter`, `instagram`, `tiktok`, `youtube`, `fake_phone_screenshot`). It has no photographic vocabulary (WD's
`realistic`, `photorealistic`, `asian` have no counterpart), people are `1boy`/`male_focus`, and scenery can be wrong
(a bus as `airplane_interior`; WD said `car_interior`). WD in turn adds art-medium tags to photos that are plainly wrong
(`colored_pencil_(medium)`) and missed the chess set. Neither is a photo tagger; together they cover each other, and PixAI's big
gain is characters and series. Heads-up for the panel: PixAI's `copyright` also returns `original` (47 of 240 previews),
`real_life`, `twitter`, `instagram`, `tiktok` and `youtube`; with `character_tags` merging all of `copyright`, these become tags.

### v2, panel side: as built (what extends or differs from the list above)

The panel code (`aitagger.py`, `searchplus.py`, the web tab) follows the list above. Where it adds to it or chose
something the list leaves open:

- **Memory constants** sit together at the top of `aitagger.py`, set from the measurements: `VRAM_GB_DEFAULT = 5`,
  `VRAM_GB_LIMITS = (3, 8)` and `VLM_UTIL = 0.22` (the describer's share of the whole card, 5.1 GB measured);
  `vlm_parallel` defaults to 16 (describing is the slower stage); `searchplus.AITAGGER_EXCLUSIVE = False`
  (everything fits together in 17.3 GB). The describer is sent `presence_penalty: 1.0` and a stricter prompt
  (no invented place, setting, light, time, weather, mood or story; tag edits only when the owner's instructions
  ask, removals only on a direct contradiction), because the measurements showed it inventing setting and mood.
- **Exclusivity** is one module-level switch, `searchplus.AITAGGER_EXCLUSIVE` (shipped `False` since v2 measurements). `False`: Search+ starts
  while the describer container runs (no `GpuBusy`), and the AI Tagger no longer stops a running Search+ before it
  starts its containers. The status `service` has `exclusive` (the switch), so the tab hides its "Search+ is paused"
  notes when it is `False`.
- **Stored scores.** `raw.scores[i]` is `{"wd": {general, character} | null, "pixai": {general, character, copyright} |
  null}` and `raw.ratings[i]` is `{"wd": {...} | null, "pixai": {...} | null}` (the raw rating probabilities; the
  `raw` table is unchanged). The panel assumes PixAI names its ratings like WD (general, sensitive, questionable,
  explicit): a name one model lacks counts as 0 in that model's mean.
- **Rating tag.** Its `source` is the model that was surest of the winning rating (a tie goes to `wd`), and the
  Test card's `models.rating` is the combined mean. Other tags merge WD and PixAI by highest score, keeping that
  model's name as `source`.
- **Scores from the v1 service (no `pixai` entry).** They count as needing a `full` reprocess:
  - The first time a store that holds such rows is opened, `settings_version` goes up once (`meta.raw_format` = 2
    remembers it), so every result made from them is "outdated" and shows in that counter and in the `outdated` scope.
  - A `retag` or `describe` that reaches an asset whose stored scores lack PixAI (a queued request, or a result that
    was stored but not yet written) is run as `full` instead: it tags again and describes again. This applies only while
    `use_pixai` is on; with it off nothing is lost by using the old scores, and switching it on is a `full` change as before.
  - Nothing is re-queued by itself: the owner starts it, for example "Also update the N already-tagged assets".
- **`describe` needs no pictures.** The describer sees only tags, so a `describe` run uses the stored scores and starts
  only the describer container; the captures are not cut. `full` still cuts them for the taggers.
- **Nothing to describe from.** With no tag at all for an asset (or both taggers off), the describer is not called
  (a text model given nothing would invent a description): the result has no description and the note "no description:
  the taggers found no tags to describe from". With both taggers off the describer container is not started either.
- **The prompt** names a video as such ("several frames of it, combined") but sends no pictures and no frame count.
  `add_tags` / `remove_tags` are asked "at most 8 each, normally empty"; the schema's `maxItems` 12 stays.
- **Labels.** `character_tags` reads "character and series names" in the tab, since it also gates PixAI's series tags.
- **Not done here:** the Android app (`TaggerScreen.kt`) still sends and reads `use_ram`, `ram_strictness` and
  `models.ram`. The panel refuses the old keys as unknown settings (400), so the app needs the same rename.

## Decisions (v1; see v2 above)

| Topic | Decision |
|---|---|
| Tagger 1 | `SmilingWolf/wd-eva02-large-tagger-v3` (ONNX, Apache-2.0): 10,861 Danbooru tags — illustration/anime, people, clothing, pose, characters, rating |
| Tagger 2 | ~~RAM++~~ → **v2: PixAI Tagger v1.0** (30,877 Danbooru-style tags) |
| VLM | ~~Qwen3.5-9B vision~~ → **v2: Qwen3.5-2B, text only** (strings the tags into a description) |
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
default 8), `IDLE_EXIT_MINUTES` (default 20; **must not exit while a request is in flight**), `WD_MODEL`, `PIXAI_MODEL`,
`PIXAI_REVISION`, `WD_PRECISION` (default `fp16`: the ONNX model is converted once and cached; `fp32` keeps the original),
`PIXAI_PRECISION` (default `fp16`; `bf16` and `fp32` work). Model files live under `/cache` (`/cache/hub` is a Hugging Face
cache); after the first start everything loads from there, offline, in about 4 s. The cap is split between the two
runtimes: onnxruntime (WD) gets an arena of 1.6 GB (at most 40% of the cap), PyTorch (PixAI) the rest after 0.5 GB for the
CUDA context. WD runs in micro-batches of at most 8 (bigger gains nothing). After loading, PixAI is warmed up at the full
micro-batch and the batch is halved until it fits under the cap. Later, a CUDA out-of-memory answer halves the micro-batch
of that model for the rest of the process (`effectiveBatch` in `/health`).

**WD** (`SmilingWolf/wd-eva02-large-tagger-v3`, Apache-2.0): 10,861 Danbooru tags, onnxruntime, input 448×448 NHWC BGR
float32 0-255, the picture padded to a white square.

**PixAI Tagger v1.0** (`pixai-labs/pixai-tagger-v1.0`, Apache-2.0): 30,877 Danbooru-style tags, a 486M-parameter ViTDet
(SAM3) backbone with a pooled attention head, run with PyTorch fp16 and SDPA (flash) attention. The model code is the
repository's own (`tagger_pipeline.py`, loaded by transformers with `trust_remote_code=True`), so the image build pins
`transformers==4.57.6` (the version it was exported with) and the service pins one repository commit,
`9fe10addf9326e292da8a85a98ea74cd91b41771` (2026-09-22): `config.json`, `preprocessor_config.json`, `tagger_pipeline.py`
and `model.safetensors` (F32, 1.9 GB) are downloaded to the cache at the first load.
- Labels: `config.json` holds the tag names and `tags_split`, so the tags are stored category after category:
  general 15,043, character 8,308, copyright 2,460, style 4,917, meta 145, rating 4 (`rating:s`, `rating:g`, `rating:q`,
  `rating:e`). Returned: general, character, copyright and rating, the last as `general`/`sensitive`/`questionable`/`explicit`
  (the same names as WD, so the panel can average them). `style` and `meta` are dropped. Names keep their underscores.
- Preprocessing (the repository's `RescalePadProcessor`, reproduced on the GPU): the RGB picture (transparency flattened
  onto white) becomes a 0-1 tensor, is scaled by `r = min(1008/h, 1008/w)` to `(int(h*r), int(w*r))` with bilinear
  interpolation and antialiasing (torchvision's tensor `resize`), centred on a **black** 1008×1008 canvas (`left = pw // 2`,
  `top = ph // 2`), and normalised with mean 0.5 / std 0.5. Pictures smaller than 1008 px are scaled up. The sigmoid is
  taken in float32 from the logits.
- Checked against the official `transformers.pipeline` on 24 real pictures: in fp32 the largest score difference is 0.0001
  (no decision differs); fp16 vs fp32 over 246 pictures: 0.20% of the decisions at 0.5 differ (largest score difference
  0.011), bf16: 1.4% (0.129). fp16 is as fast as bf16, so it is the default.

### `GET /health`
```json
{"status": "loading|ok|error", "error": null, "device": "cuda", "vramCapGb": 5, "batch": 8,
 "effectiveBatch": {"wd": 8, "pixai": 8},
 "models": [{"name": "wd-eva02-large-tagger-v3", "kind": "wd", "tags": 10861, "precision": "fp16", "loadedIn": 1.2},
            {"name": "pixai-tagger-v1.0", "kind": "pixai", "tags": 30877, "precision": "fp16", "loadedIn": 2.7}],
 "idleExitMinutes": 20, "idleSeconds": 12, "busy": 0}
```

### `POST /tag`
Request: `{"images": ["<base64 jpeg/png>", ...], "floor": 0.05}` (maximum 64 images). Optional: `"models": ["wd"]` or
`["pixai"]` runs only that model (default both; the other key is then absent), so the panel can skip a disabled tagger.
Any other name is a 400. Any failure of one picture on one model makes that slot an error (`results[i]` null, `errors[i]`
says which model).

Response:
```json
{"results": [{"wd": {"general": {"long_hair": 0.93}, "character": {"hatsune_miku": 0.81},
                     "rating": {"general": 0.02, "sensitive": 0.71, "questionable": 0.2, "explicit": 0.07}},
              "pixai": {"general": {"long_hair": 0.97}, "character": {"hatsune_miku": 0.99}, "copyright": {"vocaloid": 0.97},
                        "rating": {"general": 0.21, "sensitive": 0.82, "questionable": 0.01, "explicit": 0.0}}}, null],
 "errors": [null, "cannot identify image file"], "tookMs": 412}
```
Scores are **calibrated**, so 0.5 is the model's own recommended threshold for that tag:
`s' = sigmoid(logit(s) - logit(t))`. Here `t` is 0.35 for WD general tags, 0.75 for WD character tags, and the PixAI model
card's 0.17 (general), 0.27 (character) and 0.24 (copyright) for PixAI. Only tags with `s' >= floor` are returned. The
`rating` of both models is the model's raw probability for each rating (independent sigmoids: they need not add up to 1).
Tag names are model-native; the panel normalises them. PixAI's `copyright` category also holds
platform and medium tags, not only series: `original`, `real_life`, `twitter`, `instagram`, `tiktok`, `youtube` (in a sample of
240 library previews, scored >= 0.5: original 47, instagram 34, real_life 32, twitter 31, youtube 26, tiktok 11).

**Errors.** 503 while loading (above). A bad picture fails only its own slot (`results[i]` is `null`, `errors[i]` says
why). **Out of graphics memory** is reported as HTTP 500 `{"error": "... CUDA out of memory ..."}`, or as such a text in
a slot's `errors[i]`; the panel looks for the words `out of memory` (any case), halves its `batch_size` for the session
and sends the assets again in smaller requests. Any other 5xx counts as "the service fell over" (the assets stay
untagged and are tried again); a 4xx is a bug in the request.

## 2. Describer (`immich_aitagger_vlm`)

`vllm/vllm-openai:v0.30.0` serving `Qwen/Qwen3.5-2B` (text only) as model name `tagger-vlm`, on `127.0.0.1:11441`
(container port 8000). Flags: `--language-model-only` (every multimodal limit is 0: the vision tower is neither loaded
nor profiled; the same as `--limit-mm-per-prompt '{"image":0}'`), `--quantization fp8`, `--gpu-memory-utilization
${AITAGGER_VLM_UTIL}` (default 0.22), `--max-model-len 4096`, `--max-num-batched-tokens 4096`, `--max-num-seqs
${AITAGGER_VLM_SEQS}` (default 8) and `--default-chat-template-kwargs {"enable_thinking":false}`. The HF cache is a volume
under `~/vlm/models/aitagger/hf` (the torch.compile cache is another, `.../vllm`). Ready means `GET /health` answers 200
(about 45 s after `up` once the weights and the compile cache are there; the first start downloads 4.3 GB and compiles,
about 2.5 minutes; a `--max-num-seqs` value not used before compiles once more, about 70 s).

The panel calls `POST /v1/chat/completions` with:
- `model: "tagger-vlm"`, `temperature: 0.2`, `max_tokens: 400`
- `chat_template_kwargs: {"enable_thinking": false}`
- `response_format: {"type": "json_schema", ...}` (the schema below)

Content: one system message, then one user message with text only (no pictures). The answer must match
`{"description": str, "add_tags": [str], "remove_tags": [str]}`; `maxItems` is 12 on both lists (vLLM enforces it).

The system prompt says this is a private personal archive, that every picture is described neutrally and factually
including adult content, and that the model never refuses. The user text lists the final tags with scores, the rating,
the user's `instructions` and `vocabulary`, and asks for 1-2 sentences in `language`.

A refusal or an invalid JSON answer is retried once, then stored as `description: ""` and noted in that asset's
status. It is **not** an asset failure. (Measured: none in more than 2,500 answers, 36 of them on explicit tag lists.)

**What a text-only 2B model can do** (details under "Measured (v2)"): the descriptions are fluent and follow the tags, but
details the tags do not give (setting, mood, counts) are partly invented. `add_tags` are mostly filler or echoes of existing
tags (about two thirds are not in the prompt tags and many are generic words), and `remove_tags` are noise: about a third of
the tags it removes are scored >= 0.9 by the taggers. With `presence_penalty: 1.0` the lists no longer repeat entries (5-8 of
48 answers did, 0 with it), and a prompt that asks for `add_tags` only when the owner's instructions or preferred terms call
for it and for `remove_tags` only on a direct contradiction leaves `remove_tags` empty and makes `add_tags` mostly echoes
(10% new). The guards in section 3 stay, but the panel should treat the describer as "tags to prose" and trust its tag
edits little.

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
| vlm_parallel | 16 (v2) | int 1–32 (concurrent VLM requests, also `--max-num-seqs`) | |
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
5. **VLM** (if `describe`). Apply its `add_tags` and `remove_tags`, then normalise, rename and block again. Guards
   (measured on the library, the VLM drops sure tags and sometimes names a tag both ways): a tag in both lists is
   ignored; it cannot remove a tag a tagger scored >= 0.9, nor the rating; a tag only it saw scores 0.7, so it ranks
   below the taggers' confident tags; a tag it confirms keeps its higher tagger score.
6. **Rules**, then `blocked` again. Cap at `max_tags` by score; rule-added tags score 1.0.
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
- ~~`CATALOG_EVERY = 600`~~ (v3.2: a probe every `check_every` minutes decides when the library list is read; see "Unloading the models and picking up new uploads").
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
prepared. (v3: no describer, and the requests in flight carry over from one round to the next: see "Indexer: pipelined
across rounds" in "v3, panel side: as built".) 3 servers going away in a row, with nothing finished in between, end the run with an error. A CUDA
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
| `GET /api/aitagger/assets` | `?tag=&q=&outdated=1&page=1&size=60` | `{items: [{id, name, type, taken, tags: [str], description, settingsVersion, processedAt}], total, page, tags: [{tag, count}], tagsTotal}` (the 200 commonest tags; `tagsTotal` is how many there are) |
| `GET /api/aitagger/tags` | `?q=&limit=50&offset=0` | `{tags: [{tag, count}], total}`: every tag that contains `q`, most used first, one page (see "Search by tags and description") |
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
 "unload": {"loaded": true, "idleSeconds": 40, "unloadInSeconds": 80, "rule": "after-work|interactive|server", "busy": false},
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
6. **Tagged assets.** "Find a tag" (autocomplete from `GET /api/aitagger/tags`; picking a tag filters like its chip),
   the chips "Most common tags" with "Show more" (pages through all tags), search, an "outdated only" filter, and a
   list with thumbnails, tags and description. Selection actions: Re-tag, Re-describe, Full re-process, Remove AI text
   (with "and don't tag again").

Saving "How to tag" or "Rules" opens a choice:
- **New assets only** (default)
- **Also update the N already-tagged assets**, with the suggested mode preselected from the keys that changed (`retag`
  or `describe`).
