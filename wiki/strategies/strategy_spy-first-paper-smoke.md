---
name: spy-first-paper-smoke
status: proposed
strategy_type: hand_crafted
risk_envelope_pct: 0.005
regime_filter: []
order:
  ticker: SPY
  side: buy
  qty: 2
  limit_price: 715.00
  stop_loss: 697.13
  time_in_force: DAY
tags: [strategy, k2bi, spy, paper]
date: 2026-04-20
type: strategy
origin: k2bi-generate
up: "[[index]]"
---

## How This Works

I am buying 2 shares of SPY at a limit of $715 today because this is the
first paper ticket in K2Bi and I need to test the full pipeline on a real
broker connection. There is no market signal behind this trade. SPY is the
ticker I chose because it is the most heavily traded fund in the world, so
the fill will be clean and any problem I see is almost certainly my code,
not the market.

The spec carries a static stop_loss of 697.13, which is 2.5% below the $715
limit. The engine's IBKR connector reads this field and submits the order
as a bracket: a parent BUY limit at 715.00 plus a linked GTC STOP child at
697.13, held on the broker side. If the parent fills, the protective stop
is already armed -- no manual step from me in IB Gateway. If the parent
does not fill by the close of today's session, the DAY time-in-force
cancels the parent and the child stop never activates. No overnight risk.

I do not expect this trade to make money. "Working" for this ticket means
the machinery ran end-to-end. A success is: the order reaches IBKR paper,
the fill comes back, the decision journal records the trade with the right
fields, a position note appears in wiki/positions/, the Telegram bot pings
me with the fill alert, and the broker shows a working protective stop at
697.13 on the filled position. Profit or loss on the trade itself is
noise. The point is proving the pipeline.

Four things could break this. One, the IBKR HK paper connection drops
during order placement -- that tells me my execution skill needs better
retry logic. Two, a validator in the execution layer rejects a field I
thought was valid -- the spec and the validator disagree and I need to fix
one of them. Three, the bracket gets split at the broker (parent fills but
the child stop is rejected or mis-linked) -- that tells me I need to audit
the IBKR connector's bracket-submission path. Four, the fill price drifts
far from 715.00 and the fixed 697.13 stop becomes loose or tight in an
unintended way -- that tells me future specs should compute stop_loss
dynamically from signal context (Phase 4+ rule_based strategy work)
rather than pinning it statically at draft time.

This is not a real rotational strategy. Rotational means moving money
between instruments, for example SPY, QQQ, IWM, based on which one is
strongest at a given time. That design is Phase 4+ work and needs its own
spec with real rules. The name of this ticket, spy-first-paper-smoke, is
deliberate. When I write my real rotational strategy later, it will be a
separate file with a proper thesis, a bear-case verdict, a backtest, and a
rule tree. This ticket is only a test that the plumbing works.

## Risk Envelope

- risk_envelope_pct: 0.005 is a documentation-only field in Phase 2. The loader parses and journals it, but no validator consumes it. Real per-trade risk enforcement lives in execution/validators/config.yaml under position_size.max_trade_risk_pct = 0.01 (1% of portfolio), which is the binding cap for any order this strategy submits. Wiring risk_envelope_pct into the validator as a per-strategy ceiling is Phase 4+ work.
- position_size_cap: 20% (validator-enforced in execution/validators/position_size.py).
- stop_loss: 697.13 (static Decimal, = limit_price * 0.975). Carried in the spec because execution/validators/trade_risk.py::check() rejects buy orders with order.stop_loss is None, and /invest-ship --approve-strategy requires the field at approval time. The engine's IBKR connector submits the order as a bracket with this stop as a linked GTC child held broker-side (execution/connectors/ibkr.py). Keith does NOT manually modify the stop post-fill: the engine's journal tracks stop_loss=697.13 and recovery logic assumes the broker-held bracket child IS the authoritative protective stop. Any manual cancel/replace in IB Gateway creates live broker state the engine cannot observe and breaks restart recovery.
- expected max holding days: 5 (DAY TIF cancels unfilled orders same-session; manual close by hand if the fill happens and is still open after 5 sessions).

## Open questions before approval

- SPY thesis does not yet exist at wiki/tickers/SPY.md. Phase 3.2 prerequisite: run /invest thesis SPY first. /invest-ship --approve-strategy refuses without it because the bear-case gate reads the thesis_score field.
- regime_filter: [] (empty list) is the explicit Phase 2 representation of "fires unconditionally regardless of market regime" -- the runner treats an empty filter as unconditional, whereas any non-empty list (including the string "none" parsed as a one-element regime name) would gate on a matching current_regime. Satisfies REQUIRED_STRATEGY_FIELDS at approval time. Real regime gating gets revisited when a real rotational strategy spec lands.
- Phase 2 MVP backtest runs a fixed SMA(20)/SMA(50) crossover against SPY over 2 years. That baseline has no relationship to a pipeline smoke test with no market signal. The backtest capture at raw/backtests/<date>_spy-first-paper-smoke_backtest.md is informational only; sanity-gate thresholds (500% return, -2% max DD, 85% win rate) apply to the SMA baseline, not to this strategy's non-existent rules.
