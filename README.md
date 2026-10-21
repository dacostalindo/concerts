# Concert Digest

Reads this month's album reviews from **Pitchfork, Stereogum, The Guardian, NME,
Rolling Stone, The Quietus and Clash**. It checks the
[Concertje](https://concertje.be) MCP server to see which of those artists are
playing Belgium, then emails you a digest styled after Concertje. Each artist
gets:

- a short bio (Wikipedia) and the critic's one-line verdict
- scores per publication plus an average out of 10 (Pitchfork's score, NME and Rolling Stone stars, Stereogum's Album of the Week)
- genres (from Concertje and the reviews)
- Apple Music and Spotify links: the reviewed album first, then recent full-lengths
- each Belgian show, with its ticket status:
  - **on sale**: *Buy tickets*
  - **sale date known**: the date and time, any presales, and *Remind me & buy when live*
  - **not announced yet**: *Tell me when tickets drop*
  - **sold out**: TicketSwap resale

## How "Remind me" works

An email can't remember a click, so the button opens a pre-filled email to
yourself (`[Concert Digest] WATCH #16850 · Artist @ Venue · date`). Send it.
The next `concert-digest watch` run reads it over IMAP and adds the show to
your watchlist. You then get:

1. a confirmation,
2. a heads-up about 24 hours before tickets go on sale (with any presale),
3. a **"Tickets are live"** email with the buy link once the sale opens.

Each of these has a *Stop watching* link that works the same way. Nothing
is bought automatically, because ticket sellers forbid bots; the alert
gets you to the checkout page in time.

## Setup

```bash
uv venv && uv pip install -e .
cp .env.example .env   # fill in your Gmail app password
```

Log in to Concertje once. This opens your browser, and the tokens (including
a refresh token) are saved in `~/.concert-digest`:

```bash
.venv/bin/concert-digest auth
```

The login redirects to `http://localhost:8765/callback`. If your browser runs on
a different machine than the script, copy the URL from the address bar after
logging in and paste it into the terminal.

`auth` also prints the Concertje MCP tools and which one is used to look up
artists. If it picks the wrong tool, set `CONCERTJE_SEARCH_TOOL` and
`CONCERTJE_SEARCH_ARG` in `.env`.

## Usage

```bash
concert-digest preview --out preview.html     # sample concerts, no login needed
concert-digest digest --dry-run --out digest.html
concert-digest digest                          # send it
concert-digest watch                           # process Remind-me mails + send alerts
concert-digest watchlist
```

Shows that were already in a sent digest are not repeated
(`--include-featured` overrides this). Use `--sources pitchfork,stereogum` to
limit which publications are read.

### Schedule (cron)

```cron
# Digest every Friday at 08:00, watchlist every 15 minutes
0 8 * * 5   cd /path/to/concerts && .venv/bin/concert-digest digest
*/15 * * * * cd /path/to/concerts && .venv/bin/concert-digest watch
```

If the Concertje login ever expires, commands exit with code 2 and print
`run concert-digest auth`.

## Notes

- Scrapers read public RSS feeds. Scores come from the review page where the
  feed lacks them. The Guardian, The Quietus and Clash publish no machine-readable
  score, so their reviews show without one. Site changes break a single source
  without stopping the rest; failures appear in the log.
- Concertje's MCP tool names aren't documented publicly. The client discovers
  them at runtime and reads concerts from any JSON shape that has a date plus a
  venue or artists. It understands the fields Concertje uses on its website
  (`tickets.onSaleAt`, `presales`, `ticketswapUrl`, …).
- Apple Music links come from the iTunes Search API (no key needed). For albums
  too new to be indexed, the email links to an Apple Music search instead.

## Tests

```bash
uv pip install -e '.[dev]' && .venv/bin/pytest
```
