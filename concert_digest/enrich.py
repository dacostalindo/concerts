"""Album links (Apple Music, Spotify) and a short artist bio (Wikipedia)."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from urllib.parse import quote

import httpx

from .models import Album, Pick, normalize_name

log = logging.getLogger(__name__)

MUSIC_WORDS = (
    "band", "singer", "rapper", "musician", "songwriter", "duo", "group", "producer", "dj",
    "composer", "trio", "quartet", "artist", "ensemble", "collective", "guitarist", "drummer",
)


class Spotify:
    """Client-credentials Spotify search; falls back to search links without keys."""

    def __init__(self, client: httpx.AsyncClient, client_id: str, client_secret: str):
        self.client = client
        self.creds = (client_id, client_secret) if client_id and client_secret else None
        self._token: tuple[str, float] | None = None

    async def _auth(self) -> str | None:
        if not self.creds:
            return None
        if self._token and self._token[1] > time.time() + 30:
            return self._token[0]
        basic = base64.b64encode(f"{self.creds[0]}:{self.creds[1]}".encode()).decode()
        r = await self.client.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {basic}"},
        )
        r.raise_for_status()
        body = r.json()
        self._token = (body["access_token"], time.time() + body.get("expires_in", 3600))
        return self._token[0]

    async def search(self, q: str, kind: str) -> dict | None:
        token = await self._auth()
        if not token:
            return None
        r = await self.client.get(
            "https://api.spotify.com/v1/search",
            params={"q": q, "type": kind, "limit": 5, "market": "BE"},
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
        return r.json()

    @staticmethod
    def search_url(q: str) -> str:
        return f"https://open.spotify.com/search/{quote(q)}"


def _edition_free(title: str) -> str:
    return normalize_name(re.sub(r"\s*[\(\[].*?[\)\]]|\s+-\s+(EP|Single)$", "", title))


async def _itunes(client: httpx.AsyncClient, term: str, artist: str) -> list[dict]:
    r = await client.get(
        "https://itunes.apple.com/search",
        params={"term": term, "entity": "album", "country": "BE", "limit": 50},
    )
    r.raise_for_status()
    target = normalize_name(artist)
    return [x for x in r.json().get("results", []) if normalize_name(x.get("artistName", "")) == target]


def _album(x: dict) -> Album:
    return Album(
        title=x["collectionName"],
        apple_music_url=x["collectionViewUrl"].split("?")[0],
        artwork=x.get("artworkUrl100", "").replace("100x100bb", "600x600bb"),
        year=x.get("releaseDate", "")[:4],
    )


async def apple_albums(
    client: httpx.AsyncClient, artist: str, wanted: list[str], covers: dict[str, str] | None = None
) -> tuple[list[Album], str]:
    """The reviewed album(s) first, then the artist's latest full-lengths.

    `covers` maps reviewed titles to cover art from the review, used when Apple lacks the album.
    """
    catalogue = await _itunes(client, artist, artist)
    albums: list[Album] = []
    seen: set[str] = set()
    for title in wanted:
        key = _edition_free(title)
        if not key or key in seen:
            continue
        hits = [x for x in catalogue if _edition_free(x["collectionName"]) == key]
        if not hits:
            hits = [x for x in await _itunes(client, f"{artist} {title}", artist)
                    if _edition_free(x["collectionName"]) == key]
        # Too new for Apple's search index? Still list it, with search links.
        albums.append(_album(hits[0]) if hits else Album(
            title=title,
            apple_music_url=f"https://music.apple.com/be/search?term={quote(artist + ' ' + title)}",
            artwork=next((img for t, img in (covers or {}).items() if _edition_free(t) == key), ""),
        ))
        seen.add(key)
    catalogue.sort(key=lambda x: x.get("releaseDate", ""), reverse=True)
    for x in catalogue:
        key = _edition_free(x["collectionName"])
        if key in seen or re.search(r"\s-\s(EP|Single)$", x["collectionName"]) or x.get("trackCount", 0) < 6:
            continue
        seen.add(key)
        albums.append(_album(x))
    artist_url = catalogue[0]["artistViewUrl"].split("?")[0] if catalogue else ""
    return albums[:3], artist_url


async def wikipedia_bio(client: httpx.AsyncClient, artist: str) -> str:
    for title in (f"{artist} (band)", f"{artist} (musician)", f"{artist} (singer)", artist):
        r = await client.get(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(title.replace(' ', '_'))}",
            headers={"User-Agent": "concert-digest/0.1 (personal newsletter)"},
        )
        if r.status_code != 200:
            continue
        body = r.json()
        if body.get("type") == "disambiguation":
            continue
        about = (body.get("description", "") + " " + body.get("extract", "")[:200]).casefold()
        if any(w in about for w in MUSIC_WORDS):
            return _sentences(body.get("extract", ""), 3)
    return ""


def _sentences(text: str, n: int) -> str:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text.strip())
    return " ".join(parts[:n])


async def enrich(client: httpx.AsyncClient, picks: list[Pick], spotify: Spotify) -> None:
    sem = asyncio.Semaphore(4)

    async def one(p: Pick) -> None:
        async with sem:
            wanted = [r.album for r in p.reviews]
            # Pitchfork's feed image is the album cover; other sources use press photos.
            covers = {r.album: r.image for r in p.reviews if r.source == "Pitchfork" and r.image}
            try:
                p.albums, p.artist_apple_url = await apple_albums(client, p.artist, wanted, covers)
            except Exception as exc:
                log.warning("iTunes lookup failed for %s: %s", p.artist, exc)
            await _spotify_links(p, spotify)
            try:
                p.bio = await wikipedia_bio(client, p.artist)
            except Exception as exc:
                log.warning("Wikipedia lookup failed for %s: %s", p.artist, exc)
            # Concertje links are the most precise; prefer them for the artist.
            for c in p.concerts:
                p.artist_spotify_url = p.artist_spotify_url or c.spotify_url
                p.artist_apple_url = c.apple_music_url or p.artist_apple_url

    await asyncio.gather(*(one(p) for p in picks))


async def _spotify_links(p: Pick, spotify: Spotify) -> None:
    for album in p.albums:
        q = f"{p.artist} {album.title}"
        album.spotify_url = Spotify.search_url(q)
        try:
            res = await spotify.search(f'album:"{album.title}" artist:"{p.artist}"', "album")
        except Exception as exc:
            log.warning("Spotify lookup failed for %s: %s", q, exc)
            continue
        for item in (res or {}).get("albums", {}).get("items", []):
            if any(normalize_name(a["name"]) == normalize_name(p.artist) for a in item.get("artists", [])):
                album.spotify_url = item["external_urls"]["spotify"]
                album.artwork = album.artwork or (item.get("images") or [{}])[0].get("url", "")
                if not p.artist_spotify_url:
                    p.artist_spotify_url = item["artists"][0]["external_urls"]["spotify"]
                break
    if not p.artist_spotify_url:
        p.artist_spotify_url = Spotify.search_url(p.artist)
