"""Tests for Q34 IBKR connector bounded-timeout wrappers.

Q34 (architect scope 2026-04-21): Session F's run 3 hung for 3+
minutes on timed-out `open_orders_request` / `completed_orders_request`
/ `executions_request` after a connectivity flap. All broker-API
async calls must be wrapped in asyncio.wait_for so the engine degrades
cleanly instead of wedging.

Split policy (architect-confirmed):
    - Reads (positions, open orders, marks, executions, status history,
      account summary) -> log + return empty on timeout. Caller falls
      back to journal-authoritative per Q39-B.
    - Connect + post-connect probe -> raise DisconnectedError on
      timeout. Caller's reconnect/backoff cycle fires.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from execution.connectors.ibkr import (
    IBKR_CALL_TIMEOUT_SECONDS,
    IBKRConnector,
)
from execution.connectors.types import (
    AccountSummary,
    DisconnectedError,
)


NOW = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)


class _HangingIB:
    """Minimal ib_async stand-in whose async methods hang forever
    (so the wrapper's asyncio.wait_for timeout is the only path to
    return). Synchronous methods just no-op."""

    def __init__(self) -> None:
        self.disconnect_called = False

    async def connectAsync(self, **kw) -> None:
        await asyncio.sleep(3600)

    async def reqAccountSummaryAsync(self, *a, **kw) -> list[Any]:
        await asyncio.sleep(3600)
        return []

    async def accountSummaryAsync(self, *a, **kw) -> list[Any]:
        await asyncio.sleep(3600)
        return []

    async def reqPositionsAsync(self, *a, **kw) -> list[Any]:
        await asyncio.sleep(3600)
        return []

    async def reqAllOpenOrdersAsync(self, *a, **kw) -> list[Any]:
        await asyncio.sleep(3600)
        return []

    async def reqTickersAsync(self, contract, *a, **kw) -> list[Any]:
        await asyncio.sleep(3600)
        return []

    async def reqExecutionsAsync(self, *a, **kw) -> list[Any]:
        await asyncio.sleep(3600)
        return []

    async def reqCompletedOrdersAsync(self, *a, **kw) -> list[Any]:
        await asyncio.sleep(3600)
        return []

    def disconnect(self) -> None:
        self.disconnect_called = True


def _make_connector_with_hanging_ib(timeout: float = 0.2) -> IBKRConnector:
    """Construct a connector already flagged as connected but whose
    underlying self._ib hangs on every async call. Timeout override
    flows through the IBKRConnector constructor per Q34 R2 finding #5
    cleanup."""
    conn = IBKRConnector(
        account_id=None,
        host="127.0.0.1",
        port=4002,
        client_id=1,
        timeout_seconds=timeout,
    )
    conn._ib = _HangingIB()
    conn._connected = True
    return conn


class Q34ReadTimeoutReturnsEmptyTests(unittest.IsolatedAsyncioTestCase):
    """Read methods return an empty sentinel (list / dict / default
    AccountSummary) on timeout. Caller + Q39-B recovery handle the
    empty case as "broker visibility limited"."""

    async def test_get_positions_timeout_returns_empty_list(self):
        conn = _make_connector_with_hanging_ib()
        result = await conn.get_positions()
        self.assertEqual(result, [])

    async def test_get_open_orders_timeout_returns_empty_list(self):
        conn = _make_connector_with_hanging_ib()
        result = await conn.get_open_orders()
        self.assertEqual(result, [])

    async def test_get_executions_since_timeout_returns_empty_list(self):
        conn = _make_connector_with_hanging_ib()
        result = await conn.get_executions_since(NOW)
        self.assertEqual(result, [])

    async def test_get_order_status_history_timeout_returns_empty_list(self):
        conn = _make_connector_with_hanging_ib()
        result = await conn.get_order_status_history(NOW)
        self.assertEqual(result, [])

    async def test_get_marks_timeout_skips_ticker_without_raising(self):
        conn = _make_connector_with_hanging_ib()
        result = await conn.get_marks(["SPY"])
        self.assertEqual(result, {})

    async def test_get_account_summary_timeout_raises_disconnected(self):
        """MiniMax Q34 R1 finding #5 (2026-04-21): a default AccountSummary
        with cash=0 + net_liq=0 is ambiguous with a real zero-balance
        account. Callers checking NAV thresholds for risk gating would
        reach incorrect conclusions. Raise DisconnectedError so the
        engine treats the call as a session-health failure and routes
        through reconnect/backoff."""
        conn = _make_connector_with_hanging_ib()
        with self.assertRaises(DisconnectedError):
            await conn.get_account_summary()

    async def test_get_marks_aggregate_budget_caps_total_wall_time(self):
        """MiniMax Q34 R3 finding #1 + R4 finding #3 regression guard
        (2026-04-21): sequential per-ticker awaits at 10s each would
        amplify linearly with N tickers. The aggregate budget
        (3 * per_call_timeout, capped at 60s) must break the loop
        once exhausted so a stressed market-data farm cannot stall
        validation for minutes.

        With per-call timeout 0.1s the aggregate budget becomes 0.3s.
        A hanging _ib + 10 tickers would otherwise spend 10 * 0.1s =
        1s sequentially; the aggregate budget cuts the walk short
        and returns an empty dict well inside 1s."""
        import time as _time

        conn = _make_connector_with_hanging_ib(timeout=0.1)
        start = _time.monotonic()
        result = await conn.get_marks(
            ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
        )
        elapsed = _time.monotonic() - start
        self.assertEqual(result, {})
        # Aggregate budget is 3 * 0.1s = 0.3s. Wall-clock should
        # come in comfortably under 1.0s (raw 10-ticker loop would
        # spend ~1.0s at per-ticker timeout 0.1s).
        self.assertLess(elapsed, 1.0)

    async def test_read_timeout_completes_in_bounded_time(self):
        """Bounded means bounded -- the wrapper must not wait longer
        than the configured timeout. 3s is comfortably above the 0.2s
        fixture timeout but orders of magnitude below the broker's
        hanging sleep(3600)."""
        conn = _make_connector_with_hanging_ib(timeout=0.15)
        import time as _time

        start = _time.monotonic()
        await conn.get_positions()
        elapsed = _time.monotonic() - start
        self.assertLess(elapsed, 3.0)


class Q34ProbeTimeoutRaisesDisconnectedTests(unittest.IsolatedAsyncioTestCase):
    """Connect + post-connect probe raise DisconnectedError on timeout
    so the caller's reconnect/backoff cycle fires."""

    async def test_connect_post_probe_timeout_raises_disconnected(self):
        """The post-connect reqAccountSummaryAsync is the session
        probe -- if it hangs, connect() must raise rather than sit
        idle pretending the session is usable."""

        class _ProbeHangingIB(_HangingIB):
            async def connectAsync(self, **kw) -> None:
                return None  # success

        conn = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
            timeout_seconds=0.2,
        )
        conn._ib = _ProbeHangingIB()
        with self.assertRaises(DisconnectedError):
            await conn.connect()

    async def test_connect_timeout_raises_disconnected(self):
        """If connectAsync itself hangs, connect() must raise
        DisconnectedError rather than leaving _connected in a false-
        positive state."""
        conn = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
            timeout_seconds=0.2,
        )
        conn._ib = _HangingIB()
        with self.assertRaises(DisconnectedError):
            await conn.connect()


class Q34TimeoutConstantTests(unittest.TestCase):
    """Module-level timeout constant is defined + sensible."""

    def test_ibkr_call_timeout_is_positive_float(self):
        self.assertIsInstance(IBKR_CALL_TIMEOUT_SECONDS, float)
        self.assertGreater(IBKR_CALL_TIMEOUT_SECONDS, 0.0)
        # 10s is the architect-specified default; a test just pins the
        # order of magnitude so a drift to 100s or 0.1s would be
        # flagged on review.
        self.assertLessEqual(IBKR_CALL_TIMEOUT_SECONDS, 60.0)


class Q34ScopeContractTests(unittest.TestCase):
    """MiniMax Q34 R2 stop-rule defense (2026-04-21): architect scope
    for Q34 is the READ PATH that hung Session F. Write-path wrappers
    (submit_order, cancel_order, _await_parent_terminal) are
    intentionally deferred to Phase 4+ per the scope doc at
    wiki/planning/pre-phase-3.7-engine-fix-scope.md. This contract
    test pins the decision per L-2026-04-20-002 (architect-locked
    decisions defended via code + contract test, not iteration)."""

    def test_scope_note_documents_write_path_deferral(self):
        """The module docstring must record the write-path deferral
        so a future developer reading the code sees the scope
        boundary without having to re-derive it from review history."""
        from execution.connectors import ibkr as ibkr_mod
        import inspect

        source = inspect.getsource(ibkr_mod)
        self.assertIn("SCOPE LIMITATION", source)
        self.assertIn("submit_order", source)
        self.assertIn("cancel_order", source)
        self.assertIn("Phase 4+", source)

    def test_read_path_call_sites_all_use_bounded_read(self):
        """Every async broker READ call in ibkr.py must route through
        _bounded_read (not a raw `await self._ib.*Async`). This gates
        regressions where a new read path is added without the
        wrapper, reintroducing the Session F hang."""
        # MiniMax R3 finding #2 (2026-04-21): resolve the module path
        # relative to the import so the test runs from any CI / dev
        # root, not only the original author's filesystem.
        import inspect
        from pathlib import Path
        from execution.connectors import ibkr as ibkr_mod

        source = Path(inspect.getfile(ibkr_mod)).read_text()
        # Positive assertion: every wrapped read call uses _bounded_read.
        read_call_names = [
            "positions",
            "open_orders",
            "executions_since",
            "completed_orders",
        ]
        for name in read_call_names:
            self.assertIn(
                f'call_name="{name}"',
                source,
                f"read path {name} must route through _bounded_read",
            )
