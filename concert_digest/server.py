"""MCP server giving a Grok bot the tools to build Concertje-styled concert emails.

Grok does the research (reviews, scores, Belgian shows, ticket dates) and calls:

  lookup_album_links   Apple Music / Spotify links + artwork for an artist's albums
  render_digest        the weekly digest email
  render_ticket_alert  a single-show "tickets are live" / "on sale soon" email

Renders are stored on disk and served at an unguessable preview URL, so the tool
result stays small; pass include_html=true to get the HTML itself.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, ValidationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

from .links import lookup_album_links as _lookup
from .models import Alert, Artist, Digest, Show
from .render import render_alert as _render_alert, render_digest as _render_digest

log = logging.getLogger("concert_digest")

INSTRUCTIONS = """\
You build a Concertje-styled email about acclaimed albums whose artists are playing Belgium.

Workflow:
1. Research recent album reviews (Pitchfork, Stereogum, The Guardian, NME, Rolling Stone, The Quietus, Clash...).
   For each review note the publication, URL, album, score (as shown and normalised to 0-10), any accolade,
   and a one-sentence verdict.
2. For each artist, find upcoming shows in Belgium (concertje.be is the best source) with venue, city, date,
   the Concertje page, ticket status, ticket link and the on-sale date/time if tickets aren't on sale yet.
   Drop artists without Belgian shows.
3. Call lookup_album_links for each remaining artist with the reviewed album title(s).
4. Write a 2-3 sentence bio and pick up to 4 genres per artist.
5. Call render_digest with everything, best-reviewed artist first. Share the preview_url.
Use render_ticket_alert for a single show going on sale. Never invent scores, dates or links:
leave a field empty when you couldn't verify it.
"""

RENDER_ID = re.compile(r"^[A-Za-z0-9_-]{16,32}$")


def render_dir() -> Path:
    d = Path(os.environ.get("RENDER_DIR") or Path.home() / ".concert-digest" / "renders").expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def public_base_url() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def store(html: str) -> str:
    render_id = secrets.token_urlsafe(16)
    (render_dir() / f"{render_id}.html").write_text(html, encoding="utf-8")
    return render_id


def result(subject: str, html: str, include_html: bool) -> dict:
    render_id = store(html)
    out = {"subject": subject, "preview_url": f"{public_base_url()}/r/{render_id}", "html_bytes": len(html.encode())}
    if include_html:
        out["html"] = html
    return out


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())


mcp = MCPServer(name="concert-digest", title="Concert Digest", instructions=INSTRUCTIONS, version="0.2.0")


@mcp.tool(description="Find Apple Music and Spotify links plus cover art for an artist's albums. "
          "Put the reviewed album(s) in `albums`; the artist's other recent full-lengths are added after them. "
          "Returns entries ready for the `albums` field of render_digest.")
async def lookup_album_links(
    artist: str,
    albums: list[str] = Field(default_factory=list, description="Album titles to find first, e.g. the reviewed album."),
    country: str = Field("BE", description="Store country (ISO code)."),
    limit: int = Field(3, ge=1, le=4, description="Maximum albums to return."),
) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            return await _lookup(client, artist, albums, country, limit)
        except httpx.HTTPError as exc:
            raise ToolError(f"Album lookup failed: {exc}") from exc


@mcp.tool(name="render_digest", description="Render the Concertje-styled digest email: a 2-column grid of concert tiles, then one section "
          "per artist with scores, bio, critic quote, album links and Belgian shows with ticket buttons. "
          "Returns the subject and a preview URL (and the HTML if include_html is true).")
def render_digest_email(
    artists: list[Artist] = Field(description="Artists with reviews and Belgian shows, best-reviewed first."),
    headline: str = Field("The critics loved them. Now they’re coming to Belgium."),
    intro: str = Field("", description="Line under the headline; leave empty for an automatic one."),
    sources: list[str] = Field(default_factory=list, description="Publications you read, named in the intro."),
    include_html: bool = Field(False, description="Also return the full HTML (about 30-80 KB)."),
) -> dict:
    try:
        digest = Digest(artists=artists, headline=headline, intro=intro, sources=sources)
    except ValidationError as exc:
        raise ToolError(_validation_message(exc)) from exc
    subject, html = _render_digest(digest)
    return result(subject, html, include_html)


@mcp.tool(description="Render a single-show ticket email in the same style: 'on_sale_now' (buy button) or "
          "'on_sale_soon' (calendar reminder at the sale time, presales listed).")
def render_ticket_alert(
    kind: Literal["on_sale_now", "on_sale_soon"] = Field(description="'on_sale_now' or 'on_sale_soon'."),
    artist: str = Field(description="Artist name."),
    show: Show = Field(description="The show, same shape as in render_digest."),
    image_url: str = Field("", description="Artist image, used if the show has none."),
    message: str = Field("", description="Optional custom line."),
    include_html: bool = Field(False),
) -> dict:
    try:
        alert = Alert(kind=kind, artist=artist, show=show, image_url=image_url, message=message)
    except ValidationError as exc:
        raise ToolError(_validation_message(exc)) from exc
    subject, html = _render_alert(alert)
    return result(subject, html, include_html)


@mcp.custom_route("/r/{render_id}", methods=["GET"])
async def preview(request: Request):
    render_id = request.path_params["render_id"]
    path = render_dir() / f"{render_id}.html"
    if not RENDER_ID.match(render_id) or not path.is_file():
        return PlainTextResponse("Not found", status_code=404)
    return HTMLResponse(path.read_text(encoding="utf-8"), headers={"X-Robots-Tag": "noindex"})


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request):
    return JSONResponse({"ok": True})


class BearerAuth:
    """Requires `Authorization: Bearer <MCP_TOKEN>` on the MCP endpoint (xAI sends it via `authorization`)."""

    def __init__(self, app, token: str, path: str = "/mcp"):
        self.app, self.token, self.path = app, token, path

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(self.path):
            headers = dict(scope.get("headers") or [])
            given = headers.get(b"authorization", b"").decode()
            if not secrets.compare_digest(given, f"Bearer {self.token}"):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def http_app(host: str):
    allowed = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    app = mcp.streamable_http_app(
        host=host,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=bool(allowed), allowed_hosts=allowed, allowed_origins=[],
        ),
    )
    token = os.environ.get("MCP_TOKEN", "")
    return BearerAuth(app, token) if token else app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="concert-digest-mcp", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transport", choices=["http", "stdio"], default="http")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.transport == "stdio":
        mcp.run("stdio")
        return 0
    if args.host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("MCP_TOKEN"):
        print("Refusing to listen on a public interface without MCP_TOKEN set.", file=sys.stderr)
        return 2
    import uvicorn

    log.info("MCP endpoint: %s/mcp · previews: %s/r/<id>", public_base_url(), public_base_url())
    uvicorn.run(http_app(args.host), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
