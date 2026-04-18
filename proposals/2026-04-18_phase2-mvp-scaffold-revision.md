---
proposal-id: 2026-04-18_phase2-mvp-scaffold-revision
date: 2026-04-18
author: K2B architect session (Keith via K2B working dir)
status: pending-review
target-vault: K2Bi-Vault/wiki/planning/
affects: roadmap.md, milestones.md, nblm-mvp.md, index.md, plus new scaffold
---

# Proposal: Phase 2 MVP Scaffold Revision

Collapse Phase 2a + 2b + 3 (NBLM experiment, full data layer, strategy & backtest loop) into a single Phase 2 that scaffolds **all four architectural tiers at MVP level** so the first paper trade can flow through the full stack. Defer the NBLM experiment, walk-forward backtest harness, and Layer 4 risk audit to a new Phase 4 ("Discovery-driven hardening") that runs **after** the first paper trade reveals what actually breaks.

## Why

Keith's stated goal (2026-04-18 evening, K2B session): "set up the MVP so that we have all the components ready. Some of the things we can ship when we have the paper trade tested because we will only know what could break and what we really need."

The existing roadmap front-loaded a 4-week NBLM experiment + a full backtest harness + a full data layer **before** any order ticket existed. That sequence optimizes for theoretical correctness over discovery. Keith's reframe is: scaffold every tier minimally, send a real paper ticket, then harden based on observed failure modes — not predicted ones.

This proposal honors **all architectural non-negotiables** ([[architecture]], [[execution-model]], [[risk-controls]], [[agent-topology]]):

- 4-tier hedge fund role model — every tier exists at MVP
- Execution layer isolation — Python `execution/` engine, separate process from Claude
- Code-enforced validators with no override flag
- Strategy-level approval only (no per-trade)
- Decision journal append-only per trade
- Kill switch tested before first ticket
- NBLM remains MVP-gated pillar — never on Python execution critical path; if/when the experiment runs, BLOCKED-state contract is preserved
- Routines-Ready discipline for Analyst-tier skills (5 principles)

What changes is **sequencing**, not architecture.

## The 4 Confirmed Points (Keith sign-off 2026-04-18)

1. **Phase 2 = "MVP scaffold all tiers"** — validators + execute + alert + journal + thesis/bear/screen/regime/backtest MVPs + strategy approval flow
2. **NBLM experiment moves from Phase 2a to Phase 4** (post-paper-trade conditional)
3. **Walk-forward backtest harness moves from Phase 3 to Phase 4** (build when overfit pain is real)
4. **First strategy = SPY rotational proof-of-pipeline** (or another single-ticker thesis Keith wants to dogfood)

## Revised Phase Sequence (Summary Table)

| Phase | Old framing | New framing | Effort |
|---|---|---|---|
| 0 | K2B-side prereqs | unchanged (SHIPPED) | -- |
| 1 | Vault clone & scaffold | unchanged (SHIPPED at `4ea9b70`) | -- |
| 2 | 2a (NBLM 4 weeks) + 2b (full data layer) | **MVP scaffold all 4 tiers** | ~6-8 K2Bi sessions |
| 3 | Strategy & backtest loop | **First paper trade + 2-week pipeline burn-in** | ~2 sessions + 2 weeks runtime |
| 4 | Semi-auto paper execution | **Discovery-driven hardening** (NBLM IF /research thin, walk-forward IF overfit, Layer 4 IF news surprises, more feeds IF coverage gaps) | scope-emergent |
| 5 | 90-day paper eval | unchanged | 90 days |
| 6 | Live capital | unchanged (PARKED) | gated |

## Phase 2 Scope: MVP Component per Tier

| Tier | MVP Component (Phase 2 build) | Deferred to Phase 4 |
|---|---|---|
| **Research Library (NBLM)** | None — `/research` one-shot for theses | Per-ticker notebooks + catalog + side-by-side experiment + BLOCKED state (full nblm-mvp.md scope) |
| **Analysts (Mac Mini pm2)** | `invest-thesis` (one-shot Dave Ahern 4-phase), `invest-bear-case` (single Claude call against thesis, returns VETO/PROCEED), `invest-screen` (manual ticker input → wiki/watchlist/), `invest-regime` (manual classification → wiki/regimes/current.md), `invest-backtest` (sanity-check Python on yfinance, NOT walk-forward) | Walk-forward harness, `invest-earnings`, NBLM-grounded thesis/bear, regime auto-detection, embargoed k-fold |
| **Trader (Mac Mini permanent)** | `execution/validators/` (top 5: position_size, trade_risk, leverage, market_hours, instrument_whitelist), `execution/risk/` (3 breakers: daily soft -2%, hard -3%, total -10% writes `.killed`), `execution/connectors/ibkr.py` (`ib_async` thin wrapper), `execution/engine/main.py` (read strategy → validate → submit → journal), `execution/journal/` (append-only JSONL), `invest-execute` (Claude wrapper for manual run + state read), `invest-alert` (Telegram push), `invest-feed` MVP (1 RSS source, polling pattern proven) | Layer 4 Risk Audit (earnings calendar + regime mismatch deterministic check), more validators (sector_concentration_cap, correlation_cap, pre_trade_slippage_check, pdt_rule), more feeds, Risk Audit Agent (deterministic), sub-user with disabled withdrawals (Phase 6 prep) |
| **Portfolio Manager (MacBook + Keith)** | Strategy approval flow (extends `/invest-ship`: status: proposed → approved on `wiki/strategies/<name>.md` is the engine's gate), `invest-propose-limits` MVP (drafts validator config delta + writes review/strategy-approvals/), `invest-journal` P&L stub wired to IBKR fills | Auto P&L attribution, multi-strategy comparison views, post-mortem auto-generation |

## Phase 2 Vault Scaffolding (Folders to Create)

All folders get an `index.md` per K2Bi vault discipline. Empty folders are fine — structure exists so Phase 3 can populate immediately.

```
K2Bi-Vault/
  wiki/
    strategies/         (one .md per strategy; status frontmatter; gates execution)
    tickers/            (one .md per traded ticker; thesis frontmatter)
    positions/          (open positions only; archive on close)
    watchlist/          (manual list for MVP)
    regimes/            (current.md = manual classification for MVP)
  raw/
    journal/            (YYYY-MM-DD.jsonl per trading day, append-only)
    news/               (empty for MVP, structure ready)
    earnings/           (empty for MVP, structure ready)
  review/
    strategy-approvals/ (Keith's approval signal lands here pre-/ship)
    alerts/             (placeholder; alerts go to Telegram for MVP)
```

## Phase 2 Code Structure (K2Bi Repo)

```
K2Bi/
  .claude/skills/
    invest-thesis/          (NEW: one-shot /research wrapper, Dave Ahern 4-phase)
    invest-bear-case/       (NEW: single Claude call, VETO/PROCEED)
    invest-screen/          (NEW: manual ticker input → watchlist write)
    invest-regime/          (NEW: manual classification → regimes/current.md)
    invest-backtest/        (NEW: yfinance sanity check, NOT walk-forward)
    invest-execute/         (NEW: Claude wrapper for engine state)
    invest-alert/           (NEW: Telegram push)
    invest-feed/            (NEW: 1 RSS source MVP)
    invest-propose-limits/  (NEW: validator config delta drafter)
  execution/                (NEW: Python execution engine)
    validators/
      __init__.py
      position_size.py
      trade_risk.py
      leverage.py
      market_hours.py
      instrument_whitelist.py
      config.yaml           (file-based, restart to change)
    risk/
      __init__.py
      circuit_breakers.py   (3 layers: -2%, -3%, -10%)
      kill_switch.py        (.killed lock file mechanics)
    connectors/
      __init__.py
      ibkr.py               (thin ib_async wrapper, port 4002 paper)
    engine/
      __init__.py
      main.py               (read strategy → validate → submit → journal)
    journal/
      __init__.py
      writer.py             (append-only JSONL to raw/journal/)
  pm2/
    ecosystem.config.js     (NEW: pm2 config for invest-execute, invest-alert, invest-feed, invest-observer-loop)
```

All new skills carry tier assignment in their SKILL.md header (Analyst / Trader / Portfolio Manager) per the discipline established in [[skills-design]] line 100. Analyst-tier skills must satisfy the 5 Routines-Ready principles.

## Phase 2 Hard Rules (Code-Enforced Day One)

Per [[risk-controls]] non-negotiable layer:

1. Pre-trade validators reject orders with no override flag (top 5 in MVP, more added in Phase 4 as failure modes appear)
2. Engine refuses to execute if no approved strategy owns the order
3. `.killed` lock file blocks all new orders; only human deletes (engine never deletes)
4. Strategy `status:` transitions only via `/invest-ship` (pre-commit hook ported from K2B Phase 0 fix #8)
5. Decision journal append per trade attempt (success OR validator rejection)
6. Kill-switch dry-run test passes before first paper ticket (Phase 3 gate)

## Phase 3: First Paper Trade + Burn-In

Replaces old Phase 3 (strategy & backtest loop). Old Phase 3 work either ships in Phase 2 (basic backtest sanity check) or moves to Phase 4 (walk-forward harness).

**Goal:** prove the full stack end-to-end on the DUQ demo paper account.

| Milestone | Artifact | Verification |
|---|---|---|
| 3.1 | First strategy spec written + approved | `wiki/strategies/spy-weekly-rotation.md` (or chosen ticker) with `status: approved` via `/invest-ship` |
| 3.2 | Sanity-check backtest passes | Run `invest-backtest` on the strategy against 2 years yfinance SPY data; output Sharpe, max DD, win rate; metrics are sane (not 11000% P&L) |
| 3.3 | Kill-switch dry-run passes | `/invest kill` writes `.killed`, engine logs what it would have flattened, Telegram pings, manual delete confirms resumption |
| 3.4 | First paper ticket clears the stack | Engine reads strategy → all 5 validators pass → IBKR DUQ paper account receives order → fill returns → decision journal appended → Telegram pings within 60s |
| 3.5 | 2-week burn-in complete | Strategy runs on schedule for 10 trading days; observer logs every "what surprised me" signal; daily `invest-journal` populated; no validator override attempts |
| 3.6 | Burn-in retro committed | `wiki/insights/2026-XX_paper-trade-burnin-retro.md` documents what broke, what was missing, what was over-engineered |

The retro in 3.6 **is the input to Phase 4 prioritization**. We don't pre-decide Phase 4 scope; we let the retro drive it.

## Phase 4: Discovery-Driven Hardening

Replaces old Phase 4 (semi-auto paper execution, which is now in Phase 2). Scope is **emergent from the Phase 3 retro**, not pre-planned. Candidate items, in priority order based on architectural risk:

| Candidate | Trigger to build | Reference |
|---|---|---|
| **NBLM MVP experiment** (was Phase 2a) | If `/research` baseline thesis quality is too thin AND Keith manually reaches for grounding 3+ times during burn-in | [[nblm-mvp]] (full 5-criterion experiment design, re-tagged Phase 4) |
| **Walk-forward backtest harness** (was Phase 3.1) | If a second strategy is being added AND the sanity-check backtest can't validate it OR if Phase 3 strategy shows overfit signs (e.g., paper performance diverges from sanity-check expectation) | [[milestones]] old Phase 3.1-3.2 |
| **Layer 4 Risk Audit (deterministic)** | If burn-in surfaces a news-event surprise (earnings miss, FOMC, regime shift) that should have vetoed an order | [[risk-controls]] Layer 4 |
| **Additional validators** (sector_concentration_cap, correlation_cap, pre_trade_slippage_check, pdt_rule) | If a specific failure mode appears that the top 5 validators don't catch | [[risk-controls]] Layer 1 table |
| **Additional feed sources** (earnings calendar, SEC filings, macro releases) | If watchlist coverage gap surfaces (e.g., missed earnings drop on a position) | [[skills-design]] invest-feed row |
| **`invest-earnings` workflow** | If first earnings event during burn-in reveals manual synthesis is too slow | [[skills-design]] invest-earnings row |
| **Risk Audit Agent (sub-process)** | If Layer 4 deterministic check is insufficient AND a validated approach exists | [[risk-controls]] Layer 4 |
| **Sub-user with disabled withdrawals on IBKR** | Before live capital (Phase 6 prep, not Phase 4 unless Keith funds early) | [[keith-checklist]] Section 1c |
| **Multi-source feed pattern** (RSS/REST/webhooks) | If 1-source MVP feed proves the pattern but coverage demands more | [[roadmap]] Phase 0 Tier 2 |

Phase 4 exits when the burn-in retro punch list is closed AND a second strategy has been approved + executed cleanly + journaled.

## Phase 5 + 6: Unchanged

Phase 5 (90-day paper eval) and Phase 6 (live capital, PARKED) keep their existing milestone structure. The 90-day clock starts when Keith funds HK$10K and the paired paper account activates with real-time data — currently still deferred until Phase 4 hardening proves stable.

## On IBKR Funding (Reaffirmed)

Per [[keith-checklist]] Section 1b: HK$10K wire stays deferred until Phase 4 is ~80% built. The DUQ demo paper account (HK$1M simulated, smoke test PASSED 2026-04-15) covers all of Phase 2 + Phase 3 + most of Phase 4. Funding triggers KYC + W-8BEN + tax paperwork with zero development benefit while burn-in runs on demo. The 1-3 day onboarding window then completes in parallel with Phase 4 final polish — zero wasted calendar days.

## Concrete File Changes (K2Bi Session Applies on Merge)

### File 1: `K2Bi-Vault/wiki/planning/roadmap.md`

**Change 1 of 3 in roadmap.md** — replace the Phase Lanes table rows for Phase 2a, 2b, 3, 4:

OLD:
```
| Phase 2a -- NBLM MVP experiment (5-ticker side-by-side, 4-week dogfooding) | ideating | backlog | 0 of 5 MVP exit criteria met ([[milestones#Phase 2a -- NBLM MVP Experiment]]) | All 5 criteria from [[nblm-mvp]] pass | K2Bi |
| Phase 2b -- Full data layer (conditional on 2a) | ideating | backlog | 0 of 7 Phase 2b milestones ([[milestones#Phase 2b -- Data Layer]]) | 1 full market session ingested + 10+ watchlist tickers + 3 theses (NBLM or one-shot depending on 2a outcome) | K2Bi |
| Phase 3 -- Strategy & backtest loop | ideating | backlog | 0 of 6 milestones ([[milestones#Phase 3]]) | 3 strategies backtested with walk-forward + no look-ahead bias, bear-case tested | K2Bi |
| Phase 4 -- Semi-auto paper execution | ideating | backlog | 0 of 10 milestones ([[milestones#Phase 4]]) | Paper trades executing, risk validators + kill-switch tested | K2Bi |
```

NEW:
```
| Phase 2 -- MVP scaffold all 4 tiers (validators + execute + alert + journal + thesis/bear/screen/regime/backtest MVPs + strategy approval flow) | ideating | backlog | 0 of N milestones ([[milestones#Phase 2 -- MVP Scaffold All Tiers]]) | All architectural tiers present at MVP, kill-switch dry-run passes, decision journal infrastructure live | K2Bi |
| Phase 3 -- First paper trade + 2-week burn-in (proof-of-pipeline strategy on DUQ demo) | ideating | backlog | 0 of 6 milestones ([[milestones#Phase 3 -- First Paper Trade]]) | First ticket clears full stack, 10 trading days completed, burn-in retro committed | K2Bi |
| Phase 4 -- Discovery-driven hardening (NBLM IF /research thin, walk-forward IF overfit, Layer 4 IF news surprises, more feeds IF coverage gaps -- scope emergent from Phase 3 retro) | ideating | backlog | scope determined by Phase 3 retro | Burn-in punch list closed + second strategy approved + executed cleanly | K2Bi |
```

**Change 2 of 3 in roadmap.md** — replace the entire body sections "## Phase 2a -- NBLM MVP Experiment" and "## Phase 2b -- Full Data Layer (conditional on 2a passing or pivoting)" with a single new section:

```markdown
## Phase 2 -- MVP Scaffold All 4 Tiers

**Goal:** wireframe every architectural tier at MVP level so the first paper trade can flow through the full stack. Defer all enhancements (NBLM experiment, walk-forward harness, Layer 4 audit, additional validators) to Phase 4 where scope is driven by observed failure modes from Phase 3 burn-in.

Per the 4-tier hedge fund role model from [[architecture]], every tier gets ONE thing built minimally. The architecture is honored at every step:

- Execution layer isolation -- Python `execution/` engine, separate process from Claude
- Code-enforced validators with no override flag
- Strategy-level approval only (no per-trade override -- the 68% loss research finding stands)
- Decision journal append-only per trade attempt
- NBLM never on Python execution critical path (if/when added in Phase 4, BLOCKED-state contract preserved)
- Routines-Ready discipline for Analyst-tier skills (5 principles)

### MVP Components per Tier

**Research Library:** none -- `/research` one-shot for theses. NBLM remains MVP-gated pillar; experiment runs in Phase 4 only if `/research` baseline proves insufficient during burn-in.

**Analysts (Mac Mini pm2):**
- `invest-thesis` -- one-shot Dave Ahern 4-phase, output to `wiki/tickers/<SYM>.md` frontmatter
- `invest-bear-case` -- single Claude Code call against thesis, returns VETO (>70% conviction) or PROCEED (with top 3 counter-points)
- `invest-screen` -- manual ticker input writes to `wiki/watchlist/`. Real screening builds in Phase 4 if needed
- `invest-regime` -- manual classification writes to `wiki/regimes/current.md`. Auto-detection builds in Phase 4 if needed
- `invest-backtest` -- yfinance sanity-check (NOT walk-forward). Walk-forward harness builds in Phase 4 if overfit pain appears

All Analyst-tier skills satisfy the 5 Routines-Ready principles per [[skills-design]] discipline.

**Trader (Mac Mini permanent):**
- `execution/validators/` -- top 5 (position_size, trade_risk, leverage, market_hours, instrument_whitelist) with `config.yaml`
- `execution/risk/circuit_breakers.py` -- 3 layers (daily soft -2%, hard -3%, total -10% writes `.killed`)
- `execution/risk/kill_switch.py` -- `.killed` lock file (engine respects, only human deletes)
- `execution/connectors/ibkr.py` -- thin `ib_async` wrapper, port 4002 paper
- `execution/engine/main.py` -- main loop: read strategy → validate → submit → journal
- `execution/journal/writer.py` -- append-only JSONL to `raw/journal/<date>.jsonl`
- `invest-execute` -- Claude wrapper for manual run + engine state read
- `invest-alert` -- Telegram push (sub-1-hour delivery, real-time)
- `invest-feed` MVP -- 1 RSS source, polling pattern proven

**Portfolio Manager (MacBook + Keith):**
- Strategy approval flow -- `/invest-ship` extension: `status: proposed → approved` on `wiki/strategies/<name>.md` is the engine's gate
- `invest-propose-limits` MVP -- drafts validator config delta + writes review/strategy-approvals/
- `invest-journal` P&L stub -- wired to IBKR fills

### Vault scaffolding (folders + index.md per K2Bi vault discipline)

`wiki/strategies/`, `wiki/tickers/`, `wiki/positions/`, `wiki/watchlist/`, `wiki/regimes/`, `raw/journal/`, `raw/news/`, `raw/earnings/`, `review/strategy-approvals/`, `review/alerts/`.

### Code scaffolding (K2Bi repo)

`execution/{validators,risk,connectors,engine,journal}/` Python module skeleton + `pm2/ecosystem.config.js` for the Mac Mini daemons.

### Hard rules code-enforced day one

1. Pre-trade validators reject orders with no override flag (top 5 in MVP)
2. Engine refuses to execute if no approved strategy owns the order
3. `.killed` lock file blocks all new orders; only human deletes
4. Strategy `status:` transitions only via `/invest-ship` (pre-commit hook ported from K2B Phase 0 fix #8)
5. Decision journal append per trade attempt (success OR validator rejection)

### Exit criteria (Phase 2)

See [[milestones#Phase 2 -- MVP Scaffold All Tiers]] for the per-milestone artifact + verification table.

Per architectural discipline, kill-switch dry-run test in Phase 3.3 must pass before any first paper ticket.
```

**Change 3 of 3 in roadmap.md** — replace the entire body sections "## Phase 3 -- Strategy & Backtest Loop + Bear-Case Pattern" and "## Phase 4 -- Semi-Auto Paper Execution" with two new sections:

```markdown
## Phase 3 -- First Paper Trade + 2-Week Burn-In

**Goal:** prove the full stack end-to-end on the DUQ demo paper account. One simple strategy. End-to-end ticket flow. 2 weeks of observed behavior. Burn-in retro is the input to Phase 4 prioritization.

- First strategy: SPY weekly rotation OR another single-ticker thesis Keith wants to dogfood. The simplest possible thing that touches every layer.
- Sanity-check backtest only -- 2 years yfinance data, basic Sharpe / DD / win rate. No walk-forward in this phase.
- Kill-switch dry-run MUST pass before first ticket. Untested kill switch is a broken kill switch.
- 10 trading days of observed behavior. Observer loop logs every "what surprised me" signal.
- Burn-in retro at 3.6 documents what broke, what was missing, what was over-engineered. This document drives Phase 4 scope.

Exit criteria: see [[milestones#Phase 3 -- First Paper Trade]].

## Phase 4 -- Discovery-Driven Hardening

**Goal:** harden the stack based on observed failure modes from Phase 3 burn-in. Scope is emergent, not pre-planned.

Candidate items in priority order based on architectural risk -- but only built if the Phase 3 retro flagged them:

- **NBLM MVP experiment** (full design preserved in [[nblm-mvp]], re-tagged Phase 4) -- triggers if `/research` baseline thesis quality is too thin AND Keith reaches for grounding manually 3+ times during burn-in
- **Walk-forward backtest harness** (rolling windows + embargoed k-fold + look-ahead bias detection) -- triggers if a second strategy is being added AND sanity-check backtest can't validate it OR Phase 3 strategy shows overfit signs
- **Layer 4 Risk Audit (deterministic)** -- triggers if burn-in surfaces a news-event surprise that should have vetoed an order
- **Additional validators** (sector_concentration_cap, correlation_cap, pre_trade_slippage_check, pdt_rule) -- triggers per specific failure mode
- **Additional feed sources** (earnings calendar, SEC filings, macro releases) -- triggers if watchlist coverage gap surfaces
- **`invest-earnings` workflow** -- triggers if first earnings event reveals manual synthesis is too slow
- **Risk Audit Agent sub-process** -- triggers only if Layer 4 deterministic check is insufficient
- **Multi-source feed pattern** -- triggers if 1-source MVP proves pattern but coverage demands more

Phase 4 exits when burn-in retro punch list is closed AND a second strategy has been approved + executed cleanly + journaled.
```

### File 2: `K2Bi-Vault/wiki/planning/milestones.md`

Replace the entire `## Phase 2a` and `## Phase 2b` sections with:

```markdown
## Phase 2 -- MVP Scaffold All Tiers

| Milestone | Artifact | Verification |
|---|---|---|
| 2.1 | Vault folders created (strategies, tickers, positions, watchlist, regimes, raw/journal, raw/news, raw/earnings, review/strategy-approvals, review/alerts) with index.md per K2Bi discipline | `ls -d` returns each folder; each `index.md` has frontmatter + `up:` |
| 2.2 | execution/ Python scaffolding (validators, risk, connectors, engine, journal subdirs with __init__.py) | `tree execution/` matches spec; `python -c "import execution"` succeeds |
| 2.3 | Top 5 pre-trade validators implemented + unit tests | Each validator rejects a violating order with named reason; passing order returns approved |
| 2.4 | 3-layer circuit breakers + .killed lock file | Simulated drawdown triggers each breaker correctly; `.killed` written at -10%; engine refuses orders while file exists |
| 2.5 | IBKR connector (`execution/connectors/ibkr.py`) wraps ib_async cleanly | Connection to DUQ demo paper account succeeds; account summary returns expected HK$1M cash |
| 2.6 | Execution engine main loop reads approved strategy + submits + journals | Dry-run with mock strategy YAML produces validated order + journal entry |
| 2.7 | Decision journal append-only writer | Concurrent writes don't corrupt; restart preserves prior entries; JSONL format valid |
| 2.8 | invest-execute skill (Claude wrapper) | Reads engine state from vault; surfaces .killed status; surfaces last journal entries |
| 2.9 | invest-alert skill + Telegram bot integration | Test event → Telegram message within 60s |
| 2.10 | invest-feed MVP (1 RSS source) | Cron tick polls source; new items land in raw/news/ |
| 2.11 | invest-thesis skill (one-shot Dave Ahern 4-phase via /research) | Run on a chosen ticker → wiki/tickers/<SYM>.md frontmatter populated with thesis block |
| 2.12 | invest-bear-case skill (single Claude call) | Returns VETO or PROCEED structured output against a thesis |
| 2.13 | invest-screen skill (manual input MVP) | Adds ticker to wiki/watchlist/ with rubric frontmatter |
| 2.14 | invest-regime skill (manual classification MVP) | Updates wiki/regimes/current.md atomically |
| 2.15 | invest-backtest skill (yfinance sanity-check) | Reads strategy YAML, runs against 2y data, outputs Sharpe + DD + win rate |
| 2.16 | invest-propose-limits skill MVP | Drafts validator config delta + writes review/strategy-approvals/ |
| 2.17 | Strategy approval flow extends /invest-ship | `status: proposed → approved` on wiki/strategies/<name>.md is engine's gate; pre-commit hook blocks manual status edits outside /invest-ship |
| 2.18 | invest-journal P&L stub wired to IBKR fills | Daily journal pulls fills from IBKR + writes P&L block |
| 2.19 | pm2 config for Mac Mini daemons (invest-execute, invest-alert, invest-feed, invest-observer-loop) | `pm2 list` on Mini shows all 4 processes online after deploy |
| 2.20 | All 9 new skills carry tier assignment in SKILL.md frontmatter (Analyst / Trader / Portfolio Manager) | grep returns tier line in each new SKILL.md |
| 2.21 | All Analyst-tier skills satisfy 5 Routines-Ready principles | Per-skill audit table committed to skills-design.md |
| 2.22 | Codex adversarial review on the full Phase 2 build | Findings addressed inline before /invest-ship |
```

Replace the entire `## Phase 3` section with:

```markdown
## Phase 3 -- First Paper Trade + 2-Week Burn-In

| Milestone | Artifact | Verification |
|---|---|---|
| 3.1 | First strategy spec written + approved | wiki/strategies/<name>.md with `status: approved` via /invest-ship; rules + entry/exit/sizing/stop-loss/risk envelope/regime filter all populated |
| 3.2 | Sanity-check backtest passes | invest-backtest run on the strategy returns sane metrics (no 11000% P&L cheats); committed to wiki/strategies/<name>.md backtest frontmatter |
| 3.3 | Kill-switch dry-run passes | /invest kill writes .killed; engine logs would-flatten list; Telegram pings; manual delete confirms resumption; entry in raw/journal/ |
| 3.4 | First paper ticket clears full stack | Engine reads strategy → all 5 validators pass → IBKR DUQ paper receives order → fill returns → decision journal entry → Telegram ping within 60s |
| 3.5 | 2-week burn-in complete | Strategy runs on schedule for 10 trading days; observer logs every "what surprised me" signal to wiki/context/preference-signals.jsonl; daily invest-journal populated; zero validator override attempts |
| 3.6 | Burn-in retro committed | wiki/insights/2026-XX_paper-trade-burnin-retro.md documents: what broke, what was missing, what was over-engineered, recommended Phase 4 scope |
```

Replace the entire `## Phase 4` section with:

```markdown
## Phase 4 -- Discovery-Driven Hardening

Scope emergent from Phase 3 burn-in retro. Each candidate has a trigger condition; build only if triggered.

| Candidate | Trigger | Reference |
|---|---|---|
| NBLM MVP experiment (5-criterion exit gate) | /research baseline thesis quality too thin AND Keith manually reaches for grounding 3+ times during burn-in | [[nblm-mvp]] (re-tagged Phase 4) |
| Walk-forward backtest harness | Second strategy added AND sanity-check insufficient OR Phase 3 strategy shows overfit signs | Old Phase 3.1-3.2 design preserved |
| Layer 4 Risk Audit (deterministic) | Burn-in surfaces news-event surprise that should have vetoed | [[risk-controls]] Layer 4 |
| Additional validators (sector, correlation, slippage, pdt) | Specific failure mode appears | [[risk-controls]] Layer 1 |
| Additional feed sources | Watchlist coverage gap surfaces | [[skills-design]] invest-feed |
| invest-earnings workflow | First earnings event reveals manual synthesis too slow | [[skills-design]] invest-earnings |
| Risk Audit Agent sub-process | Layer 4 deterministic check insufficient | [[risk-controls]] Layer 4 |
| Sub-user with disabled withdrawals on IBKR | Phase 6 prep, only if Keith funds early | [[keith-checklist]] Section 1c |

Phase 4 exits when: burn-in retro punch list closed AND second strategy approved + executed cleanly + journaled.
```

### File 3: `K2Bi-Vault/wiki/planning/nblm-mvp.md`

**Update 1 of 2 in nblm-mvp.md** — replace the line `**Phase placement:** Phase 2a (first unit of work in K2Bi Phase 2). Gate before Phase 2b (full data layer) begins. See [[roadmap#Phase 2a -- NBLM MVP Experiment]].`

WITH:

```markdown
**Phase placement:** Phase 4 conditional experiment (re-tagged from Phase 2a in 2026-04-18 architectural revision -- see proposals/2026-04-18_phase2-mvp-scaffold-revision.md). Triggers only if Phase 3 burn-in retro shows `/research` baseline thesis quality is too thin AND Keith manually reached for grounding 3+ times. NBLM remains MVP-gated pillar per [[architecture]]; the gate now runs after the first paper trade reveals whether one-shot `/research` is sufficient. See [[roadmap#Phase 4 -- Discovery-Driven Hardening]].
```

**Update 2 of 2 in nblm-mvp.md** — add new section at the very top, immediately after the frontmatter and before `# NBLM MVP: Minimal Integration for K2Bi`:

```markdown
> **Re-tagged 2026-04-18:** This experiment moved from Phase 2a (pre-paper-trade) to Phase 4 (post-paper-trade conditional). Rationale: NBLM is a research aid, not an execution dependency. The architecture explicitly designates NBLM as "MVP-gated pillar" -- meaning it stays a pillar only if the experiment validates it. Running the experiment AFTER the first paper trade lets us measure NBLM against real burn-in pain instead of theoretical advantage. The 5 exit criteria below are unchanged; the trigger condition is new (see Phase placement line below). All execution-layer isolation rules and BLOCKED-state contracts are unchanged.

```

### File 4: `K2Bi-Vault/wiki/planning/index.md`

Update the Resume Card section. Specifically:

**Replace** the line starting `Last updated: 2026-04-18 (Phase 1 SHIPPED at commit \`4ea9b70\`...`

WITH:

```markdown
Last updated: 2026-04-18 evening (Phase 1 SHIPPED at `4ea9b70`; Phase 2 plan revised via proposals/2026-04-18_phase2-mvp-scaffold-revision.md -- collapse 2a/2b/3 into "MVP scaffold all tiers", defer NBLM + walk-forward to Phase 4) | Entries: 17
```

**Replace** the entire `**Next concrete action:**` paragraph

WITH:

```markdown
**Next concrete action:** **Phase 2 kickoff -- MVP scaffold all 4 tiers.** Build ONE component per architectural tier, minimum-viable, so the first paper trade can flow through the full stack. See [[roadmap#Phase 2 -- MVP Scaffold All 4 Tiers]] for the component list and [[milestones#Phase 2 -- MVP Scaffold All Tiers]] for the 22-milestone exit criteria. Phase 2a (NBLM experiment) and walk-forward backtest harness moved to Phase 4 (post-paper-trade conditional). First strategy in Phase 3 = SPY rotational proof-of-pipeline (or another single-ticker thesis Keith wants to dogfood). DUQ demo paper account covers all of Phase 2 + 3 + most of 4 -- HK$10K wire stays deferred per [[keith-checklist]] Section 1b.
```

**Remove** the entire `**Blockers / decisions waiting on Keith:**` section's Phase 2a prereqs sub-block (the two bullets about "Accuracy-delta eval log mechanism" and "Revealed-preference observer signal"). They re-emerge if Phase 4 triggers the NBLM experiment, but they are no longer pre-Phase-2 blockers.

**Add** to the `**Last session summary**` chain a new entry:

```markdown
**Last session summary (2026-04-18 evening, Phase 2 architectural revision):** K2B architect session reviewed the full roadmap under Keith's "MVP scaffold all components ready, paper-trade ASAP, harden by discovery" reframe. Confirmed 4 points: (1) Phase 2 = MVP scaffold all 4 tiers, (2) NBLM experiment moves to Phase 4 conditional, (3) walk-forward harness moves to Phase 4, (4) first strategy = SPY rotational proof-of-pipeline. Authored proposals/2026-04-18_phase2-mvp-scaffold-revision.md as PR to K2Bi repo for review + merge. K2Bi session previously shipped bootstrap commits 597e052 + 8d6d3d8 (rename + bootstrap helpers, 14 invest-* skills, 3 new). PR scope: roadmap.md (replace Phase 2a/2b/3/4 sections), milestones.md (replace Phase 2/3/4 milestone tables), nblm-mvp.md (re-tag Phase 4 conditional), index.md (this Resume Card update). On merge, K2Bi session applies the 4 file changes via vault edits + creates Phase 2 vault scaffolding folders + execution/ Python module skeleton + 9 new skill SKILL.md stubs. Phase 2 kickoff begins in next K2Bi session after merge.
```

## Acceptance Instructions (K2Bi Session)

On merge of this PR:

1. **Apply the 4 file changes above** to `K2Bi-Vault/wiki/planning/` -- roadmap.md, milestones.md, nblm-mvp.md, index.md. Use the OLD/NEW blocks above as the diff specification. These edit the vault, not git -- they propagate to Mac Mini via Syncthing.
2. **Create Phase 2 vault folders** with placeholder index.md per K2Bi vault discipline: `wiki/strategies/`, `wiki/tickers/`, `wiki/positions/`, `wiki/watchlist/`, `wiki/regimes/`, `raw/journal/`, `raw/news/`, `raw/earnings/`, `review/strategy-approvals/`, `review/alerts/`.
3. **Create K2Bi code repo scaffolding:**
   - `execution/{validators,risk,connectors,engine,journal}/__init__.py`
   - `execution/validators/config.yaml` (placeholder with the 5 validator config blocks)
   - `pm2/ecosystem.config.js` (placeholder with stub entries for invest-execute, invest-alert, invest-feed, invest-observer-loop)
4. **Create 9 new skill stub directories** in `K2Bi/.claude/skills/`: invest-thesis, invest-bear-case, invest-screen, invest-regime, invest-backtest, invest-execute, invest-alert, invest-feed, invest-propose-limits. Each gets a SKILL.md with frontmatter (tier assignment per [[skills-design]]) + the spec from this proposal's "MVP Components per Tier" section. Implementation is Phase 2 work, not part of the merge.
5. **Update `wiki/concepts/index.md`** if K2Bi has lane structure: add a `feature_phase-2-mvp-scaffold` entry with `status: in-progress` (this is now THE active feature). If no concepts/ lane structure yet, create one.
6. **Run `/invest-ship`** to land the merge with Codex review on the proposal application. Use commit message: `chore: apply Phase 2 MVP scaffold revision (proposal 2026-04-18)`.
7. **Update `wiki/log.md`** via single-writer helper with the architectural revision event.

After merge + apply, the K2Bi session is unblocked for Phase 2 build sessions.

## Open Questions for K2Bi Session

If the K2Bi session disagrees with any part of this proposal, push back via the PR review comments rather than merging. Specifically flag:

- Any milestone in Phase 2 that's actually Phase 4 territory (over-scoped MVP)
- Any milestone in Phase 4 that's actually Phase 2 territory (under-scoped MVP, would block paper trade)
- Any tier assignment in the new skills that violates the 5 Routines-Ready principles
- Any architectural rule from [[architecture]] / [[execution-model]] / [[risk-controls]] / [[agent-topology]] that this proposal contradicts

K2B architect session is ready to revise on push-back.

## Related

- [[architecture]] -- 4-tier hedge fund role model that this proposal honors
- [[execution-model]] -- strategy approval flow that Phase 2 builds
- [[risk-controls]] -- validator + breaker + .killed layer that Phase 2 builds
- [[agent-topology]] -- monolithic + bear-case decision that Phase 2 implements
- [[nblm-mvp]] -- NBLM experiment design (re-tagged Phase 4 by this proposal)
- [[keith-checklist]] -- IBKR funding deferral (reaffirmed)
- [[skills-design]] -- tier assignment + Routines-Ready discipline applied to all new skills
