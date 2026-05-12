"""Read-side helpers for journal lifecycle scans."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable


LOG = logging.getLogger("k2bi.journal.reader")


def _drop_matching_value(mapping: dict[str, str], key: Any, value: str) -> None:
    if key is not None and mapping.get(str(key)) == value:
        mapping.pop(str(key), None)


def _remaining_qty_is_zero(payload: dict[str, Any]) -> bool:
    raw = payload.get("remaining_qty")
    if raw is None:
        return False
    try:
        return Decimal(str(raw)) == Decimal("0")
    except (InvalidOperation, TypeError, ValueError):
        LOG.warning(
            "journal terminal scan: invalid order_filled remaining_qty=%r",
            raw,
        )
        return False


def is_terminal_signal_event(event: dict[str, Any]) -> bool:
    """Return True when an event closes the parent order lifecycle."""
    event_type = event.get("event_type")
    if event_type == "order_terminal":
        return True
    if event_type == "order_timeout":
        return True
    if event_type == "order_filled":
        payload = event.get("payload")
        return isinstance(payload, dict) and _remaining_qty_is_zero(payload)
    return False


def find_terminal_for_trade_id(
    trade_id: str,
    records: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the newest terminal signal for `trade_id`, if present."""
    for event in reversed(list(records)):
        if event.get("trade_id") != trade_id:
            continue
        if is_terminal_signal_event(event):
            return event
    return None


def terminal_signals_by_trade_id(
    records: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Index newest terminal signal by trade_id in one forward scan."""
    out: dict[str, dict[str, Any]] = {}
    trade_id_by_perm: dict[str, str] = {}
    trade_id_by_order: dict[str, str] = {}
    for event in records:
        event_type = event.get("event_type")
        trade_id = event.get("trade_id")
        if event_type == "order_submitted" and trade_id:
            broker_perm_id = event.get("broker_perm_id")
            broker_order_id = event.get("broker_order_id")
            if broker_perm_id:
                trade_id_by_perm[str(broker_perm_id)] = str(trade_id)
            if broker_order_id:
                trade_id_by_order[str(broker_order_id)] = str(trade_id)
        if not is_terminal_signal_event(event):
            continue
        indexed_trade_id = str(trade_id) if trade_id else None
        if indexed_trade_id is None:
            broker_perm_id = event.get("broker_perm_id")
            broker_order_id = event.get("broker_order_id")
            if broker_perm_id:
                indexed_trade_id = trade_id_by_perm.get(str(broker_perm_id))
            if indexed_trade_id is None and broker_order_id:
                indexed_trade_id = trade_id_by_order.get(str(broker_order_id))
        if indexed_trade_id is None:
            LOG.warning(
                "journal terminal scan: terminal %s missing trade_id and "
                "broker-id fallback",
                event.get("journal_entry_id"),
            )
            continue
        out[indexed_trade_id] = event
        _drop_matching_value(
            trade_id_by_perm,
            event.get("broker_perm_id"),
            indexed_trade_id,
        )
        _drop_matching_value(
            trade_id_by_order,
            event.get("broker_order_id"),
            indexed_trade_id,
        )
    return out
