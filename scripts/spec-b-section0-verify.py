"""Spec B Section 0 live broker-state verification.

Run only through scripts/gateway-query.sh. The script is executed on the VPS
against IB Gateway on 127.0.0.1:4002 and performs read-only checks.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ib_async import IB


BASELINE_AVG_COST = Decimal("30.334087507042256")
AVG_COST_TOLERANCE = Decimal("0.16")
EXPECTED_G_QTY = Decimal("71")
EXPECTED_G_STOP_PERM_ID = 1677427049
EXPECTED_G_STOP_PRICE = Decimal("30")
EXPECTED_SPY_QTY = Decimal("2")
EXPECTED_SPY_AVG_COST = Decimal("707.72")
EXPECTED_SPY_STOP_PERM_ID = 1888063981
EXPECTED_SPY_STOP_PRICE = Decimal("697.13")
EXPECTED_STOP_STATUS = {"PreSubmitted", "Submitted"}
KILL_PATH = Path.home() / "Projects" / "K2Bi-Vault" / "System" / ".killed"


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"cannot parse Decimal from {value!r}") from exc


def _systemctl_value(*args: str) -> str:
    result = subprocess.run(
        ["systemctl", *args, "k2bi-engine.service"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _position_rows(ib: IB) -> list[dict[str, Any]]:
    rows = []
    for pos in ib.positions():
        contract = pos.contract
        if getattr(contract, "symbol", "") not in {"G", "SPY"}:
            continue
        rows.append(
            {
                "account": getattr(pos, "account", None),
                "symbol": getattr(contract, "symbol", None),
                "secType": getattr(contract, "secType", None),
                "currency": getattr(contract, "currency", None),
                "exchange": getattr(contract, "exchange", None),
                "position": str(getattr(pos, "position", "")),
                "avgCost": str(getattr(pos, "avgCost", "")),
            }
        )
    return rows


def _open_order_rows(ib: IB) -> list[dict[str, Any]]:
    trades = ib.reqAllOpenOrders()
    ib.sleep(2)
    if trades is None:
        trades = ib.trades()

    rows = []
    for trade in trades:
        contract = trade.contract
        order = trade.order
        status = trade.orderStatus
        rows.append(
            {
                "symbol": getattr(contract, "symbol", None),
                "secType": getattr(contract, "secType", None),
                "currency": getattr(contract, "currency", None),
                "exchange": getattr(contract, "exchange", None),
                "orderId": getattr(order, "orderId", None),
                "permId": getattr(order, "permId", None),
                "clientId": getattr(order, "clientId", None),
                "parentId": getattr(order, "parentId", None),
                "action": getattr(order, "action", None),
                "orderType": getattr(order, "orderType", None),
                "totalQuantity": str(getattr(order, "totalQuantity", "")),
                "auxPrice": str(getattr(order, "auxPrice", "")),
                "lmtPrice": str(getattr(order, "lmtPrice", "")),
                "tif": getattr(order, "tif", None),
                "status": getattr(status, "status", None),
            }
        )
    return rows


def _verify_positions(rows: list[dict[str, Any]], failures: list[str]) -> None:
    g_rows = [row for row in rows if row.get("symbol") == "G"]
    if len(g_rows) != 1:
        failures.append(f"expected one G position, got {len(g_rows)}")
        return
    qty = _decimal(g_rows[0]["position"])
    avg_cost = _decimal(g_rows[0]["avgCost"])
    if qty != EXPECTED_G_QTY:
        failures.append(f"expected G qty {EXPECTED_G_QTY}, got {qty}")
    if abs(avg_cost - BASELINE_AVG_COST) > AVG_COST_TOLERANCE:
        failures.append(
            "expected G avgCost within "
            f"{AVG_COST_TOLERANCE} of {BASELINE_AVG_COST}, got {avg_cost}"
        )

    spy_rows = [row for row in rows if row.get("symbol") == "SPY"]
    if len(spy_rows) != 1:
        failures.append(f"expected one SPY position, got {len(spy_rows)}")
        return
    spy_qty = _decimal(spy_rows[0]["position"])
    spy_avg_cost = _decimal(spy_rows[0]["avgCost"])
    if spy_qty != EXPECTED_SPY_QTY:
        failures.append(f"expected SPY qty {EXPECTED_SPY_QTY}, got {spy_qty}")
    if spy_avg_cost != EXPECTED_SPY_AVG_COST:
        failures.append(
            f"expected SPY avgCost {EXPECTED_SPY_AVG_COST}, got {spy_avg_cost}"
        )


def _verify_g_stop(rows: list[dict[str, Any]], failures: list[str]) -> None:
    g_rows = [row for row in rows if row.get("symbol") == "G"]
    if len(g_rows) != 1:
        failures.append(f"expected exactly one G open order, got {len(g_rows)}")
        return

    order = g_rows[0]
    checks = {
        "permId": EXPECTED_G_STOP_PERM_ID,
        "action": "SELL",
        "orderType": "STP",
        "tif": "GTC",
    }
    for key, expected in checks.items():
        if order.get(key) != expected:
            failures.append(
                f"expected G stop {key}={expected!r}, got {order.get(key)!r}"
            )
    if _decimal(order.get("totalQuantity")) != EXPECTED_G_QTY:
        failures.append(
            f"expected G stop qty {EXPECTED_G_QTY}, got {order.get('totalQuantity')!r}"
        )
    if _decimal(order.get("auxPrice")) != EXPECTED_G_STOP_PRICE:
        failures.append(
            f"expected G stop price {EXPECTED_G_STOP_PRICE}, got {order.get('auxPrice')!r}"
        )
    if order.get("status") not in EXPECTED_STOP_STATUS:
        failures.append(
            "expected G stop status in "
            f"{sorted(EXPECTED_STOP_STATUS)}, got {order.get('status')!r}"
        )


def _verify_spy_stop(rows: list[dict[str, Any]], failures: list[str]) -> None:
    spy_rows = [row for row in rows if row.get("symbol") == "SPY"]
    if len(spy_rows) != 1:
        failures.append(f"expected exactly one SPY open order, got {len(spy_rows)}")
        return

    order = spy_rows[0]
    checks = {
        "permId": EXPECTED_SPY_STOP_PERM_ID,
        "action": "SELL",
        "orderType": "STP",
        "tif": "GTC",
    }
    for key, expected in checks.items():
        if order.get(key) != expected:
            failures.append(
                f"expected SPY stop {key}={expected!r}, got {order.get(key)!r}"
            )
    if _decimal(order.get("totalQuantity")) != EXPECTED_SPY_QTY:
        failures.append(
            "expected SPY stop qty "
            f"{EXPECTED_SPY_QTY}, got {order.get('totalQuantity')!r}"
        )
    if _decimal(order.get("auxPrice")) != EXPECTED_SPY_STOP_PRICE:
        failures.append(
            "expected SPY stop price "
            f"{EXPECTED_SPY_STOP_PRICE}, got {order.get('auxPrice')!r}"
        )
    if order.get("status") not in EXPECTED_STOP_STATUS:
        failures.append(
            "expected SPY stop status in "
            f"{sorted(EXPECTED_STOP_STATUS)}, got {order.get('status')!r}"
        )


def main() -> int:
    failures: list[str] = []
    active = _systemctl_value("is-active")
    enabled = _systemctl_value("is-enabled")
    if active != "inactive":
        failures.append(f"expected k2bi-engine.service inactive, got {active!r}")
    if enabled != "disabled":
        failures.append(f"expected k2bi-engine.service disabled, got {enabled!r}")
    killed_present = KILL_PATH.exists()
    if not killed_present:
        failures.append(f"expected kill sentinel present at {KILL_PATH}")

    positions: list[dict[str, Any]] = []
    open_orders: list[dict[str, Any]] = []
    broker_connected = False
    broker_query_succeeded = False
    ib = IB()
    try:
        ib.connect("127.0.0.1", 4002, clientId=99, timeout=10)
        broker_connected = True
        positions = _position_rows(ib)
        open_orders = _open_order_rows(ib)
        broker_query_succeeded = True
    except Exception as exc:  # noqa: BLE001 -- live broker gate fails closed
        failures.append(f"broker query failed: {type(exc).__name__}: {exc}")
    finally:
        if broker_connected:
            try:
                ib.disconnect()
            except Exception as exc:  # noqa: BLE001 -- preserve primary failure
                failures.append(
                    f"broker disconnect failed: {type(exc).__name__}: {exc}"
                )

    if broker_query_succeeded:
        _verify_positions(positions, failures)
        _verify_g_stop(open_orders, failures)
        _verify_spy_stop(open_orders, failures)

    result = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "clientId": 99,
        "baseline_avgCost": str(BASELINE_AVG_COST),
        "avgCost_tolerance": str(AVG_COST_TOLERANCE),
        "k2bi_engine_active": active,
        "k2bi_engine_enabled": enabled,
        "killed_present": killed_present,
        "g_positions": [
            row for row in positions if row.get("symbol") == "G"
        ],
        "g_open_orders": [
            row for row in open_orders if row.get("symbol") == "G"
        ],
        "spy_positions": [
            row for row in positions if row.get("symbol") == "SPY"
        ],
        "spy_open_orders": [
            row for row in open_orders if row.get("symbol") == "SPY"
        ],
        "all_open_orders": open_orders,
        "failures": failures,
        "passed": not failures,
    }
    print(json.dumps(result, sort_keys=True))
    return 0 if not failures else 7


if __name__ == "__main__":
    sys.exit(main())
