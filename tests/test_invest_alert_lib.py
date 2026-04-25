"""Unit tests for scripts.lib.invest_alert_lib.

Covers Tier 1 / Tier 2 classification, threshold edge cases, idempotency,
state-file corruption recovery, empty journal, and the fire-once-per-outage
rule. Uses synthetic events; no real journal or Telegram dependency.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from scripts import invest_alert_lib as ial


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _event(
    event_type: str,
    entry_id: str,
    ts: str = "2026-04-24T12:00:00+00:00",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ts": ts,
        "schema_version": 2,
        "event_type": event_type,
        "trade_id": None,
        "journal_entry_id": entry_id,
        "strategy": None,
        "git_sha": None,
        "payload": payload or {},
    }


def _seed_journal(vault_root: Path, date_str: str, events: list[dict[str, Any]]) -> None:
    journal_dir = vault_root / "raw" / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    path = journal_dir / f"{date_str}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


# ---------------------------------------------------------------------------
# Tier 1 -- disconnect_status threshold
# ---------------------------------------------------------------------------

class DisconnectThresholdTests(unittest.TestCase):
    def test_disconnect_0s_no_alert(self):
        ev = _event("disconnect_status", "id01", payload={"attempts": 1, "outage_seconds": 0})
        alerts, _ = ial.classify_events([ev], ial.ClassifierState(), threshold=300)
        self.assertEqual(len(alerts), 0)

    def test_disconnect_299s_no_alert(self):
        ev = _event("disconnect_status", "id02", payload={"attempts": 1, "outage_seconds": 299})
        alerts, _ = ial.classify_events([ev], ial.ClassifierState(), threshold=300)
        self.assertEqual(len(alerts), 0)

    def test_disconnect_300s_no_alert(self):
        ev = _event("disconnect_status", "id03", payload={"attempts": 1, "outage_seconds": 300})
        alerts, _ = ial.classify_events([ev], ial.ClassifierState(), threshold=300)
        self.assertEqual(len(alerts), 0)

    def test_disconnect_301s_fires_alert(self):
        ev = _event("disconnect_status", "id04", payload={"attempts": 1, "outage_seconds": 301})
        alerts, _ = ial.classify_events([ev], ial.ClassifierState(), threshold=300)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].tier, 1)

    def test_disconnect_3600s_fires_alert(self):
        ev = _event("disconnect_status", "id05", payload={"attempts": 10, "outage_seconds": 3600})
        alerts, _ = ial.classify_events([ev], ial.ClassifierState(), threshold=300)
        self.assertEqual(len(alerts), 1)
        self.assertIn("1.0h", alerts[0].message)

    def test_disconnect_39646s_regression(self):
        """2026-04-24 Q40 recurrence: 11h outage must fire Tier 1."""
        ev = _event("disconnect_status", "id06", payload={"attempts": 136, "outage_seconds": 39646})
        alerts, _ = ial.classify_events([ev], ial.ClassifierState(), threshold=300)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].tier, 1)
        self.assertIn("11.0h", alerts[0].message)
        self.assertIn("136", alerts[0].message)


# ---------------------------------------------------------------------------
# Tier 1 -- other events
# ---------------------------------------------------------------------------

class Tier1OtherTests(unittest.TestCase):
    def test_engine_stopped_fires_alert(self):
        ev = _event("engine_stopped", "id10", payload={"pid": 49009, "reason": "sigterm"})
        alerts, _ = ial.classify_events([ev], ial.ClassifierState(), threshold=300)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].tier, 1)
        self.assertEqual(alerts[0].event_type, "engine_stopped")
        self.assertIn("49009", alerts[0].message)

    def test_recovery_state_mismatch_fires_alert_even_with_override(self):
        ev = _event(
            "recovery_state_mismatch",
            "id11",
            payload={
                "override_env": "K2BI_ALLOW_RECOVERY_MISMATCH",
                "override_value": "1",
                "mismatch_count": 1,
                "mismatches": [{"case": "phantom_open_order"}],
            },
        )
        alerts, _ = ial.classify_events([ev], ial.ClassifierState(), threshold=300)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].tier, 1)
        self.assertEqual(alerts[0].event_type, "recovery_state_mismatch")
        self.assertIn("Override: 1", alerts[0].message)


# ---------------------------------------------------------------------------
# Tier 2 -- order / kill events
# ---------------------------------------------------------------------------

class Tier2OrderTests(unittest.TestCase):
    def test_order_filled_fires_alert_with_context(self):
        ev = _event(
            "order_filled",
            "id20",
            payload={"ticker": "SPY", "qty": 2, "price": "709.00", "side": "buy"},
        )
        alerts, _ = ial.classify_events([ev], ial.ClassifierState(), threshold=300)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].tier, 2)
        self.assertEqual(alerts[0].event_type, "order_filled")
        self.assertIn("SPY", alerts[0].message)
        self.assertIn("2", alerts[0].message)
        self.assertIn("709.00", alerts[0].message)

    def test_order_cancelled_non_op_fires_alert(self):
        ev = _event(
            "order_cancelled",
            "id21",
            payload={"ticker": "SPY", "qty": 2, "cancel_reason": "broker_reject"},
        )
        alerts, _ = ial.classify_events([ev], ial.ClassifierState(), threshold=300)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].tier, 2)
        self.assertEqual(alerts[0].event_type, "order_cancelled")

    def test_order_cancelled_op_no_alert(self):
        ev = _event(
            "order_cancelled",
            "id22",
            payload={"ticker": "SPY", "qty": 2, "cancel_reason": "operator_initiated"},
        )
        alerts, _ = ial.classify_events([ev], ial.ClassifierState(), threshold=300)
        self.assertEqual(len(alerts), 0)

    def test_kill_switch_killed_fires_alert(self):
        ev = _event("kill_switch_triggered", "id23", payload={"trigger": ".killed"})
        alerts, _ = ial.classify_events([ev], ial.ClassifierState(), threshold=300)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].tier, 2)
        self.assertEqual(alerts[0].event_type, "kill_switch_triggered")
        self.assertIn(".killed", alerts[0].message)

    def test_kill_switch_flag_stub(self):
        """Post-Q41 kill.flag alias: fires alert now, wires after Q41 ships."""
        ev = _event("kill_switch_triggered", "id24", payload={"trigger": "kill.flag"})
        alerts, _ = ial.classify_events([ev], ial.ClassifierState(), threshold=300)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].tier, 2)
        self.assertIn("kill.flag", alerts[0].message)


# ---------------------------------------------------------------------------
# Idempotency & safety
# ---------------------------------------------------------------------------

class IdempotencyTests(unittest.TestCase):
    def test_replay_same_journal_twice_zero_alerts_second_run(self):
        events = [
            _event("engine_stopped", "id30", payload={"pid": 1}),
            _event("order_filled", "id31", payload={"ticker": "A", "qty": 1, "price": "1"}),
        ]
        state0 = ial.ClassifierState()
        alerts1, state1 = ial.classify_events(events, state0, threshold=300)
        self.assertEqual(len(alerts1), 2)

        # Second run with updated state sees zero new events
        alerts2, _ = ial.classify_events([], state1, threshold=300)
        self.assertEqual(len(alerts2), 0)

    def test_state_corruption_reset_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            bad_path = state_dir / ial.STATE_FILE_NAME
            bad_path.write_text("not json {{{", encoding="utf-8")
            state = ial.load_state(state_dir)
            self.assertIsNone(state.last_processed_entry_id)
            # Should not raise

    def test_empty_journal_no_alerts(self):
        alerts, _ = ial.classify_events([], ial.ClassifierState(), threshold=300)
        self.assertEqual(len(alerts), 0)

    def test_unrelated_event_types_no_alert(self):
        events = [
            _event("eod_complete", "id40", payload={"open_orders_seen": 1}),
            _event("validator_pass", "id41"),
            _event("reconnected", "id42", payload={"outage_seconds": 36}),
        ]
        alerts, _ = ial.classify_events(events, ial.ClassifierState(), threshold=300)
        self.assertEqual(len(alerts), 0)


class OutageFireOnceTests(unittest.TestCase):
    def test_fifty_disconnect_events_one_alert(self):
        """50 disconnect_status ticks during same outage -> one alert at 300s crossing."""
        events = []
        base_ts = 1745487600  # 2025-04-24 12:00:00 UTC
        for i in range(50):
            ts = f"2026-04-24T12:{i:02d}:00+00:00"
            outage = float(i * 310)  # crosses 300 at i=1
            events.append(
                _event(
                    "disconnect_status",
                    f"id50_{i:02d}",
                    ts=ts,
                    payload={"attempts": i + 1, "outage_seconds": outage},
                )
            )
        alerts, state = ial.classify_events(events, ial.ClassifierState(), threshold=300)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].event_type, "disconnect_status")
        # Only the first event that crossed the threshold should alert
        self.assertEqual(alerts[0].journal_entry_id, "id50_01")

    def test_outage_resets_on_reconnected(self):
        """After reconnected, a new outage sequence can alert again."""
        events = [
            _event("disconnect_status", "id60_00", payload={"attempts": 1, "outage_seconds": 350}),
            _event("disconnect_status", "id60_01", payload={"attempts": 2, "outage_seconds": 650}),
            _event("reconnected", "id60_02", payload={"outage_seconds": 650}),
            _event("disconnect_status", "id60_03", payload={"attempts": 1, "outage_seconds": 350}),
        ]
        alerts, _ = ial.classify_events(events, ial.ClassifierState(), threshold=300)
        self.assertEqual(len(alerts), 2)
        self.assertEqual(alerts[0].journal_entry_id, "id60_00")
        self.assertEqual(alerts[1].journal_entry_id, "id60_03")

    def test_below_threshold_then_crosses_later(self):
        """Outage starts below threshold, later tick crosses it -> alert then."""
        events = [
            _event("disconnect_status", "id70_00", payload={"attempts": 1, "outage_seconds": 100}),
            _event("disconnect_status", "id70_01", payload={"attempts": 2, "outage_seconds": 250}),
            _event("disconnect_status", "id70_02", payload={"attempts": 3, "outage_seconds": 310}),
            _event("disconnect_status", "id70_03", payload={"attempts": 4, "outage_seconds": 610}),
        ]
        alerts, _ = ial.classify_events(events, ial.ClassifierState(), threshold=300)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].journal_entry_id, "id70_02")


# ---------------------------------------------------------------------------
# End-to-end with tmp vault
# ---------------------------------------------------------------------------

class EndToEndTests(unittest.TestCase):
    def test_run_classification_skips_processed(self):
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td) / "vault"
            state_dir = Path(td) / "state"
            _seed_journal(vault_root, "2026-04-24", [
                _event("engine_stopped", "id80", payload={"pid": 1}),
                _event("order_filled", "id81", payload={"ticker": "X", "qty": 1, "price": "1"}),
            ])
            os.environ["K2BI_VAULT_ROOT"] = str(vault_root)
            os.environ["K2BI_ALERT_STATE_DIR"] = str(state_dir)

            alerts1, _, _ = ial.run_classification(vault_root, state_dir, threshold=300)
            self.assertEqual(len(alerts1), 2)

            alerts2, _, _ = ial.run_classification(vault_root, state_dir, threshold=300)
            self.assertEqual(len(alerts2), 0)

    def test_run_classification_reads_today_and_yesterday(self):
        from datetime import date
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td) / "vault"
            state_dir = Path(td) / "state"
            # Yesterday has an event
            _seed_journal(vault_root, "2026-04-23", [
                _event("engine_stopped", "id90", payload={"pid": 1}),
            ])
            # Today has an event
            _seed_journal(vault_root, "2026-04-24", [
                _event("order_filled", "id91", payload={"ticker": "X", "qty": 1, "price": "1"}),
            ])
            os.environ["K2BI_VAULT_ROOT"] = str(vault_root)
            os.environ["K2BI_ALERT_STATE_DIR"] = str(state_dir)

            alerts, _, _ = ial.run_classification(vault_root, state_dir, threshold=300, today=date(2026, 4, 24))
            ids = {a.journal_entry_id for a in alerts}
            self.assertIn("id90", ids)
            self.assertIn("id91", ids)

    def test_empty_journal_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td) / "vault"
            state_dir = Path(td) / "state"
            os.environ["K2BI_VAULT_ROOT"] = str(vault_root)
            os.environ["K2BI_ALERT_STATE_DIR"] = str(state_dir)
            alerts, _, _ = ial.run_classification(vault_root, state_dir, threshold=300)
            self.assertEqual(len(alerts), 0)


# ---------------------------------------------------------------------------
# Threshold configurability
# ---------------------------------------------------------------------------

class ThresholdConfigTests(unittest.TestCase):
    def test_custom_threshold_600s(self):
        ev = _event("disconnect_status", "id100", payload={"attempts": 1, "outage_seconds": 500})
        alerts, _ = ial.classify_events([ev], ial.ClassifierState(), threshold=600)
        self.assertEqual(len(alerts), 0)

        ev2 = _event("disconnect_status", "id101", payload={"attempts": 1, "outage_seconds": 605})
        alerts2, _ = ial.classify_events([ev2], ial.ClassifierState(), threshold=600)
        self.assertEqual(len(alerts2), 1)


if __name__ == "__main__":
    unittest.main()
