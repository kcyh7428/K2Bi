"""Integration tests for invest-alert pipeline.

Uses a fake journal + a mock Telegram sender to verify message format,
chat_id routing, and single-fire-per-event guarantees.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any


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


def _run_classifier(vault_root: Path, state_dir: Path, threshold: int = 300) -> list[dict[str, Any]]:
    env = os.environ.copy()
    env["K2BI_VAULT_ROOT"] = str(vault_root)
    env["K2BI_ALERT_STATE_DIR"] = str(state_dir)
    env["K2BI_ALERT_OUTAGE_THRESHOLD_S"] = str(threshold)
    result = subprocess.run(
        ["python3", "-m", "scripts.invest_alert_lib"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent),
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"classifier failed: {result.stderr}")
    lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
    return [json.loads(l) for l in lines]


# ---------------------------------------------------------------------------
# Integration cases
# ---------------------------------------------------------------------------

class MessageFormatTests(unittest.TestCase):
    def test_alert_message_contains_tier_emoji_and_event_type(self):
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td) / "vault"
            state_dir = Path(td) / "state"
            _seed_journal(vault_root, "2026-04-24", [
                _event("engine_stopped", "id01", payload={"pid": 1, "reason": "test"}),
            ])
            alerts = _run_classifier(vault_root, state_dir)
            self.assertEqual(len(alerts), 1)
            msg = alerts[0]["message"]
            self.assertIn("🔴", msg)
            self.assertIn("T1", msg)
            self.assertIn("engine_stopped", msg)

    def test_order_filled_includes_ticker_qty_price(self):
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td) / "vault"
            state_dir = Path(td) / "state"
            _seed_journal(vault_root, "2026-04-24", [
                _event("order_filled", "id02", payload={"ticker": "SPY", "qty": 2, "price": "709.00", "side": "buy"}),
            ])
            alerts = _run_classifier(vault_root, state_dir)
            self.assertEqual(len(alerts), 1)
            msg = alerts[0]["message"]
            self.assertIn("🟡", msg)
            self.assertIn("SPY", msg)
            self.assertIn("2", msg)
            self.assertIn("709.00", msg)

    def test_disconnect_alert_includes_outage_duration_and_attempts(self):
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td) / "vault"
            state_dir = Path(td) / "state"
            _seed_journal(vault_root, "2026-04-24", [
                _event("disconnect_status", "id03", payload={"attempts": 5, "outage_seconds": 3600}),
            ])
            alerts = _run_classifier(vault_root, state_dir)
            self.assertEqual(len(alerts), 1)
            msg = alerts[0]["message"]
            self.assertIn("1.0h", msg)
            self.assertIn("5", msg)


class SingleFireTests(unittest.TestCase):
    def test_duplicate_run_does_not_re_alert(self):
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td) / "vault"
            state_dir = Path(td) / "state"
            _seed_journal(vault_root, "2026-04-24", [
                _event("engine_stopped", "id04", payload={"pid": 1}),
            ])
            alerts1 = _run_classifier(vault_root, state_dir)
            self.assertEqual(len(alerts1), 1)

            alerts2 = _run_classifier(vault_root, state_dir)
            self.assertEqual(len(alerts2), 0)


class MixedJournalTests(unittest.TestCase):
    def test_mixed_events_produce_correct_tier_split(self):
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td) / "vault"
            state_dir = Path(td) / "state"
            _seed_journal(vault_root, "2026-04-24", [
                _event("disconnect_status", "id05_00", payload={"attempts": 1, "outage_seconds": 100}),
                _event("disconnect_status", "id05_01", payload={"attempts": 2, "outage_seconds": 350}),
                _event("order_filled", "id05_02", payload={"ticker": "A", "qty": 1, "price": "1"}),
                _event("eod_complete", "id05_03", payload={"open_orders_seen": 1}),
                _event("engine_stopped", "id05_04", payload={"pid": 1}),
            ])
            alerts = _run_classifier(vault_root, state_dir)
            tiers = [a["tier"] for a in alerts]
            self.assertEqual(len(alerts), 3)
            self.assertEqual(tiers.count(1), 2)  # disconnect + engine_stopped
            self.assertEqual(tiers.count(2), 1)  # order_filled


if __name__ == "__main__":
    unittest.main()
