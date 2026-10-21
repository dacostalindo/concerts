"""Client for the Concertje MCP server (https://ai.concertje.be/mcp).

The server uses OAuth (dynamic client registration + PKCE). `concert-digest auth`
runs the browser login once; tokens, including the refresh token, are kept in
CONCERT_DIGEST_HOME so later runs (cron) work unattended.

The tool names aren't published, so the search tool is discovered at runtime
(override with CONCERTJE_SEARCH_TOOL / CONCERTJE_SEARCH_ARG), and results are
read leniently: any JSON object with a date plus a venue or artists is a concert.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import webbrowser
from contextlib import AsyncExitStack
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from .config import TZ, Settings
from .models import Concert, Presale, normalize_name

log = logging.getLogger(__name__)

SITE = "https://concertje.be"
QUERY_ARGS = ("artist", "artistname", "artist_name", "query", "q", "search", "term", "keyword", "keywords", "name", "text")


class LoginRequired(RuntimeError):
    pass


# --- OAuth plumbing --------------------------------------------------------


class FileTokenStorage(TokenStorage):
    def __init__(self, home: Path):
        self.tokens_path = home / "concertje-tokens.json"
        self.client_path = home / "concertje-client.json"

    async def get_tokens(self) -> OAuthToken | None:
        return OAuthToken.model_validate_json(self.tokens_path.read_text()) if self.tokens_path.exists() else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens_path.write_text(tokens.model_dump_json())
        self.tokens_path.chmod(0o600)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        if not self.client_path.exists():
            return None
        return OAuthClientInformationFull.model_validate_json(self.client_path.read_text())

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_path.write_text(client_info.model_dump_json())


class BrowserLogin:
    """Opens the login page and waits for the redirect on localhost.

    If the browser runs on another machine than this script, the user can
    paste the URL they were redirected to into the terminal instead.
    """

    def __init__(self, port: int):
        self.port = port
        self.redirect_uri = f"http://localhost:{port}/callback"
        self._result: asyncio.Future[AuthorizationCodeResult] | None = None

    async def redirect(self, url: str) -> None:
        loop = asyncio.get_running_loop()
        self._result = loop.create_future()
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        print("\nLog in to Concertje in your browser:\n\n  " + url + "\n")
        print("If the browser can't reach this machine, paste the full URL it redirected to here:")
        webbrowser.open(url)
        threading.Thread(target=self._read_stdin, args=(loop,), daemon=True).start()

    async def callback(self) -> AuthorizationCodeResult:
        assert self._result is not None
        try:
            return await asyncio.wait_for(self._result, timeout=600)
        finally:
            self._server.close()

    def _complete(self, url: str) -> bool:
        q = parse_qs(urlparse(url.strip()).query)
        if "code" not in q or self._result is None or self._result.done():
            return False
        self._result.set_result(
            AuthorizationCodeResult(code=q["code"][0], state=q.get("state", [None])[0], iss=q.get("iss", [None])[0])
        )
        return True

    def _read_stdin(self, loop: asyncio.AbstractEventLoop) -> None:
        try:
            line = input()
        except EOFError:
            return
        loop.call_soon_threadsafe(self._complete, line)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = (await reader.readline()).decode(errors="ignore")
        path = request_line.split(" ")[1] if " " in request_line else "/"
        ok = self._complete(f"http://localhost{path}")
        body = "Logged in to Concertje. You can close this tab." if ok else "Waiting for the Concertje login…"
        writer.write(
            f"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {len(body.encode())}\r\n"
            f"Connection: close\r\n\r\n{body}".encode()
        )
        await writer.drain()
        writer.close()


# --- Client ----------------------------------------------------------------


class Concertje:
    def __init__(self, settings: Settings, interactive: bool = False):
        self.settings = settings
        self.interactive = interactive
        self.session: ClientSession | None = None
        self.tools: list[Any] = []
        self._stack = AsyncExitStack()
        self._search: tuple[str, str, dict] | None = None

    async def __aenter__(self) -> "Concertje":
        login = BrowserLogin(self.settings.oauth_port)
        storage = FileTokenStorage(self.settings.home)
        if not self.interactive and not await storage.get_tokens():
            raise LoginRequired("Not logged in to Concertje yet: run `concert-digest auth` once.")

        async def redirect(url: str) -> None:
            if not self.interactive:
                raise LoginRequired("Concertje login expired or missing: run `concert-digest auth` once.")
            await login.redirect(url)

        auth = OAuthClientProvider(
            server_url=self.settings.concertje_url,
            client_metadata=OAuthClientMetadata(
                client_name="Concert Digest",
                redirect_uris=[login.redirect_uri],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope="concertje:read offline_access",
                token_endpoint_auth_method="none",
            ),
            storage=storage,
            redirect_handler=redirect,
            callback_handler=login.callback,
        )
        http = await self._stack.enter_async_context(create_mcp_http_client(auth=auth))
        read, write = await self._stack.enter_async_context(
            streamable_http_client(self.settings.concertje_url, http_client=http)
        )
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        self.tools = (await self.session.list_tools()).tools
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()

    # Tool discovery ---------------------------------------------------------

    def describe_tools(self) -> str:
        lines = []
        for t in self.tools:
            props = (t.input_schema or {}).get("properties", {})
            req = set((t.input_schema or {}).get("required", []))
            args = ", ".join(f"{k}{'*' if k in req else ''}: {v.get('type', '?')}" for k, v in props.items())
            lines.append(f"- {t.name}({args})\n    {(t.description or '').strip()[:300]}")
        return "\n".join(lines)

    def search_tool(self) -> tuple[str, str, dict]:
        """(tool name, query argument, extra fixed arguments)."""
        if self._search:
            return self._search
        best: tuple[int, str, str, dict] | None = None
        for t in self.tools:
            if self.settings.search_tool and t.name != self.settings.search_tool:
                continue
            schema = t.input_schema or {}
            props = schema.get("properties", {})
            by_lower = {k.lower(): k for k, v in props.items() if v.get("type", "string") in ("string", ["string", "null"])}
            arg = self.settings.search_arg or next((by_lower[a] for a in QUERY_ARGS if a in by_lower), None)
            if not arg:
                continue
            extra = {}
            for k, v in props.items():
                if k.lower() in {"limit", "per_page", "perpage", "pagesize", "page_size", "max_results"} and v.get("type") == "integer":
                    extra[k] = min(int(v.get("maximum", 50)), 50)
                elif k.lower() == "country" and "BE" in json.dumps(v):
                    extra[k] = "BE"
            if set(schema.get("required", [])) - {arg} - set(extra):
                continue  # needs inputs we can't provide
            words = f"{t.name} {t.description or ''}".lower()
            score = (
                4 * ("search" in t.name or "find" in t.name)
                + 3 * ("artist" in words)
                + 3 * any(w in words for w in ("concert", "event", "show", "gig"))
                + 2 * (arg.lower().startswith("artist"))
                - 3 * any(w in t.name for w in ("venue", "festival", "review", "zaal"))
            )
            if not best or score > best[0]:
                best = (score, t.name, arg, extra)
        if not best:
            raise RuntimeError(
                "Couldn't find a Concertje search tool. Run `concert-digest tools` and set "
                "CONCERTJE_SEARCH_TOOL / CONCERTJE_SEARCH_ARG."
            )
        self._search = best[1:]
        log.info("Concertje search tool: %s(%s=…, %s)", *self._search)
        return self._search

    # Queries ------------------------------------------------------------------

    async def concerts_for(self, artist: str) -> list[Concert]:
        tool, arg, extra = self.search_tool()
        assert self.session
        result = await self.session.call_tool(tool, {arg: artist, **extra})
        if getattr(result, "is_error", False):
            log.warning("Concertje %s(%r) failed: %s", tool, artist, _content_text(result)[:200])
            return []
        concerts = [c for c in (to_concert(e) for e in _events(_payload(result))) if c]
        unique = {c.id: c for c in concerts}
        return sorted(unique.values(), key=lambda c: c.date)


def _content_text(result) -> str:
    return "\n".join(getattr(b, "text", "") for b in getattr(result, "content", []) or [])


def _payload(result) -> Any:
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured
    out = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            out.append(json.loads(text))
        except ValueError:
            out.append(text)
    return out


# --- Normalising Concertje records -------------------------------------------


def _get(d: dict, *keys: str, default=None):
    """Case/underscore-insensitive lookup: _get(d, 'onSaleAt') also finds 'on_sale_at'."""
    folded = {k.replace("_", "").lower(): v for k, v in d.items()}
    for k in keys:
        v = folded.get(k.replace("_", "").lower())
        if v not in (None, "", []):
            return v
    return default


def _events(obj: Any) -> Iterator[dict]:
    if isinstance(obj, dict):
        if _get(obj, "date", "startDate", "start", "startsAt") and (
            _get(obj, "venue", "venueName", "location") or _get(obj, "artists", "artist", "lineup")
        ):
            yield obj
            return
        for v in obj.values():
            yield from _events(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _events(v)
    elif isinstance(obj, str):
        yield from _events_from_text(obj)


def _events_from_text(text: str) -> Iterator[dict]:
    """Some MCP tools answer in prose; recover concerts from Concertje links in it."""
    for m in re.finditer(r"/concert/(\d{4}-\d{2}-\d{2})/([a-z0-9-]+)/([a-z0-9-]+?)-(\d+)\b", text):
        day, venue_slug, artist_slug, event_id = m.groups()
        yield {
            "id": event_id,
            "date": day,
            "venue": {"slug": venue_slug, "name": venue_slug.split("-", 1)[-1].replace("-", " ").title(),
                      "city": venue_slug.split("-", 1)[0].title()},
            "artists": [{"name": artist_slug.replace("-", " ").title(), "slug": artist_slug}],
            "url": SITE + m.group(0),
        }


def _dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(TZ) if dt.tzinfo else dt.replace(tzinfo=TZ)


def _url(v) -> str:
    if isinstance(v, str):
        return v if v.startswith("http") else ""
    if isinstance(v, dict):
        return _url(_get(v, "url", "href", "link", "externalUrl", default=""))
    return ""


def to_concert(d: dict) -> Concert | None:
    day = _dt(_get(d, "date", "startDate", "start", "startsAt"))
    if not day:
        return None
    venue = _get(d, "venue", "location", default={})
    if isinstance(venue, str):
        venue = {"name": venue}
    artists_raw = _get(d, "artists", "lineup", default=[]) or [_get(d, "artist", default={})]
    artists = [a if isinstance(a, dict) else {"name": str(a)} for a in artists_raw if a]
    names = [a.get("name", "") for a in artists if a.get("name")] or [str(_get(d, "title", "name", default=""))]
    tickets = _get(d, "tickets", default={}) or {}
    tickets = tickets if isinstance(tickets, dict) else {}

    genres: list[str] = []
    spotify = apple = ""
    for a in artists:
        for g in a.get("genreTags") or a.get("genres") or []:
            name = g.get("name") if isinstance(g, dict) else str(g)
            if name and name not in genres:
                genres.append(name)
        spotify = spotify or _url(a.get("spotify")) or (
            f"https://open.spotify.com/artist/{a['spotifyId']}" if a.get("spotifyId") else "")
        apple = apple or _url(a.get("appleMusic")) or (
            f"https://music.apple.com/be/artist/{a['appleMusicId']}" if a.get("appleMusicId") else "")

    presales = []
    for p in _get(tickets, "presales", default=[]) or _get(d, "presales", default=[]) or []:
        if isinstance(p, dict):
            presales.append(Presale(
                name=str(_get(p, "name", "label", "type", default="Presale")),
                starts_at=_dt(_get(p, "startsAt", "startAt", "start", "date", "onSaleAt")),
            ))

    event_id = str(_get(d, "id", "eventId", default=""))
    venue_slug = venue.get("slug", "")
    first_slug = artists[0].get("slug", "") if artists else ""
    url = _url(_get(d, "concertjeUrl", "url", "link", "permalink", default=""))
    if "concertje" not in url and venue_slug and first_slug and event_id:
        url = f"{SITE}/concert/{day.date().isoformat()}/{venue_slug}/{first_slug}-{event_id}"

    price = _get(d, "ticketPrice", default=None) or _get(tickets, "price", default=None)
    festival = _get(d, "festival", default="")
    return Concert(
        id=event_id or f"{day.date()}-{normalize_name(names[0])}",
        title=str(_get(d, "title", "name", default=names[0])),
        date=day.date() if isinstance(day, datetime) else day,
        venue=str(venue.get("name", "")),
        city=str(venue.get("city", _get(d, "city", default=""))),
        artists=names,
        concertje_url=url,
        ticket_status=str(_get(d, "ticketStatus", default="") or _get(tickets, "status", default="")).lower(),
        ticket_url=_url(_get(d, "ticketUrl", default="")) or _url(_get(tickets, "url", default="")),
        resale_url=_url(_get(d, "ticketswapUrl", "resaleUrl", default="")),
        price=f"€{float(price):.2f}".replace(".", ",") if isinstance(price, (int, float)) else str(price or ""),
        on_sale_at=_dt(_get(tickets, "onSaleAt", "onSale", "saleStart", default=None) or _get(d, "onSaleAt", default=None)),
        presales=presales,
        image=_url(_get(d, "imageUrl", "image", default="")),
        genres=genres,
        spotify_url=spotify,
        apple_music_url=apple,
        festival=(festival.get("name", "") if isinstance(festival, dict) else str(festival or "")),
    )


def matches(concert: Concert, artist: str) -> bool:
    target = normalize_name(artist)
    return any(normalize_name(a) == target for a in concert.artists) or normalize_name(concert.title) == target


def upcoming(concerts: list[Concert], artist: str, today: date) -> list[Concert]:
    return [c for c in concerts if c.date >= today and matches(c, artist)]
