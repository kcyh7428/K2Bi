---
name: invest-backtest
description: Yfinance sanity-check backtest for a strategy spec. 2-year window, basic Sharpe / max-DD / win-rate. Explicitly NOT walk-forward (walk-forward harness is Phase 4 only, triggered by overfit signs during burn-in or when a second strategy needs it). Use when Keith says /backtest <strategy>, "backtest this", "sanity check the strategy on 2 years".
tier: Analyst
routines-ready: true
phase: 2
status: stub
---

# invest-backtest (STUB -- Phase 2 build work)

Stub skill. Implementation is Phase 2 milestone 2.15. Specs below.

## MVP shape

**Input:** `<strategy-slug>` (required, maps to `wiki/strategies/<slug>.md`).

**Pipeline:**
1. Read `wiki/strategies/<slug>.md` -- extract symbol, entry rules, exit rules, sizing logic.
2. Pull 2 years of daily yfinance bars for the symbol (+ SPY for reference).
3. Simulate the strategy day-by-day: apply entry/exit rules, track position, compute daily P&L.
4. Compute metrics: annualized Sharpe, max drawdown, win rate, average winner, average loser, total return, number of trades.
5. Sanity gate: if total return > 500% OR max DD < 2% OR win rate > 85%, flag as "suspicious, probable look-ahead cheat" and refuse to write results (forces Keith to review the strategy logic).
6. Write backtest block to `wiki/strategies/<slug>.md` frontmatter:
   ```yaml
   backtest-window: "2024-01-01 to 2026-04-18"
   backtest-source: yfinance
   sharpe: <float>
   max-dd-pct: <float>
   win-rate-pct: <float>
   avg-winner-pct: <float>
   avg-loser-pct: <float>
   total-return-pct: <float>
   n-trades: <int>
   look-ahead-check: passed | suspicious
   backtest-last-run: YYYY-MM-DD
   ```
7. Append via `scripts/wiki-log-append.sh`.

## Routines-Ready discipline (Analyst tier)

- **Stateless:** each run reads strategy spec + pulls yfinance, writes metrics
- **Vault-in/vault-out:** strategy spec round-trip with metrics written back
- **Schedulable:** can run nightly to refresh metrics (Phase 4 if needed)
- **JSON I/O:** all metrics YAML-serializable
- **Self-contained prompts:** no cross-skill dependency

## Non-goals (not in Phase 2)

- **Walk-forward harness.** Rolling windows + embargoed k-fold explicitly deferred to Phase 4. Trigger: second strategy being added AND sanity-check can't validate it, OR Phase 3 strategy shows overfit signs (paper performance diverges from sanity-check expectation).
- **Point-in-time data stores.** yfinance returns what it has today; Phase 2 accepts this limitation. Phase 4 adds PIT if look-ahead bias proves to be a real failure mode.
- **Multi-strategy portfolio backtest.** Phase 2 is one strategy at a time.
- **Slippage + commission modeling.** MVP uses mid-price fills. Phase 4 adds realistic cost model if first paper trade reveals meaningful slippage drag.

## Hard rule

The 500%/2%/85% sanity gate is not overridable. If it trips, the strategy does NOT get `status: approved` until Keith manually reviews + provides an explanation in the strategy note. This protects against the "11,000% P&L look-ahead cheat" pattern from 2026 retail trading research.
