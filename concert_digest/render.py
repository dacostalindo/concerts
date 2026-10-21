"""Turns picks into the Concertje-styled HTML email (plus a plain-text part)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import Settings
from .mailer import watch_mailto
from .models import Concert, Pick
from .state import Watch

SOURCES_LINE = "Pitchfork, Stereogum, The Guardian, NME, Rolling Stone, The Quietus and Clash"

env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["j2", "html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def fmt_day(d: date | datetime) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def fmt_moment(dt: datetime) -> str:
    return f"{fmt_day(dt)} · {dt:%H:%M}"


def fmt_longday(d: date | datetime) -> str:
    """Concertje's tile date: 'Wed 14 Apr 2027'."""
    return f"{d:%a} {d.day} {d:%b %Y}"


def sized(url: str, width: int = 640) -> str:
    """Concertje's media server resizes on request; other hosts are left alone."""
    if "admin.concertje.be/api/media/" in url and "?" not in url:
        return f"{url}?w={width}"
    return url


def status_pill(c: Concert, state: str) -> str:
    """Wording of the status pill on Concertje's tiles ('On sale 23 Oct')."""
    if state == "soon" and c.on_sale_at:
        return f"On sale {c.on_sale_at.day} {c.on_sale_at:%b}"
    return {"onsale": "On sale", "soon": "On sale soon", "soldout": "Sold out",
            "tba": "Sale TBA", "cancelled": "Cancelled"}[state]


env.filters["day"] = fmt_day
env.filters["moment"] = fmt_moment
env.filters["longday"] = fmt_longday
env.filters["sized"] = sized


def ticket_view(c: Concert, now: datetime, settings: Settings, artist: str) -> dict:
    """What the ticket box says and which buttons it shows."""
    label = f"{artist} @ {c.venue} · {fmt_day(c.date)}"
    watch = watch_mailto(settings, c.id, label)
    upcoming_presales = [p for p in c.presales if p.starts_at and p.starts_at > now]
    if c.ticket_status in {"cancelled", "canceled"}:
        return {"state": "cancelled", "headline": "Cancelled", "detail": "", "buttons": []}
    if c.is_sold_out():
        buttons = [{"label": "Resale on TicketSwap", "href": c.resale_url, "primary": True}] if c.resale_url else []
        return {"state": "soldout", "headline": "Sold out", "detail": "", "buttons": buttons}
    if c.is_on_sale(now):
        detail = f"From {c.price}" if c.price else ""
        href = c.ticket_url or c.concertje_url
        return {"state": "onsale", "headline": "Tickets on sale now", "detail": detail,
                "buttons": [{"label": "Buy tickets  →", "href": href, "primary": True}] if href else []}
    if c.on_sale_at:
        detail = "; ".join(f"{p.name} presale {fmt_moment(p.starts_at)}" for p in upcoming_presales)
        return {"state": "soon", "headline": f"On sale {fmt_moment(c.on_sale_at)}", "detail": detail,
                "buttons": [{"label": "Remind me & buy when live", "href": watch, "primary": True}]}
    return {"state": "tba", "headline": "Ticket sale not announced yet", "detail": "We'll check every run.",
            "buttons": [{"label": "Tell me when tickets drop", "href": watch, "primary": True}]}


def render_digest(picks: list[Pick], settings: Settings, now: datetime, preview_note: str = "") -> tuple[str, str, str]:
    cards = []
    tiles = []
    for p in picks:
        concerts = []
        for c in p.concerts:
            t = ticket_view(c, now, settings, p.artist)
            concerts.append({"c": c, "t": t, "pill": status_pill(c, t["state"])})
            tiles.append({"artist": p.artist, "c": c, "t": t, "pill": status_pill(c, t["state"]),
                          "image": sized(c.image or p.image), "score": p.score})
        quotes = sorted((r for r in p.reviews if r.blurb), key=lambda r: (r.source not in ("Pitchfork", "Stereogum"), -len(r.blurb)))
        cards.append({"p": p, "concerts": concerts, "quote": quotes[0] if quotes else None})

    tiles.sort(key=lambda x: x["c"].date)
    n_shows = sum(len(p.concerts) for p in picks)
    week = now.isocalendar().week
    subject = f"Critics' picks in Belgium · {picks[0].artist}" + (f" + {len(picks) - 1} more" if len(picks) > 1 else "")
    ctx = {
        "cards": cards,
        "tile_rows": [tiles[i:i + 2] for i in range(0, len(tiles), 2)],  # Concertje's 2-up compact grid
        "n_picks": len(picks),
        "n_shows": n_shows,
        "week": week,
        "today": now,
        "sources_line": SOURCES_LINE,
        "preview_note": preview_note,
        "preheader": ", ".join(p.artist for p in picks[:6]) + " — reviewed, rated and touring Belgium.",
    }
    html = env.get_template("digest.html.j2").render(**ctx)
    text = env.get_template("digest.txt.j2").render(**ctx)
    return f"[Concert Digest] {subject}", html, text


def render_alert(kind: str, w: Watch, settings: Settings, extra: str = "") -> tuple[str, str, str]:
    """kind: 'confirm' | 'heads_up' | 'on_sale' | 'already_on_sale'."""
    when = datetime.fromisoformat(w.on_sale_at) if w.on_sale_at else None
    copy = {
        "confirm": ("Watching", f"We'll email you the moment tickets for {w.artist} go live"
                    + (f" ({fmt_moment(when)})." if when else ".")),
        "heads_up": ("Tomorrow", f"Tickets for {w.artist} go on sale {fmt_moment(when) if when else 'soon'}. Set an alarm."),
        "on_sale": ("Tickets are live", f"Tickets for {w.artist} are on sale right now. Good shows sell out fast."),
        "already_on_sale": ("Already on sale", f"Tickets for {w.artist} are already on sale, no need to wait."),
    }[kind]
    subjects = {
        "confirm": f"Watching: {w.artist}, {w.venue}",
        "heads_up": f"Tomorrow: tickets for {w.artist}",
        "on_sale": f"Tickets on sale NOW: {w.artist}, {w.venue}",
        "already_on_sale": f"Tickets already on sale: {w.artist}",
    }
    ctx = {
        "kind": kind, "w": w, "kicker": copy[0], "message": copy[1], "extra": extra,
        "show_date": date.fromisoformat(w.date),
        "unwatch": watch_mailto(settings, w.event_id, f"{w.artist} @ {w.venue} · {fmt_day(date.fromisoformat(w.date))}", unwatch=True),
        "buy": w.ticket_url or w.concertje_url,
    }
    html = env.get_template("alert.html.j2").render(**ctx)
    text = f"{copy[0].upper()}\n\n{copy[1]}\n\n{w.artist}, {w.venue}, {w.city}, {w.date}\n{ctx['buy']}\n"
    return f"[Concert Digest] {subjects[kind]}", html, text
