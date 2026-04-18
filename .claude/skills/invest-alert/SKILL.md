---
name: invest-alert
description: Push real-time signals to Keith via Telegram. Used by the execution engine for order events, fills, breaker trips, .killed writes, and by scheduled Analyst skills for regime-change alerts. Sub-1-hour delivery target. Phase 2 MVP uses a single Telegram chat; Phase 4 may add routing by event class. Use when Keith says /alert "<text>" to test-send, or automatically by other invest-* skills that detect an alert-worthy event.
tier: Trader
phase: 2
status: stub
---

# invest-alert (STUB -- Phase 2 build work)

Stub skill. Implementation is Phase 2 milestone 2.9. Specs below.

## MVP shape

**Input (programmatic, from other skills):**
```yaml
event_class: order_submitted | order_filled | breaker_triggered | kill_switch_written | kill_switch_cleared | regime_change | thesis_veto | custom
message: "short text, Telegram-ready"
severity: info | warn | critical
context: { free-form dict }
```

**Input (manual, from Keith):**
- `/alert "test message"` -- send a test message immediately
- `/alert --dry-run "would send this"` -- preview without sending

**Pipeline:**
1. Compose the outgoing message: `[event_class] [severity] message` + short context summary (first 3 key-value pairs).
2. POST to Telegram Bot API with `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` from env. Never log the token.
3. On failure (network, API error, rate limit), retry with exponential backoff up to 3 times. If still failing after retries, write a fallback entry to `raw/journal/<date>.jsonl` with `event_type: alert_failed` so the engine can log that the alert did not reach Keith.
4. On success, return the Telegram message_id for downstream references.

**Sub-1-hour delivery target:** Phase 2 budget is 60s for `order_submitted`, `order_filled`, `breaker_triggered`, `kill_switch_written`, `kill_switch_cleared`. Other classes can be batched (Phase 4 if volume warrants).

## Non-goals (not in Phase 2)

- Multi-channel routing (Phase 4 if Telegram rate-limiting or different audiences emerge)
- Interactive Telegram commands (`/invest kill` via Telegram is Phase 4 work; Phase 2 `.killed` is written by manual CLI or engine breaker)
- Alert deduplication / rollups (Phase 4 if alert storm becomes an issue)

## Secrets

- `TELEGRAM_BOT_TOKEN` -- from `.env` on the machine running the engine, never committed
- `TELEGRAM_CHAT_ID` -- same

Both are read by the engine's alert module directly. Claude does not see or write to `.env`.
