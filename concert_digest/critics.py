"""Scrapers for music critics' album reviews.

Each source reads the site's public RSS feed and, where the score is not in
the feed, the review page itself. Feed URLs and page markup are the parts
most likely to change; every parser fails soft (logs and skips the review).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import feedparser
import httpx
from bs4 import BeautifulSoup

from .models import Review

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36"
)
DASH = r"\s+[–—-]\s+"
QUOTES = "‘’“”'\""
GENERIC_TAGS = {"music", "culture", "reviews", "album reviews", "news", "album of the week"}


def _text(fragment: str) -> str:
    text = re.sub(r"\s+", " ", BeautifulSoup(fragment or "", "html.parser").get_text(" ")).strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", text)


def _published(entry) -> datetime | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def _tags(entry) -> list[str]:
    return [t.get("term", "").strip() for t in entry.get("tags", []) if t.get("term")]


GENRE_WORDS = (
    "pop", "rock", "rap", "hip-hop", "hip hop", "r&b", "soul", "jazz", "electronic", "dance", "folk",
    "country", "metal", "punk", "indie", "experimental", "psych", "ambient", "blues", "reggae",
    "techno", "house", "alternative", "singer-songwriter", "funk", "gospel", "latin", "afrobeat",
    "grime", "drill", "shoegaze", "emo", "hardcore", "noise", "drum and bass", "dubstep", "disco",
    "americana", "world", "garage", "trap", "k-pop", "post-",
)


def _genres(tags: list[str], *exclude: str) -> list[str]:
    """Keep only tags that look like genres ('Dance music' -> 'Dance')."""
    skip = GENERIC_TAGS | {e.casefold() for e in exclude}
    out = []
    for t in tags:
        low = t.casefold()
        if low in skip or not any(w in low for w in GENRE_WORDS):
            continue
        out.append(re.sub(r"\s+music$", "", t, flags=re.I))
    return out


def _names(tags: list[str]) -> list[str]:
    """Tags that are not genres or site sections, i.e. probably people/bands."""
    return [t for t in tags if t.casefold() not in GENERIC_TAGS and not _genres([t])]


def _recent(entries, days: int):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return [e for e in entries if (_published(e) or cutoff) >= cutoff]


def _image(entry) -> str:
    for key in ("media_thumbnail", "media_content"):
        if entry.get(key):
            return entry[key][0].get("url", "")
    m = re.search(r'<img[^>]+src="([^"]+)"', entry.get("summary", ""))
    return m.group(1) if m else ""


def _json_after(page: str, key: str):
    """Decode the JSON value that follows the first `"key":` in a page."""
    i = page.find(f'"{key}":')
    if i < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(page, i + len(key) + 3)
        return value
    except ValueError:
        return None


def _stars(n: float, out_of: int = 5) -> str:
    full = int(n)
    half = "½" if n - full >= 0.5 else ""
    return "★" * full + half + "☆" * (out_of - full - (1 if half else 0))


class Fetcher:
    def __init__(self, client: httpx.AsyncClient, concurrency: int = 6):
        self.client = client
        self.sem = asyncio.Semaphore(concurrency)

    async def get(self, url: str) -> str:
        async with self.sem:
            r = await self.client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
            r.raise_for_status()
            return r.text

    async def feed(self, url: str):
        return feedparser.parse(await self.get(url))


# --- Sources ---------------------------------------------------------------


async def pitchfork(f: Fetcher) -> list[Review]:
    feed = await f.feed("https://pitchfork.com/feed/feed-album-reviews/rss")

    async def one(e) -> Review | None:
        page = await f.get(e.link)
        artists = _json_after(page, "artists") or []
        rating = _json_after(page, "musicRating") or {}
        if not artists:
            return None
        score = rating.get("score")
        accolade = (
            "Best New Music" if rating.get("isBestNewMusic")
            else "Best New Reissue" if rating.get("isBestNewReissue") else ""
        )
        genres = [g["node"]["name"] for a in artists for g in a.get("genres", []) if g.get("node")]
        return Review(
            source="Pitchfork",
            artist=" & ".join(a["name"] for a in artists),
            album=html.unescape(e.title),
            url=e.link,
            published=_published(e),
            blurb=_clip(re.sub(r"^Each Sunday, Pitchfork .*?\. Today, we [a-z]+ ", "", _text(e.get("summary", "")))),
            score=float(score) if score is not None else None,
            score_label=f"{float(score):.1f}" if score is not None else "",
            accolade=accolade,
            genres=[g for genre in genres for g in genre.split("/")],
            image=_image(e),
        )

    return await _gather(one, feed.entries, "Pitchfork")


async def stereogum(f: Fetcher) -> list[Review]:
    feed = await f.feed("https://www.stereogum.com/category/reviews/album-of-the-week/feed/")

    async def one(e) -> Review | None:
        m = re.search(r"Album Of The Week:\s*(.+?)\s*<em>(.+?)</em>", html.unescape(e.get("summary", "")))
        if not m:
            return None
        page = await f.get(e.link)
        meta = re.search(r'<meta (?:property|name)="(?:og:)?description" content="([^"]+)"', page)
        return Review(
            source="Stereogum", artist=_text(m.group(1)), album=_text(m.group(2)), url=e.link,
            published=_published(e), blurb=_clip(html.unescape(meta.group(1))) if meta else "",
            score_label="Album of the Week", accolade="Album of the Week", image=_image(e),
        )

    return await _gather(one, _recent(feed.entries, 45), "Stereogum")


async def guardian(f: Fetcher) -> list[Review]:
    feed = await f.feed("https://www.theguardian.com/music+tone/albumreview/rss")
    out = []
    for e in feed.entries:
        tags = _tags(e)
        if "Classical music" in tags:
            continue
        m = re.match(r"^(.+?):\s*(.+?)\s+(?:album\s+)?review\s*[–—-]\s*(.*)$", html.unescape(e.title))
        if not m:
            continue
        artist, album, tagline = m.groups()
        out.append(Review(
            source="The Guardian", artist=artist.strip(), album=album.strip(QUOTES + " "), url=e.link,
            published=_published(e), blurb=_clip(tagline[:1].upper() + tagline[1:]),
            genres=_genres(tags, artist, "Pop and rock"), image=_image(e),
        ))
    return out


async def nme(f: Fetcher) -> list[Review]:
    feed = await f.feed("https://www.nme.com/reviews/album/feed")

    async def one(e) -> Review | None:
        m = re.match(rf"^(.+?){DASH}[{QUOTES}](.+?)[{QUOTES}]\s+review:?\s*(.*)$", html.unescape(e.title))
        if not m:
            return None
        artist, album, tagline = m.groups()
        page = await f.get(e.link)
        stars = re.findall(r"lucide-star size-7 (fill-current|fill-none)", page)[:5]
        score = stars.count("fill-current") if len(stars) == 5 else None
        return Review(
            source="NME", artist=artist, album=album, url=e.link, published=_published(e),
            blurb=_clip(tagline[:1].upper() + tagline[1:]),
            score=score * 2.0 if score else None, score_label=_stars(score) if score else "",
            genres=_genres(_tags(e)), image=_image(e),
        )

    return await _gather(one, feed.entries, "NME")


async def rolling_stone(f: Fetcher) -> list[Review]:
    feed = await f.feed("https://www.rollingstone.com/music/music-album-reviews/feed/")

    async def one(e) -> Review | None:
        names = _names(_tags(e))
        if not names:
            return None
        page = await f.get(e.link)
        block = page[page.find("article-review__stars"):][:6000] if "article-review__stars" in page else ""
        full = block.count("article-review__star--active")
        half = block.count("article-review__star--half")
        score = full + 0.5 * half if block else None
        album = re.search(rf"[{QUOTES}]([^{QUOTES}]{{2,80}})[{QUOTES}]", html.unescape(e.title)) or re.search(
            r"\b(?:On|With)\s+([A-Z][^,]{1,60}?)\s*,", _text(e.get("summary", ""))
        )
        return Review(
            source="Rolling Stone", artist=names[-1], album=album.group(1).strip() if album else "",
            url=e.link, published=_published(e), blurb=_clip(_text(e.get("summary", ""))),
            score=score * 2 if score else None, score_label=_stars(score) if score else "",
            image=_image(e),
        )

    return await _gather(one, _recent(feed.entries, 45), "Rolling Stone")


def _artist_dash_album(source: str, feed_url: str, need_tag: str | None = None):
    async def scrape(f: Fetcher) -> list[Review]:
        feed = await f.feed(feed_url)
        out = []
        for e in feed.entries:
            tags = _tags(e)
            if need_tag and need_tag not in tags:
                continue
            parts = re.split(DASH, html.unescape(e.title), maxsplit=1)
            if len(parts) != 2:
                continue
            artist, album = (p.strip(QUOTES + " ") for p in parts)
            blurb = _text(e.get("summary", ""))
            blurb = blurb.removeprefix(artist).strip().removeprefix(album).strip()
            out.append(Review(
                source=source, artist=artist, album=album, url=e.link, published=_published(e),
                blurb=_clip(blurb), genres=_genres(tags, artist), image=_image(e),
            ))
        return out

    return scrape


SOURCES: dict[str, Callable[[Fetcher], Awaitable[list[Review]]]] = {
    "pitchfork": pitchfork,
    "stereogum": stereogum,
    "guardian": guardian,
    "nme": nme,
    "rollingstone": rolling_stone,
    "quietus": _artist_dash_album("The Quietus", "https://thequietus.com/feed", need_tag="Reviews"),
    "clash": _artist_dash_album("Clash", "https://www.clashmusic.com/reviews/feed/"),
}


# --- Helpers ---------------------------------------------------------------


def _clip(text: str, limit: int = 320) -> str:
    text = re.sub(r"\s*The post .* appeared first on .*$", "", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;:–— ") + "…"


async def _gather(fn, entries, source: str) -> list[Review]:
    async def safe(e):
        try:
            return await fn(e)
        except Exception as exc:  # one broken review shouldn't sink the source
            log.warning("%s: skipping %s (%s)", source, e.get("link"), exc)
            return None

    return [r for r in await asyncio.gather(*(safe(e) for e in entries)) if r]


async def fetch_reviews(
    client: httpx.AsyncClient, lookback_days: int, sources: list[str] | None = None
) -> list[Review]:
    f = Fetcher(client)
    names = sources or list(SOURCES)
    results = await asyncio.gather(*(SOURCES[n](f) for n in names), return_exceptions=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    reviews: list[Review] = []
    for name, res in zip(names, results):
        if isinstance(res, Exception):
            log.warning("source %s failed: %s", name, res)
            continue
        fresh = [r for r in res if not r.published or r.published >= cutoff]
        log.info("%-12s %d reviews (%d in the last %d days)", name, len(res), len(fresh), lookback_days)
        reviews.extend(fresh)
    return reviews
