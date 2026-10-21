"""Apple Music and Spotify album links for an artist (the lookup_album_links tool)."""

from __future__ import annotations

import base64
import logging
import os
import re
import time
from urllib.parse import quote

import httpx

from .models import Album, normalize_name

log = logging.getLogger(__name__)


class Spotify:
    """Client-credentials Spotify search; falls back to search links without keys."""

    def __init__(self, client: httpx.AsyncClient, client_id: str = "", client_secret: str = ""):
        self.client = client
        client_id = client_id or os.environ.get("SPOTIFY_CLIENT_ID", "")
        client_secret = client_secret or os.environ.get("SPOTIFY_CLIENT_SECRET", "")
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

    async def search(self, q: str, kind: str, market: str) -> dict | None:
        token = await self._auth()
        if not token:
            return None
        r = await self.client.get(
            "https://api.spotify.com/v1/search",
            params={"q": q, "type": kind, "limit": 5, "market": market},
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
        return r.json()

    @staticmethod
    def search_url(q: str) -> str:
        return f"https://open.spotify.com/search/{quote(q)}"


def _edition_free(title: str) -> str:
    return normalize_name(re.sub(r"\s*[\(\[].*?[\)\]]|\s+-\s+(EP|Single)$", "", title))


async def _itunes(client: httpx.AsyncClient, term: str, artist: str, country: str) -> list[dict]:
    r = await client.get(
        "https://itunes.apple.com/search",
        params={"term": term, "entity": "album", "country": country, "limit": 50},
    )
    r.raise_for_status()
    target = normalize_name(artist)
    return [x for x in r.json().get("results", []) if normalize_name(x.get("artistName", "")) == target]


def _album(x: dict) -> Album:
    return Album(
        title=x["collectionName"],
        apple_music_url=x["collectionViewUrl"].split("?")[0],
        artwork_url=x.get("artworkUrl100", "").replace("100x100bb", "600x600bb"),
        year=x.get("releaseDate", "")[:4],
    )


async def apple_albums(
    client: httpx.AsyncClient, artist: str, wanted: list[str], country: str, limit: int
) -> tuple[list[Album], str]:
    """The wanted album(s) first, then the artist's latest full-lengths."""
    catalogue = await _itunes(client, artist, artist, country)
    albums: list[Album] = []
    seen: set[str] = set()
    for title in wanted:
        key = _edition_free(title)
        if not key or key in seen:
            continue
        hits = [x for x in catalogue if _edition_free(x["collectionName"]) == key]
        if not hits:
            hits = [x for x in await _itunes(client, f"{artist} {title}", artist, country)
                    if _edition_free(x["collectionName"]) == key]
        # Too new for Apple's search index? Still list it, with a search link.
        albums.append(_album(hits[0]) if hits else Album(
            title=title,
            apple_music_url=f"https://music.apple.com/{country.lower()}/search?term={quote(artist + ' ' + title)}",
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
    return albums[:limit], artist_url


async def lookup_album_links(
    client: httpx.AsyncClient, artist: str, albums: list[str], country: str = "BE", limit: int = 3
) -> dict:
    """Albums with Apple Music + Spotify links and artwork, ready for Artist.albums."""
    spotify = Spotify(client)
    found, artist_apple = await apple_albums(client, artist, albums, country, limit)
    artist_spotify = ""
    for album in found:
        album.spotify_url = Spotify.search_url(f"{artist} {album.title}")
        try:
            res = await spotify.search(f'album:"{album.title}" artist:"{artist}"', "album", country)
        except Exception as exc:
            log.warning("Spotify lookup failed for %s – %s: %s", artist, album.title, exc)
            continue
        for item in (res or {}).get("albums", {}).get("items", []):
            if any(normalize_name(a["name"]) == normalize_name(artist) for a in item.get("artists", [])):
                album.spotify_url = item["external_urls"]["spotify"]
                album.artwork_url = album.artwork_url or (item.get("images") or [{}])[0].get("url", "")
                artist_spotify = artist_spotify or item["artists"][0]["external_urls"]["spotify"]
                break
    return {
        "artist": artist,
        "artist_apple_music_url": artist_apple,
        "artist_spotify_url": artist_spotify or Spotify.search_url(artist),
        "albums": [a.model_dump() for a in found],
        "note": "" if spotify.creds else "Spotify links are search links (no SPOTIFY_CLIENT_ID/SECRET set).",
    }
