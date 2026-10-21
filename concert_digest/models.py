from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime


def normalize_name(name: str) -> str:
    """Fold an artist name for matching: 'The Beatles!' -> 'beatles'."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.casefold().replace("&", " and ")
    s = re.sub(r"[.'’]", "", s)  # "D.C." == "DC", "Guns N' Roses" == "Guns N Roses"
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"^the\s+", "", s.strip())
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class Review:
    source: str  # "Pitchfork"
    artist: str
    album: str
    url: str
    published: datetime | None = None
    blurb: str = ""  # the critic's dek / standfirst
    score: float | None = None  # normalised to /10
    score_label: str = ""  # how the source shows it: "8.4", "★★★★☆", "Album of the Week"
    accolade: str = ""  # "Best New Music", "Album of the Week", ...
    genres: list[str] = field(default_factory=list)
    image: str = ""


@dataclass
class Presale:
    name: str
    starts_at: datetime | None


@dataclass
class Concert:
    id: str
    title: str
    date: date
    venue: str
    city: str
    artists: list[str]
    concertje_url: str = ""
    ticket_status: str = ""  # available | soldout | upcoming | cancelled | ...
    ticket_url: str = ""
    resale_url: str = ""
    price: str = ""
    on_sale_at: datetime | None = None
    presales: list[Presale] = field(default_factory=list)
    image: str = ""
    genres: list[str] = field(default_factory=list)
    spotify_url: str = ""
    apple_music_url: str = ""
    festival: str = ""

    def is_on_sale(self, now: datetime) -> bool:
        if self.ticket_status in {"soldout", "sold_out", "cancelled", "canceled", "postponed"}:
            return False
        if self.on_sale_at:
            return self.on_sale_at <= now
        return self.ticket_status in {"available", "onsale", "on_sale", "low"}

    def is_sold_out(self) -> bool:
        return self.ticket_status in {"soldout", "sold_out"}


@dataclass
class Album:
    title: str
    apple_music_url: str = ""
    spotify_url: str = ""
    artwork: str = ""
    year: str = ""


@dataclass
class Pick:
    """One artist in the digest: their reviews, records and Belgian shows."""

    artist: str
    reviews: list[Review]
    concerts: list[Concert]
    bio: str = ""
    genres: list[str] = field(default_factory=list)
    albums: list[Album] = field(default_factory=list)
    artist_apple_url: str = ""
    artist_spotify_url: str = ""

    @property
    def score(self) -> float | None:
        scores = [r.score for r in self.reviews if r.score is not None]
        return round(sum(scores) / len(scores), 1) if scores else None

    @property
    def image(self) -> str:
        for c in self.concerts:
            if c.image:
                return c.image
        for r in self.reviews:
            if r.image:
                return r.image
        return self.albums[0].artwork if self.albums else ""
