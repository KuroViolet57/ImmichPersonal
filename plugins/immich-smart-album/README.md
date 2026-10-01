# Smart Album — an Immich workflow plugin

Adds a **"Filter by smart search"** step to Immich's native Workflows, so a
workflow can decide whether a photo matches *what it looks like* rather than
only its metadata.

Immich's built-in plugin ships filters for filename, date, geolocation, EXIF,
tags, asset type and missing time zone. All of them match on metadata. This
plugin adds the missing one: it asks Immich's own smart search (the CLIP
endpoint behind the web UI's search bar) whether the asset flowing through the
workflow is among the closest matches for a description or a reference photo.

Combined with the core plugin's **Add to Album(s)** action, that gives you
*"photos that look like a mountain → Mountains"* as a native workflow.

---

## Read this first: the timing constraint

**A freshly uploaded photo has no CLIP embedding yet, so it cannot match.**

Immich queues the `SmartSearch` job (which produces the embedding) only after
thumbnail generation finishes. But the `AssetCreate` and
`AssetMetadataExtraction` triggers both fire *earlier* in the pipeline. A
workflow on those triggers therefore runs before the photo is searchable, and
this filter will correctly — but uselessly — reject it.

There is no "embedding ready" trigger to hook, so the practical options are:

| Approach | When it works |
|---|---|
| **`AssetTagged` trigger** (what the bundled template uses) | Reliable. By the time you tag a photo, it has been indexed. |
| `AssetCreate` / `AssetMetadataExtraction` | Only for photos already indexed — e.g. a re-run. Not for fresh uploads. |
| The `immich-organizer` CLI in this repository, on a schedule | Reliable, and it also sweeps photos you uploaded *before* the workflow existed. |

The filter detects this case and writes a clear line to the workflow log rather
than failing silently. Leave **Diagnose non-matches** on until you trust it.

## Requirements

- An Immich version with Workflows (the `/workflows` page and `/api/plugins`).
  Run `immich-organizer doctor` from this repository to check.
- External plugins enabled on the server (see below).

## Install

> Running Immich in Docker on WSL2? There is a step-by-step walkthrough at
> [`docs/INSTALL-PLUGIN-WSL2.md`](../../docs/INSTALL-PLUGIN-WSL2.md), and
> `scripts/install-plugin.sh` automates most of it.

### 1. Enable external plugins

Immich only loads third-party plugins when you opt in. Add to the
`immich-server` service in your `docker-compose.yml`:

```yaml
services:
  immich-server:
    environment:
      IMMICH_ALLOW_EXTERNAL_PLUGINS: 'true'
      IMMICH_PLUGINS_INSTALL_FOLDER: /plugins
    volumes:
      - ./plugins:/plugins
```

`IMMICH_PLUGINS_INSTALL_FOLDER` must be an absolute path. Every subdirectory of
it is imported as a plugin at startup.

### 2. Drop the plugin in

```bash
mkdir -p ./plugins/immich-smart-album
cp manifest.json ./plugins/immich-smart-album/
cp dist/plugin.wasm ./plugins/immich-smart-album/
docker compose up -d
```

The folder must contain `manifest.json` and the `plugin.wasm` it names.

### 3. Check it loaded

```bash
immich-organizer doctor
```

should now report a content-matching filter. Or open **Workflows** in Immich
and look for *Filter by smart search* when adding a step.

## Use

1. In Immich, go to **Workflows → New**, or pick the bundled **Smart album**
   template.
2. Trigger: **Asset tagged** (see the timing constraint above).
3. Step 1 — *Filter by smart search*:
   - **Description**: `person in a mountain`
   - **Match depth**: `200`
   - **API key**: an Immich API key (Account Settings → API Keys)
4. Step 2 — *Add to Album(s)*: pick or name the target album.

Tagging a photo now files it if it looks like the description.

### Settings

| Setting | Meaning |
|---|---|
| **Description** | Natural language, e.g. `a plate of food in a restaurant`. |
| **Reference photo ID** | Match against a photo instead of a description — the same thing as the web UI's *Search similar*. Set this **or** the description, not both. |
| **Match depth** | How many of the closest results count as a match. See below. |
| **Invert** | Continue only when the asset does *not* match. |
| **Immich URL (internal)** | How the plugin reaches Immich from inside the container. `http://localhost:2283` is usually right. |
| **API key** | Used for the search call only. Grant it just `asset.read` — that is the sole permission `POST /api/search/smart` requires. See [Why an API key](#why-an-api-key). |
| **Diagnose non-matches** | On a non-match, check whether the asset is indexed yet and log why. One extra request per non-matching asset. |

### Why "match depth" and not a confidence threshold

Immich's smart search returns results ranked by embedding distance but exposes
**no score**. There is no number to threshold on. So the filter's test is
"is this asset inside the top N results", and *Match depth* is N.

Two consequences:

- A photo has to crack the **global** top N for the whole library, so set N
  comfortably larger than the album you expect. Too small and genuine matches
  fall outside it.
- Too large and the tail — the least-similar results — starts matching. Preview
  a query with `immich-organizer search --query "..." --limit N --html out.html`
  to see where it goes wrong before committing to a number.

### Why an API key

The plugin reaches smart search over HTTP, and Immich treats that as an
ordinary API call, so it needs its own credential.

It cannot borrow the workflow's identity. The `authToken` a plugin receives is
a JWT signed with a secret the server generates fresh on every boot, and only
the workflow host validates it — it authorises the six host functions
(`searchAlbums`, `createAlbum`, `addAssetsToAlbum`, `addAssetsToAlbums`,
`bulkTagAssets`, `httpRequest`), not the REST API. And `httpRequest` performs a
plain `fetch` with exactly the headers the plugin supplies; the host adds no
credentials of its own.

None of those six host functions can search, which is why this step goes over
HTTP at all. If Immich ever adds a search host function, the key becomes
unnecessary and this plugin should drop it.

Two consequences worth acting on:

- **Scope the key to `asset.read`.** That is the only permission the endpoint
  requires. Adding to the album is the *next* step's job, done through host
  functions, so the key needs no album permissions.
- **Use the key of the user who owns the photos.** The filter asks "is this
  asset among the closest matches", and the search only sees libraries the key
  can read. A key belonging to another account searches a different library,
  so nothing ever matches.

Step config is stored in Immich's database and shown to anyone who can open
the workflow, so treat the key as visible to your Immich admins.

### Network access

`allowedHosts` in `manifest.json` limits which hosts the plugin may reach. It
ships with `localhost`, `127.0.0.1`, `immich-server` and `immich_server`. If
your **Immich URL (internal)** uses a different hostname, add it there and
restart, otherwise the request is blocked by the host.

## Build from source

A prebuilt `dist/plugin.wasm` is committed so you can install without a
toolchain. To rebuild it yourself:

```bash
./build.sh
```

That fetches `extism-js` and `binaryen` into `.tools/` if they are missing,
type-checks, bundles with esbuild, compiles to WASM, and runs the tests.

```bash
npm test
```

runs the compiled `plugin.wasm` through the real Extism runtime with the same
host-function contract the Immich server uses, against a fake Immich whose
search results the test controls.

## Layout

| Path | What it is |
|---|---|
| `manifest.json` | Declares the method, its config schema, `allowedHosts`, and the workflow template. |
| `src/index.ts` | The filter. |
| `src/runtime.ts` | Minimal vendored copy of `@immich/plugin-sdk`, which is workspace-only and not published to npm. |
| `scripts/prepare-build.mjs` | Generates the `.d.ts` extism-js needs, from the manifest. |
| `test/run.mjs` | Runs the compiled WASM the way the server does. |

## Licence

AGPL-3.0, matching Immich, since it builds against Immich's plugin interface.
