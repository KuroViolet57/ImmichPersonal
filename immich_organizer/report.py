"""Human-readable renderings of a plan: terminal text and a self-contained HTML page."""

from __future__ import annotations

import base64
import html
from pathlib import Path

from .client import ImmichClient, ImmichError
from .engine import Plan

# Keeping every thumbnail would make the report enormous; a sample per rule is
# enough to judge whether the matches are sane.
DEFAULT_THUMBS_PER_RULE = 60


def render_text(plan: Plan, *, verbose: bool = False) -> str:
    lines: list[str] = []
    lines.append(f"Plan against {plan.server_url}  ({plan.created_at})")
    lines.append("=" * 72)

    for entry in plan.entries:
        if entry.error:
            lines.append(f"\n  {entry.rule.name}  [FAILED]")
            lines.append(f"    {entry.error}")
            continue

        marker = "new album" if not entry.album_exists else "existing album"
        lines.append(f"\n  {entry.rule.name}")
        lines.append(f"    match:  {entry.rule.describe_match()}")
        lines.append(f"    album:  {entry.album_name}  ({marker})")
        if entry.rule.refine.all_of or entry.rule.refine.none_of:
            if entry.rule.refine.all_of:
                lines.append(f"    all_of: {', '.join(entry.rule.refine.all_of)}")
            if entry.rule.refine.none_of:
                lines.append(f"    none_of: {', '.join(entry.rule.refine.none_of)}")
        lines.append(
            f"    result: {len(entry.to_add)} to add, "
            f"{len(entry.already_in_album)} already in album "
            f"(limit {entry.rule.limit})"
        )
        if verbose:
            for asset in entry.to_add[:25]:
                taken = (asset.get("localDateTime") or asset.get("fileCreatedAt") or "")[:10]
                lines.append(f"      - {taken}  {asset.get('originalFileName', '?')}")
            if len(entry.to_add) > 25:
                lines.append(f"      ... and {len(entry.to_add) - 25} more")

    lines.append("\n" + "=" * 72)
    lines.append(f"  TOTAL: {plan.total_to_add} asset(s) would be added")
    new_albums = plan.albums_to_create()
    if new_albums:
        lines.append(f"  New albums to create: {', '.join(new_albums)}")
    if plan.errors:
        lines.append(f"  {len(plan.errors)} rule(s) failed")
    return "\n".join(lines)


def _thumb_data_uri(client: ImmichClient, asset_id: str) -> str | None:
    try:
        payload, content_type = client.thumbnail(asset_id, size="thumbnail")
    except ImmichError:
        return None
    if not payload:
        return None
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{content_type.split(';')[0]};base64,{encoded}"


_CSS = """
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #16181d; --muted: #61656e;
  --card: #f6f7f9; --line: #e3e5ea; --accent: #2f6df6; --warn: #b3261e;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#14161a; --fg:#e8eaed; --muted:#9aa0a8;
          --card:#1d2026; --line:#2c3038; --accent:#7aa2ff; --warn:#f2b8b5; }
}
* { box-sizing: border-box; }
body { margin:0; padding:24px 16px 64px; background:var(--bg); color:var(--fg);
       font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
.wrap { max-width: 1100px; margin: 0 auto; }
h1 { font-size:1.5rem; margin:0 0 4px; letter-spacing:-0.01em; }
.sub { color:var(--muted); font-size:.875rem; margin-bottom:28px; }
.totals { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:28px; }
.stat { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:10px 14px; min-width:120px; }
.stat b { display:block; font-size:1.375rem; letter-spacing:-0.02em; }
.stat span { color:var(--muted); font-size:.75rem; text-transform:uppercase; letter-spacing:.04em; }
.rule { background:var(--card); border:1px solid var(--line); border-radius:12px;
        padding:18px; margin-bottom:20px; }
.rule h2 { font-size:1.0625rem; margin:0 0 10px; }
.meta { color:var(--muted); font-size:.8125rem; margin-bottom:14px; }
.meta code { background:var(--bg); border:1px solid var(--line); border-radius:4px;
             padding:1px 5px; font-size:.8125rem; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(104px,1fr)); gap:8px; }
.tile { position:relative; aspect-ratio:1; border-radius:8px; overflow:hidden;
        background:var(--line); }
.tile img { width:100%; height:100%; object-fit:cover; display:block; }
.tile .cap { position:absolute; inset:auto 0 0 0; padding:3px 5px; font-size:.625rem;
             background:rgba(0,0,0,.62); color:#fff; white-space:nowrap;
             overflow:hidden; text-overflow:ellipsis; }
.more { color:var(--muted); font-size:.8125rem; margin-top:12px; }
.failed { border-color:var(--warn); }
.failed h2 { color:var(--warn); }
.note { border-left:3px solid var(--accent); padding:10px 14px; margin-bottom:28px;
        background:var(--card); border-radius:0 8px 8px 0; font-size:.875rem; color:var(--muted); }
@media (max-width:520px) { .grid { grid-template-columns:repeat(auto-fill,minmax(88px,1fr)); } }
"""


def render_html(
    plan: Plan,
    client: ImmichClient | None = None,
    *,
    thumbs_per_rule: int = DEFAULT_THUMBS_PER_RULE,
) -> str:
    """Build a single self-contained HTML page previewing the plan.

    Thumbnails are inlined as data URIs so the file can be copied to a phone
    or opened from anywhere without needing the server or the API key.
    """
    esc = html.escape
    parts: list[str] = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>Immich Organizer plan</title>",
        f"<style>{_CSS}</style></head><body><div class='wrap'>",
        "<h1>Album plan</h1>",
        f"<p class='sub'>{esc(plan.server_url)} &middot; {esc(plan.created_at)}</p>",
    ]

    parts.append(
        "<div class='note'>Nothing has been changed yet. Immich's smart search "
        "returns no similarity score, so these matches are the top-N closest "
        "results &mdash; skim them before applying.</div>"
    )

    new_albums = plan.albums_to_create()
    parts.append(
        "<div class='totals'>"
        f"<div class='stat'><b>{plan.total_to_add}</b><span>assets to add</span></div>"
        f"<div class='stat'><b>{len(plan.entries)}</b><span>rules</span></div>"
        f"<div class='stat'><b>{len(new_albums)}</b><span>new albums</span></div>"
        f"<div class='stat'><b>{len(plan.errors)}</b><span>failed</span></div>"
        "</div>"
    )

    for entry in plan.entries:
        if entry.error:
            parts.append(
                f"<section class='rule failed'><h2>{esc(entry.rule.name)}</h2>"
                f"<p class='meta'>{esc(entry.error)}</p></section>"
            )
            continue

        marker = "new album" if not entry.album_exists else "existing album"
        meta = [
            f"match <code>{esc(entry.rule.describe_match())}</code>",
            f"album <code>{esc(entry.album_name)}</code> ({marker})",
            f"limit {entry.rule.limit}",
        ]
        if entry.rule.refine.all_of:
            meta.append("all_of " + ", ".join(f"<code>{esc(p)}</code>" for p in entry.rule.refine.all_of))
        if entry.rule.refine.none_of:
            meta.append("none_of " + ", ".join(f"<code>{esc(p)}</code>" for p in entry.rule.refine.none_of))
        if entry.already_in_album:
            meta.append(f"{len(entry.already_in_album)} already in album")

        parts.append(
            f"<section class='rule'><h2>{esc(entry.rule.name)} &rarr; "
            f"{len(entry.to_add)} to add</h2>"
            f"<p class='meta'>{' &middot; '.join(meta)}</p>"
        )

        shown = entry.to_add[:thumbs_per_rule] if client else []
        if shown:
            parts.append("<div class='grid'>")
            for asset in shown:
                uri = _thumb_data_uri(client, asset["id"])
                caption = esc(asset.get("originalFileName") or asset.get("id", ""))
                if uri:
                    parts.append(
                        f"<div class='tile'><img loading='lazy' src='{uri}' alt='{caption}'>"
                        f"<div class='cap'>{caption}</div></div>"
                    )
                else:
                    parts.append(f"<div class='tile'><div class='cap'>{caption}</div></div>")
            parts.append("</div>")
        if len(entry.to_add) > len(shown):
            parts.append(
                f"<p class='more'>+ {len(entry.to_add) - len(shown)} more not shown</p>"
            )
        if not entry.to_add:
            parts.append("<p class='more'>Nothing new to add.</p>")
        parts.append("</section>")

    parts.append("</div></body></html>")
    return "".join(parts)


def write_html_report(
    plan: Plan,
    path: str | Path,
    client: ImmichClient | None = None,
    *,
    thumbs_per_rule: int = DEFAULT_THUMBS_PER_RULE,
) -> Path:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(plan, client, thumbs_per_rule=thumbs_per_rule), "utf-8")
    return path
