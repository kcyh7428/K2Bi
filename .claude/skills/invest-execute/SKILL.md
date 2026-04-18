---
name: invest-execute
description: Claude-side wrapper for the Python execution engine. Reads engine state from the vault (kill-switch, last decision journal entries, open positions), surfaces it to Keith, and can manually trigger a run of the engine's main loop. Does NOT call any validator or bypass; the engine owns all enforcement. Use when Keith says /execute, "run the engine", "what's the engine doing", "show me the last trades", "is the kill switch on".
tier: Trader
phase: 2
status: stub
---

# invest-execute (STUB -- Phase 2 build work)

Stub skill. Implementation is Phase 2 milestone 2.8. Specs below.

## MVP shape

**Sub-commands:**
- `/execute status` -- print .killed state, last 10 decision journal entries, open IBKR positions (from engine's cached state, NOT a fresh ib_async call by Claude)
- `/execute run` -- trigger one pass of the engine main loop (in Phase 2, invoke locally; in Phase 4, SSH the Mini and launch via pm2 restart)
- `/execute journal` -- tail today's `raw/journal/YYYY-MM-DD.jsonl` with pretty-printing
- `/execute kill-status` -- show whether `.killed` is present and when it was written

**Read-only boundary:**
Claude CANNOT:
- Edit `execution/validators/config.yaml` (invest-propose-limits drafts a delta into review/)
- Delete `.killed` (human-only)
- Place orders directly (only the engine process can submit via ibkr.py)
- Bypass any validator (the engine refuses on a reject, period)

Claude CAN:
- Trigger a run (the engine decides whether to act based on its own state)
- Read any file the engine writes to the vault
- Surface engine state to Keith for human judgment

## Non-goals (not in Phase 2)

- Continuous polling dashboard (Phase 4 if needed; pm2 cron already keeps engine alive)
- Cross-strategy view (Phase 2 runs one strategy; multi-strategy view is Phase 4)
- P&L attribution (Phase 2 P&L stub is manual; Phase 4 auto-attributes to strategies)

## Hard rule

Any Phase 2 edit to this skill that grants Claude the ability to override a validator, delete .killed, or submit orders without going through the engine is a rejection during /invest-ship Codex review. The boundary is architectural, not convenience.
