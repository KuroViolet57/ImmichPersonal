"""Command line interface for immich-organizer."""

from __future__ import annotations

import argparse
import getpass
import sys
import webbrowser
from pathlib import Path

from . import __version__
from .client import AuthError, ImmichClient, ImmichError
from .config import ENV_KEY, ENV_URL, Settings, config_path, load_settings, save_settings
from .engine import (
    Plan,
    RuleMatch,
    apply_plan,
    build_plan,
    evaluate_rule,
    find_album,
    journal_path,
    read_journal,
    undo_run,
)
from .report import DEFAULT_THUMBS_PER_RULE, render_text, write_html_report
from .rules import Rule, RuleError, load_rules, parse_filters

EXIT_OK, EXIT_ERROR, EXIT_CONFIG = 0, 1, 2


def _echo(message: str = "") -> None:
    print(message, flush=True)


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def build_client(args: argparse.Namespace) -> ImmichClient:
    settings = load_settings()
    if getattr(args, "server", None):
        settings.server_url = args.server
    if getattr(args, "api_key", None):
        settings.api_key = args.api_key
    if not settings.configured:
        raise SystemExit(
            f"Not configured yet. Run `immich-organizer setup`, or set the "
            f"{ENV_URL} and {ENV_KEY} environment variables.\n"
            f"(config file: {config_path()})"
        )
    return ImmichClient(
        settings.server_url,
        settings.api_key,
        timeout=settings.timeout,
        verify_tls=settings.verify_tls,
    )


# ------------------------------------------------------------------- commands


def cmd_setup(args: argparse.Namespace) -> int:
    settings = load_settings()
    _echo("Immich Organizer setup")
    _echo("-" * 40)

    default_url = settings.server_url or "http://localhost:2283"
    url = getattr(args, "server", None) or input(
        f"Immich server URL [{default_url}]: "
    ).strip() or default_url

    if getattr(args, "api_key", None):
        key = args.api_key
    else:
        _echo("\nCreate an API key in Immich: click your avatar -> Account Settings")
        _echo("-> API Keys -> New API Key. It is shown only once.")
        key = getpass.getpass("API key (input hidden): ").strip()
    if not key:
        _echo("No API key given; nothing saved.")
        return EXIT_CONFIG

    settings.server_url = url
    settings.api_key = key
    if args.insecure:
        settings.verify_tls = False

    client = ImmichClient(url, key, verify_tls=settings.verify_tls)
    _echo(f"\nChecking {client.base_url} ...")
    try:
        user = client.me()
        about = client.about()
    except AuthError as exc:
        _echo(f"  {exc}")
        return EXIT_CONFIG
    except ImmichError as exc:
        _echo(f"  {exc}")
        return EXIT_CONFIG

    _echo(f"  connected as {user.get('email') or user.get('name') or 'unknown user'}")
    _echo(f"  Immich version {about.get('version', 'unknown')}")
    path = save_settings(settings)
    _echo(f"\nSaved to {path}")
    _echo("Next: `immich-organizer doctor` to confirm smart search is ready.")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    client = build_client(args)
    problems = 0
    _echo(f"Server:  {client.base_url}")

    try:
        client.ping()
        _echo("  [ok]   reachable")
    except ImmichError as exc:
        _echo(f"  [FAIL] unreachable: {exc}")
        return EXIT_ERROR

    try:
        user = client.me()
        _echo(f"  [ok]   authenticated as {user.get('email') or user.get('name')}")
    except ImmichError as exc:
        _echo(f"  [FAIL] auth: {exc}")
        return EXIT_ERROR

    try:
        about = client.about()
        _echo(f"  [ok]   Immich {about.get('version', '?')}")
    except ImmichError as exc:
        _echo(f"  [warn] version unavailable: {exc}")

    # Smart search is the whole point of this tool, so prove it works and that
    # the machine-learning job has actually produced embeddings.
    try:
        found = client.search_smart({"query": "a photograph", "size": 1})
        total = ((found.get("assets") or {}).get("items") or [])
        if total:
            _echo("  [ok]   smart search returns results (CLIP embeddings exist)")
        else:
            problems += 1
            _echo(
                "  [warn] smart search returned nothing. If your library is not "
                "empty, run Administration -> Jobs -> Smart Search to build embeddings."
            )
    except ImmichError as exc:
        problems += 1
        _echo(f"  [FAIL] smart search: {exc}")

    try:
        sample = client.search_metadata({"size": 1, "withExif": False})
        items = (sample.get("assets") or {}).get("items") or []
        if items:
            asset_id = items[0]["id"]
            client.search_smart({"queryAssetId": asset_id, "size": 1})
            _echo("  [ok]   similar-asset search works (queryAssetId accepted)")
        else:
            _echo("  [warn] library looks empty; skipped similar-asset check")
    except ImmichError as exc:
        problems += 1
        _echo(f"  [FAIL] similar-asset search: {exc}")

    try:
        albums = client.list_albums()
        _echo(f"  [ok]   {len(albums)} album(s) visible")
    except ImmichError as exc:
        problems += 1
        _echo(f"  [FAIL] albums: {exc}")

    _echo("\nAll good." if not problems else f"\n{problems} thing(s) need attention.")
    return EXIT_OK if not problems else EXIT_ERROR


def cmd_albums(args: argparse.Namespace) -> int:
    client = build_client(args)
    albums = sorted(client.list_albums(), key=lambda a: (a.get("albumName") or "").casefold())
    if not albums:
        _echo("No albums yet.")
        return EXIT_OK
    width = max(len(a.get("albumName") or "") for a in albums)
    _echo(f"{'ALBUM'.ljust(width)}  {'ASSETS':>6}  ID")
    for album in albums:
        _echo(
            f"{(album.get('albumName') or '').ljust(width)}  "
            f"{album.get('assetCount', 0):>6}  {album.get('id')}"
        )
    return EXIT_OK


def _ad_hoc_filters(args: argparse.Namespace) -> dict:
    raw = {}
    if args.type:
        raw["type"] = args.type
    if args.taken_after:
        raw["taken_after"] = args.taken_after
    if args.taken_before:
        raw["taken_before"] = args.taken_before
    if args.unfiled:
        raw["only_unfiled"] = True
    if args.favorite:
        raw["favorite"] = True
    if args.city:
        raw["city"] = args.city
    return parse_filters(raw, "options")


def cmd_search(args: argparse.Namespace) -> int:
    client = build_client(args)
    rule = Rule(
        name="ad-hoc",
        album=args.album or "(preview only)",
        query=args.query,
        like_asset=args.like,
        limit=args.limit,
        filters=_ad_hoc_filters(args),
    )
    matches = evaluate_rule(client, rule, progress=_progress if args.verbose else (lambda _: None))

    if not matches:
        _echo("No matches.")
        return EXIT_OK

    _echo(f"{len(matches)} match(es) for {rule.describe_match()}:\n")
    for i, asset in enumerate(matches, 1):
        taken = (asset.get("localDateTime") or asset.get("fileCreatedAt") or "")[:10]
        _echo(
            f"{i:>4}. {taken}  {asset.get('type', ''):<5} "
            f"{(asset.get('originalFileName') or '')[:44]:<44} {asset.get('id')}"
        )

    if args.html:
        entry = RuleMatch(
            rule=rule, album_name=rule.album, album_exists=bool(args.album),
            matched=matches, to_add=matches,
        )
        plan = Plan(entries=[entry], server_url=client.base_url)
        path = write_html_report(plan, args.html, client, thumbs_per_rule=args.limit)
        _echo(f"\nPreview written to {path}")
        if args.open:
            webbrowser.open(path.resolve().as_uri())

    if args.album:
        if not args.apply:
            _echo(f"\nDry run. Re-run with --apply to add these to album {args.album!r}.")
            return EXIT_OK
        albums = client.list_albums()
        album = find_album(albums, args.album)
        if album is None:
            album = client.create_album(args.album, description="Created by immich-organizer")
            _echo(f"\nCreated album {args.album!r}")
        responses = client.add_assets_to_album(album["id"], [a["id"] for a in matches])
        added = sum(1 for r in responses if r.get("success"))
        dupes = sum(1 for r in responses if r.get("error") == "duplicate")
        _echo(f"\nAdded {added} asset(s) to {args.album!r} ({dupes} already there).")
    return EXIT_OK


def cmd_validate(args: argparse.Namespace) -> int:
    ruleset = load_rules(args.rules)
    _echo(f"{args.rules}: OK - {len(ruleset.rules)} rule(s)")
    for rule in ruleset.rules:
        state = "" if rule.enabled else "  (disabled)"
        _echo(f"  - {rule.name}: {rule.describe_match()} -> {rule.album!r}, limit {rule.limit}{state}")
    return EXIT_OK


def _make_plan(args: argparse.Namespace, client: ImmichClient) -> Plan:
    ruleset = load_rules(args.rules)
    return build_plan(
        client,
        ruleset,
        only=args.only or None,
        progress=_progress if not args.quiet else (lambda _: None),
    )


def _emit_report(args: argparse.Namespace, client: ImmichClient, plan: Plan) -> None:
    _echo(render_text(plan, verbose=args.verbose))
    if args.html:
        path = write_html_report(plan, args.html, client, thumbs_per_rule=args.thumbs)
        _echo(f"\nHTML preview: {path}")
        if args.open:
            webbrowser.open(path.resolve().as_uri())


def cmd_plan(args: argparse.Namespace) -> int:
    client = build_client(args)
    plan = _make_plan(args, client)
    _emit_report(args, client, plan)
    _echo("\nNothing changed. Run `apply` to carry this out.")
    return EXIT_ERROR if plan.errors else EXIT_OK


def cmd_apply(args: argparse.Namespace) -> int:
    client = build_client(args)
    plan = _make_plan(args, client)
    _emit_report(args, client, plan)

    if plan.errors and not args.ignore_errors:
        _echo(f"\n{len(plan.errors)} rule(s) failed. Fix them, or pass --ignore-errors.")
        return EXIT_ERROR
    if not plan.total_to_add and not plan.albums_to_create():
        _echo("\nNothing to do.")
        return EXIT_OK

    if not args.yes:
        _echo("")
        answer = input(f"Add {plan.total_to_add} asset(s) as shown above? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            _echo("Aborted; nothing changed.")
            return EXIT_OK

    result = apply_plan(client, plan, progress=_progress if not args.quiet else (lambda _: None))
    _echo("")
    _echo(f"Added {result.total_added} asset(s) across {len(result.added)} album(s).")
    if result.created_albums:
        _echo(f"Created album(s): {', '.join(result.created_albums)}")
    if result.skipped:
        total_skipped = sum(result.skipped.values())
        _echo(f"Skipped {total_skipped} already-present asset(s).")
    if result.archived:
        _echo(f"Archived {result.archived} asset(s).")
    if result.favorited:
        _echo(f"Favorited {result.favorited} asset(s).")
    if result.failures:
        _echo(f"\n{len(result.failures)} failure(s):")
        for failure in result.failures[:20]:
            _echo(f"  - {failure}")
    if result.total_added:
        _echo(f"\nRun id {result.run_id} - undo with `immich-organizer undo`.")
    return EXIT_ERROR if result.failures else EXIT_OK


def cmd_history(args: argparse.Namespace) -> int:
    records = read_journal(limit=args.limit)
    if not records:
        _echo(f"No runs recorded yet ({journal_path()}).")
        return EXIT_OK
    for record in records:
        total = sum(len(v) for v in (record.get("added") or {}).values())
        _echo(f"{record.get('timestamp')}  run {record.get('run_id')}  +{total} asset(s)")
        for album, ids in (record.get("added") or {}).items():
            _echo(f"    {album}: {len(ids)}")
    return EXIT_OK


def cmd_undo(args: argparse.Namespace) -> int:
    client = build_client(args)
    records = read_journal(limit=200)
    if not records:
        _echo("Nothing to undo.")
        return EXIT_OK

    if args.run_id:
        matching = [r for r in records if r.get("run_id") == args.run_id]
        if not matching:
            _echo(f"No run with id {args.run_id!r}. See `immich-organizer history`.")
            return EXIT_ERROR
        record = matching[-1]
    else:
        record = records[-1]

    total = sum(len(v) for v in (record.get("added") or {}).values())
    _echo(f"Run {record.get('run_id')} ({record.get('timestamp')}) added {total} asset(s):")
    for album, ids in (record.get("added") or {}).items():
        _echo(f"  {album}: {len(ids)}")
    if record.get("created_albums"):
        _echo(
            f"\nAlbums created by that run ({', '.join(record['created_albums'])}) "
            "will be emptied but not deleted."
        )

    if not args.yes:
        answer = input("\nRemove these assets from those albums? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            _echo("Aborted; nothing changed.")
            return EXIT_OK

    removed, failures = undo_run(client, record, progress=_progress)
    _echo(f"\nRemoved {removed} asset(s) from their albums.")
    if failures:
        _echo(f"{len(failures)} problem(s):")
        for failure in failures[:20]:
            _echo(f"  - {failure}")
    return EXIT_ERROR if failures else EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    from .web.server import serve

    client = build_client(args)
    return serve(
        client,
        host=args.host,
        port=args.port,
        rules_path=Path(args.rules).expanduser() if args.rules else None,
        open_browser=args.open,
    )


# --------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="immich-organizer",
        description="Organize Immich photos into albums using its smart-search AI.",
    )
    parser.add_argument("--version", action="version", version=f"immich-organizer {__version__}")
    parser.add_argument("--server", help=f"Immich URL (overrides config and ${ENV_URL})")
    parser.add_argument("--api-key", help=f"API key (overrides config and ${ENV_KEY})")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_rules_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("-r", "--rules", default="rules.yaml", help="rules file (default: rules.yaml)")
        sp.add_argument("--only", action="append", metavar="NAME", help="run only this rule (repeatable)")
        sp.add_argument("--html", metavar="PATH", help="write an HTML preview with thumbnails")
        sp.add_argument("--thumbs", type=int, default=DEFAULT_THUMBS_PER_RULE,
                        help=f"thumbnails per rule in the HTML preview (default: {DEFAULT_THUMBS_PER_RULE})")
        sp.add_argument("--open", action="store_true", help="open the HTML preview in a browser")
        sp.add_argument("-v", "--verbose", action="store_true", help="list matched filenames")
        sp.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")

    p = sub.add_parser("setup", help="store the server URL and API key")
    p.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed certs)")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("doctor", help="check connectivity and that smart search is ready")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("albums", help="list albums with their ids")
    p.set_defaults(func=cmd_albums)

    p = sub.add_parser("search", help="run a one-off search, optionally filing the results")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--query", help='natural language, e.g. "person in a mountain"')
    group.add_argument("--like", metavar="ASSET_ID", help="find assets similar to this one")
    p.add_argument("--limit", type=int, default=50, help="max results (default: 50)")
    p.add_argument("--album", help="file the results into this album")
    p.add_argument("--apply", action="store_true", help="actually add them (default is a dry run)")
    p.add_argument("--type", choices=["IMAGE", "VIDEO"], help="restrict to photos or videos")
    p.add_argument("--taken-after", help="YYYY-MM-DD or a relative offset like -30d")
    p.add_argument("--taken-before", help="YYYY-MM-DD or a relative offset like -30d")
    p.add_argument("--unfiled", action="store_true", help="only assets not in any album")
    p.add_argument("--favorite", action="store_true", help="only favorites")
    p.add_argument("--city", help="only assets taken in this city")
    p.add_argument("--html", metavar="PATH", help="write an HTML preview with thumbnails")
    p.add_argument("--open", action="store_true", help="open the HTML preview in a browser")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("validate", help="check a rules file without contacting the server")
    p.add_argument("-r", "--rules", default="rules.yaml")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("plan", help="preview what the rules would do (changes nothing)")
    add_rules_opts(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("apply", help="carry out the rules after confirmation")
    add_rules_opts(p)
    p.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--ignore-errors", action="store_true", help="apply the rules that did succeed")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("history", help="show previous apply runs")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("undo", help="remove the assets added by a previous run")
    p.add_argument("--run-id", help="undo this run instead of the most recent one")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(func=cmd_undo)

    p = sub.add_parser("serve", help="start the mobile-friendly web UI")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address; use 0.0.0.0 to reach it from your phone (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8777)
    p.add_argument("-r", "--rules", default=None, help="rules file the UI should load")
    p.add_argument("--open", action="store_true", help="open the UI in a browser")
    p.set_defaults(func=cmd_serve)

    # Accept the connection flags after the subcommand too, so both
    # `immich-organizer --server X albums` and `immich-organizer albums --server X`
    # work. SUPPRESS keeps an omitted flag from clobbering the parent's value.
    for subparser in sub.choices.values():
        subparser.add_argument("--server", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        subparser.add_argument("--api-key", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RuleError as exc:
        _echo(f"Rules file problem -> {exc}")
        return EXIT_CONFIG
    except AuthError as exc:
        _echo(str(exc))
        return EXIT_CONFIG
    except ImmichError as exc:
        _echo(f"Immich error: {exc}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        _echo("\nInterrupted.")
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
