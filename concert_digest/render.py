"""Renders the Concertje-styled digest and alert emails from tool inputs."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .models import TZ, Alert, Artist, Digest, Show

env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["j2", "html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def fmt_day(d: date | datetime) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def fmt_moment(dt: datetime) -> str:
    return f"{fmt_day(dt.astimezone(TZ))} · {dt.astimezone(TZ):%H:%M}"


def fmt_longday(d: date | datetime) -> str:
    """Concertje's tile date: 'Wed 14 Apr 2027'."""
    return f"{d:%a} {d.day} {d:%b %Y}"


def sized(url: str, width: int = 640) -> str:
    """Concertje's media server resizes on request; other hosts are left alone."""
    if "admin.concertje.be/api/media/" in url and "?" not in url:
        return f"{url}?w={width}"
    return url


env.filters["day"] = fmt_day
env.filters["moment"] = fmt_moment
env.filters["longday"] = fmt_longday
env.filters["sized"] = sized

STATE = {"on_sale": "onsale", "upcoming": "soon", "not_announced": "tba", "sold_out": "soldout", "cancelled": "cancelled"}


def calendar_link(artist: str, show: Show, when: datetime) -> str:
    """Google Calendar event at the moment tickets go on sale."""
    start = when.astimezone(timezone.utc)
    end = start + timedelta(minutes=30)
    details = f"Tickets for {artist} at {show.venue} ({fmt_longday(show.date)}) go on sale now."
    if show.ticket_url or show.concertje_url:
        details += f"\n{show.ticket_url or show.concertje_url}"
    return "https://calendar.google.com/calendar/render?" + urlencode({
        "action": "TEMPLATE",
        "text": f"Tickets: {artist} @ {show.venue}",
        "dates": f"{start:%Y%m%dT%H%M%SZ}/{end:%Y%m%dT%H%M%SZ}",
        "details": details,
    })


def status_pill(show: Show) -> str:
    """Wording of the status pill on Concertje's tiles ('On sale 23 Oct')."""
    if show.ticket_status == "upcoming" and show.on_sale_at:
        local = show.on_sale_at.astimezone(TZ)
        return f"On sale {local.day} {local:%b}"
    return {"on_sale": "On sale", "upcoming": "On sale soon", "not_announced": "Sale TBA",
            "sold_out": "Sold out", "cancelled": "Cancelled"}[show.ticket_status]


def ticket_view(show: Show, artist: str, now: datetime) -> dict:
    """What a show row says and which buttons it shows."""
    status = show.ticket_status
    if status == "upcoming" and show.on_sale_at and show.on_sale_at <= now:
        status = "on_sale"  # the sale date has passed since the data was gathered
    state = STATE[status]
    if status == "cancelled":
        return {"state": state, "detail": "", "buttons": []}
    if status == "sold_out":
        buttons = [{"label": "Resale on TicketSwap", "href": show.resale_url, "primary": True}] if show.resale_url else []
        return {"state": state, "detail": "", "buttons": buttons}
    if status == "on_sale":
        href = show.ticket_url or show.concertje_url
        return {"state": state, "detail": f"From {show.price}" if show.price else "",
                "buttons": [{"label": "Buy tickets  →", "href": href, "primary": True}] if href else []}
    if status == "upcoming":
        presales = [p for p in show.presales if p.starts_at > now]
        first = min([p.starts_at for p in presales] + [show.on_sale_at])
        detail = "; ".join([f"General sale {fmt_moment(show.on_sale_at)}"]
                           + [f"{p.name} presale {fmt_moment(p.starts_at)}" for p in presales])
        return {"state": state, "detail": detail,
                "buttons": [{"label": "Add on-sale reminder", "href": calendar_link(artist, show, first), "primary": True}]}
    buttons = [{"label": "Follow on Concertje", "href": show.concertje_url, "primary": True}] if show.concertje_url else []
    return {"state": state, "detail": "Ticket sale not announced yet", "buttons": buttons}


def artist_image(a: Artist) -> str:
    """Press photo, else the first album cover that has artwork."""
    return a.image_url or next((al.artwork_url for al in a.albums if al.artwork_url), "")


def _intro(d: Digest) -> str:
    if d.intro:
        return d.intro
    n_a, n_s = len(d.artists), sum(len(a.shows) for a in d.artists)
    line = f"{n_a} acclaimed record{'s' if n_a != 1 else ''}, {n_s} Belgian show{'s' if n_s != 1 else ''}."
    if d.sources:
        names = d.sources if len(d.sources) < 2 else [", ".join(d.sources[:-1]), d.sources[-1]]
        line += f" Picked from this month’s reviews in {' and '.join(names)}."
    return line


def _quote(a: Artist):
    quoted = [r for r in a.reviews if r.quote]
    return max(quoted, key=lambda r: len(r.quote)) if quoted else None


def render_digest(d: Digest, now: datetime | None = None, preview_note: str = "") -> tuple[str, str]:
    """Returns (subject, html)."""
    now = now or datetime.now(TZ)
    cards, tiles = [], []
    for a in d.artists:
        shows = []
        for s in sorted(a.shows, key=lambda s: s.date):
            view = {"c": s, "t": ticket_view(s, a.name, now), "pill": status_pill(s)}
            shows.append(view)
            tiles.append({**view, "artist": a.name, "image": sized(s.image_url or artist_image(a)), "score": a.score})
        cards.append({"p": a, "image": artist_image(a), "concerts": shows, "quote": _quote(a)})
    tiles.sort(key=lambda x: x["c"].date)

    first = d.artists[0].name
    subject = f"Critics' picks in Belgium · {first}" + (f" + {len(d.artists) - 1} more" if len(d.artists) > 1 else "")
    html = env.get_template("digest.html.j2").render(
        cards=cards,
        tile_rows=[tiles[i:i + 2] for i in range(0, len(tiles), 2)],  # Concertje's 2-up compact grid
        headline=d.headline,
        intro=_intro(d),
        week=now.isocalendar().week,
        today=now,
        preview_note=preview_note,
        preheader=", ".join(a.name for a in d.artists[:6]) + " — reviewed, rated and touring Belgium.",
    )
    return subject, html


def render_alert(alert: Alert, now: datetime | None = None) -> tuple[str, str]:
    """Returns (subject, html)."""
    now = now or datetime.now(TZ)
    s = alert.show
    when = s.on_sale_at
    if alert.kind == "on_sale_now":
        kicker, subject = "Tickets are live", f"Tickets on sale now: {alert.artist}, {s.venue}"
        message = alert.message or f"Tickets for {alert.artist} are on sale right now. Good shows sell out fast."
        buttons = [{"label": "Buy tickets  →", "href": s.ticket_url or s.concertje_url, "primary": True}]
    else:
        kicker = f"On sale {fmt_moment(when)}" if when else "On sale soon"
        subject = f"Tickets for {alert.artist} go on sale {fmt_moment(when) if when else 'soon'}"
        message = alert.message or f"Tickets for {alert.artist} at {s.venue} go on sale {fmt_moment(when) if when else 'soon'}."
        buttons = [{"label": "Add on-sale reminder", "href": calendar_link(alert.artist, s, when), "primary": True}] if when else []
        if s.ticket_url:
            buttons.append({"label": "Ticket page", "href": s.ticket_url, "primary": False})
    presales = [f"{p.name} presale {fmt_moment(p.starts_at)}" for p in s.presales if p.starts_at > now]
    html = env.get_template("alert.html.j2").render(
        kind=alert.kind, artist=alert.artist, show=s, kicker=kicker, message=message,
        extra=" · ".join(presales), image=sized(s.image_url or alert.image_url, 1200),
        buttons=[b for b in buttons if b["href"]],
    )
    return subject, html
