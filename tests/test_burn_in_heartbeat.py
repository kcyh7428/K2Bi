"""Tests for the daily burn-in Telegram heartbeat."""

from __future__ import annotations

import io
import importlib.util
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "burn-in-heartbeat.py"
NOW = datetime(2026, 5, 14, 1, 0, tzinfo=timezone.utc)


def _load_module():
    if not SCRIPT_PATH.exists():
        raise AssertionError("scripts/burn-in-heartbeat.py is missing")
    spec = importlib.util.spec_from_file_location("burn_in_heartbeat", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load burn-in-heartbeat.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event(
    event_type: str,
    entry_id: str,
    ts: str = "2026-05-13T14:00:00+00:00",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ts": ts,
        "schema_version": 2,
        "event_type": event_type,
        "trade_id": None,
        "journal_entry_id": entry_id,
        "strategy": None,
        "git_sha": "test",
        "payload": payload or {},
    }


def _seed_journal(vault_root: Path, date_str: str, events: list[dict[str, Any]]) -> None:
    journal_dir = vault_root / "raw" / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    path = journal_dir / f"{date_str}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")


class FakeIB:
    def __init__(self, connect_exc: Exception | None = None) -> None:
        self.connect_exc = connect_exc
        self.connected = False

    def connect(self, *_args: Any, **_kwargs: Any) -> None:
        if self.connect_exc is not None:
            raise self.connect_exc
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def positions(self) -> list[Any]:
        return [
            SimpleNamespace(
                contract=SimpleNamespace(symbol="G"),
                position=71,
                avgCost="31.3340875",
            ),
            SimpleNamespace(
                contract=SimpleNamespace(symbol="SPY"),
                position=2,
                avgCost="707.72",
            ),
        ]

    def reqAllOpenOrders(self) -> list[Any]:
        return [
            SimpleNamespace(
                contract=SimpleNamespace(symbol="G"),
                order=SimpleNamespace(
                    action="SELL",
                    orderType="STP",
                    auxPrice="30",
                    totalQuantity=71,
                ),
                orderStatus=SimpleNamespace(status="Submitted"),
            ),
            SimpleNamespace(
                contract=SimpleNamespace(symbol="SPY"),
                order=SimpleNamespace(
                    action="SELL",
                    orderType="STP",
                    auxPrice="697.13",
                    totalQuantity=2,
                ),
                orderStatus=SimpleNamespace(status="PreSubmitted"),
            ),
        ]

    def sleep(self, _seconds: float) -> None:
        return None


class ConnectorError(Exception):
    pass


class BurnInHeartbeatTests(unittest.TestCase):
    def _run(
        self,
        module: Any,
        vault_root: Path,
        ib: FakeIB,
    ) -> tuple[int, str]:
        engine_state = module.EngineState(status="active", uptime="12h 03m")
        stdout = io.StringIO()
        with (
            patch.object(module, "IB", return_value=ib),
            patch.object(module, "get_engine_state", return_value=engine_state),
            redirect_stdout(stdout),
        ):
            code = module.main(
                [
                    "--vault-root",
                    str(vault_root),
                    "--now-utc",
                    NOW.isoformat(),
                    "--no-send",
                ]
            )
        return code, stdout.getvalue()

    def test_clean_day_reports_positions_and_zero_anomalies(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td)
            events = [
                _event("cycle_evaluated_skip_position_held", f"id{i:04d}")
                for i in range(1247)
            ]
            _seed_journal(vault_root, "2026-05-14", events)

            code, out = self._run(module, vault_root, FakeIB())

        self.assertEqual(code, 0)
        self.assertIn("1247 cycle skips, 0 anomalies", out)
        self.assertIn("G: 71 @ avg $31.33, STP $30 Submitted", out)
        self.assertIn("SPY: 2 @ avg $707.72, STP $697.13 PreSubmitted", out)
        self.assertNotIn("Anomalies:", out)

    def test_engine_bounced_counts_both_engine_started_events(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td)
            _seed_journal(
                vault_root,
                "2026-05-14",
                [
                    _event("engine_started", "id1", "2026-05-13T13:46:00+00:00"),
                    _event("engine_started", "id2", "2026-05-13T14:05:00+00:00"),
                ],
            )

            code, out = self._run(module, vault_root, FakeIB())

        self.assertEqual(code, 0)
        self.assertIn("2 anomalies", out)
        self.assertIn("2026-05-13T13:46:00+00:00 engine_started", out)
        self.assertIn("2026-05-13T14:05:00+00:00 engine_started", out)

    def test_broker_unreachable_exits_1_and_reports_anomaly(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td)
            _seed_journal(
                vault_root,
                "2026-05-14",
                [_event("cycle_evaluated_skip_position_held", "id1")],
            )

            code, out = self._run(
                module,
                vault_root,
                FakeIB(connect_exc=ConnectorError("connection refused")),
            )

        self.assertEqual(code, 1)
        self.assertIn("broker-unreachable", out)
        self.assertIn("ConnectorError", out)

    def test_journal_absent_exits_2_but_reports_broker_positions(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td)

            code, out = self._run(module, vault_root, FakeIB())

        self.assertEqual(code, 2)
        self.assertIn("no journal for date 2026-05-14", out)
        self.assertIn("G: 71 @ avg $31.33, STP $30 Submitted", out)
        self.assertIn("SPY: 2 @ avg $707.72, STP $697.13 PreSubmitted", out)

    def test_burn_in_state_file_absent_reports_unknown_day(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td)
            _seed_journal(
                vault_root,
                "2026-05-14",
                [_event("cycle_evaluated_skip_position_held", "id1")],
            )

            code, out = self._run(module, vault_root, FakeIB())

        self.assertEqual(code, 0)
        self.assertIn("Burn-in: day ? (state file missing)", out)

    def test_telegram_send_failure_returns_nonzero_without_traceback(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td)
            _seed_journal(
                vault_root,
                "2026-05-14",
                [_event("cycle_evaluated_skip_position_held", "id1")],
            )
            engine_state = module.EngineState(status="active", uptime="12h 03m")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.object(module, "IB", return_value=FakeIB()),
                patch.object(module, "get_engine_state", return_value=engine_state),
                patch.object(
                    module,
                    "send_telegram",
                    side_effect=subprocess.CalledProcessError(1, ["sender"]),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                code = module.main(
                    [
                        "--vault-root",
                        str(vault_root),
                        "--now-utc",
                        NOW.isoformat(),
                    ]
                )

        self.assertEqual(code, 1)
        self.assertIn("K2Bi heartbeat", stdout.getvalue())
        self.assertIn("telegram-send-failed", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_broker_disconnect_runs_even_when_connect_raises(self) -> None:
        module = _load_module()

        class PartialConnectIB(FakeIB):
            def __init__(self) -> None:
                super().__init__(connect_exc=ConnectorError("partial connect"))
                self.disconnect_called = False

            def disconnect(self) -> None:
                self.disconnect_called = True

        ib = PartialConnectIB()

        with patch.object(module, "IB", return_value=ib):
            snapshot = module.query_broker()

        self.assertIsNotNone(snapshot.error)
        self.assertTrue(ib.disconnect_called)

    def test_malformed_journal_line_reports_error_but_keeps_tail_anomaly(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td)
            journal_dir = vault_root / "raw" / "journal"
            journal_dir.mkdir(parents=True)
            path = journal_dir / "2026-05-14.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            _event("cycle_evaluated_skip_position_held", "id1")
                        ),
                        "{not-json",
                        json.dumps(_event("order_submitted", "id2")),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            code, out = self._run(module, vault_root, FakeIB())

        self.assertEqual(code, 2)
        self.assertIn("journal read failed", out)
        self.assertIn("order_submitted", out)

    def test_malformed_journal_timestamp_reports_error_but_keeps_event(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td)
            _seed_journal(
                vault_root,
                "2026-05-14",
                [_event("order_submitted", "id1", ts="not-a-timestamp")],
            )

            code, out = self._run(module, vault_root, FakeIB())

        self.assertEqual(code, 2)
        self.assertIn("invalid ts", out)
        self.assertIn("order_submitted", out)

    def test_window_includes_event_exactly_at_24h_cutoff(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td)
            _seed_journal(
                vault_root,
                "2026-05-13",
                [
                    _event(
                        "cycle_evaluated_skip_position_held",
                        "old",
                        "2026-05-13T01:00:00+00:00",
                    ),
                    _event(
                        "cycle_evaluated_skip_position_held",
                        "new",
                        "2026-05-13T01:00:01+00:00",
                    ),
                ],
            )

            code, out = self._run(module, vault_root, FakeIB())

        self.assertEqual(code, 0)
        self.assertIn("2 cycle skips, 0 anomalies", out)

    def test_broker_query_timeout_reports_broker_unreachable(self) -> None:
        module = _load_module()
        with (
            patch.object(module, "IB", object),
            patch.object(
                module,
                "_query_broker_inner",
                side_effect=module.BrokerQueryTimeout("broker query timed out"),
            ),
        ):
            snapshot = module.query_broker()

        self.assertIsNotNone(snapshot.error)
        self.assertIn("BrokerQueryTimeout", snapshot.error)

    def test_journal_reader_uses_shared_sidecar_lock(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td)
            _seed_journal(
                vault_root,
                "2026-05-14",
                [_event("cycle_evaluated_skip_position_held", "id1")],
            )
            calls: list[int] = []

            def fake_flock(_fd: int, operation: int) -> None:
                calls.append(operation)

            with patch.object(module.fcntl, "flock", side_effect=fake_flock):
                journal = module.read_journal_window(vault_root, NOW)

        self.assertIsNone(journal.error)
        self.assertIn(module.fcntl.LOCK_SH, calls)
        self.assertIn(module.fcntl.LOCK_UN, calls)

    def test_load_project_env_sources_dotenv_without_overriding_existing(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as td:
            project_root = Path(td)
            (project_root / ".env").write_text(
                "\n".join(
                    [
                        "TELEGRAM_BOT_TOKEN=from-file",
                        "K2BI_TELEGRAM_CHAT_ID='chat-123'",
                        "EXISTING=from-file",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with patch.dict(
                module.os.environ,
                {"EXISTING": "keep-me"},
                clear=True,
            ):
                module.load_project_env(project_root)
                self.assertEqual(module.os.environ["TELEGRAM_BOT_TOKEN"], "from-file")
                self.assertEqual(module.os.environ["K2BI_TELEGRAM_CHAT_ID"], "chat-123")
                self.assertEqual(module.os.environ["EXISTING"], "keep-me")


if __name__ == "__main__":
    unittest.main()
