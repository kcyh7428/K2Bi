---
name: invest-execute
description: Claude-side wrapper for the Python execution engine. Reads engine state from the vault (kill-switch, last decision journal entries, open positions), surfaces it to Keith, and can manually trigger a run of the engine's main loop. Does NOT call any validator or bypass; the engine owns all enforcement. Use when Keith says /execute, "run the engine", "what's the engine doing", "show me the last trades", "is the kill switch on".
tier: trader
phase: 2
status: shipped
---

# invest-execute

Thin Claude wrapper over the Python execution engine (`execution/engine/main.py`).

Claude orchestrates; the engine enforces. Every sub-command below reads or triggers; none of them bypass a validator, delete `.killed`, or submit an order outside the engine process.

## Sub-commands

Keith triggers with `/execute <sub>`. If `<sub>` is omitted, default to `status`.

### `/execute status`

Surface a one-screen summary of engine state.

```bash
VAULT="$HOME/Projects/K2Bi-Vault"
KILL_FILE="$VAULT/System/.killed"
JOURNAL_DIR="$VAULT/raw/journal"
TODAY_JOURNAL="$JOURNAL_DIR/$(date -u +%Y-%m-%d).jsonl"
```

Show:

1. **Kill switch** — present or absent; if present, who wrote it + when (`jq . "$KILL_FILE"`).
2. **Last 10 journal events** — `tail -n 10 "$TODAY_JOURNAL"` piped through `jq -c '{ts, event_type, strategy, ticker, side, qty}'` so the line-per-event digest fits a terminal width.
3. **Open positions** — parsed from the most recent `engine_started` or `engine_recovered` event's `adopted_positions` payload. Engine owns the authoritative list; Claude does NOT call `ib_async` directly.
4. **Last submitted orders with broker IDs** — grep for `event_type=order_submitted` in today's journal, show `ticker`, `side`, `qty`, `broker_order_id`, `broker_perm_id`, `ts`.

Render as a Markdown report with headings `### Kill`, `### Recent events`, `### Open positions`, `### Last submitted orders`. Keep under 30 lines on a typical day.

### `/execute run`

Trigger one tick of the engine main loop.

IB Gateway and the engine both run on the VPS. The engine connects to the gateway natively at `127.0.0.1:4002`. Manually triggering one tick from the MacBook means SSH'ing to the VPS and invoking the engine module there:

```bash
scripts/ssh-vps.sh "cd ~/Projects/K2Bi && .venv/bin/python3 -m execution.engine.main --once"
```

The `k2bi-engine.service` systemd unit is the normal driver; `--once` is for ad-hoc operator ticks only. Do NOT try to run the engine on the MacBook -- it cannot reach the gateway and there is no tunnel by design.

After the tick returns, surface the newest journal events it produced by comparing `$(wc -l "$TODAY_JOURNAL")` before and after the call; print only the delta.

### `/execute journal`

Pretty-print today's journal with one event per line.

```bash
VAULT="$HOME/Projects/K2Bi-Vault"
JOURNAL="$VAULT/raw/journal/$(date -u +%Y-%m-%d).jsonl"
if [ ! -f "$JOURNAL" ]; then
  echo "No journal for today yet."
  exit 0
fi
jq -c '{ts, event_type, strategy, trade_id, ticker, side, qty, broker_order_id, broker_perm_id, payload}' < "$JOURNAL"
```

Accept an optional `--since <ISO-ts>` filter to narrow output.

### `/execute kill-status`

Fast check. Print one line:

```bash
VAULT="$HOME/Projects/K2Bi-Vault"
KILL_FILE="$VAULT/System/.killed"
if [ -f "$KILL_FILE" ]; then
  TS=$(jq -r .ts "$KILL_FILE")
  WHY=$(jq -r .reason "$KILL_FILE")
  SRC=$(jq -r .source "$KILL_FILE")
  echo "kill active since ${TS} (source=${SRC}, reason=${WHY})"
else
  echo "kill inactive"
fi
```

## Read-only boundary (architectural)

Claude **cannot**:

- Edit `execution/validators/config.yaml` (invest-propose-limits drafts a delta into `review/strategy-approvals/` for Keith's explicit approval; only `/invest-ship` lands the edit).
- Delete `.killed` (human-only filesystem operation; no Claude-accessible path modifies it).
- Place orders directly (the IBKR connector in `execution/connectors/ibkr.py` is invoked only inside the engine's `run_forever` or `run_once`).
- Bypass any validator (the engine's `run_all` short-circuits on the first rejection; there is no `--force` flag).

Claude **can**:

- Trigger a tick via `/execute run`; the engine decides whether to submit based on its own state, strategies, validators, and breakers.
- Read any file the engine writes to the vault (journal, `.killed`, engine-started payload).
- Surface engine state to Keith for human judgment.

## Hard rule

Any edit to this skill that grants Claude the ability to override a validator, delete `.killed`, or submit orders without going through the engine is a rejection during `/invest-ship` Codex review. The boundary is architectural, not convenience.

## Pedagogical layer (Teach Mode)

At the start of every invocation, read the learning-stage dial:

```bash
LEARNING_STAGE=$(grep -E '^learning-stage:' ~/Projects/K2Bi-Vault/System/memory/active_rules.md 2>/dev/null | sed 's/learning-stage: *//' | tr -d '[:space:]')
LEARNING_STAGE=${LEARNING_STAGE:-novice}
```

If `$LEARNING_STAGE` is `novice` or `intermediate`, append a **Why this matters for your position** footer after the standard output whenever the command produced a fill or a kill transition. The footer covers:

- Position change: new qty, new percentage of NAV, cost basis
- Stop-loss level translated to both ticker-side dollars and HKD account risk
- Daily risk envelope remaining
- Any correlation / concentration flag active
- Watch-points (next strategy rule that would close the position)

Example after a `/execute run` that filled a 70-share SPY buy:

```markdown
**Fill received:**

- trade-id: T-2026-04-22-0017
- ticker: SPY
- action: buy
- quantity: 70
- fill-price: $498.32
- slippage-vs-expected: -$0.08 (0.016%)

---
**Why this matters for your position:**

You now hold 70 SPY at HK$50,820 (5.1% of NAV). Stop-loss $448.49 -> HK$5,082 max loss -> exactly at 1% trade-risk cap. Daily risk budget: fully consumed; no further buys until tomorrow. No correlated positions open. Take-profit at Friday close per strategy rules.
```

If `$LEARNING_STAGE` is `advanced`, skip the footer.

Terms appearing for the first time that exist in `K2Bi-Vault/wiki/reference/glossary.md` render as `[[glossary#term-name]]` wiki-links (first occurrence only per output). Terms not yet in the glossary get stubbed at the bottom of the glossary file in the same skill run per the Teach Mode convention in `CLAUDE.md`.

## Engine internals (reference, not operational)

The Python engine in `execution/engine/main.py` is a state machine with these states:

- `INIT` — startup; connect to IBKR, reconcile journal vs broker
- `CONNECTED_IDLE` — healthy, waiting for the next tick
- `PROCESSING_TICK` — evaluating approved strategies
- `SUBMITTING` — order in flight to IBKR
- `AWAITING_FILL` — broker acknowledged, waiting for fill / partial / rejection
- `RECONCILING` — fill received, updating positions, journaling
- `KILLED` — `.killed` file present; no new orders submitted
- `DISCONNECTED` — IB Gateway unreachable; exponential-backoff reconnect
- `SHUTDOWN` — graceful exit

State transitions are fully specified in `K2Bi-Vault/wiki/planning/m2.6-engine-state-machine.md`. Each transition emits one or more journal events (see `K2Bi-Vault/wiki/reference/journal-schema.md`).

Claude never calls into any module under `execution/` directly. The only entry points are the bash commands above and the `k2bi-engine.service` systemd unit on the VPS.

## Non-goals (not in Phase 2)

- Continuous polling dashboard (Phase 4 if needed; the `k2bi-engine.service` systemd unit already keeps the engine alive on the VPS).
- Cross-strategy view (Phase 2 runs one strategy at a time; multi-strategy view lands in Phase 4).
- P&L attribution (Phase 2 P&L stub is manual; Bundle 5 wires IBKR fills; Phase 4 auto-attributes to strategies).
