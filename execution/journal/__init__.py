"""Decision journal (append-only JSONL).

Phase 2 scope: writer.py -- append each event to
    ~/Projects/K2Bi-Vault/raw/journal/YYYY-MM-DD.jsonl

Guarantees:
    - Append-only: no in-place rewrites, no partial-write corruption.
    - Restart-safe: each line is a complete JSON object flushed + fsynced
      before return; process crash mid-write loses at most the current
      event, never corrupts prior entries.
    - Schema forward-compat: readers tolerate unknown keys; writers must
      include required fields (ts, event_type, payload) but may extend.

Required fields per event:
    ts          ISO-8601 UTC with microsecond precision
    event_type  one of {validator_pass, validator_reject, order_submitted,
                order_filled, breaker_triggered, kill_switch_written,
                kill_switch_cleared}
    strategy    slug of the strategy owning this event (if applicable)
    payload     object with event-specific data

See raw/journal/index.md in the vault for the full schema contract.
"""
