"""K2Bi execution engine.

Phase 2 scaffold. The engine is a standalone Python process (not Claude Code)
that reads approved strategies from the vault and enforces deterministic
pre-trade validators, circuit breakers, and the .killed lock file before
submitting orders to IBKR DUQ paper via ib_async.

Claude generates / backtests / approves strategies and monitors engine state.
Claude CANNOT bypass validators, force a trade, or delete .killed. That
boundary is enforced by the engine running as a separate process with no
import of Claude code.

Phase 2 milestones:
    2.2 -- this package scaffolding
    2.3 -- validators (top 5)
    2.4 -- circuit breakers + kill switch
    2.5 -- IBKR connector
    2.6 -- engine main loop
    2.7 -- decision journal writer

See proposals/2026-04-18_phase2-mvp-scaffold-revision.md.
"""

__version__ = "0.0.1-scaffold"
