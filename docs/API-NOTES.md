# How this maps onto the Immich API

Verified against the Immich OpenAPI specification
(`open-api/immich-openapi-specs.json`), API version **3.2.0**. Everything below
is a documented public endpoint — no database access and no internal services.

## Authentication

An API key created under *Account Settings → API Keys*, sent as:

```
x-api-key: <key>
```

The spec also allows a bearer JWT and a session cookie; this tool uses the API
key because it is the only one that survives unattended, scheduled runs.

Base URL is `<server>/api` — e.g. `http://localhost:2283/api`.

## The two searches that matter

Both are the **same endpoint**, `POST /api/search/smart`, which runs a CLIP
embedding similarity search over the library.

### Text search

```json
{ "query": "person in a mountain", "type": "IMAGE", "size": 100, "page": 1 }
```

This is exactly what the web UI's search bar does.

### Similar-to-this-photo

```json
{ "queryAssetId": "7f3e1a2b-...", "size": 100 }
```

`queryAssetId` is described in the spec as *"Asset ID to use as search
reference"*. This is what the asset viewer's three-dots → *Search similar*
uses. The server already holds the reference photo's embedding, so nothing has
to be uploaded or computed client-side.

### Shared options

`SmartSearchDto` accepts, alongside `query`/`queryAssetId`:

| Field | Notes |
|---|---|
| `type` | `IMAGE`, `VIDEO`, `AUDIO`, `OTHER` |
| `takenAfter`, `takenBefore` | ISO 8601 |
| `createdAfter`, `createdBefore` | ISO 8601 |
| `isNotInAlbum` | only assets in no album — good for triage |
| `isFavorite`, `isMotion`, `isOffline`, `isEncoded` | booleans |
| `personIds`, `albumIds`, `tagIds` | UUID arrays |
| `city`, `state`, `country` | |
| `make`, `model`, `lensModel` | camera metadata |
| `rating` | −1 to 5 |
| `visibility` | `timeline`, `archive`, `hidden`, `locked` |
| `libraryId`, `language`, `ocr` | |
| `page`, `size` | `size` is capped at **1000** |

The response is a `SearchResponseDto`:

```json
{
  "assets": {
    "total": 1234, "count": 100, "items": [ /* AssetResponseDto */ ],
    "nextPage": "2", "nextCursor": null, "facets": []
  },
  "albums": { "total": 0, "count": 0, "items": [], "facets": [] }
}
```

### No similarity score — the design constraint

`SearchAssetResponseDto` carries `total`, `count`, `facets`, `nextPage`,
`nextCursor` and `items`. `AssetResponseDto` carries no distance, score, or
confidence field. Results arrive **ordered by embedding distance**, closest
first, but how close is never stated.

Consequences, all of which shaped this tool:

1. There is no honest way to write "file everything above 80% confidence".
   Ranking is all you get, so `limit` (top-N) is the control.
2. The result set never comes back empty for a well-formed query. The tail is
   simply the least-bad matches in the library.
3. Precision has to come from somewhere else. This tool intersects independent
   searches (`refine.all_of` / `refine.none_of`) and shows thumbnails before
   writing anything.

## Albums

| Operation | Call |
|---|---|
| List | `GET /api/albums` |
| Create | `POST /api/albums` — `{ "albumName", "description", "assetIds" }` |
| Add assets | `PUT /api/albums/{id}/assets` — `{ "ids": [...] }` |
| Remove assets | `DELETE /api/albums/{id}/assets` — `{ "ids": [...] }` |
| Add to several albums at once | `PUT /api/albums/assets` |

Add and remove return an array of `BulkIdResponseDto`:

```json
[ { "id": "...", "success": true },
  { "id": "...", "success": false, "error": "duplicate" } ]
```

`error` is one of `duplicate`, `no_permission`, `not_found`, `unknown`,
`validation`. Re-adding an asset already in the album is reported as
`duplicate` rather than failing the request, which is what makes rules
naturally idempotent.

**`AlbumResponseDto` does not include the asset list** — only `assetCount`. To
find what is already in an album, this tool uses a metadata search scoped to
it:

```json
POST /api/search/metadata
{ "albumIds": ["<album-id>"], "size": 1000, "page": 1, "withExif": false }
```

## Other endpoints used

| Purpose | Call |
|---|---|
| Reachability (no auth) | `GET /api/server/ping` → `{"res": "pong"}` |
| Version | `GET /api/server/about` |
| Whose key is this | `GET /api/users/me` |
| Thumbnails for previews | `GET /api/assets/{id}/thumbnail?size=thumbnail\|preview\|fullsize` |
| Archive / favorite in bulk | `PUT /api/assets` — `{ "ids": [...], "visibility": "archive" }` |
| Exact (non-AI) search | `POST /api/search/metadata` |

## Things Immich can do that this tool does not (yet)

Worth knowing, since they are all reachable the same way:

- **People.** `GET /api/people` supports `closestAssetId` and
  `closestPersonId` for face-similarity lookups. Face recognition is a
  separate model from CLIP, so "every photo of this person" is better served
  by `personIds` than by a text query.
- **Tags.** `PUT /api/tags/assets` bulk-tags assets. Tags may suit
  rule-driven classification better than albums, since a photo carries tags
  more naturally than it belongs to many albums.
- **Duplicates.** `GET /api/duplicates` exposes Immich's own duplicate
  detection.
- **OCR.** Smart search accepts an `ocr` field, and `GET /api/assets/{id}/ocr`
  returns recognised text — a route to filing receipts and documents.
- **Structured filters.** `SearchFilter` / `SearchFilterBranch` support
  `and`/`or` branches with typed operators, a richer query model than the flat
  filters this tool exposes.

## Checking against your own server

Versions move quickly. Your server publishes its own spec:

```bash
curl -s http://localhost:2283/api/spec.json | python3 -m json.tool | less
```

The same document is available as YAML at `/api/spec.yaml`, and the
interactive Swagger UI is at `http://localhost:2283/doc` (note: no `/api`
prefix on that one).

`immich-organizer doctor` also probes the endpoints this tool depends on and
reports which of them your server actually accepts.
