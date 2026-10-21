"""Tool inputs. Field descriptions are what the calling model (Grok) reads,
so they say where each value should come from."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

TZ = ZoneInfo("Europe/Brussels")

TicketStatus = Literal["on_sale", "upcoming", "not_announced", "sold_out", "cancelled"]


def normalize_name(name: str) -> str:
    """Fold an artist name for matching: 'The Beatles!' -> 'beatles'."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.casefold().replace("&", " and ")
    s = re.sub(r"[.'’]", "", s)  # "D.C." == "DC", "Guns N' Roses" == "Guns N Roses"
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"^the\s+", "", s.strip())
    return re.sub(r"\s+", " ", s).strip()


class Review(BaseModel):
    source: str = Field(description="Publication name, e.g. 'Pitchfork', 'Stereogum', 'NME', 'The Guardian'.")
    url: str = Field(description="Link to the review.")
    album: str = Field("", description="Title of the reviewed album.")
    score_10: float | None = Field(
        None, ge=0, le=10,
        description="Score normalised to 0-10 (Pitchfork 8.4 -> 8.4; 4/5 stars -> 8.0). Omit if the review has no score.",
    )
    score_label: str = Field(
        "", description="The score as the publication shows it: '8.4', '★★★★☆', 'A-'. Shown on the review pill.",
    )
    accolade: str = Field("", description="Badge such as 'Best New Music' or 'Album of the Week'. Empty if none.")
    quote: str = Field("", max_length=400, description="One-sentence verdict from the review (its subheading or a short quote).")


class Album(BaseModel):
    title: str
    year: str = ""
    artwork_url: str = ""
    apple_music_url: str = ""
    spotify_url: str = ""


class Presale(BaseModel):
    name: str = Field(description="Who runs it: 'Artist', 'Venue', 'Live Nation', 'Spotify'...")
    starts_at: datetime = Field(description="ISO 8601 with timezone, e.g. 2026-10-23T10:00:00+02:00.")


class Show(BaseModel):
    date: date
    venue: str
    city: str = ""
    festival: str = Field("", description="Festival name if the show is part of one.")
    concertje_url: str = Field("", description="The show's page on concertje.be.")
    image_url: str = Field("", description="Concert image (Concertje's event image); falls back to the artist image.")
    ticket_status: TicketStatus = Field(
        description="on_sale: buy now. upcoming: sale date announced (set on_sale_at). "
        "not_announced: no sale date yet. sold_out. cancelled.",
    )
    ticket_url: str = Field("", description="Official ticket shop link.")
    resale_url: str = Field("", description="Resale link (e.g. TicketSwap), used when sold out.")
    on_sale_at: datetime | None = Field(None, description="When general sale opens, ISO 8601 with timezone. Required for 'upcoming'.")
    presales: list[Presale] = Field(default_factory=list)
    price: str = Field("", description="Display price, e.g. '€28,50'.")

    @model_validator(mode="after")
    def _upcoming_needs_date(self) -> "Show":
        if self.ticket_status == "upcoming" and not self.on_sale_at:
            raise ValueError("ticket_status 'upcoming' needs on_sale_at (use 'not_announced' if the date is unknown)")
        for field in ("on_sale_at",):
            v = getattr(self, field)
            if v is not None and v.tzinfo is None:
                setattr(self, field, v.replace(tzinfo=TZ))
        for p in self.presales:
            if p.starts_at.tzinfo is None:
                p.starts_at = p.starts_at.replace(tzinfo=TZ)
        return self


class Artist(BaseModel):
    name: str
    image_url: str = Field("", description="Press photo or album cover for the artist tile.")
    bio: str = Field("", max_length=700, description="Two or three sentences on who they are and what they sound like.")
    genres: list[str] = Field(default_factory=list, max_length=4, description="Up to 4 genres, e.g. ['Indie', 'Post-punk'].")
    reviews: list[Review] = Field(min_length=1)
    albums: list[Album] = Field(
        default_factory=list, max_length=4,
        description="Reviewed album first, then other records. Use the lookup_album_links tool to fill this.",
    )
    shows: list[Show] = Field(min_length=1, description="Upcoming shows in Belgium.")

    @property
    def score(self) -> float | None:
        scores = [r.score_10 for r in self.reviews if r.score_10 is not None]
        return round(sum(scores) / len(scores), 1) if scores else None


class Digest(BaseModel):
    artists: list[Artist] = Field(min_length=1, max_length=20, description="Best-reviewed first.")
    headline: str = Field("The critics loved them. Now they’re coming to Belgium.", max_length=120)
    intro: str = Field("", max_length=400, description="One or two sentences under the headline. Default: a count of records and shows.")
    sources: list[str] = Field(default_factory=list, description="Publications you read, for the intro line.")


class Alert(BaseModel):
    kind: Literal["on_sale_now", "on_sale_soon"] = Field(
        description="on_sale_now: tickets just went live. on_sale_soon: reminder before the sale opens.",
    )
    artist: str
    show: Show
    image_url: str = ""
    message: str = Field("", max_length=300, description="Optional custom line; a default is written for each kind.")
