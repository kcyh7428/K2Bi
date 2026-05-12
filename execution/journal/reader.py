"""Read-side helpers for journal lifecycle scans."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable


LOG = logging.getLogger("k2bi.journal.reader")


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
    for event in records:
        trade_id = event.get("trade_id")
        if not trade_id:
            continue
        if is_terminal_signal_event(event):
            out[str(trade_id)] = event
    return out
