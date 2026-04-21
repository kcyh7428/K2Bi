"""IBKR HK connector.

Wraps ib_async 2.1.0 around localhost:4002 (IB Gateway 10.37). Smoke
test on DUQ demo paper account passed 2026-04-15 via the standalone
script that proved ib_async connectivity before Bundle 2.

Design boundaries:
    - Lazy import of ib_async: module-level import would make the whole
      `execution.connectors` package unimportable on hosts that haven't
      installed the library (e.g. CI runners that only unit-test the
      engine against MockIBKRConnector). The import happens inside
      connect() -- callers that never construct a live connector never
      need the dependency.
    - Credentials: none in code. IB Gateway reads login from its own
      config; the connector knows only (host, port, clientId).
    - Error taxonomy: ib_async raises a flat Exception hierarchy with
      error codes in messages. This module maps the codes we care
      about (502 auth, 504 disconnect, 201 order rejected) into the
      typed exceptions in connectors.types so the engine branches on
      class, not string-match.
    - Read-only API toggle: IB Gateway's Read-Only API mode blocks
      order submission. Phase 2 paper trading requires Read-Only OFF.
      The connector surfaces the clear-order rejection code so Keith
      sees a loud failure instead of silent no-op.

Protocol conformance: methods match IBKRConnectorProtocol verbatim.
Tests exercise the mock connector at the same interface so swapping
this in for Phase 3 paper trading is a construction-site change only.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

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
    ConnectorError,
    DisconnectedError,
)


LOG = logging.getLogger("k2bi.connector.ibkr")


def _broker_id_str(value: Any) -> str:
    """Convert an ib_async orderId / permId to its canonical string.

    Codex round-13 P2: an unassigned orderId/permId comes back as
    int 0, which str() turns into the truthy "0". Recovery keys
    pending orders by broker_order_id / broker_perm_id -- multiple
    unassigned IDs would all collide on "oid:0" / "perm:0", so two
    in-flight orders could swap identities after a restart. Empty
    string means "no broker ID yet"; recovery falls through to
    trade_id matching instead.
    """
    if value in (None, 0, "0"):
        return ""
    return str(value)


# ib_async error codes we branch on. Keeping the list small and
# referenced from one place (here) so we don't sprinkle magic numbers.
# See IB TWS API error codes documentation.
_AUTH_ERROR_CODES = {502, 1100, 2110}       # login / cold-connect / connectivity
_DISCONNECT_ERROR_CODES = {504, 1102, 2103} # socket-level / market-data farm
_ORDER_REJECT_CODES = {201, 202, 203, 399}  # broker order rejection


# Q34 (2026-04-21) bounded broker-API calls. Session F's run 3 hung
# for 3+ minutes on timed-out `open_orders_request` /
# `completed_orders_request` / `executions_request` after a
# connectivity flap. Every READ-PATH broker call is wrapped in
# asyncio.wait_for so the engine degrades cleanly instead of wedging.
#
# Split policy (architect-confirmed 2026-04-21):
#   - Reads (positions, open orders, marks, executions, status
#     history) return an empty sentinel on timeout. Caller / Q39-B
#     recovery treat empty results as "broker visibility limited"
#     (journal-authoritative fallback).
#   - Connect + account-summary-probe raise DisconnectedError on
#     timeout. Caller's reconnect/backoff cycle fires. The account
#     summary is handled as a probe (not a read) because the default
#     AccountSummary(cash=0, net_liq=0) is ambiguous with a real
#     zero-balance account and could mislead risk gates (MiniMax
#     Q34 R1 finding #5).
#
# SCOPE LIMITATION (2026-04-21): write-path calls -- submit_order,
# cancel_order, _await_parent_terminal -- are NOT wrapped by this
# change. They remain de-facto bounded by their existing ib_async
# polling loops (parent orderId/permId assignment: 50 iterations x
# 0.1s = 5s; child rejection cleanup: 30 x 0.1s = 3s; cancel broker
# confirmation: 30 x 0.1s = 3s). Total submit_order wall-time is
# bounded at ~15s worst case without explicit asyncio.wait_for. A
# formal write-path wrapper is deferred to Phase 4+ when execution-
# layer changes re-open; the Q34 scope per architect 2026-04-21 is
# the read path that hung Session F.
IBKR_CALL_TIMEOUT_SECONDS = 10.0


def _resolve_timeout(conn: "IBKRConnector") -> float:
    """Return the connector's configured per-call timeout. Defaults to
    IBKR_CALL_TIMEOUT_SECONDS; tests pass a sub-second override via
    the `timeout_seconds` constructor parameter (Q34 MiniMax R2
    finding #5)."""
    return conn._call_timeout_seconds


async def _bounded_read(
    conn: "IBKRConnector",
    awaitable: Any,
    *,
    call_name: str,
    empty: Any,
) -> Any:
    """Bound a broker-API READ call. On timeout: log + return `empty`
    so the caller falls back to journal-authoritative per Q39-B. Does
    NOT catch non-timeout exceptions -- those flow through the caller's
    existing _classify_and_raise path unchanged."""
    timeout = _resolve_timeout(conn)
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError:
        LOG.warning(
            "broker_api_timeout call=%s timeout=%.1fs; returning empty "
            "(Q34 journal-authoritative fallback)",
            call_name,
            timeout,
        )
        return empty


async def _bounded_probe(
    conn: "IBKRConnector",
    awaitable: Any,
    *,
    call_name: str,
) -> Any:
    """Bound a broker-API PROBE or CONNECT call. On timeout: raise
    DisconnectedError so the engine's reconnect/backoff cycle fires
    rather than silently degrading. Non-timeout exceptions flow
    through unchanged."""
    timeout = _resolve_timeout(conn)
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise DisconnectedError(
            f"broker_api_timeout call={call_name} timeout={timeout:.1f}s"
        ) from exc


class IBKRConnector:
    """Live ib_async-backed connector.

    Construct with broker coordinates; call `await connect()` before
    any other method. All reads + writes raise typed ConnectorError
    subclasses on failure; the engine's state machine owns reconnect
    backoff (per architect Q4 decision -- 5s start, 2x, 300s cap).
    """

    def __init__(
        self,
        *,
        account_id: str | None,
        host: str = "127.0.0.1",
        port: int = 4002,
        client_id: int = 1,
        default_currency: str = "HKD",
        timeout_seconds: float | None = None,
    ) -> None:
        """Construct with account scoping declared explicitly.

        Codex R16/R18 fixes introduced account_id filtering on
        get_positions / get_open_orders / get_executions_since /
        get_order_status_history, but the original constructor
        defaulted account_id=None, so any caller that forgot the
        kwarg silently lost the filter. Keith's architect ruling
        (post-R18): make this the K2Bi equivalent of Bundle 1's
        cash_only canonical helper -- discipline enforced at the
        type level. The kwarg is keyword-only + has no default, so a
        missing account decision is a TypeError at construction
        time, not a silent runtime bug.

        Single-account paper deployments pass account_id=None
        explicitly; multi-account live must pass the actual account
        id. Either is a conscious choice; neither is implicit.
        """
        self._host = host
        self._port = port
        self._client_id = client_id
        self._account_id = account_id
        self._default_currency = default_currency
        # Q34 MiniMax R2 finding #5 (2026-04-21): explicit constructor
        # parameter replaces the earlier _test_timeout_override getattr
        # lookup so the override path is part of the class contract,
        # not an undocumented runtime attribute that silently reverts
        # to the 10s production default after a rename.
        self._call_timeout_seconds: float = (
            float(timeout_seconds)
            if timeout_seconds is not None and timeout_seconds > 0
            else IBKR_CALL_TIMEOUT_SECONDS
        )

        self._ib: Any = None  # ib_async.IB instance, typed loosely to avoid import
        self._connected = False
        self._auth_required = False
        self._last_error: str | None = None

    # ---------- connection lifecycle ----------

    async def connect(self) -> None:
        try:
            import ib_async  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ConnectorImportError(
                "ib_async is not installed in this environment. "
                "Install with `pip install ib_async==2.1.0` or run the "
                "engine against MockIBKRConnector for unit tests."
            ) from exc

        if self._ib is None:
            self._ib = ib_async.IB()

        try:
            await _bounded_probe(
                self,
                self._ib.connectAsync(
                    host=self._host,
                    port=self._port,
                    clientId=self._client_id,
                    readonly=False,  # paper trading must allow order submission
                ),
                call_name="connect",
            )
        except Exception as exc:  # ib_async raises a flat hierarchy
            self._classify_and_raise(exc, phase="connect")

        # ib_async can report "connected" before auth settles. Poll
        # once for account summary to prove the session is usable --
        # if this call triggers 502, we learn about it now rather than
        # on the first order submission.
        try:
            await _bounded_probe(
                self,
                self._ib.reqAccountSummaryAsync(),
                call_name="post-connect-probe",
            )
        except Exception as exc:
            self._classify_and_raise(exc, phase="post-connect probe")

        self._connected = True
        self._auth_required = False
        self._last_error = None

    async def disconnect(self) -> None:
        if self._ib is not None and self._connected:
            try:
                self._ib.disconnect()
            except Exception as exc:  # pragma: no cover - shutdown path
                LOG.warning("ib_async disconnect raised: %s", exc)
        self._connected = False

    def connection_status(self) -> ConnectionStatus:
        return ConnectionStatus(
            connected=self._connected,
            auth_required=self._auth_required,
            last_error=self._last_error,
        )

    # ---------- reads ----------

    async def get_account_summary(self) -> AccountSummary:
        """Aggregate the broker account snapshot.

        Codex round-5 P2: IBKR returns one row per (account, tag,
        currency), so multi-currency accounts have multiple
        TotalCashValue rows. The previous implementation overwrote
        cash/NLV on every row, giving whatever currency happened to
        come last. The correct filter is "BASE" currency for the
        account's base-currency snapshot (IBKR names the summary row
        for the base currency with currency="BASE"). If BASE is
        absent, fall back to the configured default_currency.
        """
        self._require_connected()
        try:
            # Q34 MiniMax R1 finding #5 (2026-04-21): use the PROBE
            # wrapper here, not the read wrapper. A default
            # AccountSummary(cash=0, net_liq=0) returned on timeout is
            # semantically indistinguishable from a real zero-balance
            # account; callers gating on NAV thresholds would reach
            # wrong conclusions. Raising DisconnectedError funnels the
            # call through the engine's reconnect/backoff cycle so a
            # cash/NLV snapshot only surfaces when broker visibility
            # is confirmed.
            rows = await _bounded_probe(
                self,
                self._ib.accountSummaryAsync(),
                call_name="account_summary",
            )
        except Exception as exc:
            self._classify_and_raise(exc, phase="account_summary")

        def _pick(
            preferred_currencies: tuple[str, ...],
            tag: str,
        ) -> Decimal | None:
            for target in preferred_currencies:
                for row in rows:
                    if getattr(row, "tag", "") != tag:
                        continue
                    if (
                        self._account_id
                        and getattr(row, "account", "") != self._account_id
                    ):
                        continue
                    row_currency = getattr(row, "currency", "") or ""
                    if row_currency.upper() != target.upper():
                        continue
                    try:
                        return Decimal(str(getattr(row, "value", "0")))
                    except Exception:  # pragma: no cover - broker edge
                        return None
            return None

        preferred = ("BASE", self._default_currency)
        cash = _pick(preferred, "TotalCashValue") or Decimal("0")
        net_liq = _pick(preferred, "NetLiquidation") or Decimal("0")

        # Determine which currency the picked values actually came
        # from. Codex R17 P3 / R20 P2: the prior implementation set
        # `currency` to the first non-BASE NetLiquidation row, which
        # mislabels BASE-sourced cash/NLV as (say) USD on multi-
        # currency accounts. Walk the same preferred order we used
        # for the values so cash/net_liq/currency are consistent.
        account_id = self._account_id or ""
        currency = self._default_currency
        for target in preferred:
            for row in rows:
                if getattr(row, "tag", "") != "NetLiquidation":
                    continue
                if (
                    self._account_id
                    and getattr(row, "account", "") != self._account_id
                ):
                    continue
                row_currency = getattr(row, "currency", "") or ""
                if row_currency.upper() != target.upper():
                    continue
                if not account_id:
                    account_id = getattr(row, "account", "") or ""
                currency = row_currency
                break
            else:
                continue
            break
        return AccountSummary(
            account_id=account_id,
            cash=cash,
            net_liquidation=net_liq,
            currency=currency,
        )

    async def get_positions(self) -> list[BrokerPosition]:
        """Broker positions filtered to the configured account_id.

        Codex R16 P1: `reqPositionsAsync` returns every position
        visible to the login, including sub-accounts and other client-
        IDs on the same Gateway. Without the account filter, engine
        startup / recovery / risk checks would see another account's
        holdings and either refuse to start (phantom_position on
        another account's ticker) or mis-size new orders. Matches the
        filter `get_account_summary` already applies.
        """
        self._require_connected()
        try:
            rows = await _bounded_read(
                self,
                self._ib.reqPositionsAsync(),
                call_name="positions",
                empty=[],
            )
        except Exception as exc:
            self._classify_and_raise(exc, phase="positions")

        out: list[BrokerPosition] = []
        for row in rows:
            if self._account_id:
                row_account = getattr(row, "account", "") or ""
                if row_account and row_account != self._account_id:
                    continue
            contract = getattr(row, "contract", None)
            qty = int(getattr(row, "position", 0))
            avg = getattr(row, "avgCost", 0)
            symbol = getattr(contract, "symbol", "") if contract else ""
            if not symbol or qty == 0:
                continue
            out.append(
                BrokerPosition(
                    ticker=symbol,
                    qty=qty,
                    avg_price=Decimal(str(avg)),
                )
            )
        return out

    async def get_open_orders(self) -> list[BrokerOpenOrder]:
        """Open orders filtered to the configured account_id.

        Codex R16 P1: `reqAllOpenOrdersAsync` returns every visible
        open order including other accounts/clients on the same
        Gateway. Without the account filter, recovery can refuse
        startup because another account has a live order, and EOD can
        cancel another account's DAY order if it happens to share a
        `k2bi:` prefix. Engine enforcement hangs on isolating this
        account's own activity.
        """
        self._require_connected()
        try:
            rows = await _bounded_read(
                self,
                self._ib.reqAllOpenOrdersAsync(),
                call_name="open_orders",
                empty=[],
            )
        except Exception as exc:
            self._classify_and_raise(exc, phase="open_orders")

        out: list[BrokerOpenOrder] = []
        for trade in rows:
            order = getattr(trade, "order", None)
            contract = getattr(trade, "contract", None)
            status_obj = getattr(trade, "orderStatus", None)
            if order is None or contract is None:
                continue
            if self._account_id:
                order_account = getattr(order, "account", "") or ""
                if order_account and order_account != self._account_id:
                    continue
            tif = str(getattr(order, "tif", "") or "DAY").upper()
            out.append(
                BrokerOpenOrder(
                    broker_order_id=_broker_id_str(getattr(order, "orderId", 0)),
                    broker_perm_id=_broker_id_str(getattr(order, "permId", 0)),
                    ticker=getattr(contract, "symbol", ""),
                    side=str(getattr(order, "action", "")).lower(),
                    qty=int(getattr(order, "totalQuantity", 0)),
                    filled_qty=int(getattr(status_obj, "filled", 0)) if status_obj else 0,
                    limit_price=Decimal(str(getattr(order, "lmtPrice", "0"))),
                    status=str(getattr(status_obj, "status", "")) if status_obj else "",
                    submitted_at=None,
                    tif=tif,
                    client_tag=str(getattr(order, "orderRef", "") or ""),
                    # Q31: surface auxPrice so recovery's protective-
                    # stop validation can compare against the
                    # checkpoint's trigger_price. STP children carry
                    # their trigger in auxPrice; LMT parents leave
                    # it at 0 and Q31's price-drift check skips them.
                    aux_price=Decimal(str(getattr(order, "auxPrice", "0") or "0")),
                )
            )
        return out

    async def get_marks(self, tickers: list[str]) -> dict[str, Decimal]:
        self._require_connected()
        if not tickers:
            return {}
        import ib_async  # type: ignore[import-not-found]

        # Q34 MiniMax R3 finding #1 (2026-04-21): sequential per-ticker
        # awaits with 10s timeouts amplify worst-case wall time linearly
        # (50 tickers x 10s = 500s). Cap the TOTAL wall budget for the
        # whole marks pass so a slow market-data farm cannot stall
        # validation for minutes. Individual ticker timeouts still
        # apply; this is an outer envelope around the whole loop.
        aggregate_budget = min(
            3.0 * _resolve_timeout(self), 60.0
        )
        deadline = time.monotonic() + aggregate_budget
        out: dict[str, Decimal] = {}
        for ticker in tickers:
            if time.monotonic() >= deadline:
                LOG.warning(
                    "get_marks aggregate budget %.1fs exhausted; "
                    "remaining tickers skipped (Q34 R3 finding #1)",
                    aggregate_budget,
                )
                break
            contract = ib_async.Stock(ticker, "SMART", "USD")
            try:
                rows = await _bounded_read(
                    self,
                    self._ib.reqTickersAsync(contract),
                    call_name=f"tickers[{ticker}]",
                    empty=[],
                )
            except Exception as exc:
                # Market data errors are logged but non-fatal: validators
                # fall back to avg_price when a mark is missing.
                LOG.warning("mark fetch failed for %s: %s", ticker, exc)
                continue
            if not rows:
                # Q34 timeout path: reqTickersAsync timed out; skip.
                continue
            ticker_row = rows[0]
            mark = getattr(ticker_row, "marketPrice", None)
            if mark is None or mark != mark:  # NaN check
                continue
            out[ticker] = Decimal(str(mark))
        return out

    async def get_executions_since(
        self, since: datetime
    ) -> list[BrokerExecution]:
        self._require_connected()
        import ib_async  # type: ignore[import-not-found]

        since_utc = since.astimezone(timezone.utc) if since.tzinfo else since
        # Codex R16 P1 (applied transitively): ExecutionFilter accepts
        # an account param; populate when configured so cross-account
        # fills cannot leak into engine state.
        filter_kwargs: dict[str, Any] = {
            "time": since_utc.strftime("%Y%m%d %H:%M:%S"),
        }
        if self._account_id:
            filter_kwargs["acctCode"] = self._account_id
        exec_filter = ib_async.ExecutionFilter(**filter_kwargs)
        try:
            fills = await _bounded_read(
                self,
                self._ib.reqExecutionsAsync(exec_filter),
                call_name="executions_since",
                empty=[],
            )
        except Exception as exc:
            self._classify_and_raise(exc, phase="executions_since")

        out: list[BrokerExecution] = []
        for fill in fills:
            execution = getattr(fill, "execution", None)
            contract = getattr(fill, "contract", None)
            if execution is None or contract is None:
                continue
            # Defense-in-depth: if the filter is honored by ib_async
            # the list is already scoped, but double-check in case a
            # future lib version changes semantics.
            if self._account_id:
                exec_account = getattr(execution, "acctNumber", "") or ""
                if exec_account and exec_account != self._account_id:
                    continue
            out.append(
                BrokerExecution(
                    exec_id=str(getattr(execution, "execId", "")),
                    broker_order_id=str(getattr(execution, "orderId", "")),
                    broker_perm_id=str(getattr(execution, "permId", "")),
                    ticker=getattr(contract, "symbol", ""),
                    side=str(getattr(execution, "side", "")).lower(),
                    qty=int(getattr(execution, "shares", 0)),
                    price=Decimal(str(getattr(execution, "price", "0"))),
                    filled_at=_parse_ib_time(getattr(execution, "time", None)),
                )
            )
        return out

    async def get_order_status_history(
        self, since: datetime
    ) -> list[BrokerOrderStatusEvent]:
        """Union of currently-open-order status + recent completed
        orders.

        ib_async does not expose a direct "order status history" call;
        completed orders come from `reqCompletedOrdersAsync`. We merge
        with currently-open orders so recovery sees every order that
        was in flight at crash time in one pass.
        """
        self._require_connected()
        out: list[BrokerOrderStatusEvent] = []

        try:
            completed = await _bounded_read(
                self,
                self._ib.reqCompletedOrdersAsync(apiOnly=False),
                call_name="completed_orders",
                empty=[],
            )
        except Exception as exc:
            self._classify_and_raise(exc, phase="completed_orders")

        for trade in completed:
            order = getattr(trade, "order", None)
            status_obj = getattr(trade, "orderStatus", None)
            if order is None or status_obj is None:
                continue
            # Codex R16 P1: scope completed orders to the configured
            # account so recovery cannot reconcile against another
            # account's fills/cancels.
            if self._account_id:
                order_account = getattr(order, "account", "") or ""
                if order_account and order_account != self._account_id:
                    continue
            last_update = _last_log_time(trade) or datetime.now(timezone.utc)
            if last_update < (since.astimezone(timezone.utc) if since.tzinfo else since):
                continue
            out.append(
                BrokerOrderStatusEvent(
                    broker_order_id=_broker_id_str(getattr(order, "orderId", 0)),
                    broker_perm_id=_broker_id_str(getattr(order, "permId", 0)),
                    status=str(getattr(status_obj, "status", "")),
                    filled_qty=int(getattr(status_obj, "filled", 0)),
                    remaining_qty=int(getattr(status_obj, "remaining", 0)),
                    avg_fill_price=(
                        Decimal(str(getattr(status_obj, "avgFillPrice", "0")))
                        if getattr(status_obj, "avgFillPrice", None) is not None
                        else None
                    ),
                    last_update_at=last_update,
                    reason=getattr(status_obj, "whyHeld", None) or None,
                    client_tag=str(getattr(order, "orderRef", "") or ""),
                )
            )
        return out

    # ---------- writes ----------

    async def submit_order(
        self,
        ticker: str,
        side: str,
        qty: int,
        limit_price: Decimal,
        stop_loss: Decimal | None,
        time_in_force: str = "DAY",
        client_tag: str | None = None,
    ) -> BrokerOrderAck:
        """Submit a limit order; if stop_loss is set, submit a linked
        stop child so the broker itself holds the protective stop.

        Codex round-3 P1: if the engine journals a stop and the broker
        does not hold one, a disconnect or process crash leaves the
        position completely unprotected. The bracket pattern below
        (parent.transmit=False, then child.transmit=True with
        parentId) tells IB Gateway to commit both orders atomically as
        a linked pair. The child is a GTC stop so it persists past the
        parent's DAY tif and survives engine restarts.
        """
        self._require_connected()
        import ib_async  # type: ignore[import-not-found]

        contract = ib_async.Stock(ticker, "SMART", "USD")
        action = "BUY" if side.lower() == "buy" else "SELL"
        parent = ib_async.LimitOrder(
            action,
            int(qty),
            float(limit_price),
            tif=time_in_force,
        )
        # transmit=False means IB Gateway holds the parent until a
        # child order with transmit=True arrives referencing its
        # parentId. If there is no stop, parent transmits on its own.
        parent.transmit = stop_loss is None
        if client_tag is not None:
            parent.orderRef = client_tag

        try:
            parent_trade = self._ib.placeOrder(contract, parent)
            # Wait for orderId assignment so the stop-child can
            # reference parentId. permId is not required yet; we wait
            # again after both orders transmit. Codex round-5 P1:
            # raise a typed error if IB never assigns the ID rather
            # than silently proceeding with orderId=0 (which would
            # break the parent/child linkage + make later recovery
            # matching impossible).
            for _ in range(50):
                if getattr(parent_trade.order, "orderId", 0):
                    break
                await asyncio.sleep(0.1)
            if not getattr(parent_trade.order, "orderId", 0):
                raise BrokerRejectionError(
                    "IB Gateway did not assign orderId within 5s of submit",
                    broker_reason="orderid_assignment_timeout",
                )

            child_trade = None
            if stop_loss is not None:
                stop_action = "SELL" if action == "BUY" else "BUY"
                child = ib_async.StopOrder(
                    stop_action,
                    int(qty),
                    float(stop_loss),
                    tif="GTC",  # outlive parent's DAY
                )
                child.parentId = parent_trade.order.orderId
                child.transmit = True  # transmit edge: commits pair
                if client_tag is not None:
                    child.orderRef = f"{client_tag}:stop"
                child_trade = self._ib.placeOrder(contract, child)

            # Wait for permId on parent now that both orders are live.
            for _ in range(50):
                if getattr(parent_trade.order, "permId", 0):
                    break
                await asyncio.sleep(0.1)
            if not getattr(parent_trade.order, "permId", 0):
                raise BrokerRejectionError(
                    "IB Gateway did not assign permId within 5s of transmit",
                    broker_reason="permid_assignment_timeout",
                    broker_order_id=str(parent_trade.order.orderId),
                )

            # Codex round-8 P1 / round-9 P1: the stop child can be
            # rejected asynchronously by IBKR while a marketable parent
            # is already filling. Two paths below:
            #   - Parent still inactive/submitted  -> cancel parent + raise
            #   - Parent already live or filled    -> leave parent in place,
            #     return ack with a warning so the engine records the
            #     unprotected state and can escalate (kill / re-stop).
            warnings: list[str] = []
            if child_trade is not None:
                for _ in range(50):
                    child_status_obj = getattr(
                        child_trade, "orderStatus", None
                    )
                    child_status_str = (
                        str(getattr(child_status_obj, "status", ""))
                        if child_status_obj
                        else ""
                    )
                    if child_status_str == "Rejected":
                        parent_status_str = str(
                            getattr(parent_trade.orderStatus, "status", "")
                        )
                        parent_is_live = parent_status_str in {
                            "Filled",
                            "PartiallyFilled",
                            "Submitted",
                            "PreSubmitted",
                        }
                        reason = str(
                            getattr(child_status_obj, "whyHeld", "")
                            or "stop_child_rejected"
                        )
                        if parent_is_live:
                            warnings.append(
                                "protective_stop_child_rejected_parent_live:"
                                f"{reason}:parent_status={parent_status_str}"
                            )
                            break
                        try:
                            self._ib.cancelOrder(parent_trade.order)
                        except Exception as cancel_exc:  # pragma: no cover
                            LOG.warning(
                                "parent cancel after child rejection raised: %s",
                                cancel_exc,
                            )
                        # Codex R17 P1: cancelOrder is async at IBKR.
                        # Wait for broker-confirmed terminal status
                        # before raising BrokerRejectionError -- the
                        # engine's handler drops pending state on
                        # that exception, so a still-live parent in
                        # the unconfirmed window would become a
                        # phantom open order on restart.
                        await self._await_parent_terminal(
                            parent_trade, reason="child_rejected"
                        )
                        raise BrokerRejectionError(
                            "broker rejected protective stop child; parent cancelled",
                            broker_reason=reason,
                            broker_order_id=str(parent_trade.order.orderId),
                        )
                    if getattr(child_trade.order, "permId", 0):
                        break
                    await asyncio.sleep(0.1)
                if (
                    not getattr(child_trade.order, "permId", 0)
                    and not warnings
                ):
                    parent_status_str = str(
                        getattr(parent_trade.orderStatus, "status", "")
                    )
                    parent_is_live = parent_status_str in {
                        "Filled",
                        "PartiallyFilled",
                        "Submitted",
                        "PreSubmitted",
                    }
                    if parent_is_live:
                        warnings.append(
                            "protective_stop_child_permid_timeout_parent_live:"
                            f"parent_status={parent_status_str}"
                        )
                    else:
                        try:
                            self._ib.cancelOrder(parent_trade.order)
                        except Exception as cancel_exc:  # pragma: no cover
                            LOG.warning(
                                "parent cancel after child timeout raised: %s",
                                cancel_exc,
                            )
                        # Codex R17 P1: same async-cancel concern as
                        # the sibling rejected branch above.
                        await self._await_parent_terminal(
                            parent_trade, reason="child_permid_timeout"
                        )
                        raise BrokerRejectionError(
                            "stop child did not receive permId within 5s; parent cancelled",
                            broker_reason="stop_child_permid_timeout",
                            broker_order_id=str(parent_trade.order.orderId),
                        )
        except Exception as exc:
            self._classify_and_raise(exc, phase="submit")

        status = getattr(parent_trade.orderStatus, "status", "")
        return BrokerOrderAck(
            broker_order_id=str(parent_trade.order.orderId),
            broker_perm_id=str(parent_trade.order.permId),
            submitted_at=datetime.now(timezone.utc),
            status=status,
            warnings=tuple(warnings),
        )

    async def cancel_order(self, broker_order_id: str) -> None:
        self._require_connected()
        try:
            trades = self._ib.openTrades()
        except Exception as exc:
            self._classify_and_raise(exc, phase="cancel_lookup")
        for trade in trades:
            if str(trade.order.orderId) == broker_order_id:
                try:
                    self._ib.cancelOrder(trade.order)
                except Exception as exc:
                    self._classify_and_raise(exc, phase="cancel")
                return
        # Unknown order id: not an error -- EOD cancel can race with a
        # fill, and the engine's recovery path will observe the
        # terminal status on next tick.
        LOG.info(
            "cancel_order: no open order with broker_order_id=%s; "
            "likely already terminal",
            broker_order_id,
        )

    async def _await_parent_terminal(
        self, parent_trade: Any, *, reason: str
    ) -> None:
        """Poll up to ~3s for parent order to reach a terminal status.

        Codex R17 P1: cancelOrder is async. After requesting cancel
        we must give the broker time to confirm (Cancelled / Rejected
        / Inactive) before telling the engine the order is dead. If
        the terminal never arrives within the window we still proceed
        -- the engine's tick-based reconcile catches up on next poll
        -- but we log so the audit trail records the uncertainty.
        """
        terminal = {"Cancelled", "ApiCancelled", "Rejected", "Inactive"}
        for _ in range(30):
            status = str(
                getattr(parent_trade.orderStatus, "status", "")
            )
            if status in terminal:
                return
            await asyncio.sleep(0.1)
        LOG.warning(
            "parent cancel (%s) not broker-confirmed within 3s; "
            "engine poll will reconcile on next tick",
            reason,
        )

    # ---------- error classification ----------

    def _classify_and_raise(self, exc: Exception, *, phase: str) -> None:
        # If the exception is already one of our typed ConnectorError
        # subclasses, do not re-classify (that would demote
        # BrokerRejectionError to a generic DisconnectedError when
        # the "code" we extract from a message-less internal error is
        # None).
        if isinstance(exc, ConnectorError):
            # Still record the last error string for operator visibility,
            # but re-raise the original typed exception.
            self._last_error = f"{phase}: {exc}"
            raise exc
        code = _extract_error_code(exc)
        message = f"ib_async error during {phase}: code={code} err={exc}"
        self._last_error = message
        if code in _AUTH_ERROR_CODES:
            self._connected = False
            self._auth_required = True
            raise AuthRequiredError(message) from exc
        if code in _DISCONNECT_ERROR_CODES:
            self._connected = False
            raise DisconnectedError(message) from exc
        if code in _ORDER_REJECT_CODES:
            raise BrokerRejectionError(
                message,
                broker_reason=str(exc),
            ) from exc
        # Unknown code: treat as disconnect so the engine pauses + reconnects
        # instead of auto-retrying a busted session.
        self._connected = False
        raise DisconnectedError(message) from exc

    def _require_connected(self) -> None:
        if self._auth_required:
            raise AuthRequiredError("IB Gateway requires re-login")
        if not self._connected:
            raise DisconnectedError("not connected")


class ConnectorImportError(ConnectorError):
    """ib_async missing at connect-time. Tests use MockIBKRConnector.

    Codex R21 P2: inherits from ConnectorError (not RuntimeError) so
    the engine's _run_init exception handler catches it alongside
    AuthRequiredError / DisconnectedError and halts with a clean
    journal entry, instead of the exception escaping the state
    machine.
    """


def _extract_error_code(exc: Exception) -> int | None:
    """Pull the numeric IB error code off whatever ib_async raised.

    ib_async commonly exposes `.errorCode` on its errors; fall back to
    scanning the string for "code=NNN" or leading "NNN:". None if nothing
    parseable is found -- caller treats as unknown.
    """
    code = getattr(exc, "errorCode", None)
    if isinstance(code, int):
        return code
    msg = str(exc)
    import re

    m = re.search(r"(?:code=|reqId=\d+\s+)(\d{3,5})", msg)
    if m:
        return int(m.group(1))
    m = re.match(r"^(\d{3,5})\b", msg.strip())
    if m:
        return int(m.group(1))
    return None


def _parse_ib_time(value: Any) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value)
    for fmt in ("%Y%m%d  %H:%M:%S", "%Y%m%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def _last_log_time(trade: Any) -> datetime | None:
    log = getattr(trade, "log", None)
    if not log:
        return None
    last = log[-1]
    t = getattr(last, "time", None)
    if t is None:
        return None
    if isinstance(t, datetime):
        return t.astimezone(timezone.utc) if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return _parse_ib_time(t)


__all__ = ["IBKRConnector", "ConnectorImportError"]
