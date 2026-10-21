"""Stand-in for Concertje so `concert-digest preview` works before logging in.

Real reviews and album links; the concerts are invented, one per ticket state.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .config import TZ
from .models import Concert, Presale

VENUES = [
    ("Ancienne Belgique", "Brussel"),
    ("Trix", "Antwerpen"),
    ("Botanique", "Brussel"),
    ("Vooruit", "Gent"),
    ("Het Depot", "Leuven"),
    ("Cactus Club", "Brugge"),
]


class SampleConcertje:
    def __init__(self, n_artists: int = 4):
        self.n = n_artists
        self.assigned: dict[str, int] = {}

    async def concerts_for(self, artist: str) -> list[Concert]:
        if artist not in self.assigned:
            if len(self.assigned) >= self.n:
                return []
            self.assigned[artist] = len(self.assigned)
        i = self.assigned[artist]
        now = datetime.now(TZ).replace(minute=0, second=0, microsecond=0)
        venue, city = VENUES[i % len(VENUES)]
        show = (now + timedelta(days=40 + 17 * i)).date()
        base = dict(id=f"sample-{i}", title=artist, date=show, venue=venue, city=city, artists=[artist],
                    concertje_url="https://concertje.be")
        states = [
            dict(ticket_status="available", ticket_url="https://concertje.be", price="€28,50",
                 on_sale_at=now - timedelta(days=5)),
            dict(ticket_status="upcoming", on_sale_at=(now + timedelta(days=3)).replace(hour=10),
                 presales=[Presale("Artist", (now + timedelta(days=2)).replace(hour=10))]),
            dict(ticket_status=""),
            dict(ticket_status="soldout", resale_url="https://www.ticketswap.be"),
        ]
        return [Concert(**base, **states[i % len(states)])]
