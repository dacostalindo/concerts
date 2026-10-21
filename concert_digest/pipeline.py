"""The two jobs: build/send the digest, and run the watchlist."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from datetime import datetime, timedelta

import httpx

from . import mailer
from .concertje import Concertje, upcoming
from .config import TZ, Settings
from .critics import fetch_reviews
from .enrich import Spotify, enrich
from .models import Concert, Pick, Review, normalize_name
from .render import render_alert, render_digest
from .state import State, Watch

log = logging.getLogger(__name__)


def group_reviews(reviews: list[Review]) -> "OrderedDict[str, list[Review]]":
    groups: OrderedDict[str, list[Review]] = OrderedDict()
    for r in sorted(reviews, key=lambda r: (r.score is None, -(r.score or 0))):
        key = normalize_name(r.artist)
        if len(key) >= 2:  # names like "@" can't be matched reliably
            groups.setdefault(key, []).append(r)
    return groups


async def find_concerts(cj: Concertje, artist: str, today) -> list[Concert]:
    found = upcoming(await cj.concerts_for(artist), artist, today)
    if not found and " & " in artist:  # collaborations: try each name
        for part in artist.split(" & "):
            found += upcoming(await cj.concerts_for(part.strip()), part.strip(), today)
    return found


def merge_genres(p: Pick) -> list[str]:
    seen, out = set(), []
    for g in [g for c in p.concerts for g in c.genres] + [g for r in p.reviews for g in r.genres]:
        key = normalize_name(g)
        if key and key not in seen:
            seen.add(key)
            out.append(g.title() if g.islower() else g)
    return out[:4]


async def build_picks(settings: Settings, cj: Concertje, client: httpx.AsyncClient, state: State,
                      include_featured: bool, sources: list[str] | None) -> list[Pick]:
    now = datetime.now(TZ)
    reviews = await fetch_reviews(client, settings.lookback_days, sources)
    groups = group_reviews(reviews)
    log.info("%d reviews, %d distinct artists; checking Concertje…", len(reviews), len(groups))

    sem = asyncio.Semaphore(4)

    async def check(rs: list[Review]) -> Pick | None:
        async with sem:
            try:
                concerts = await find_concerts(cj, rs[0].artist, now.date())
            except Exception as exc:
                log.warning("Concertje lookup failed for %s: %s", rs[0].artist, exc)
                return None
        if not include_featured:
            concerts = [c for c in concerts if c.id not in state.featured]
        if not concerts:
            return None
        log.info("  ✓ %s: %d show(s)", rs[0].artist, len(concerts))
        return Pick(artist=rs[0].artist, reviews=rs, concerts=concerts)

    picks = [p for p in await asyncio.gather(*(check(rs) for rs in groups.values())) if p]
    await enrich(client, picks, Spotify(client, settings.spotify_client_id, settings.spotify_client_secret))
    for p in picks:
        p.genres = merge_genres(p)
    picks.sort(key=lambda p: (p.score is None, -(p.score or 0), -len(p.reviews), p.concerts[0].date))
    return picks


async def run_digest(settings: Settings, dry_run: bool, out: str | None, include_featured: bool,
                     sources: list[str] | None) -> int:
    state = State.load(settings.home)
    async with httpx.AsyncClient(timeout=30) as client, Concertje(settings) as cj:
        picks = await build_picks(settings, cj, client, state, include_featured, sources)
    if not picks:
        log.info("No reviewed artists with new Belgian shows this time. Nothing sent.")
        return 0
    now = datetime.now(TZ)
    subject, html, text = render_digest(picks, settings, now)
    if out:
        with open(out, "w") as f:
            f.write(html)
        log.info("wrote %s", out)
    if dry_run:
        log.info("dry run: not sending %r", subject)
        return 0
    mailer.send(settings, subject, html, text)
    for p in picks:
        for c in p.concerts:
            state.featured.setdefault(c.id, {"sent": now.date().isoformat(), "artist": p.artist})
    state.save()
    return 0


# --- Watchlist ---------------------------------------------------------------


def watch_from(c: Concert, artist: str, now: datetime) -> Watch:
    return Watch(
        event_id=c.id, artist=artist, title=c.title, date=c.date.isoformat(), venue=c.venue, city=c.city,
        added=now.isoformat(timespec="minutes"), concertje_url=c.concertje_url, ticket_url=c.ticket_url,
        on_sale_at=c.on_sale_at.isoformat() if c.on_sale_at else "",
    )


async def run_watch(settings: Settings, dry_run: bool) -> int:
    state = State.load(settings.home)
    now = datetime.now(TZ)
    outbox: list[tuple[str, str, str]] = []
    requests = mailer.fetch_watch_requests(settings, set(state.processed_messages))

    async with Concertje(settings) as cj:
        # 1. New WATCH / UNWATCH requests.
        for req in requests:
            state.processed_messages.append(req.message_id)
            if req.unwatch:
                gone = state.watch.pop(req.event_id, None)
                log.info("unwatch #%s (%s)", req.event_id, gone.artist if gone else "unknown")
                continue
            if req.event_id in state.watch:
                continue
            concert, artist = await _locate(cj, req.event_id, req.artist, state)
            if not concert:
                log.warning("watch #%s: show not found on Concertje", req.event_id)
                continue
            w = watch_from(concert, artist, now)
            if concert.is_on_sale(now):
                w.notified_on_sale = True
                outbox.append(render_alert("already_on_sale", w, settings))
            else:
                outbox.append(render_alert("confirm", w, settings))
            state.watch[w.event_id] = w
            log.info("watching #%s %s @ %s", w.event_id, w.artist, w.venue)

        # 2. Refresh every watched show and send what's due.
        for event_id, w in list(state.watch.items()):
            if w.date < now.date().isoformat():
                state.watch.pop(event_id)
                continue
            if w.notified_on_sale:
                continue
            fresh = next((c for c in await cj.concerts_for(w.artist) if c.id == event_id), None)
            if fresh:
                w.ticket_url = fresh.ticket_url or w.ticket_url
                w.on_sale_at = fresh.on_sale_at.isoformat() if fresh.on_sale_at else w.on_sale_at
                presale = next((p for p in fresh.presales if p.starts_at and now < p.starts_at <= now + timedelta(hours=24)), None)
            else:
                presale = None
            on_sale_at = datetime.fromisoformat(w.on_sale_at) if w.on_sale_at else None
            if (fresh and fresh.is_on_sale(now)) or (on_sale_at and on_sale_at <= now):
                w.notified_on_sale = True
                outbox.append(render_alert("on_sale", w, settings))
            elif not w.notified_heads_up and on_sale_at and on_sale_at - now <= timedelta(hours=24):
                w.notified_heads_up = True
                extra = f"{presale.name} presale starts {presale.starts_at:%a %d %b %H:%M}." if presale else ""
                outbox.append(render_alert("heads_up", w, settings, extra))

    for subject, html, text in outbox:
        if dry_run:
            log.info("dry run: would send %r", subject)
        else:
            mailer.send(settings, subject, html, text)
    if not dry_run:
        state.save()
    return 0


async def _locate(cj: Concertje, event_id: str, artist_hint: str, state: State) -> tuple[Concert | None, str]:
    """Find a show by id, searching by the artist we featured it under."""
    artists = [state.featured.get(event_id, {}).get("artist", ""), artist_hint]
    for artist in dict.fromkeys(a for a in artists if a):
        for c in await cj.concerts_for(artist):
            if c.id == event_id:
                return c, artist
    return None, ""
