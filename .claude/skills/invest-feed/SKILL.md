---
name: invest-feed
description: Scheduled RSS poller that lands raw items in raw/news/. Phase 2 MVP wires one source end-to-end to prove the polling pattern; additional sources (earnings calendar, SEC filings, macro releases) land in Phase 4 only if watchlist coverage gaps surface during burn-in. Use when Keith says /feed, "pull the feed", "show me the feed", or automatically via pm2 cron during market hours.
tier: Trader
phase: 2
status: stub
---

# invest-feed (STUB -- Phase 2 build work)

Stub skill. Implementation is Phase 2 milestone 2.10. Specs below.

## MVP shape

**Source for Phase 2:** one RSS feed. Candidate defaults (Keith picks during Phase 2 kickoff):
- SeekingAlpha broad-market feed
- Bloomberg Markets (if RSS accessible)
- Barron's front page
- SEC EDGAR recent filings (if RSS-able at MVP)

Only one source is wired to prove the pattern. Additional sources are Phase 4 work.

**Pipeline:**
1. On invocation (or cron tick), fetch the feed URL.
2. Parse new items (deduplicate against `raw/news/` via URL hash or item id).
3. For each new item, write `raw/news/YYYY-MM-DD_news_<short-slug>_<hash8>.md` (where `<hash8>` is the first 8 chars of `source-hash`). The hash suffix prevents filename collisions when two items share a date + headline slug -- common on RSS feeds that publish multiple articles with similar titles. Filenames:
   ```yaml
   ---
   tags: [raw, news, <source-slug>]
   date: YYYY-MM-DD
   type: news
   origin: k2bi-extract
   source: <feed name>
   source-url: <original URL>
   source-hash: <sha256 of URL for dedupe>
   up: "[[index]]"
   ---

   # <headline>

   <item summary or excerpt>

   [link to source](<URL>)
   ```
4. Append via `scripts/wiki-log-append.sh`.
5. Log one-line summary: "fetched N new items from <source>".

**Cron cadence:**
- Phase 2: every 30 min during US market hours (Mon-Fri 09:30-16:00 ET). Set in `pm2/ecosystem.config.js`.
- Phase 4: expand cadence + add out-of-hours pulls if burn-in shows missing news on open.

## Non-goals (not in Phase 2)

- Multi-source feed (1 source proves pattern)
- Earnings calendar integration (Phase 4 if coverage gap)
- SEC filing RSS parser (Phase 4 if 10-K / 10-Q / 8-K coverage gap)
- MCP `netanelavr/trading-mcp` integration (Phase 4 if screener-driven ingestion is needed)
- Reddit sentiment ingestion (Phase 4 if thesis needs it)

## Hard rule

Only writes to `raw/news/`. Never writes to `wiki/`. The compile step (`/compile`) is what digests raw items into wiki pages; feed is ingestion only.
