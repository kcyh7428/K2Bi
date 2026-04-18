"""Pre-trade validators.

Top 5 for Phase 2 MVP:
    - position_size       (per-trade risk cap, per-ticker concentration)
    - trade_risk          (portfolio-level risk ceiling per order)
    - leverage            (no margin; cash-only for MVP)
    - market_hours        (reject outside US regular + extended windows)
    - instrument_whitelist  (only tickers listed in config.yaml)

Every validator takes a normalized Order + RiskContext and returns
ValidatorResult(approved: bool, rule: str, reason: str, detail: dict).

Additional validators (sector_concentration_cap, correlation_cap,
pre_trade_slippage_check, pdt_rule) land in Phase 4 when a specific
failure mode appears during Phase 3 burn-in.

Hard rule per risk-controls.md: NO override flag. The engine does NOT
accept a --force argument and will NOT call through if any validator
rejects.
"""
