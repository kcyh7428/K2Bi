"""Spec B §4 tests for child-stop attachment and recovery-only stop repair."""

from __future__ import annotations

import inspect
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from execution.connectors.ibkr import IBKRConnector
from execution.connectors.types import (
    BrokerOrderAck,
    BrokerPosition,
    BrokerRejectionError,
    POSITION_SOURCE_LIVE_REQ_POSITIONS,
    PositionSnapshot,
)
from execution.engine import recovery as recovery_mod
from execution.engine import recovery_context as recovery_context_mod
from execution.engine.main import Engine
from execution.journal.schema import (
    JournalSchemaError,
    validate_protective_stop_attached_payload,
)
from execution.journal.writer import JournalWriter
from execution.strategies import runner as strategy_runner


class _FakeStock:
    def __init__(self, symbol: str, exchange: str, currency: str) -> None:
        self.symbol = symbol
        self.exchange = exchange
        self.currency = currency


class _FakeBaseOrder:
    def __init__(self, action: str, qty: int, *, tif: str) -> None:
        self.action = action
        self.totalQuantity = qty
        self.tif = tif
        self.transmit = True
        self.parentId = 0
        self.orderRef = ""
        self.orderId = 0
        self.permId = 0
        self.lmtPrice = 0.0
        self.auxPrice = 0.0


class _FakeLimitOrder(_FakeBaseOrder):
    def __init__(self, action: str, qty: int, limit_price: float, *, tif: str) -> None:
        super().__init__(action, qty, tif=tif)
        self.orderType = "LMT"
        self.lmtPrice = limit_price


class _FakeMarketOrder(_FakeBaseOrder):
    def __init__(self, action: str, qty: int, *, tif: str) -> None:
        super().__init__(action, qty, tif=tif)
        self.orderType = "MKT"


class _FakeStopOrder(_FakeBaseOrder):
    def __init__(self, action: str, qty: int, stop_price: float, *, tif: str) -> None:
        super().__init__(action, qty, tif=tif)
        self.orderType = "STP"
        self.auxPrice = stop_price


class _FakeOrderStatus:
    def __init__(self, status: str = "Submitted") -> None:
        self.status = status
        self.whyHeld = ""


class _FakeTrade:
    def __init__(self, order: _FakeBaseOrder) -> None:
        self.order = order
        self.orderStatus = _FakeOrderStatus()


class _FakeIB:
    def __init__(self) -> None:
        self.placed_orders: list[_FakeBaseOrder] = []
        self.cancelled_orders: list[_FakeBaseOrder] = []
        self._next_order_id = 100
        self._next_perm_id = 9000

    def placeOrder(self, contract: _FakeStock, order: _FakeBaseOrder) -> _FakeTrade:
        order.orderId = self._next_order_id
        order.permId = self._next_perm_id
        self._next_order_id += 1
        self._next_perm_id += 1
        self.placed_orders.append(order)
        return _FakeTrade(order)

    def cancelOrder(self, order: _FakeBaseOrder) -> None:
        self.cancelled_orders.append(order)


class _FakeIBNoPerm(_FakeIB):
    def placeOrder(self, contract: _FakeStock, order: _FakeBaseOrder) -> _FakeTrade:
        trade = super().placeOrder(contract, order)
        order.permId = 0
        return trade


async def _no_sleep(_: float) -> None:
    return None


def _fake_ib_async_module() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        Stock=_FakeStock,
        LimitOrder=_FakeLimitOrder,
        MarketOrder=_FakeMarketOrder,
        StopOrder=_FakeStopOrder,
    )


class _AttachmentConnector:
    def __init__(self, positions: list[BrokerPosition] | None = None) -> None:
        self.positions = positions or []
        self.stop_orders: list[dict] = []

    async def get_positions(self) -> PositionSnapshot:
        return PositionSnapshot(
            positions=list(self.positions),
            valid=True,
            source=POSITION_SOURCE_LIVE_REQ_POSITIONS,
            fetched_at=datetime.now(timezone.utc),
        )

    async def submit_standalone_stop_order(
        self,
        *,
        ticker: str,
        side: str,
        qty: int,
        stop_price: Decimal,
        time_in_force: str,
        client_tag: str | None = None,
    ) -> BrokerOrderAck:
        self.stop_orders.append(
            {
                "ticker": ticker,
                "side": side,
                "qty": qty,
                "stop_price": stop_price,
                "time_in_force": time_in_force,
                "client_tag": client_tag,
                "parent_id": 0,
                "transmit": True,
            }
        )
        return BrokerOrderAck(
            broker_order_id="7001",
            broker_perm_id="8001",
            submitted_at=datetime.now(timezone.utc),
            status="Submitted",
        )


class ConnectorBracketTests(unittest.IsolatedAsyncioTestCase):
    async def test_c1_buy_with_stop_creates_parent_child_bracket(self) -> None:
        fake_ib = _FakeIB()
        connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )
        connector._ib = fake_ib
        connector._connected = True

        with patch.dict(sys.modules, {"ib_async": _fake_ib_async_module()}):
            await connector.submit_order(
                ticker="G",
                side="buy",
                qty=71,
                limit_price=None,
                stop_loss=Decimal("30.00"),
                time_in_force="DAY",
                client_tag="k2bi:g-2026-05:T-parent",
                order_type="MKT",
            )

        self.assertEqual(len(fake_ib.placed_orders), 2)
        parent, child = fake_ib.placed_orders
        self.assertEqual(parent.orderType, "MKT")
        self.assertEqual(parent.action, "BUY")
        self.assertFalse(parent.transmit)
        self.assertEqual(child.orderType, "STP")
        self.assertEqual(child.action, "SELL")
        self.assertEqual(child.parentId, parent.orderId)
        self.assertTrue(child.transmit)
        self.assertEqual(child.tif, "GTC")

    def test_c2_parent_cancel_relies_on_broker_child_auto_cancel(self) -> None:
        """IBKR bracket orders auto-cancel the child when the parent cancels.

        Assumption reference: Interactive Brokers TWS API bracket-order
        pattern documents parent/child orders with parent.transmit=False,
        child.parentId set, and final child.transmit=True:
        https://interactivebrokers.github.io/tws-api/bracket_order.html
        """

        source = inspect.getsource(IBKRConnector.submit_order)
        self.assertIn("child.parentId = parent_trade.order.orderId", source)
        self.assertIn("child.transmit = True", source)
        self.assertNotIn("cancelOrder(child", source)
        self.assertNotIn("cancelOrder(child_trade", source)

    async def test_standalone_stop_permid_timeout_cancels_partial_order(self) -> None:
        fake_ib = _FakeIBNoPerm()
        connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )
        connector._ib = fake_ib
        connector._connected = True

        with patch.dict(sys.modules, {"ib_async": _fake_ib_async_module()}):
            with patch("execution.connectors.ibkr.asyncio.sleep", _no_sleep):
                with self.assertRaises(BrokerRejectionError):
                    await connector.submit_standalone_stop_order(
                        ticker="G",
                        side="sell",
                        qty=71,
                        stop_price=Decimal("30.00"),
                        time_in_force="GTC",
                        client_tag="k2bi:g-2026-05:repair:stop",
                    )

        self.assertEqual(len(fake_ib.cancelled_orders), 1)
        self.assertIs(fake_ib.cancelled_orders[0], fake_ib.placed_orders[0])


class ChildStopAttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.journal = JournalWriter(base_dir=self.base / "journal", git_sha="test04")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _events(self, event_type: str) -> list[dict]:
        return [
            record
            for record in self.journal.read_all()
            if record["event_type"] == event_type
        ]

    async def test_c6_missing_recovery_context_refuses_without_broker_call(self) -> None:
        connector = _AttachmentConnector(
            positions=[BrokerPosition(ticker="G", qty=71, avg_price=Decimal("32.00"))]
        )

        with self.assertRaises(strategy_runner.RecoveryContextError):
            await strategy_runner.attach_protective_stop_to_existing_position(
                connector=connector,
                journal=self.journal,
                symbol="G",
                qty=71,
                stop_price=Decimal("30.00"),
                strategy_id="g-2026-05",
                recovery_context=None,
            )

        self.assertEqual(connector.stop_orders, [])
        refused = self._events("protective_stop_attach_refused_no_recovery_context")
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["payload"]["strategy_id"], "g-2026-05")
        self.assertEqual(refused[0]["payload"]["symbol"], "G")

    async def test_c3_explicit_verb_refuses_on_position_drift(self) -> None:
        connector = _AttachmentConnector(
            positions=[BrokerPosition(ticker="G", qty=70, avg_price=Decimal("32.00"))]
        )

        with self.assertRaises(strategy_runner.PositionDriftError):
            await strategy_runner.attach_protective_stop_to_existing_position(
                connector=connector,
                journal=self.journal,
                symbol="G",
                qty=71,
                stop_price=Decimal("30.00"),
                strategy_id="g-2026-05",
                recovery_context=recovery_mod._RECOVERY_CONTEXT_TOKEN,
            )

        self.assertEqual(connector.stop_orders, [])
        refused = self._events("protective_stop_attach_refused_drift")
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["payload"]["expected_qty"], 71)
        self.assertEqual(refused[0]["payload"]["actual_qty"], "70")

    async def test_c3_multiple_symbol_positions_are_drift_even_if_sum_matches(
        self,
    ) -> None:
        connector = _AttachmentConnector(
            positions=[
                BrokerPosition(ticker="G", qty=50, avg_price=Decimal("32.00")),
                BrokerPosition(ticker="G", qty=21, avg_price=Decimal("33.00")),
            ]
        )

        with self.assertRaises(strategy_runner.PositionDriftError):
            await strategy_runner.attach_protective_stop_to_existing_position(
                connector=connector,
                journal=self.journal,
                symbol="G",
                qty=71,
                stop_price=Decimal("30.00"),
                strategy_id="g-2026-05",
                recovery_context=recovery_mod._RECOVERY_CONTEXT_TOKEN,
            )

        self.assertEqual(connector.stop_orders, [])
        refused = self._events("protective_stop_attach_refused_drift")
        self.assertEqual(refused[0]["payload"]["matching_position_count"], 2)

    async def test_c3_fractional_position_qty_is_drift_not_truncated(self) -> None:
        connector = _AttachmentConnector(
            positions=[
                BrokerPosition(
                    ticker="G",
                    qty=Decimal("70.9"),
                    avg_price=Decimal("32.00"),
                )
            ]
        )

        with self.assertRaises(strategy_runner.PositionDriftError):
            await strategy_runner.attach_protective_stop_to_existing_position(
                connector=connector,
                journal=self.journal,
                symbol="G",
                qty=70,
                stop_price=Decimal("30.00"),
                strategy_id="g-2026-05",
                recovery_context=recovery_mod._RECOVERY_CONTEXT_TOKEN,
            )

        self.assertEqual(connector.stop_orders, [])
        refused = self._events("protective_stop_attach_refused_drift")
        self.assertEqual(refused[0]["payload"]["actual_qty"], "70.9")

    async def test_c3_short_position_is_refused_before_sell_stop(self) -> None:
        connector = _AttachmentConnector(
            positions=[BrokerPosition(ticker="G", qty=-71, avg_price=Decimal("32.00"))]
        )

        with self.assertRaises(strategy_runner.PositionDriftError):
            await strategy_runner.attach_protective_stop_to_existing_position(
                connector=connector,
                journal=self.journal,
                symbol="G",
                qty=71,
                stop_price=Decimal("30.00"),
                strategy_id="g-2026-05",
                recovery_context=recovery_mod._RECOVERY_CONTEXT_TOKEN,
            )

        self.assertEqual(connector.stop_orders, [])
        refused = self._events("protective_stop_attach_refused_drift")
        self.assertEqual(refused[0]["payload"]["actual_qty"], "-71")

    async def test_c3_negative_requested_qty_is_refused(self) -> None:
        connector = _AttachmentConnector(
            positions=[BrokerPosition(ticker="G", qty=-71, avg_price=Decimal("32.00"))]
        )

        with self.assertRaises(strategy_runner.PositionDriftError):
            await strategy_runner.attach_protective_stop_to_existing_position(
                connector=connector,
                journal=self.journal,
                symbol="G",
                qty=-71,
                stop_price=Decimal("30.00"),
                strategy_id="g-2026-05",
                recovery_context=recovery_mod._RECOVERY_CONTEXT_TOKEN,
            )

        self.assertEqual(connector.stop_orders, [])
        refused = self._events("protective_stop_attach_refused_drift")
        self.assertEqual(refused[0]["payload"]["expected_qty"], -71)

    async def test_c4_explicit_verb_succeeds_on_exact_position_match(self) -> None:
        connector = _AttachmentConnector(
            positions=[BrokerPosition(ticker="G", qty=71, avg_price=Decimal("32.00"))]
        )

        ack = await strategy_runner.attach_protective_stop_to_existing_position(
            connector=connector,
            journal=self.journal,
            symbol="G",
            qty=71,
            stop_price=Decimal("30.00"),
            strategy_id="g-2026-05",
            recovery_context=recovery_mod._RECOVERY_CONTEXT_TOKEN,
        )

        self.assertEqual(ack.broker_order_id, "7001")
        self.assertEqual(connector.stop_orders[0]["side"], "sell")
        self.assertEqual(connector.stop_orders[0]["time_in_force"], "GTC")
        self.assertEqual(connector.stop_orders[0]["parent_id"], 0)
        self.assertTrue(connector.stop_orders[0]["transmit"])
        attached = self._events("protective_stop_attached_to_existing_position")
        self.assertEqual(len(attached), 1)
        self.assertEqual(attached[0]["payload"]["broker_order_id"], "7001")

    def test_c5_normal_cycle_does_not_call_recovery_only_verb(self) -> None:
        for method in (Engine._process_strategies, Engine._submit):
            self.assertNotIn(
                "attach_protective_stop_to_existing_position",
                inspect.getsource(method),
            )

    async def test_c6_forged_recovery_context_refuses_without_broker_call(self) -> None:
        connector = _AttachmentConnector(
            positions=[BrokerPosition(ticker="G", qty=71, avg_price=Decimal("32.00"))]
        )

        with self.assertRaises(strategy_runner.RecoveryContextError):
            await strategy_runner.attach_protective_stop_to_existing_position(
                connector=connector,
                journal=self.journal,
                symbol="G",
                qty=71,
                stop_price=Decimal("30.00"),
                strategy_id="g-2026-05",
                recovery_context=object(),
            )

        self.assertEqual(connector.stop_orders, [])
        refused = self._events("protective_stop_attach_refused_no_recovery_context")
        self.assertEqual(len(refused), 1)

    def test_c6_recovery_context_token_is_not_public_api(self) -> None:
        self.assertNotIn(
            "_RECOVERY_CONTEXT_TOKEN",
            getattr(recovery_context_mod, "__all__", ()),
        )
        self.assertNotIn(
            "_RECOVERY_CONTEXT_TOKEN",
            inspect.getsource(strategy_runner),
        )

    def test_c_schema_rejects_malformed_attached_payload(self) -> None:
        with self.assertRaises(JournalSchemaError):
            validate_protective_stop_attached_payload(
                {
                    "strategy_id": "g-2026-05",
                    "symbol": "G",
                    "qty": 71,
                    "stop_price": "30.00",
                    "broker_order_id": "",
                    "broker_perm_id": "8001",
                }
            )


if __name__ == "__main__":
    unittest.main()
