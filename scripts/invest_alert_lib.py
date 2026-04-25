"""Alert classifier for K2Bi journal events.

Reads new events from today's + yesterday's journal files since the last
processed entry, classifies them into Tier 1 or Tier 2 alerts, and returns
a list of alert dicts ready for Telegram delivery.

Idempotency: persists last-processed journal_entry_id in
~/.k2bi/alert-state.json so restarts don't re-fire historical events.

Safe against: malformed state file (reset cleanly), empty journal (no
alerts / no false positives), Tier-1 outage firing repeatedly during the
same outage (fire once at threshold crossing, not per retry tick).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_OUTAGE_THRESHOLD_S = 300
DEFAULT_VAULT_ROOT = Path.home() / "Projects" / "K2Bi-Vault"
DEFAULT_STATE_DIR = Path.home() / ".k2bi"

# Journal files are named YYYY-MM-DD.jsonl in raw/journal/
JOURNAL_DIR = "raw/journal"
STATE_FILE_NAME = "alert-state.json"


# ---------------------------------------------------------------------------
# Tier definitions
# ---------------------------------------------------------------------------

TIER_1_EVENTS = frozenset({
    "engine_stopped",
    "recovery_state_mismatch",
})

TIER_2_EVENTS = frozenset({
    "order_filled",
    "order_cancelled",
    "kill_switch_triggered",
})

# disconnect_status is handled specially (threshold-based)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Alert:
    tier: int
    event_type: str
    journal_entry_id: str
    ts: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClassifierState:
    last_processed_entry_id: str | None = None
    last_processed_ts: str | None = None
    alerted_outage_start_ts: str | None = None  # first disconnect_status ts of current alerted outage

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_processed_entry_id": self.last_processed_entry_id,
            "last_processed_ts": self.last_processed_ts,
            "alerted_outage_start_ts": self.alerted_outage_start_ts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClassifierState":
        return cls(
            last_processed_entry_id=d.get("last_processed_entry_id"),
            last_processed_ts=d.get("last_processed_ts"),
            alerted_outage_start_ts=d.get("alerted_outage_start_ts"),
        )


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _state_path(state_dir: Path) -> Path:
    return state_dir / STATE_FILE_NAME


def load_state(state_dir: Path) -> ClassifierState:
    path = _state_path(state_dir)
    if not path.exists():
        return ClassifierState()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("state file is not a dict")
        return ClassifierState.from_dict(data)
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        # Malformed state: reset cleanly. We resume from the max entry_id
        # seen in the current scan rather than replaying history.
        print(f"[warn] alert-state malformed ({e}), resetting cleanly", file=sys.stderr)
        return ClassifierState()


def save_state(state: ClassifierState, state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _state_path(state_dir)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Journal reading
# ---------------------------------------------------------------------------

def _journal_dates(today: datetime.date | None = None) -> list[str]:
    """Return yesterday and today as YYYY-MM-DD strings (chronological order)."""
    if today is None:
        today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    return [yesterday.isoformat(), today.isoformat()]


def _read_journal_lines(vault_root: Path, date_str: str) -> list[dict[str, Any]]:
    path = vault_root / JOURNAL_DIR / f"{date_str}.jsonl"
    if not path.exists():
        return []
    lines: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            lines.append(record)
    return lines


def _iter_new_events(
    vault_root: Path,
    state: ClassifierState,
    today: datetime.date | None = None,
) -> list[dict[str, Any]]:
    """Read all events from today + yesterday, skipping already-processed."""
    all_events: list[dict[str, Any]] = []
    for date_str in _journal_dates(today):
        all_events.extend(_read_journal_lines(vault_root, date_str))
    if state.last_processed_entry_id is None:
        return all_events
    # Skip events up to and including the last processed id.
    # If an event lacks journal_entry_id, we warn and continue scanning
    # rather than dropping the tail.
    new_events = []
    skip = True
    for ev in all_events:
        if skip:
            entry_id = ev.get("journal_entry_id")
            if entry_id is None:
                print(f"[warn] event lacks journal_entry_id at ts={ev.get('ts', '?')}; continuing scan", file=sys.stderr)
                continue
            if entry_id == state.last_processed_entry_id:
                skip = False
            continue
        new_events.append(ev)
    return new_events


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _fmt_outage(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def _build_disconnect_alert(event: dict[str, Any], threshold: int) -> Alert | None:
    payload = event.get("payload") or {}
    outage_seconds = payload.get("outage_seconds", 0)
    if outage_seconds <= threshold:
        return None
    attempts = payload.get("attempts", "?")
    last_error = payload.get("last_error_class", "UnknownError")
    msg = (
        f"🔴 T1: disconnect_status outage > {threshold}s\n"
        f"Outage: {_fmt_outage(outage_seconds)}\n"
        f"Attempts: {attempts}\n"
        f"Error: {last_error}"
    )
    return Alert(
        tier=1,
        event_type="disconnect_status",
        journal_entry_id=event["journal_entry_id"],
        ts=event["ts"],
        message=msg,
        context={"outage_seconds": outage_seconds, "attempts": attempts},
    )


def _build_engine_stopped_alert(event: dict[str, Any]) -> Alert:
    payload = event.get("payload") or {}
    pid = payload.get("pid", "?")
    reason = payload.get("reason", "unknown")
    msg = (
        f"🔴 T1: engine_stopped\n"
        f"PID: {pid}\n"
        f"Reason: {reason}"
    )
    return Alert(
        tier=1,
        event_type="engine_stopped",
        journal_entry_id=event["journal_entry_id"],
        ts=event["ts"],
        message=msg,
        context={"pid": pid, "reason": reason},
    )


def _build_recovery_mismatch_alert(event: dict[str, Any]) -> Alert:
    payload = event.get("payload") or {}
    mismatches = payload.get("mismatches", [])
    override = payload.get("override_value", "?")
    mismatch_count = payload.get("mismatch_count", len(mismatches))
    msg = (
        f"🔴 T1: recovery_state_mismatch\n"
        f"Override: {override}\n"
        f"Mismatches: {mismatch_count}"
    )
    return Alert(
        tier=1,
        event_type="recovery_state_mismatch",
        journal_entry_id=event["journal_entry_id"],
        ts=event["ts"],
        message=msg,
        context={"override": override, "mismatch_count": mismatch_count},
    )


def _build_order_filled_alert(event: dict[str, Any]) -> Alert:
    payload = event.get("payload") or {}
    ticker = payload.get("ticker", "?")
    qty = payload.get("qty", "?")
    price = payload.get("price", "?")
    side = payload.get("side", "?")
    msg = (
        f"🟡 T2: order_filled\n"
        f"{ticker} {side} {qty} @ ${price}"
    )
    return Alert(
        tier=2,
        event_type="order_filled",
        journal_entry_id=event["journal_entry_id"],
        ts=event["ts"],
        message=msg,
        context={"ticker": ticker, "qty": qty, "price": price, "side": side},
    )


def _build_order_cancelled_alert(event: dict[str, Any]) -> Alert | None:
    payload = event.get("payload") or {}
    cancel_reason = payload.get("cancel_reason", "")
    if cancel_reason == "operator_initiated":
        return None
    ticker = payload.get("ticker", "?")
    qty = payload.get("qty", "?")
    msg = (
        f"🟡 T2: order_cancelled\n"
        f"{ticker} {qty}\n"
        f"Reason: {cancel_reason}"
    )
    return Alert(
        tier=2,
        event_type="order_cancelled",
        journal_entry_id=event["journal_entry_id"],
        ts=event["ts"],
        message=msg,
        context={"ticker": ticker, "qty": qty, "cancel_reason": cancel_reason},
    )


def _build_kill_switch_alert(event: dict[str, Any]) -> Alert:
    payload = event.get("payload") or {}
    trigger = payload.get("trigger", ".killed")
    msg = (
        f"🟡 T2: kill_switch_triggered\n"
        f"Trigger: {trigger}"
    )
    return Alert(
        tier=2,
        event_type="kill_switch_triggered",
        journal_entry_id=event["journal_entry_id"],
        ts=event["ts"],
        message=msg,
        context={"trigger": trigger},
    )


def classify_events(
    events: list[dict[str, Any]],
    state: ClassifierState,
    threshold: int,
) -> tuple[list[Alert], ClassifierState]:
    """Classify journal events into alerts, returning (alerts, updated_state)."""
    alerts: list[Alert] = []
    new_state = ClassifierState(
        last_processed_entry_id=state.last_processed_entry_id,
        last_processed_ts=state.last_processed_ts,
        alerted_outage_start_ts=state.alerted_outage_start_ts,
    )

    # Track the current contiguous disconnect sequence.
    # current_outage_start_ts = first disconnect of the current sequence.
    # alerted_outage_start_ts = first disconnect of the sequence we already alerted on.
    current_outage_start_ts: str | None = None

    for ev in events:
        entry_id = ev.get("journal_entry_id", "")
        ts = ev.get("ts", "")
        event_type = ev.get("event_type", "")

        # Update state cursor regardless of alert outcome
        new_state.last_processed_entry_id = entry_id
        new_state.last_processed_ts = ts

        # Reset outage tracker on reconnection or engine start
        if event_type in ("reconnected", "engine_started"):
            current_outage_start_ts = None
            new_state.alerted_outage_start_ts = None
            continue

        if event_type == "disconnect_status":
            # Start tracking this sequence if not already tracking
            if current_outage_start_ts is None:
                current_outage_start_ts = ts

            alert = _build_disconnect_alert(ev, threshold)
            if alert is not None:
                # Fire only once per contiguous outage sequence
                if new_state.alerted_outage_start_ts != current_outage_start_ts:
                    new_state.alerted_outage_start_ts = current_outage_start_ts
                    alerts.append(alert)
                # else: already alerted for this outage, skip
            continue

        if event_type in TIER_1_EVENTS:
            if event_type == "engine_stopped":
                alerts.append(_build_engine_stopped_alert(ev))
            elif event_type == "recovery_state_mismatch":
                alerts.append(_build_recovery_mismatch_alert(ev))
            continue

        if event_type in TIER_2_EVENTS:
            if event_type == "order_filled":
                alerts.append(_build_order_filled_alert(ev))
            elif event_type == "order_cancelled":
                alert = _build_order_cancelled_alert(ev)
                if alert is not None:
                    alerts.append(alert)
            elif event_type == "kill_switch_triggered":
                alerts.append(_build_kill_switch_alert(ev))
            continue

        # All other event types: silently skip (no alert)

    return alerts, new_state


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_classification(
    vault_root: Path | None = None,
    state_dir: Path | None = None,
    threshold: int | None = None,
    today: datetime.date | None = None,
    commit_state: bool = True,
) -> tuple[list[Alert], ClassifierState, bool]:
    """Main entry point: load state, read journal, classify, optionally save state.

    Returns (alerts, new_state, had_events).
    """
    if vault_root is None:
        vault_root = Path(os.environ.get("K2BI_VAULT_ROOT", DEFAULT_VAULT_ROOT)).expanduser()
    if state_dir is None:
        state_dir = Path(os.environ.get("K2BI_ALERT_STATE_DIR", DEFAULT_STATE_DIR)).expanduser()
    if threshold is None:
        threshold = int(os.environ.get("K2BI_ALERT_OUTAGE_THRESHOLD_S", DEFAULT_OUTAGE_THRESHOLD_S))

    state = load_state(state_dir)
    events = _iter_new_events(vault_root, state, today)
    alerts, new_state = classify_events(events, state, threshold)
    if events and commit_state:
        save_state(new_state, state_dir)
    return alerts, new_state, bool(events)


def main() -> int:
    """CLI entry point: prints alerts as JSON lines to stdout."""
    import argparse
    parser = argparse.ArgumentParser(description="K2Bi alert classifier")
    parser.add_argument("--no-save-state", action="store_true", help="Classify without persisting state")
    parser.add_argument("--state-json-out", type=str, default=None, help="Write new state JSON to this file")
    args = parser.parse_args()

    vault_root = Path(os.environ.get("K2BI_VAULT_ROOT", DEFAULT_VAULT_ROOT)).expanduser()
    state_dir = Path(os.environ.get("K2BI_ALERT_STATE_DIR", DEFAULT_STATE_DIR)).expanduser()
    threshold = int(os.environ.get("K2BI_ALERT_OUTAGE_THRESHOLD_S", DEFAULT_OUTAGE_THRESHOLD_S))

    alerts, new_state, had_events = run_classification(
        vault_root, state_dir, threshold, commit_state=not args.no_save_state
    )
    for alert in alerts:
        print(json.dumps({
            "tier": alert.tier,
            "event_type": alert.event_type,
            "journal_entry_id": alert.journal_entry_id,
            "ts": alert.ts,
            "message": alert.message,
            "context": alert.context,
        }))
    if args.state_json_out and had_events:
        with open(args.state_json_out, "w", encoding="utf-8") as f:
            json.dump(new_state.to_dict(), f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
