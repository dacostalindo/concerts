"""concert-digest: critics' picks from Pitchfork & co. that play Belgium soon.

  concert-digest auth        log in to Concertje once (opens the browser)
  concert-digest tools       list the Concertje MCP tools
  concert-digest digest      build the email and send it
  concert-digest watch       process 'Remind me' mails, send on-sale alerts
  concert-digest preview     render the email with sample concerts (no login)
  concert-digest watchlist   show what you're watching
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime

import httpx

from .concertje import Concertje, LoginRequired
from .config import TZ, load_settings
from .critics import SOURCES
from .pipeline import build_picks, run_digest, run_watch
from .render import render_digest
from .sample import SampleConcertje
from .state import State


async def _tools(interactive: bool) -> int:
    settings = load_settings()
    async with Concertje(settings, interactive=interactive) as cj:
        if interactive:
            print("\nLogged in. Tokens saved in", settings.home)
        print("\nConcertje tools:\n" + cj.describe_tools())
        try:
            print("\nSearch tool picked for artist lookups: %s(%s=…)" % cj.search_tool()[:2])
        except RuntimeError as exc:
            print("\n" + str(exc))
    return 0


async def _preview(out: str, sources: list[str] | None) -> int:
    settings = load_settings()
    state = State(settings.home / "preview-state.json")
    async with httpx.AsyncClient(timeout=30) as client:
        picks = await build_picks(settings, SampleConcertje(), client, state, True, sources)
    if not picks:
        print("No reviews found; check your network.")
        return 1
    note = "Preview: the reviews, scores and album links are live, but the concerts are sample data. Run `concert-digest auth` to use Concertje."
    _, html, _ = render_digest(picks, settings, datetime.now(TZ), preview_note=note)
    with open(out, "w") as f:
        f.write(html)
    print("wrote", out)
    return 0


def _watchlist() -> int:
    state = State.load(load_settings().home)
    if not state.watch:
        print("Watchlist is empty.")
    for w in state.watch.values():
        status = "alerted" if w.notified_on_sale else f"on sale {w.on_sale_at or 'TBA'}"
        print(f"#{w.event_id:<8} {w.date}  {w.artist} @ {w.venue}, {w.city}  ({status})")
    return 0


def _login_error(exc: BaseException | None) -> LoginRequired | None:
    """LoginRequired raised inside the MCP transport arrives wrapped in exception groups."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, LoginRequired):
            return exc
        for inner in getattr(exc, "exceptions", ()):
            if found := _login_error(inner):
                return found
        exc = exc.__cause__ or exc.__context__
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="concert-digest", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth")
    sub.add_parser("tools")
    sub.add_parser("watchlist")
    d = sub.add_parser("digest")
    d.add_argument("--dry-run", action="store_true", help="build but don't send or update state")
    d.add_argument("--out", help="also write the HTML to this file")
    d.add_argument("--include-featured", action="store_true", help="repeat shows from earlier digests")
    d.add_argument("--sources", help=f"comma-separated subset of: {','.join(SOURCES)}")
    w = sub.add_parser("watch")
    w.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("preview")
    p.add_argument("--out", default="preview.html")
    p.add_argument("--sources", help=f"comma-separated subset of: {','.join(SOURCES)}")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    for noisy in ("httpx", "httpx2", "mcp"):
        logging.getLogger(noisy).setLevel(logging.INFO if args.verbose else logging.WARNING)
    sources = args.sources.split(",") if getattr(args, "sources", None) else None

    try:
        if args.cmd == "auth":
            return asyncio.run(_tools(interactive=True))
        if args.cmd == "tools":
            return asyncio.run(_tools(interactive=False))
        if args.cmd == "watchlist":
            return _watchlist()
        if args.cmd == "digest":
            return asyncio.run(run_digest(load_settings(), args.dry_run, args.out, args.include_featured, sources))
        if args.cmd == "watch":
            return asyncio.run(run_watch(load_settings(), args.dry_run))
        if args.cmd == "preview":
            return asyncio.run(_preview(args.out, sources))
    except BaseException as exc:
        if login := _login_error(exc):
            print(login, file=sys.stderr)
            return 2
        raise
    return 1


if __name__ == "__main__":
    sys.exit(main())
