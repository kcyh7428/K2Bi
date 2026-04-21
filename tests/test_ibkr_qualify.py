"""Tests for Q35 qualifyContractsAsync before mark-fetch.

Q35 (architect scope 2026-04-21): Session G Run 2 logged
`WARNING:k2bi.connector.ibkr:mark fetch failed for SPY:
Contract Stock(symbol='SPY', exchange='SMART', currency='USD') can't
be hashed because no 'conId' value exists. Qualify contract to
populate 'conId'.` before the new-order submit. Engine fell back to
rule-derived LMT 715 (strategy-safe because the limit came from the
approved spec, not a live mark).

Fix: call `await ib.qualifyContractsAsync(contract)` before
reqTickersAsync so the contract has a conId populated. Q34's bounded
read wrappers still apply to both calls.
"""

from __future__ import annotations

import asyncio
import unittest
from decimal import Decimal
from typing import Any

from execution.connectors.ibkr import IBKRConnector


class _FakeContract:
    """Minimal Stock stand-in tracking whether qualify was called."""

    def __init__(
        self, symbol: str, exchange: str = "SMART", currency: str = "USD"
    ) -> None:
        self.symbol = symbol
        self.exchange = exchange
        self.currency = currency
        self.conId = 0  # unqualified default


class _FakeTickerRow:
    def __init__(self, price: float) -> None:
        self.marketPrice = price


class _RecordingIB:
    """Records qualifyContractsAsync calls and returns tickers with
    prices so the loop path in get_marks completes normally."""

    def __init__(self) -> None:
        self.qualify_calls: list[str] = []
        self.req_tickers_calls: list[str] = []
        self._qualified_conid: dict[str, int] = {"SPY": 12345}

    async def qualifyContractsAsync(self, *contracts: Any) -> list[Any]:
        out = []
        for c in contracts:
            self.qualify_calls.append(c.symbol)
            # ib_async mutates the contract in place with conId.
            c.conId = self._qualified_conid.get(c.symbol, 99999)
            out.append(c)
        return out

    async def reqTickersAsync(self, contract: Any, *a, **kw) -> list[Any]:
        self.req_tickers_calls.append(contract.symbol)
        return [_FakeTickerRow(500.0)]


class Q35QualifyContractsTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_marks_calls_qualify_before_req_tickers(self):
        """qualifyContractsAsync must fire BEFORE reqTickersAsync so
        the contract has conId when the ticker request lands. Without
        the fix, Session G's warning re-surfaces and the mark is lost
        (forcing fallback to rule-derived limit prices)."""
        conn = IBKRConnector(
            account_id=None, host="127.0.0.1", port=4002, client_id=1
        )
        conn._ib = _RecordingIB()
        conn._connected = True

        # Monkey-patch ib_async.Stock construction so we use our
        # recording contract instead of the real import.
        import execution.connectors.ibkr as ibkr_mod

        class _ShimIBAsync:
            Stock = _FakeContract

        orig_import = __import__

        def _fake_import(name, *a, **kw):
            if name == "ib_async":
                return _ShimIBAsync()
            return orig_import(name, *a, **kw)

        # Simpler: just poke ib_async into sys.modules.
        import sys

        orig_mod = sys.modules.get("ib_async")
        sys.modules["ib_async"] = _ShimIBAsync()  # type: ignore[assignment]
        try:
            result = await conn.get_marks(["SPY"])
        finally:
            if orig_mod is not None:
                sys.modules["ib_async"] = orig_mod
            else:
                sys.modules.pop("ib_async", None)

        self.assertEqual(result, {"SPY": Decimal("500.0")})
        # Qualify was called BEFORE the ticker request.
        self.assertEqual(conn._ib.qualify_calls, ["SPY"])
        self.assertEqual(conn._ib.req_tickers_calls, ["SPY"])

    async def test_get_marks_qualify_empty_response_skips_ticker(self):
        """MiniMax Q35 R1 finding #1 (2026-04-21): if
        qualifyContractsAsync returns an empty list without raising
        (timeout path, delisted symbol, unqualified response), conId
        stays 0 and proceeding to reqTickersAsync would reproduce the
        original Session G 'can't be hashed' warning. Skip the
        ticker."""

        class _EmptyQualifyIB(_RecordingIB):
            async def qualifyContractsAsync(self, *contracts):
                for c in contracts:
                    self.qualify_calls.append(c.symbol)
                    # Do NOT mutate conId -- simulate unqualified
                    # response.
                return []

        conn = IBKRConnector(
            account_id=None, host="127.0.0.1", port=4002, client_id=1
        )
        conn._ib = _EmptyQualifyIB()
        conn._connected = True

        import sys

        class _ShimIBAsync:
            Stock = _FakeContract

        orig_mod = sys.modules.get("ib_async")
        sys.modules["ib_async"] = _ShimIBAsync()  # type: ignore[assignment]
        try:
            result = await conn.get_marks(["SPY"])
        finally:
            if orig_mod is not None:
                sys.modules["ib_async"] = orig_mod
            else:
                sys.modules.pop("ib_async", None)

        # SPY absent + reqTickersAsync never called (qualify empty
        # short-circuited the ticker).
        self.assertEqual(result, {})
        self.assertEqual(conn._ib.qualify_calls, ["SPY"])
        self.assertEqual(conn._ib.req_tickers_calls, [])

    async def test_get_marks_qualify_zero_conid_success_skips_ticker(self):
        """MiniMax Q35 R1 finding #2 (2026-04-21) defensive guard:
        even if qualifyContractsAsync returns a non-empty list, a
        contract whose conId remains 0 after qualify must be treated
        as unqualified. The broker occasionally replies with a shell
        contract that lacks conId; proceeding would reproduce the
        hashing warning."""

        class _ZeroConIdQualifyIB(_RecordingIB):
            async def qualifyContractsAsync(self, *contracts):
                out = []
                for c in contracts:
                    self.qualify_calls.append(c.symbol)
                    # Leave conId=0 even though we return the contract.
                    out.append(c)
                return out

        conn = IBKRConnector(
            account_id=None, host="127.0.0.1", port=4002, client_id=1
        )
        conn._ib = _ZeroConIdQualifyIB()
        conn._connected = True

        import sys

        class _ShimIBAsync:
            Stock = _FakeContract

        orig_mod = sys.modules.get("ib_async")
        sys.modules["ib_async"] = _ShimIBAsync()  # type: ignore[assignment]
        try:
            result = await conn.get_marks(["SPY"])
        finally:
            if orig_mod is not None:
                sys.modules["ib_async"] = orig_mod
            else:
                sys.modules.pop("ib_async", None)

        self.assertEqual(result, {})
        self.assertEqual(conn._ib.req_tickers_calls, [])

    async def test_get_marks_qualify_failure_is_non_fatal(self):
        """Per architect scope Q35 is cosmetic/non-blocking: if qualify
        itself raises or times out, the mark is simply missing from
        the output and the caller falls back to avg_price -- the same
        behavior as the pre-Q35 warning path."""

        class _QualifyRaisingIB(_RecordingIB):
            async def qualifyContractsAsync(self, *contracts):
                raise RuntimeError("simulated broker unreachable")

        conn = IBKRConnector(
            account_id=None, host="127.0.0.1", port=4002, client_id=1
        )
        conn._ib = _QualifyRaisingIB()
        conn._connected = True

        import sys

        class _ShimIBAsync:
            Stock = _FakeContract

        orig_mod = sys.modules.get("ib_async")
        sys.modules["ib_async"] = _ShimIBAsync()  # type: ignore[assignment]
        try:
            result = await conn.get_marks(["SPY"])
        finally:
            if orig_mod is not None:
                sys.modules["ib_async"] = orig_mod
            else:
                sys.modules.pop("ib_async", None)

        # No exception propagated; SPY simply absent from result.
        self.assertEqual(result, {})
