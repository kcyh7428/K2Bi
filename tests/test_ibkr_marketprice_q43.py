"""Tests for Q43 marketPrice-as-method bug in get_marks.

Q43 (2026-04-24): the first Mac Mini engine tick post-Q35-fix crashed
with `decimal.InvalidOperation` because ib_async 2.1.0 exposes
`Ticker.marketPrice` as a method, not an attribute. Pre-Q35 the
mark-fetch path failed earlier (no conId), masking this latent bug;
post-Q35 the Ticker lands and the attribute-style access returned a
bound method, which `Decimal(str(...))` could not convert.

Fix: detect callable marketPrice and invoke it. Keep the
attribute-passthrough for hypothetical future ib_async versions. Wrap
the Decimal conversion in a try/except for defense-in-depth.
"""

from __future__ import annotations

import logging
import unittest
from decimal import Decimal
from typing import Any

from execution.connectors.ibkr import IBKRConnector


class _FakeContract:
    """Minimal Stock stand-in with pre-populated conId so get_marks
    progresses past the Q35 qualify guard and exercises the Q43 path."""

    def __init__(
        self, symbol: str, exchange: str = "SMART", currency: str = "USD"
    ) -> None:
        self.symbol = symbol
        self.exchange = exchange
        self.currency = currency
        self.conId = 0


class _MethodTickerRow:
    """Simulates ib_async 2.1.0 Ticker where marketPrice is a METHOD."""

    def __init__(self, price: Any) -> None:
        self._price = price

    def marketPrice(self) -> Any:
        return self._price


class _RaisingMethodTickerRow:
    """Ticker whose marketPrice() call raises."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def marketPrice(self) -> Any:
        raise self._exc


class _AttributeTickerRow:
    """Ticker where marketPrice is a plain float attribute (back-compat
    with the existing Q35 test fixture and any future library shape
    that exposes the value directly)."""

    def __init__(self, price: float) -> None:
        self.marketPrice = price


class _IBShim:
    """Minimal ib_async shim: qualify populates conId, reqTickers
    returns the row provided by the test."""

    def __init__(self, row: Any) -> None:
        self._row = row
        self.qualify_calls: list[str] = []
        self.req_tickers_calls: list[str] = []

    async def qualifyContractsAsync(self, *contracts: Any) -> list[Any]:
        out = []
        for c in contracts:
            self.qualify_calls.append(c.symbol)
            c.conId = 12345
            out.append(c)
        return out

    async def reqTickersAsync(self, contract: Any, *a: Any, **kw: Any) -> list[Any]:
        self.req_tickers_calls.append(contract.symbol)
        return [self._row]


class _ShimIBAsync:
    Stock = _FakeContract


def _install_shim() -> Any:
    import sys

    orig = sys.modules.get("ib_async")
    sys.modules["ib_async"] = _ShimIBAsync()  # type: ignore[assignment]
    return orig


def _restore_shim(orig: Any) -> None:
    import sys

    if orig is not None:
        sys.modules["ib_async"] = orig
    else:
        sys.modules.pop("ib_async", None)


def _make_connected_connector(row: Any) -> IBKRConnector:
    conn = IBKRConnector(
        account_id=None, host="127.0.0.1", port=4002, client_id=1
    )
    conn._ib = _IBShim(row)
    conn._connected = True
    return conn


class Q43MarketPriceMethodTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_marks_calls_method_when_marketprice_is_method(self):
        """When marketPrice is a method (ib_async 2.1.0 shape), it must
        be invoked and the returned numeric stored as Decimal."""
        conn = _make_connected_connector(_MethodTickerRow(Decimal("708.81")))
        orig = _install_shim()
        try:
            result = await conn.get_marks(["SPY"])
        finally:
            _restore_shim(orig)
        self.assertEqual(result, {"SPY": Decimal("708.81")})

    async def test_get_marks_handles_method_returning_nan(self):
        """float('nan') from marketPrice() must be treated as missing
        (no mark entry, no crash). NaN-ness is detected via the
        idempotent x != x test since Decimal(str(nan)) would silently
        produce a Decimal NaN."""
        conn = _make_connected_connector(_MethodTickerRow(float("nan")))
        orig = _install_shim()
        try:
            result = await conn.get_marks(["SPY"])
        finally:
            _restore_shim(orig)
        self.assertEqual(result, {})

    async def test_get_marks_handles_method_raising(self):
        """If marketPrice() itself raises, swallow + warn + skip the
        ticker; do not propagate and crash the engine tick."""
        conn = _make_connected_connector(
            _RaisingMethodTickerRow(ValueError("simulated ib_async failure"))
        )
        orig = _install_shim()
        with self.assertLogs("k2bi.connector.ibkr", level=logging.WARNING) as cm:
            try:
                result = await conn.get_marks(["SPY"])
            finally:
                _restore_shim(orig)
        self.assertEqual(result, {})
        joined = "\n".join(cm.output)
        self.assertIn("marketPrice() call failed for SPY", joined)

    async def test_get_marks_handles_attribute_value_for_back_compat(self):
        """If a future or alternate ib_async exposes marketPrice as a
        plain float attribute, the connector must still work. This also
        covers the Q35 test fixture shape (AttributeTickerRow style)."""
        conn = _make_connected_connector(_AttributeTickerRow(123.45))
        orig = _install_shim()
        try:
            result = await conn.get_marks(["SPY"])
        finally:
            _restore_shim(orig)
        self.assertEqual(result, {"SPY": Decimal("123.45")})

    async def test_get_marks_decimal_conversion_failure_skipped(self):
        """Defense-in-depth: if marketPrice() returns a non-numeric
        string that slips past the None/NaN guards, the Decimal
        conversion must fail soft (warn + skip) rather than crash."""
        conn = _make_connected_connector(_MethodTickerRow("abc"))
        orig = _install_shim()
        with self.assertLogs("k2bi.connector.ibkr", level=logging.WARNING) as cm:
            try:
                result = await conn.get_marks(["SPY"])
            finally:
                _restore_shim(orig)
        self.assertEqual(result, {})
        joined = "\n".join(cm.output)
        self.assertIn("Decimal conversion failed for SPY", joined)


if __name__ == "__main__":
    unittest.main()
