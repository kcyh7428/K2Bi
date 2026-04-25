---
name: invest-alert
description: Push real-time signals to Keith via Telegram. Polls the decision journal for new events, classifies them into Tier 1 (critical) or Tier 2 (informational), and sends Telegram messages within 60 seconds. Used for disconnect outages, engine stops, recovery mismatches, order fills, cancellations, and kill-switch triggers. Phase 2 MVP uses a single Telegram chat; Phase 4 may add routing by event class. Use when Keith says /alert "<text>" to test-send, or automatically by other invest-* skills that detect an alert-worthy event.
tier: Trader
phase: 2
status: mvp
---

# invest-alert

Push real-time signals to Keith via Telegram. Runs on the Mac Mini as a cron-driven one-shot tick (interim until m2.19 pm2 daemon replaces cron).

## Architecture

**Journal-poll model** (not push):
1. `scripts/invest-alert-tick.sh` runs every minute from cron.
2. Calls `scripts/invest_alert_lib.py` to classify new journal events.
3. Sends any alerts via `scripts/send-telegram.sh`.
4. Logs to `~/Projects/K2Bi/logs/invest-alert-YYYY-MM-DD.log`.

This keeps the engine untouched and avoids adding latency or failure modes to the order path.

## Alert Tiers

### Tier 1 (always alert)

| Event | Condition | Message shape |
|---|---|---|
| `disconnect_status` | `outage_seconds > K2BI_ALERT_OUTAGE_THRESHOLD_S` (default 300s) | 🔴 T1: disconnect_status outage > 300s\nOutage: Nh\nAttempts: N\nError: class |
| `engine_stopped` | any | 🔴 T1: engine_stopped\nPID: N\nReason: reason |
| `recovery_state_mismatch` | any, even with override | 🔴 T1: recovery_state_mismatch\nOverride: value\nMismatches: N |

### Tier 2 (always alert)

| Event | Condition | Message shape |
|---|---|---|
| `order_filled` | any | 🟡 T2: order_filled\nticker side qty @ $price |
| `order_cancelled` | `cancel_reason != "operator_initiated"` | 🟡 T2: order_cancelled\nticker qty\nReason: reason |
| `kill_switch_triggered` | `.killed` or `kill.flag` (post-Q41) | 🟡 T2: kill_switch_triggered\nTrigger: source |

All other event types are silently skipped.

## Idempotency & Safety

- **State file**: `~/.k2bi/alert-state.json` tracks `last_processed_entry_id`.
- **Replay safe**: Re-running the classifier on the same journal produces zero alerts.
- **State corruption**: Malformed state file resets cleanly; no crash; resumes from "now".
- **Outage fire-once**: A contiguous sequence of `disconnect_status` events produces exactly one alert at the threshold crossing. The sequence resets on `reconnected` or `engine_started`.
- **Empty journal**: No events → no alerts → exit 0.

## Environment Variables

| Variable | Default | Required |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | yes |
| `K2BI_TELEGRAM_CHAT_ID` | — | yes |
| `K2BI_ALERT_OUTAGE_THRESHOLD_S` | 300 | no |
| `K2BI_VAULT_ROOT` | `~/Projects/K2Bi-Vault` | no |
| `K2BI_ALERT_STATE_DIR` | `~/.k2bi` | no |

## CLI Usage

```bash
# One-shot classification (prints JSON lines to stdout)
python3 -m scripts.invest_alert_lib

# Send a test alert
scripts/invest-alert-test.sh

# Cron entry (interim until m2.19)
* * * * * /Users/fastshower/Projects/K2Bi/scripts/invest-alert-tick.sh >/dev/null 2>&1
```

## Manual Test (`/alert`)

When Keith says `/alert "test message"`, pipe the message through `send-telegram.sh`:

```bash
echo "test message" | scripts/send-telegram.sh
```

## Acceptance Criteria (m2.9)

1. Test event (`invest-alert-test.sh`) → Telegram message within 60s.
2. `disconnect_status` with `outage_seconds > 300s` fires exactly one Tier 1 alert.
3. `order_filled` fires a Tier 2 alert with ticker, qty, and price.
4. Replay of the same journal produces zero duplicate alerts.
5. Malformed state file does not crash the classifier.

## Non-Goals (not in Phase 2 / Bundle 5a)

- pm2 daemonization (m2.19 in Bundle 5 remainder)
- IBC auto-restart for Gateway (m2.19)
- Interactive Telegram acks or commands (Phase 4+)
- P&L notifications / daily summaries (deferred)
- Alert deduplication beyond idempotency on `journal_entry_id`
- Multi-channel routing

## Related

- `scripts/invest_alert_lib.py` -- classifier implementation
- `scripts/send-telegram.sh` -- Telegram sender
- `scripts/invest-alert-tick.sh` -- cron wrapper
- `scripts/invest-alert-test.sh` -- test primitive
- `execution/journal/schema.py` -- journal event taxonomy
