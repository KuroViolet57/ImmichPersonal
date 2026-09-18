# Rules reference

A rules file says *what to find* and *which album it belongs in*. Write it in
YAML (`.yaml`/`.yml`, needs PyYAML) or JSON (`.json`, no dependencies).

Check it any time without touching the server:

```bash
immich-organizer validate -r rules.yaml
```

Errors name the exact field, e.g. `rules[2].filters.taken_after: 'last tuesday'
is not a date.`

## Shape

```yaml
version: 1          # required, must be 1

defaults:           # optional; every rule inherits these
  limit: 150
  create_album: true
  archive: false
  favorite: false
  refine_pool: 600
  filters:
    type: IMAGE

rules:              # required, at least one
  - name: Mountains         # optional; defaults to the album name, must be unique
    album: Mountains        # required
    query: person in a mountain
    limit: 200
    enabled: true
    create_album: true
    filters: { ... }
    refine: { ... }
    actions: { ... }
```

## Matching: pick exactly one

### `query` — natural language

```yaml
query: person in a mountain
```

The same CLIP text search as the web UI's search bar.

### `like_asset` — a reference photo

```yaml
like_asset: 7f3e1a2b-4c5d-6e7f-8a9b-0c1d2e3f4a5b
```

The same thing as the asset viewer's three-dots → *Search similar*. To find an
asset's ID, open the photo in Immich and copy the last part of the URL, or:

```bash
immich-organizer search --query "my dog on the sofa" --limit 10
```

and copy the ID from the last column of the best match.

Setting both, or neither, is an error.

## `limit`

The maximum number of matches the rule will file. **This is the only real
precision control** — see [API-NOTES.md](API-NOTES.md#no-similarity-score--the-design-constraint)
for why there is no confidence threshold to set.

Default 200, hard cap 5000. Start low, look at `plan --html`, raise it until
the tail stops being relevant.

## `filters`

Narrow the search before ranking. All optional.

| Key | Type | Notes |
|---|---|---|
| `type` | `IMAGE` / `VIDEO` / `AUDIO` / `OTHER` | |
| `taken_after`, `taken_before` | date | `2023-01-01`, an ISO timestamp, or a relative offset |
| `created_after`, `created_before` | date | upload time rather than capture time |
| `only_unfiled` | bool | only assets in no album — good for triage |
| `favorite` | bool | |
| `person_ids` | UUID or list | from `GET /api/people` |
| `album_ids` | UUID or list | search within albums |
| `tag_ids` | UUID or list | |
| `city`, `state`, `country` | string | |
| `make`, `model`, `lens_model` | string | camera metadata |
| `rating` | int −1…5 | |
| `visibility` | `timeline` / `archive` / `hidden` / `locked` | |
| `library_id` | UUID | |
| `language` | string | search language code |

**Relative dates** work anywhere a date does: `-30d`, `-6m`, `-2y`. They are
resolved at run time, so `taken_after: -6m` in a scheduled rule means "the last
six months", always.

## `refine`

Smart search returns no score, so the way to raise precision is to intersect
independent searches.

```yaml
refine:
  all_of:                 # a match must ALSO appear in each of these searches
    - sand and ocean
  none_of:                # a match must appear in NONE of these
    - swimming pool
    - bathroom
  pool: 800               # how deep each refinement search looks (default 600)
```

The base query's ranking is preserved — refinement only removes candidates, it
never reorders them. `limit` is applied *after* refinement, so a refined rule
searches deeper than `limit` to compensate.

`none_of` is usually the bigger win. A query for `beach` pulls in swimming
pools and bathroom tiles; naming those explicitly removes them.

Each refinement term costs one extra search per rule, so `all_of` with three
terms plus `none_of` with two means six searches for that rule.

## `actions`

Applied only to assets the run **newly** filed, never to ones already there.

```yaml
actions:
  archive: true     # hide from the main timeline (does NOT delete)
  favorite: true    # star them
```

## `create_album` and `enabled`

- `create_album: false` makes the rule fail loudly if the album is missing,
  instead of creating it. Useful when you are filing into albums you curate by
  hand.
- `enabled: false` keeps a rule in the file without running it.

## Re-running

Rules are idempotent: `plan` reads the target album's current contents and
counts only genuinely new matches. Re-running a file weekly picks up whatever
you have uploaded since, and nothing else.

Two caveats worth knowing:

- If you **remove** a photo from an album by hand, a later run will put it back
  when it still matches. Narrow the rule, or add the unwanted subject to
  `none_of`.
- Raising `limit` on an existing rule files the newly-reached tail of the
  ranking, which is the least confident part. Preview after raising it.

## Worked example

```yaml
version: 1

defaults:
  limit: 150
  filters:
    type: IMAGE

rules:
  # Broad, high volume, reviewed once.
  - name: Mountains
    album: Mountains
    query: person hiking in mountains
    limit: 300
    refine:
      none_of: [indoor climbing gym]

  # A specific pet, by example rather than description.
  - name: Bailey
    album: Bailey
    like_asset: 7f3e1a2b-4c5d-6e7f-8a9b-0c1d2e3f4a5b
    limit: 400

  # Inbox triage: only look at what has not been filed yet.
  - name: Recent food
    album: Food
    query: a plate of food in a restaurant
    limit: 100
    filters:
      only_unfiled: true
      taken_after: -12m

  # Clutter, filed and hidden from the timeline.
  - name: Screenshots
    album: Screenshots
    query: a screenshot of a phone screen
    limit: 500
    actions:
      archive: true
```
