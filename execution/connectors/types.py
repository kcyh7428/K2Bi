"""Broker-neutral result types + connector protocol.

The engine + recovery module import from here and never from ib_async
directly. That keeps the engine testable against `MockIBKRConnector`
(tests/test_engine_*) without requiring ib_async to be installed and
without a real IB Gateway on the test host.

Why Protocol and not abc.ABC: we want duck-typed compatibility for
mocks without inheritance coupling. Tests can define a class that
implements the interface without importing or subclassing anything from
this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class AccountSummary:
    """Point-in-time account snapshot from the broker."""

    account_id: str
    cash: Decimal
    net_liquidation: Decimal
    currency: str


@dataclass(frozen=True)
class BrokerPosition:
    """An open position as the broker reports it.

    Broker is authoritative for qty and avg_price on crash-restart (see
    execution.engine.recovery). The engine's in-memory positions are
    always snapped from this type.
    """

    ticker: str
    qty: int
    avg_price: Decimal


@dataclass(frozen=True)
class BrokerOrderAck:
    """Submission receipt from the broker.

    broker_order_id is the per-session orderId; broker_perm_id is the
    persistent ID that survives IB Gateway restarts. Both are recorded
    in the journal so recovery can join by permId when orderId re-
    issues after a restart.

    `warnings` carries non-fatal submit-time anomalies the connector
    observed (e.g. protective stop child was rejected after the parent
    filled, leaving the position unprotected). Engine journals these
    and can escalate to KILLED if the situation demands human action.
    An empty tuple means the submit was fully clean.
    """

    broker_order_id: str
    broker_perm_id: str
    submitted_at: datetime
    status: str  # "Submitted" | "PreSubmitted" | ...
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class BrokerOpenOrder:
    """An order still visible in the broker's open-order list.

    status values we treat as terminal vs live are enumerated in
    execution.engine.recovery; do not hard-code status strings
    elsewhere.

    `tif` carries the broker's time-in-force for the order. Engine's
    EOD routine uses this to decide which open orders to cancel at
    the session boundary -- DAY orders get cancelled, GTC/GTD stay
    live across sessions. Default "DAY" matches IBKR's default
    when a connector implementation can't surface the true value.

    `client_tag` carries the engine-assigned `orderRef` ib_async sent
    at submit time. Recovery uses this to match broker-side orders to
    journal entries when the post-submit journal write was interrupted
    (broker-side orderId/permId never made it to disk) and to
    recognize linked stop-loss child orders so they are not flagged
    as phantoms after parent fills. Empty when the connector cannot
    surface a client reference.

    `aux_price` carries the broker's `auxPrice` field -- the STOP
    trigger for STP orders and the trail amount for TRAIL orders.
    For LMT parents the broker populates `lmtPrice` (mapped to
    `limit_price` here) and leaves `auxPrice` at 0. For STP children
    the reverse holds: `lmtPrice` is 0 and `auxPrice` carries the
    trigger. Q31 protective-stop-drift validation compares this
    against the checkpoint's `trigger_price`.

    CONTRACT: Any connector that surfaces STP orders MUST populate
    `aux_price` with the order's trigger. The default Decimal("0")
    is a FAIL-CLOSED sentinel per Session A Design Decision 6:
    recovery compares this field via EXACT Decimal equality against
    the journaled trigger, so an unpopulated aux_price on a real
    STP order will emit `protective_stop_price_drift` and block
    startup. This is intentional -- a connector that drops stop
    trigger info should refuse-to-start, not silently proceed with
    unverified protective stops. Live IBKR connector pulls from
    ib_async's `order.auxPrice`; mock connectors for tests must set
    aux_price explicitly whenever the mock returns a stop child.
    """

    broker_order_id: str
    broker_perm_id: str
    ticker: str
    side: str
    qty: int
    filled_qty: int
    limit_price: Decimal
    status: str
    submitted_at: datetime | None = None
    tif: str = "DAY"
    client_tag: str = ""
    aux_price: Decimal = Decimal("0")


CLIENT_TAG_PREFIX = "k2bi:"
CLIENT_TAG_STOP_SUFFIX = ":stop"


def parse_client_tag(tag: str) -> tuple[str | None, str | None, bool]:
    """Extract (strategy, trade_id, is_stop_child) from a client_tag.

    The format emitted by the engine is:
        parent: "k2bi:<strategy>:<trade_id>"
        stop:   "k2bi:<strategy>:<trade_id>:stop"

    Returns (None, None, False) if the tag is empty or not in the
    expected format. Non-k2bi tags are ignored -- their open orders
    are phantom from our perspective and surface as mismatches.
    """
    if not tag or not tag.startswith(CLIENT_TAG_PREFIX):
        return None, None, False
    rest = tag[len(CLIENT_TAG_PREFIX):]
    is_stop = rest.endswith(CLIENT_TAG_STOP_SUFFIX)
    if is_stop:
        rest = rest[: -len(CLIENT_TAG_STOP_SUFFIX)]
    parts = rest.split(":", 1)
    if len(parts) != 2:
        return None, None, is_stop
    strategy, trade_id = parts[0], parts[1]
    if not strategy or not trade_id:
        return None, None, is_stop
    return strategy, trade_id, is_stop


@dataclass(frozen=True)
class BrokerOrderStatusEvent:
    """A terminal-status update for an order the engine may not have
    been connected for (e.g. fill that landed while IB Gateway was
    down).

    Recovery consumes these via get_order_status_history() to reconcile
    journal-pending orders against their real fate.

    `client_tag` mirrors BrokerOpenOrder.client_tag and is populated
    by the connector when ib_async preserves `orderRef` on completed
    orders. Recovery uses it to match by trade_id in the crash-window
    case where the journal has only order_proposed (no broker IDs yet)
    but the order terminated at the broker.
    """

    broker_order_id: str
    broker_perm_id: str
    status: str
    filled_qty: int
    remaining_qty: int
    avg_fill_price: Decimal | None
    last_update_at: datetime
    reason: str | None = None  # broker-supplied rejection / cancel reason
    client_tag: str = ""


@dataclass(frozen=True)
class BrokerExecution:
    """A fill event. One submitted order can produce multiple fills
    (partials) -- each is its own execution with its own exec_id."""

    exec_id: str
    broker_order_id: str
    broker_perm_id: str
    ticker: str
    side: str
    qty: int
    price: Decimal
    filled_at: datetime


@dataclass(frozen=True)
class ConnectionStatus:
    """What the engine sees when it asks the connector 'are we up'."""

    connected: bool
    auth_required: bool
    last_error: str | None = None


@runtime_checkable
class IBKRConnectorProtocol(Protocol):
    """Shape every connector (live + mock) must satisfy.

    All methods are async because ib_async drives an asyncio event
    loop; the protocol matches that shape so live + mock are drop-in
    swap at the engine boundary.

    Recovery methods (`get_executions_since`, `get_order_status_history`)
    are architect-mandated for Bundle 2: without them, orders that
    completed while the engine was down cannot be reconciled, and the
    engine would restart into phantom state.
    """

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def connection_status(self) -> ConnectionStatus: ...

    async def get_account_summary(self) -> AccountSummary: ...
    async def get_positions(self) -> list[BrokerPosition]: ...
    async def get_open_orders(self) -> list[BrokerOpenOrder]: ...
    async def get_marks(self, tickers: list[str]) -> dict[str, Decimal]: ...

    async def submit_order(
        self,
        ticker: str,
        side: str,
        qty: int,
        limit_price: Decimal,
        stop_loss: Decimal | None,
        time_in_force: str = "DAY",
        client_tag: str | None = None,
    ) -> BrokerOrderAck: ...

    async def cancel_order(self, broker_order_id: str) -> None: ...

    async def get_executions_since(
        self, since: datetime
    ) -> list[BrokerExecution]: ...

    async def get_order_status_history(
        self, since: datetime
    ) -> list[BrokerOrderStatusEvent]: ...


# Order statuses that are terminal (no further fills expected). The
# recovery module + engine both consume this set, so centralizing it
# here prevents drift.
TERMINAL_ORDER_STATUSES = frozenset(
    {
        "Filled",
        "Cancelled",
        "ApiCancelled",
        "Inactive",       # IB's "order is not in an eligible state" bucket
        "Rejected",
    }
)

# Statuses we treat as "order is still live at the broker". Everything
# else is ambiguous and should be surfaced as a discrepancy.
LIVE_ORDER_STATUSES = frozenset(
    {
        "Submitted",
        "PreSubmitted",
        "PendingSubmit",
        "PendingCancel",
    }
)


class ConnectorError(Exception):
    """Base class for connector-layer failures.

    Subclasses let the engine branch on failure class without pattern-
    matching on error strings (which drift across ib_async versions).
    """


class AuthRequiredError(ConnectorError):
    """IB Gateway needs a human re-login. See broker-research.md#15."""


class DisconnectedError(ConnectorError):
    """Socket dropped; reconnect is pending."""


class BrokerRejectionError(ConnectorError):
    """Broker refused the order (not a validator reject -- the order
    already passed pre-trade validators and the broker side refused).

    Common causes: out-of-hours for a DAY order, ticker halted,
    fat-finger protection on an outlier limit price.
    """

    def __init__(
        self,
        message: str,
        *,
        broker_reason: str = "",
        broker_order_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.broker_reason = broker_reason
        self.broker_order_id = broker_order_id
