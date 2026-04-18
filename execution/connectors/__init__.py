"""Broker connectors.

Phase 2 scope: ibkr.py only. IBKR DUQ demo paper account via ib_async
2.1.0 over TCP to localhost:4002 (IB Gateway 10.37 running on the
machine hosting the engine). Smoke test PASSED 2026-04-15.

Credentials: none in code. IB Gateway reads from its own config; the
connector only knows (host, port, clientId).

Other broker connectors (if ever) land here as peers. For K2Bi we are
single-broker stack (IBKR HK) per Q#26 -- Alpaca dropped.
"""
