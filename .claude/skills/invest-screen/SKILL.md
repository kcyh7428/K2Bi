---
name: invest-screen
description: Manual ticker intake that writes to wiki/watchlist/ with a structured rubric (moat, balance sheet, thesis-fit, catalysts, risk flags). Phase 2 MVP takes ticker symbols from Keith directly; real multi-factor screening with data-feed input lands in Phase 4 only if watchlist coverage gaps surface during burn-in. Use when Keith says /screen <SYMBOL>, "add X to watchlist", "screen X", "put X on the watchlist with reason Y".
tier: Analyst
routines-ready: true
phase: 2
status: stub
---

# invest-screen (STUB -- Phase 2 build work)

Stub skill. Implementation is Phase 2 milestone 2.13. Specs below.

## MVP shape

**Input:** `<SYMBOL>` (required). Optional `--reason <text>` to pre-populate the rubric rationale.

**Pipeline:**
1. Create or update `wiki/watchlist/<SYMBOL>.md` with frontmatter:
   ```yaml
   tags: [watchlist, <SYMBOL>]
   date: YYYY-MM-DD
   type: watchlist
   origin: keith | k2bi-extract
   symbol: <SYMBOL>
   added: YYYY-MM-DD
   moat: strong | medium | weak | n/a
   balance-sheet: strong | medium | weak | stressed
   thesis-fit: 1-5
   catalysts: [upcoming earnings, regulatory, management change, ...]
   risk-flags: [concentration, leverage, cyclicality, ...]
   status: active | removed
   up: "[[index]]"
   ```
2. Body: human-readable rationale. Keith fills this via multi-turn if not passed as `--reason`.
3. Append via `scripts/wiki-log-append.sh`.

## Routines-Ready discipline (Analyst tier)

- **Stateless:** each run reads + writes the watchlist file only
- **Vault-in/vault-out:** watchlist page round-trip
- **Schedulable:** trivially schedulable (stale-watchlist cleanup job, Phase 4)
- **JSON I/O:** frontmatter is YAML-serializable
- **Self-contained prompts:** no cross-skill dependency

## Non-goals (not in Phase 2)

- Automated screening against feed data (Phase 4 if needed)
- Auto-removal based on thesis expiry (Phase 4 if stale-watchlist coverage gaps surface)
- Peer ranking within sector (Phase 4)
