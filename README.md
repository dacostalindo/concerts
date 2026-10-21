# Concert Digest MCP tools for Grok

A small MCP server that gives a Grok bot what it needs to produce the
Concertje-styled concert email. Grok does the research (album reviews, scores,
Belgian shows, ticket dates) and fills in the data. This server only renders
the result and looks up album links.

| Tool | What it does |
| --- | --- |
| `lookup_album_links` | Apple Music + Spotify links and cover art for an artist's albums (reviewed album first). |
| `render_digest` | The digest email: a 2-column grid of Concertje concert tiles, then one section per artist with scores, bio, critic quote, album links and Belgian shows with ticket buttons. |
| `render_ticket_alert` | A single-show email: `on_sale_now` (buy button) or `on_sale_soon` (calendar reminder at the sale time). |

The render tools return `{subject, preview_url, html_bytes}`. The HTML is saved
on the server and served at an unguessable `preview_url`, which keeps Grok's
context small. Pass `include_html: true` to get the HTML itself, for example to
send it.

The workflow Grok should follow is in the server's MCP `instructions`, and each
input field has a description, so Grok knows what to fill in. See
[examples/digest.example.json](examples/digest.example.json) for a complete
`render_digest` input.

## Run it

```bash
uv venv && uv pip install -e .
cp .env.example .env   # set MCP_TOKEN and PUBLIC_BASE_URL
set -a; . ./.env; set +a
HOST=0.0.0.0 .venv/bin/concert-digest-mcp     # serves /mcp, /r/<id> and /healthz on :8000
```

Grok calls the server from xAI's side, so it needs a public HTTPS address. A
tunnel works, for example `cloudflared tunnel --url http://localhost:8000`.
Put the resulting address in `PUBLIC_BASE_URL`.

The server refuses to listen on a public interface unless `MCP_TOKEN` is set.
For local MCP clients there is also `--transport stdio`.

## Connect Grok (xAI Responses API)

```python
import os
from openai import OpenAI

client = OpenAI(api_key=os.environ["XAI_API_KEY"], base_url="https://api.x.ai/v1")
response = client.responses.create(
    model="grok-4.7",
    tools=[{
        "type": "mcp",
        "server_url": "https://concert-digest.example.com/mcp",
        "server_label": "concert_digest",
        "authorization": os.environ["MCP_TOKEN"],
    }],
    input="Make this week's concert digest: well-reviewed albums from the last 3 weeks "
          "whose artists play Belgium. Give me the preview link.",
)
print(response.output_text)
```

Grok still needs its own research tools (web search; optionally Concertje's MCP
at `https://ai.concertje.be/mcp`, which requires a Concertje login).

## Input notes

- Ticket status is one of `on_sale`, `upcoming` (requires `on_sale_at`),
  `not_announced`, `sold_out` or `cancelled`. Times without a timezone are read
  as Brussels time.
- A show still marked `upcoming` after its sale time has passed renders as on sale.
- For `upcoming` shows, the reminder button is a Google Calendar event at the
  first presale or general sale.
- Tile images use the show image, then the artist image, then the first album
  cover.
- Invalid input comes back as a tool error naming the field, so Grok can fix it
  and retry.

## Tests

```bash
uv pip install -e '.[dev]' && .venv/bin/pytest
```

The earlier version, which scraped critics, queried Concertje, sent mail and
ran a watchlist, is in commit `1ba4323`.
