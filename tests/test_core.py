from datetime import date, datetime, timedelta
from pathlib import Path

from concert_digest.concertje import _events, _payload, to_concert, upcoming
from concert_digest.config import TZ, Settings
from concert_digest.mailer import REQUEST_RE, watch_mailto
from concert_digest.models import Pick, Review, normalize_name
from concert_digest.render import render_alert, render_digest, ticket_view
from concert_digest.state import State, Watch

# Shape of a concert as Concertje serves it on concertje.be (trimmed).
RECORD = {
    "id": 16850,
    "title": "Terri Lyne Carrington We Insist!",
    "date": "2026-10-23",
    "venue": {"id": 433, "name": "Bijloke", "city": "Gent", "slug": "gent-bijloke"},
    "artists": [{
        "name": "Terri Lyne Carrington", "slug": "terri-lyne-carrington", "spotifyId": "abc",
        "genreTags": ["jazz"],
    }],
    "tickets": {
        "status": "available", "url": "https://www.bijloke.be/x", "price": None,
        "onSaleAt": "2026-10-21T08:17:43.000Z",
        "presales": [{"type": "artist", "startsAt": "2026-10-20T08:00:00.000Z"}],
    },
    "ticketStatus": "available",
    "imageUrl": "https://admin.concertje.be/a.jpg",
    "ticketswapUrl": None,
    "ticketPrice": 26.5,
    "festival": None,
}


def settings(tmp_path: Path) -> Settings:
    return Settings(
        home=tmp_path, concertje_url="", oauth_port=0, search_tool="", search_arg="", lookback_days=21,
        smtp_host="", smtp_port=465, smtp_user="me@example.com", smtp_password="", imap_host="",
        mail_from="me@example.com", mail_to="me@example.com", watch_allowed_from=["me@example.com"],
    )


def test_normalize_name():
    assert normalize_name("The Beatles!") == "beatles"
    assert normalize_name("Angèle") == "angele"
    assert normalize_name("Fontaines D.C.") == normalize_name("fontaines dc")
    assert normalize_name("Simon & Garfunkel") == "simon and garfunkel"


def test_to_concert_reads_concertje_record():
    c = to_concert(RECORD)
    assert c.id == "16850" and c.date == date(2026, 10, 23)
    assert (c.venue, c.city) == ("Bijloke", "Gent")
    assert c.artists == ["Terri Lyne Carrington"]
    assert c.ticket_url == "https://www.bijloke.be/x"
    assert c.on_sale_at == datetime(2026, 10, 21, 10, 17, 43, tzinfo=TZ)
    assert c.price == "€26,50"
    assert c.genres == ["jazz"]
    assert c.spotify_url == "https://open.spotify.com/artist/abc"
    assert c.concertje_url == "https://concertje.be/concert/2026-10-23/gent-bijloke/terri-lyne-carrington-16850"
    assert c.presales[0].name == "artist"


def test_events_found_in_nested_and_text_payloads():
    class Block:
        def __init__(self, text):
            self.text = text

    class Result:
        structured_content = None
        content = [Block('{"results": {"concerts": [%s]}}' % __import__("json").dumps(RECORD)),
                   Block("Also: https://concertje.be/concert/2026-11-02/antwerpen-trix/dave-east-1664")]

    events = list(_events(_payload(Result())))
    assert [str(e["id"]) for e in events] == ["16850", "1664"]
    assert to_concert(events[1]).venue == "Trix"


def test_upcoming_matches_artist_and_drops_past():
    c = to_concert(RECORD)
    assert upcoming([c], "terri lyne carrington", date(2026, 10, 1)) == [c]
    assert upcoming([c], "Terri Lyne", date(2026, 10, 1)) == []
    assert upcoming([c], "Terri Lyne Carrington", date(2026, 11, 1)) == []


def test_ticket_states(tmp_path):
    s = settings(tmp_path)
    now = datetime(2026, 10, 21, 12, tzinfo=TZ)
    c = to_concert(RECORD)
    assert ticket_view(c, now, s, "T")["state"] == "onsale"
    c.on_sale_at, c.ticket_status = now + timedelta(days=2), "upcoming"
    view = ticket_view(c, now, s, "T")
    assert view["state"] == "soon" and view["buttons"][0]["href"].startswith("mailto:me@example.com")
    c.on_sale_at, c.ticket_status = None, ""
    assert ticket_view(c, now, s, "T")["state"] == "tba"
    c.ticket_status = "soldout"
    assert ticket_view(c, now, s, "T")["state"] == "soldout"


def test_watch_mailto_round_trips_through_subject_parser(tmp_path):
    from urllib.parse import parse_qs, urlparse

    link = watch_mailto(settings(tmp_path), "16850", "Fontaines D.C. @ Vorst Nationaal · Thu 23 Oct")
    subject = parse_qs(urlparse(link).query)["subject"][0]
    m = REQUEST_RE.search("Re: " + subject)
    assert m.group(1) is None and m.group(2) == "16850" and m.group(3) == "Fontaines D.C."
    unwatch = parse_qs(urlparse(watch_mailto(settings(tmp_path), "7", "A @ B · x", unwatch=True)).query)
    assert REQUEST_RE.search(unwatch["subject"][0]).group(1) == "UN"


def test_render_digest_and_alerts(tmp_path):
    s = settings(tmp_path)
    now = datetime(2026, 10, 21, 12, tzinfo=TZ)
    review = Review(source="Pitchfork", artist="Terri Lyne Carrington", album="We Insist 2025!",
                    url="https://pitchfork.com/x", blurb="Great & <loud>", score=8.4, score_label="8.4")
    pick = Pick(artist="Terri Lyne Carrington", reviews=[review], concerts=[to_concert(RECORD)])
    subject, html, text = render_digest([pick], s, now)
    assert "Terri Lyne Carrington" in subject
    assert "Great &amp; &lt;loud&gt;" in html and "8.4" in html and "Bijloke" in html
    assert "Bijloke" in text

    w = Watch(event_id="16850", artist="Terri Lyne Carrington", title="x", date="2026-10-23", venue="Bijloke",
              city="Gent", added="", ticket_url="https://www.bijloke.be/x", on_sale_at=now.isoformat())
    for kind in ("confirm", "heads_up", "on_sale", "already_on_sale"):
        subject, html, _ = render_alert(kind, w, s)
        assert "Bijloke" in html and subject.startswith("[Concert Digest]")


def test_state_round_trip(tmp_path):
    st = State.load(tmp_path)
    st.featured["1"] = {"sent": "2026-10-21", "artist": "A"}
    st.watch["1"] = Watch(event_id="1", artist="A", title="A", date="2026-12-01", venue="V", city="C", added="")
    st.processed_messages.append("<m@x>")
    st.save()
    again = State.load(tmp_path)
    assert again.watch["1"].venue == "V" and again.featured["1"]["artist"] == "A"
    assert again.processed_messages == ["<m@x>"]
