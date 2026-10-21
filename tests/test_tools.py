import json
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import ValidationError

from concert_digest.models import TZ, Alert, Digest, Show, normalize_name
from concert_digest.render import render_alert, render_digest, ticket_view

EXAMPLE = json.loads((Path(__file__).parent.parent / "examples" / "digest.example.json").read_text())
NOW = datetime(2026, 10, 21, 12, tzinfo=TZ)


def test_normalize_name():
    assert normalize_name("The Beatles!") == "beatles"
    assert normalize_name("Fontaines D.C.") == normalize_name("fontaines dc")
    assert normalize_name("Angèle") == "angele"


def test_example_renders_every_ticket_state():
    subject, html = render_digest(Digest(**EXAMPLE), now=NOW)
    assert subject == "Critics' picks in Belgium · Fontaines D.C. + 2 more"
    for text in ("Fontaines D.C.", "Ancienne Belgique", "Buy tickets", "On sale 24 Oct", "Add on-sale reminder",
                 "Sale TBA", "Follow on Concertje", "Sold out", "Resale on TicketSwap", "Album of the Week",
                 "8.6", "Picked from this month’s reviews in Pitchfork, NME, Rolling Stone and Stereogum."):
        assert text in html, text


def test_user_text_is_escaped():
    data = json.loads(json.dumps(EXAMPLE))
    data["artists"][0]["bio"] = "<script>alert(1)</script>"
    _, html = render_digest(Digest(**data), now=NOW)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_upcoming_requires_on_sale_at():
    with pytest.raises(ValidationError, match="on_sale_at"):
        Show(date="2027-01-01", venue="AB", ticket_status="upcoming")


def test_naive_times_are_brussels_time():
    s = Show(date="2027-01-01", venue="AB", ticket_status="upcoming", on_sale_at="2026-10-24T10:00:00")
    assert s.on_sale_at.utcoffset().total_seconds() == 7200


def test_reminder_is_calendar_event_at_first_presale():
    show = Show(**EXAMPLE["artists"][1]["shows"][0])
    view = ticket_view(show, "Rachel Chinouriri", NOW)
    q = parse_qs(urlparse(view["buttons"][0]["href"]).query)
    assert q["dates"] == ["20261023T080000Z/20261023T083000Z"]  # 10:00 Brussels = 08:00 UTC
    assert "Artist presale" in view["detail"]


def test_upcoming_flips_to_on_sale_after_the_date():
    show = Show(**EXAMPLE["artists"][1]["shows"][0])
    assert ticket_view(show, "x", datetime(2026, 10, 25, tzinfo=TZ))["state"] == "onsale"


def test_alerts():
    show = EXAMPLE["artists"][1]["shows"][0]
    subject, html = render_alert(Alert(kind="on_sale_soon", artist="Rachel Chinouriri", show=show), now=NOW)
    assert "go on sale Sat 24 Oct · 10:00" in subject and "Add on-sale reminder" in html
    subject, html = render_alert(Alert(kind="on_sale_now", artist="Fontaines D.C.", show=EXAMPLE["artists"][0]["shows"][0]), now=NOW)
    assert subject.startswith("Tickets on sale now") and "https://www.abconcerts.be" in html
