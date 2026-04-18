---
name: invest-regime
description: Manually classify the current market regime (risk-on, risk-off, choppy, trending, volatility-expansion, etc.) and atomically update wiki/regimes/current.md. Auto-detection using feed data lands in Phase 4 only if regime-mismatched trades surface during burn-in. Use when Keith says /regime, "classify regime", "update regime to X", "what's the current regime".
tier: Analyst
routines-ready: true
phase: 2
status: stub
---

# invest-regime (STUB -- Phase 2 build work)

Stub skill. Implementation is Phase 2 milestone 2.14. Specs below.

## MVP shape

**Input:** `<classification>` optional; if absent, runs multi-turn conversation to decide.

**Pipeline:**
1. Read `wiki/regimes/current.md` to show Keith what's currently classified.
2. Multi-turn: ask about VIX level, SPY trend, rate direction, credit spreads, geopolitical/macro setup. Keith narrates; skill distills.
3. Atomic write `wiki/regimes/current.md` via temp + rename:
   ```yaml
   tags: [regime, current]
   date: YYYY-MM-DD
   type: regime
   origin: keith
   classification: risk-on | risk-off | choppy | trending-up | trending-down | volatility-expansion | volatility-contraction
   confidence: 1-5
   rationale: "..."
   valid-until: YYYY-MM-DD  # default 7 days out, Keith can override
   previous: <classification-from-prior-regime>
   up: "[[index]]"
   ```
4. Append via `scripts/wiki-log-append.sh`.
5. Archive previous regime to `wiki/regimes/<YYYY-MM-DD>_<classification>.md` (one archive per classification change).

## Routines-Ready discipline (Analyst tier)

- **Stateless:** each run reads + writes regime files only
- **Vault-in/vault-out:** current.md + archive round-trip
- **Schedulable:** stale-regime alerter (Phase 4) runs as cron when `valid-until` passes
- **JSON I/O:** classification is YAML-serializable
- **Self-contained prompts:** regime taxonomy lives in this skill body

## Non-goals (not in Phase 2)

- Auto-detection from VIX + term-structure feeds (Phase 4 if regime-mismatched trades appear)
- Regime-conditional strategy gating at execution time (Phase 4 validator, if needed)
- Per-sector regime classification (Phase 4 if single-regime is insufficient)

## Engine use

Strategies may reference `regime-required:` in their frontmatter. In Phase 2 this is an advisory tag; Phase 4 makes it a deterministic validator if burn-in shows regime-mismatched trades slipping through.
