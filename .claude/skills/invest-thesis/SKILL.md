---
name: invest-thesis
description: Produce a ticker-level thesis using the Dave Ahern 4-phase framework (business quality, financial health, growth drivers, valuation). MVP runs one-shot via /research (NBLM-grounded variant lands in Phase 4 only if the experiment passes its 5 exit criteria). Output lands in wiki/tickers/<SYMBOL>.md frontmatter + body. Use when Keith says /thesis <SYMBOL>, "write a thesis on X", "generate thesis for X", "update thesis on X", or before a strategy is drafted that depends on a ticker.
tier: Analyst
routines-ready: true
phase: 2
status: stub
---

# invest-thesis (STUB -- Phase 2 build work)

Stub skill. Implementation is Phase 2 milestone 2.11. Specs below.

## MVP shape

**Input:** `<SYMBOL>` (required). Optional `--refresh` flag to force re-run vs. re-using recent thesis.

**Pipeline:**
1. Check `wiki/tickers/<SYMBOL>.md` -- if present with `thesis-last-verified:` within 30 days and no `--refresh`, report "fresh thesis exists, skipping" and exit.
2. Gather sources: latest 10-K, latest 10-Q, last 4 earnings transcripts, investor deck. MVP path uses `/research <SYMBOL> --sources <explicit urls>` because the default source-gathering path still has a Phase 2 port gap (see invest-research/SKILL.md).
3. Run the Dave Ahern 4-phase framework on the gathered material:
   - **Phase 1: Business Quality** -- moat, pricing power, customer concentration, TAM expansion, structural tailwinds
   - **Phase 2: Financial Health** -- revenue trajectory, margin trajectory, FCF conversion, balance sheet strength, capital allocation quality
   - **Phase 3: Growth Drivers** -- organic vs. M&A, pipeline visibility, reinvestment rate, return on incremental capital
   - **Phase 4: Valuation** -- EV/EBITDA, EV/FCF, P/E, DCF range, margin of safety vs. current price
4. Write structured thesis block to `wiki/tickers/<SYMBOL>.md` frontmatter + body:
   ```yaml
   thesis-phase-1-business-quality: "..."
   thesis-phase-2-financial-health: "..."
   thesis-phase-3-growth-drivers: "..."
   thesis-phase-4-valuation: "..."
   thesis-verdict: bull | neutral | bear
   thesis-conviction: 1-10
   thesis-last-verified: YYYY-MM-DD
   ```
5. Append via `scripts/wiki-log-append.sh`.

## Routines-Ready discipline (Analyst tier)

- **Stateless:** each run reads from vault + IBKR + source URLs, writes to vault, no process-local state
- **Vault-in/vault-out:** inputs are `wiki/tickers/<SYMBOL>.md` + sources; output is the same file + raw/research/ capture
- **Schedulable:** can run in cron (Phase 4 "refresh-stale-theses" job)
- **JSON I/O:** structured thesis block is YAML-in-frontmatter; fully machine-readable
- **Self-contained prompts:** no Claude cross-context dependencies

## Non-goals (not in Phase 2)

- NBLM-grounded thesis generation (Phase 4 if NBLM experiment passes)
- Peer-comparison auto-generation (Phase 4 if needed)
- Auto-refresh on 10-Q drop (Phase 4 if invest-feed coverage warrants)

## Source-gathering gap

The default `/research deep` source-gathering path has a Phase 2 port gap (YouTube helper + Perplexity MCP neither ship with K2Bi). Until those land, `invest-thesis` must pass explicit `--sources` to `/research` to avoid hitting the gap. See `invest-research/SKILL.md` TODO Phase 2 port.
