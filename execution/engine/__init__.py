# cash-only invariant: Bundle 2 main.py (m2.6) MUST call through
# execution.risk.cash_only.check_sell_covered before any sell order
# reaches the connector. The validator runner already enforces this via
# the leverage validator, but the engine's own pre-submit path is the
# backstop -- never add a shortcut that skips validators for sells.
"""Engine main loop.

Phase 2 scope: main.py -- read approved strategy from vault, evaluate
entry/exit signal, construct order, run validators, submit to IBKR,
append to decision journal.

Loop shape (Phase 2 MVP):

    1. Check .killed lock file. If present, log and exit 0 (no orders).
    2. Load strategies from wiki/strategies/*.md with status: approved.
       Refuse if none approved.
    3. For each approved strategy, evaluate its signal logic against
       current market data from IBKR.
    4. If entry/exit signal fires, construct the Order object.
    5. Run order through ALL validators (validators/*). Any reject ->
       journal the rejection with named rule + reason, return to next
       strategy.
    6. If all validators pass, submit via connectors.ibkr.submit().
       Receive fill. Journal the fill.
    7. Update circuit-breaker state from fill P&L. If breaker trips,
       journal + alert + (if total breaker) write .killed.

Phase 2 runs this loop manually (invest-execute skill triggers it via
Claude wrapper). Phase 4 moves it under pm2 cron trigger on the Mac
Mini.
"""
