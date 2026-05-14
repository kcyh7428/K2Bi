"""In-memory connector for tests + dry-run.

Matches IBKRConnectorProtocol. Tests inject a MockIBKRConnector into
the engine so state-machine, recovery, and order-submission tests run
without ib_async or a live IB Gateway.

Deterministic: every method returns pre-loaded state or records the
call, no timing assumptions. Tests control time via explicit fixture
inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from .types import (
    AccountSummary,
    AuthRequiredError,
    BrokerExecution,
    BrokerOpenOrder,
    BrokerOrderAck,
    BrokerOrderStatusEvent,
    BrokerPosition,
    BrokerRejectionError,
    ConnectionStatus,
    DisconnectedError,
    POSITION_SOURCE_LIVE_REQ_POSITIONS,
    PositionSnapshot,
)


@dataclass
class SubmittedOrderRecord:
    """What the mock saw when submit_order was called. Tests assert
    against these to confirm the engine built the broker payload the
    way it intended."""

    ticker: str
    side: str
    qty: int
    limit_price: Decimal | None
    stop_loss: Decimal | None
    time_in_force: str
    client_tag: str | None
    broker_order_id: str
    broker_perm_id: str
    order_type: str = "LMT"


@dataclass
class StandaloneStopOrderRecord:
    """Recovery-only standalone protective stop sent through the mock."""

    ticker: str
    side: str
    qty: int
    stop_price: Decimal
    time_in_force: str
    client_tag: str | None
    broker_order_id: str
    broker_perm_id: str
    parent_id: int = 0
    transmit: bool = True


@dataclass
class MockIBKRConnector:
    """Protocol-compatible mock.

    Fields with default factories are the *state* the mock reports back:
        - account_summary, positions, open_orders, marks
        - executions_history, order_status_history (for recovery tests)

    Fields starting with `fail_` are failure-injection hooks -- set them
    to True/exception to make the next N calls raise.
    """

    account_summary: AccountSummary = field(
        default_factory=lambda: AccountSummary(
            account_id="DUQ-TEST",
            cash=Decimal("1000000"),
            net_liquidation=Decimal("1000000"),
            currency="HKD",
        )
    )
    positions: list[BrokerPosition] = field(default_factory=list)
    open_orders: list[BrokerOpenOrder] = field(default_factory=list)
    marks: dict[str, Decimal] = field(default_factory=dict)

    executions_history: list[BrokerExecution] = field(default_factory=list)
    order_status_history: list[BrokerOrderStatusEvent] = field(default_factory=list)

    submitted_orders: list[SubmittedOrderRecord] = field(default_factory=list)
    standalone_stop_orders: list[StandaloneStopOrderRecord] = field(
        default_factory=list
    )
    cancelled_order_ids: list[str] = field(default_factory=list)

    # connection state
    _connected: bool = False
    _auth_required: bool = False
    _last_error: str | None = None

    # failure injection
    fail_connect_with_auth: bool = False
    fail_connect_with_disconnect: bool = False
    fail_next_submit: Exception | None = None

    # id generation
    _next_order_id: int = 1000
    _next_perm_id: int = 2_000_000
    submit_hook: Callable[[SubmittedOrderRecord], BrokerOrderAck] | None = None

    # ---------- connection ----------

    async def connect(self) -> None:
        if self.fail_connect_with_auth:
            self._connected = False
            self._auth_required = True
            self._last_error = "auth_required"
            raise AuthRequiredError("IB Gateway requires re-login")
        if self.fail_connect_with_disconnect:
            self._connected = False
            self._auth_required = False
            self._last_error = "socket_reset"
            raise DisconnectedError("socket reset")
        self._connected = True
        self._auth_required = False
        self._last_error = None

    async def disconnect(self) -> None:
        self._connected = False

    def connection_status(self) -> ConnectionStatus:
        return ConnectionStatus(
            connected=self._connected,
            auth_required=self._auth_required,
            last_error=self._last_error,
        )

    # ---------- reads ----------

    async def get_account_summary(self) -> AccountSummary:
        self._require_connected()
        return self.account_summary

    async def get_positions(self) -> PositionSnapshot:
        self._require_connected()
        return PositionSnapshot(
            positions=list(self.positions),
            valid=True,
            source=POSITION_SOURCE_LIVE_REQ_POSITIONS,
            fetched_at=datetime.now(timezone.utc),
        )

    async def get_open_orders(self) -> list[BrokerOpenOrder]:
        self._require_connected()
        return list(self.open_orders)

    async def get_marks(self, tickers: list[str]) -> dict[str, Decimal]:
        self._require_connected()
        return {t: self.marks[t] for t in tickers if t in self.marks}

    async def get_executions_since(
        self, since: datetime
    ) -> list[BrokerExecution]:
        self._require_connected()
        return [e for e in self.executions_history if e.filled_at >= since]

    async def get_order_status_history(
        self, since: datetime
    ) -> list[BrokerOrderStatusEvent]:
        self._require_connected()
        return [e for e in self.order_status_history if e.last_update_at >= since]

    # ---------- writes ----------

    async def submit_order(
        self,
        ticker: str,
        side: str,
        qty: int,
        limit_price: Decimal | None,
        stop_loss: Decimal | None,
        time_in_force: str = "DAY",
        client_tag: str | None = None,
        order_type: str = "LMT",
    ) -> BrokerOrderAck:
        self._require_connected()
        if self.fail_next_submit is not None:
            err = self.fail_next_submit
            self.fail_next_submit = None
            raise err

        # Round-6 (2026-05-08): MKT requires limit_price=None or non-null
        # reference; LMT requires Decimal. Mirror the broker contract
        # so tests catch shape mismatches.
        if order_type == "LMT" and limit_price is None:
            raise ValueError(
                "MockIBKRConnector.submit_order: LMT requires a Decimal "
                "limit_price; got None"
            )

        broker_order_id = str(self._next_order_id)
        broker_perm_id = str(self._next_perm_id)
        self._next_order_id += 1
        self._next_perm_id += 1

        record = SubmittedOrderRecord(
            ticker=ticker,
            side=side,
            qty=qty,
            limit_price=limit_price,
            stop_loss=stop_loss,
            time_in_force=time_in_force,
            client_tag=client_tag,
            broker_order_id=broker_order_id,
            broker_perm_id=broker_perm_id,
            order_type=order_type,
        )
        self.submitted_orders.append(record)

        if self.submit_hook is not None:
            return self.submit_hook(record)

        return BrokerOrderAck(
            broker_order_id=broker_order_id,
            broker_perm_id=broker_perm_id,
            submitted_at=datetime.now(timezone.utc),
            status="Submitted",
        )

    async def submit_standalone_stop_order(
        self,
        *,
        ticker: str,
        side: str,
        qty: int,
        stop_price: Decimal,
        time_in_force: str = "GTC",
        client_tag: str | None = None,
    ) -> BrokerOrderAck:
        self._require_connected()
        broker_order_id = str(self._next_order_id)
        broker_perm_id = str(self._next_perm_id)
        self._next_order_id += 1
        self._next_perm_id += 1
        self.standalone_stop_orders.append(
            StandaloneStopOrderRecord(
                ticker=ticker,
                side=side,
                qty=qty,
                stop_price=stop_price,
                time_in_force=time_in_force,
                client_tag=client_tag,
                broker_order_id=broker_order_id,
                broker_perm_id=broker_perm_id,
            )
        )
        return BrokerOrderAck(
            broker_order_id=broker_order_id,
            broker_perm_id=broker_perm_id,
            submitted_at=datetime.now(timezone.utc),
            status="Submitted",
        )

    async def cancel_order(self, broker_order_id: str) -> None:
        self._require_connected()
        self.cancelled_order_ids.append(broker_order_id)
        self.open_orders = [
            o for o in self.open_orders if o.broker_order_id != broker_order_id
        ]

    # ---------- helpers ----------

    def _require_connected(self) -> None:
        if self._auth_required:
            raise AuthRequiredError("IB Gateway requires re-login")
        if not self._connected:
            raise DisconnectedError("not connected")

    # Test-friendly helpers for injecting state mid-test.
    def set_connected(self, value: bool) -> None:
        self._connected = value
        if value:
            self._auth_required = False
            self._last_error = None

    def trigger_auth_required(self) -> None:
        self._connected = False
        self._auth_required = True
        self._last_error = "auth_required"


__all__ = [
    "MockIBKRConnector",
    "StandaloneStopOrderRecord",
    "SubmittedOrderRecord",
    "BrokerRejectionError",
]
