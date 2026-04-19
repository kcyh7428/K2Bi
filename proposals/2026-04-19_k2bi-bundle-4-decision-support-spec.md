---
tags: [k2bi, phase-2, bundle-4, architect-spec, decision-support]
date: 2026-04-19
type: architect-spec
origin: k2b-architect
up: "[[Home]]"
for-repo: K2Bi
target-bundle: phase-2-bundle-4
milestones: [m2.11, m2.12, m2.13, m2.14, m2.15]
plan-review-required: true
---

# K2Bi Phase 2 Bundle 4 -- Decision Support (m2.11-m2.15) -- Architect Spec

**Goal:** Five Analyst-tier decision-support skills land as parallel-shippable cycles. Each writes a structured signal to vault that downstream consumers (engine approval flow, invest-alert in Bundle 5, Keith's eyeball) can read. Bundle 4 closes when all 5 are shipped + cumulative cross-vendor MiniMax sweep is clean.

**Architecture:** Five independent Analyst-tier skills, each Routines-Ready (stateless + vault-in/vault-out + schedulable + JSON I/O + self-contained prompts). Each emits a numeric scorecard, an Action Plan / signal block, and writes to a deterministic vault location. invest-bear-case is the only intra-bundle dependency (requires invest-thesis output present).

**Tech stack:** Python where compute matters (invest-backtest needs yfinance + pandas + numpy); bash + shared frontmatter helper from Bundle 3 cycle 4 where vault-write matters; Claude Code direct calls for invest-thesis + invest-bear-case + invest-screen + invest-regime (LLM reasoning skills).

**Bundle estimation:** 5 skills, 4 truly parallel + 1 sequential (bear-case after thesis). No external integrations beyond yfinance (invest-backtest). Per Bundle 3 retro: lock data shapes upfront avoids multi-round implementer convergence. Expected: 1-3 review rounds per skill (MiniMax-primary), ~5-10 Codex final-gate calls across the bundle + 1 cumulative-bundle sweep.

---

## 0. Prerequisites (must hold before Bundle 4 cycle 1)

### 0.1 Bundle 3 closure verified

Bundle 3 closed at `9f14dca`. 10 of 22 Phase 2 milestones shipped. Cycle 7 e2e tests confirmed m2.16 + m2.17 verified. Test suite at 618 passing. Mac Mini synced.

Bundle 4 inherits these Bundle 3 surfaces (do NOT reinvent):
- `scripts/lib/strategy_frontmatter.py` -- canonical YAML frontmatter parser + serializer with NFC normalization. Bundle 4 vault writes MUST use this. Do not author parallel YAML helpers.
- `scripts/lib/deploy_config.py` -- structured deploy-config.yml reader. Bundle 4 doesn't add new categories; existing `skills` + `scripts` + `execution` cover all 5 skills.
- `.githooks/pre-commit` Checks A-D + commit-msg + post-commit + sentinel scheme `.retired-<sha256(filename_stem)[:16]>.json`. Bundle 4 skills DO NOT touch strategy files (except invest-backtest -- see Q1) so Check D content-immutability is mostly out-of-scope for the bundle.
- `K2Bi-Vault/wiki/insights/insight_bundle-3-retro.md` -- the carry-forward learnings. Read before drafting any cycle.

### 0.2 Bundle 4 prior-art mandatory pre-read

`K2Bi-Vault/wiki/planning/trade-skills-reference-pass.md` is BLOCKING-read for any session drafting m2.11 (invest-thesis) or m2.13 (invest-screen). It contains:
- Adopt/adapt/skip verdicts against the captured `/trade` skill pack at `~/Desktop/trading-skills/`.
- 9 K2Bi architecture conflicts that pre-emptively reject patterns (e.g. flat-file output, embedded position sizing, 5-agent orchestration, WebSearch-as-sole-source).
- Per-skill author checklists for invest-thesis + invest-screen.
- Cross-cutting patterns (numeric scorecards, Action Plan blocks, edge-case handling, JSON contracts) that ALL Bundle 4 skills inherit.

Sessions drafting m2.12, m2.14, m2.15 should still read it for the cross-cutting patterns and the architecture-conflicts list.

### 0.3 Architectural constraints from prior planning (do NOT relitigate)

Bundle 4 spec inherits these locked decisions; cycle prompts MUST honor them, not re-litigate:

1. **Monolithic single agent + bear-case (per [[agent-topology]]).** invest-bear-case is ONE Claude Code call, not a standing agent. invest-thesis is one call, not a 5-agent pipeline. The /trade analyze 5-parallel-subagent orchestration is explicitly skipped per `trade-skills-reference-pass` cross-cutting patterns.
2. **Validator isolation (per [[risk-controls]]).** No Bundle 4 skill embeds position-sizing rules in prompt text. invest-thesis proposes R/R + stop only; the validator layer (Bundle 1) owns sizing ceilings. Hard rule from Bundle 3 cycle 4 architectural integrity.
3. **NBLM is Phase 4 conditional (per [[nblm-mvp]]).** Phase 2 MVP uses one-shot `/research` with explicit `--sources`. invest-thesis MUST NOT depend on NBLM in Bundle 4. NBLM promotion to invest-thesis primary path is Phase 4 if the 5-criterion exit gate passes.
4. **AI vs Human Ideas (per CLAUDE.md).** Bundle 4 skills surface analysis with `> [!robot] K2Bi analysis` callouts. They DO NOT propose trades. invest-thesis emits `recommended_action: bull|neutral|bear` as a SIGNAL, not a "buy SPY" directive.
5. **Routines-Ready discipline (per [[skills-design]]).** All 5 Bundle 4 skills are Analyst tier. All 5 must satisfy 5 Routines-Ready principles or fail their own eval harness.

---

## 1. Scope + non-goals

### In scope

| Milestone | Skill | Tier | Output target |
|---|---|---|---|
| m2.11 | invest-thesis | Analyst | `wiki/tickers/<SYM>.md` (frontmatter + body) |
| m2.12 | invest-bear-case | Analyst | append to `wiki/tickers/<SYM>.md` (frontmatter + section) |
| m2.13 | invest-screen | Analyst | `wiki/watchlist/<SYM>.md` (frontmatter + body) |
| m2.14 | invest-regime | Analyst | `wiki/regimes/current.md` (atomic) + `wiki/regimes/<YYYY-MM-DD>_<class>.md` (archive) |
| m2.15 | invest-backtest | Analyst | `raw/backtests/YYYY-MM-DD_<slug>_backtest.md` (immutable per-run capture) -- see Q1 |

Tests + integration + per-skill MiniMax-iterate + Codex final-gate per spec §10 from Bundle 3.

### Out of scope (document explicitly)

- NBLM-grounded thesis path (Phase 4 conditional per nblm-mvp).
- Auto-screening from feed data (Phase 4 conditional; Phase 2 is manual ticker intake).
- Multi-round adversarial debate in invest-bear-case (explicitly rejected per agent-topology -- single call only).
- Walk-forward backtest harness (Phase 4 conditional).
- Point-in-time data stores (Phase 4 conditional).
- Auto-detection of regime from VIX/term-structure feeds (Phase 4 conditional).
- Multi-strategy portfolio backtest (Phase 4+).
- Slippage + commission modeling in backtest (Phase 4+).
- invest-earnings standalone skill (folded into invest-thesis refresh mode per `trade-skills-reference-pass`).
- invest-sector standalone skill (deferred to Phase 3+ per `trade-skills-reference-pass`).
- File-lock guard on /invest-ship (deferred to Bundle 6 per Bundle 3 Q11; un-defer trigger = first concurrency near-miss).

---

## 2. Data contracts (LOCK these before writing code -- per Bundle 3 retro learning #1)

### 2.1 invest-thesis output -- `wiki/tickers/<SYMBOL>.md`

Filename: uppercase ticker. Special tickers (`0700.HK`, `BRK.B`) preserve casing + period. Glob: `wiki/tickers/*.md`.

```yaml
---
tags: [ticker, <SYMBOL>, thesis]
date: YYYY-MM-DD
type: ticker
origin: k2bi-extract
up: "[[tickers/index]]"
symbol: <SYMBOL>
confidence-last-verified: YYYY-MM-DD
thesis-last-verified: YYYY-MM-DD
# Composite signal (0-100) -- 5 thesis sub-dimensions from agents/trade-thesis.md
thesis_score: 73
sub_scores:
  catalyst_clarity: 16   # 0-20
  asymmetry: 14          # 0-20
  timeline_precision: 15 # 0-20
  edge_identification: 12 # 0-20
  conviction_level: 16   # 0-20
# Fundamental sub-scoring (0-20 each) -- from agents/trade-fundamental.md
# Plugs into Ahern phase 3 (financial quality) + phase 2 (competitive position / moat)
# NOT recomposed into thesis_score; enriches body content + drives objective fundamental assessment
fundamental_sub_scores:
  valuation: 13          # 0-20
  growth: 16             # 0-20
  profitability: 17      # 0-20
  financial_health: 15   # 0-20
  moat_strength: 13      # 0-20
# Bull/bear/base structured cases
bull_case:
  reasons:
    - reason: "Data-center revenue concentration in hyperscaler capex cycle"
      evidence: "Q3 2025 hyperscaler capex +47% YoY (Microsoft, Meta earnings calls)"
      impact_estimate: "Adds 15-20% to forward revenue trajectory"
bear_case:
  reasons:
    - reason: "Single-customer concentration risk"
      evidence: "Top-3 customers = 62% of data-center revenue (10-Q footnote)"
      impact_estimate: "One delayed capex cycle compresses multiple by 30-40%"
base_case:
  scenario: "Steady hyperscaler capex through 2027"
  probability: 0.55
  target_price: 850.00
# Order spec proposal (R/R + stops only -- NO position sizing per Q3 validator-isolation)
entry_exit_levels:
  entry: 700.00
  stop: 630.00
  targets:                  # structured per target with sell_pct (from trade-thesis SKILL.md §5)
    - level: T1
      price: 800.00
      sell_pct: 33            # % of position sold at this target
      reasoning: "Prior resistance level"
    - level: T2
      price: 900.00
      sell_pct: 33
      reasoning: "Bull case fair value"
    - level: T3
      price: 1000.00
      sell_pct: 34
      reasoning: "Stretch target -- sector re-rating"
  risk_reward_ratio: 4.3
# Entry triggers (conditions that MUST be met) -- from trade-thesis SKILL.md §4
entry_triggers:
  - "RSI < 40 on daily timeframe"
  - "Volume above 20-day average on green day"
  - "No earnings within 14 days"
# Entry invalidation (conditions that MUST NOT hold) -- from trade-thesis SKILL.md §4
entry_invalidation:
  - "Price breaks below $630 support on heavy volume"
  - "Insider selling accelerates"
  - "Sector rotation signals turn negative"
# Exit signals (sell regardless of price) -- from trade-thesis SKILL.md §5
exit_signals:
  - "Thesis-breaking news (loss of major customer, fraud, accounting restatement)"
  - "Fundamental deterioration: 2+ consecutive revenue misses"
  - "Better opportunity identified (opportunity cost)"
# Time stop (max hold period + reassessment triggers) -- from trade-thesis SKILL.md §5
time_stop:
  max_hold_period: "6 months"
  reassessment_triggers:
    - "Re-evaluate after each earnings report"
    - "If thesis hasn't played out in 6 months, reassess regardless of P/L"
# Direction signal (NOT a trade order per CLAUDE.md AI vs Human Ideas)
recommended_action: bull   # bull | neutral | bear -- direction sentiment only
# Conviction band derived from thesis_score (presentation layer, not separate score)
conviction_band: good      # high (80-100) | good (65-79) | watchlist (50-64) | pass (35-49) | avoid (0-34)
# Catalyst timeline (table in body, structured here for Routines-Ready JSON I/O)
next_catalyst:
  event: "Q4 2025 earnings"
  date: 2026-02-15
  expected_impact: "guidance for FY26 hyperscaler capex"
catalyst_timeline:           # multi-event timeline; rendered as table in body
  - date: 2026-02-15
    event: "Q4 2025 earnings"
    expected_impact: "Positive -- consensus 5% upside surprise potential"
    probability: high
  - date: 2026-03-20
    event: "GTC keynote"
    expected_impact: "Positive -- new product reveals"
    probability: medium
---

> [!robot] K2Bi analysis -- Phase 2 MVP via one-shot /research

**Plain-English summary (Teach Mode novice/intermediate gate):**
[2-3 sentences in non-jargon explaining what the thesis says]

## Phase 1: Business Model
[Ahern phase 1 content]

## Phase 2: Competitive Position / Moat
[Ahern phase 2 + bull reasons land here]

## Phase 3: Financial Quality
[Ahern phase 3 + company-type key-metric selector]

## Phase 4: Risks + Valuation
[Ahern phase 4 + bear reasons land here + fundamental_sub_scores.valuation drives content]

## Catalyst Timeline
[Table from frontmatter catalyst_timeline rendered for human readability]

| Date | Catalyst | Expected Impact | Probability |
|---|---|---|---|
| 2026-02-15 | Q4 2025 earnings | Positive -- consensus 5% upside surprise potential | high |
| 2026-03-20 | GTC keynote | Positive -- new product reveals | medium |

## Entry Strategy (from trade-thesis SKILL.md §4)
- **Primary entry:** $700 -- 50-day MA support + volume shelf
- **Secondary entry (aggressive):** $720 -- breakout above pattern resistance
- **Secondary entry (conservative):** $660 -- pullback to 200-day MA

### Entry Triggers (conditions that MUST be met -- frontmatter mirrors)
1. RSI < 40 on daily timeframe
2. Volume above 20-day average on green day
3. No earnings within 14 days

### Entry Invalidation (do NOT enter if -- frontmatter mirrors)
1. Price breaks below $630 support on heavy volume
2. Insider selling accelerates
3. Sector rotation signals turn negative

## Exit Strategy (from trade-thesis SKILL.md §5)

### Profit Targets (frontmatter mirrors)
| Target | Price | % Gain | Action | Reasoning |
|---|---|---|---|---|
| T1 | $800 | +14% | Sell 33% of position | Prior resistance level |
| T2 | $900 | +29% | Sell 33% of position | Bull case fair value |
| T3 | $1000 | +43% | Sell remaining 34% | Stretch target -- sector re-rating |

### Stop Loss
- **Initial stop:** $630 (-10% from entry) -- below the 200-day MA
- **Trailing stop:** after T1 hits, move stop to breakeven

### Time Stop (frontmatter mirrors)
- **Maximum hold:** 6 months
- **Reassessment triggers:** re-evaluate after each earnings report; reassess regardless of P/L if thesis hasn't played out in 6 months

### Exit Signals (sell regardless of price -- frontmatter mirrors)
1. Thesis-breaking news (loss of major customer, fraud, accounting restatement)
2. Fundamental deterioration: 2+ consecutive revenue misses
3. Better opportunity identified (opportunity cost)

## Asymmetry Analysis
[EV-weighted scenario table: Bull/Base/Neutral/Bear probability x target = expected value]
[Asymmetry Score 1-10 with meaning bands]

## Thesis Scorecard
[Table presenting the 5 sub_scores with the trade-thesis-SKILL.md presentation-layer weighting (Business Quality 15% + Valuation 20% + Growth 15% + Technical 15% + Catalyst Clarity 15% + R/R 20% = 100%) for human readability. The composite thesis_score in frontmatter uses the agent's 5-sub-dim x 0-20 scoring; this table is a HUMAN-FACING re-presentation, not an alternate scoring system.]

## Fundamental Sub-Scoring (from agents/trade-fundamental.md)
[5-dim breakdown: valuation + growth + profitability + financial_health + moat_strength @ 0-20 each. Plugs Phase 3 financial quality + Phase 2 moat content above. NOT recomposed into thesis_score -- enrichment data only.]

## Action Plan Summary
[Code-block format from trade-thesis SKILL.md §10 -- glanceable one-screen handoff]

```
TICKER:        <SYMBOL>
DIRECTION:     <bull / neutral / bear>
ENTRY:         $<price> (limit order)
STOP LOSS:     $<price> (-X%)
TARGET 1:      $<price> (+X%) -- sell 33%
TARGET 2:      $<price> (+X%) -- sell 33%
TARGET 3:      $<price> (+X%) -- sell remaining
RISK/REWARD:   X:1
POSITION:      validator-owned (see config.yaml position_size cap)
TIMEFRAME:     <max-hold-period>
NEXT CATALYST: <event> on <date>
```
```

### 2.2 invest-bear-case output -- appended to `wiki/tickers/<SYMBOL>.md`

Adds frontmatter fields + appends a `## Bear Case (YYYY-MM-DD)` section. Multiple bear-case runs append multiple dated sections (audit trail). Frontmatter holds LATEST verdict only.

```yaml
# Added to existing thesis frontmatter:
bear-last-verified: YYYY-MM-DD
bear_conviction: 65         # 0-100
bear_top_counterpoints:
  - "Single-customer concentration ..."
  - "Forward P/E pricing perfection ..."
  - "Geopolitical export-control extension risk ..."
bear_invalidation_scenarios:
  - "Hyperscaler capex deceleration > 20% YoY"
  - "Tier-2 chip export ban announcement"
bear_verdict: PROCEED       # VETO | PROCEED
```

Body section appended:
```markdown
## Bear Case (2026-04-19)

**Verdict:** PROCEED (conviction: 65)

### Top counterpoints to monitor
1. ...
2. ...
3. ...

### Invalidation scenarios
- ...

### Why this matters for your position (Teach Mode novice/intermediate)
[2-3 sentences translating bear case to dollar/risk impact in HKD against current portfolio state]
```

### 2.3 invest-screen output -- `wiki/watchlist/<SYMBOL>.md`

```yaml
---
tags: [watchlist, <SYMBOL>]
date: YYYY-MM-DD
type: watchlist
origin: keith         # keith for manual intake; k2bi-generate for auto-screen (Phase 4)
up: "[[watchlist/index]]"
symbol: <SYMBOL>
added: YYYY-MM-DD
confidence-last-verified: YYYY-MM-DD
status: active        # active | removed
# Quick Score composite per /trade-watchlist (40 + 35 + 25 = 100)
quick_score: 78
quick_score_breakdown:
  technical: 32      # max 40
  technical_sub:
    trend_alignment: 8     # max 10
    momentum: 7            # max 8
    volume_pattern: 5      # max 7
    pattern_quality: 6     # max 8
    key_level_proximity: 6 # max 7
  fundamental: 28    # max 35
  fundamental_sub:
    valuation: 6           # max 8
    growth: 7              # max 8
    profitability: 6       # max 7
    balance_sheet: 5       # max 6
    analyst: 4             # max 6
  catalyst: 18       # max 25
  catalyst_sub:
    catalyst_clarity: 6    # max 8
    timeline: 5            # max 6
    sentiment: 4           # max 5
    rr_setup: 3            # max 6
rating_band: B        # A 80-100 | B 65-79 | C 50-64 | D 35-49 | F <35
band_definition_version: 1   # bumps when sub-factor band definitions change (MiniMax R4); score_delta only valid within same version
previous_score: 72
previous_score_band_version: 1   # if differs from band_definition_version, score_delta is null and a re-score warning is emitted
score_delta: 6        # signed; null if band_definition_version mismatch
# Flags (booleans, consumed by invest-alert in Bundle 5)
flags:
  earnings_within_7d: false
  short_interest_over_20: false
  sub_300m_market_cap: false
# Rubric (Phase 2 MVP from existing stub)
moat: medium          # strong | medium | weak | n/a
balance_sheet: strong # strong | medium | weak | stressed
thesis_fit: 4         # 1-5
catalysts: ["Q4 earnings 2026-02-15"]
risk_flags: ["concentration"]
---

> [!robot] K2Bi analysis -- Phase 2 MVP manual ticker intake

[Body: Keith's rationale or extracted summary]

## Quick Score Breakdown
[Human-readable table presenting sub-scores]
```

### 2.4 invest-regime output -- `wiki/regimes/current.md` (atomic) + `wiki/regimes/<date>_<class>.md` (archive)

`current.md` is single-file canonical state. Each classification CHANGE archives a copy with ISO date + class slug. Same-classification refreshes (e.g. extending `valid_until`) update `current.md` in place without archiving.

```yaml
---
tags: [regime, current]
date: YYYY-MM-DD
type: regime
origin: keith
up: "[[regimes/index]]"
classification: risk_on   # locked enum -- see below
confidence: 4             # 1-5
rationale: "VIX 14, SPY above 200MA, credit spreads tight, no upcoming Fed event"
valid_until: 2026-04-26   # default +7d, Keith can override
previous: choppy          # prior classification
classified_at: 2026-04-19T08:30:00Z
# Optional structured signals if Keith narrates them
signals:
  vix: 14.2
  spy_vs_200ma_pct: 8.5
  credit_spread_bps: 95
  rate_direction: stable
  geopolitical_temp: low
---

> [!robot] K2Bi analysis -- manual regime classification

[Body: Keith's rationale, multi-turn distilled]

## Why this matters for your strategies
[Lists active strategies with `regime-required:` frontmatter and whether they ALIGN or MISMATCH this regime. Phase 2 advisory only; Phase 4 deterministic gate if needed.]
```

**Regime classification enum (LOCKED -- adding values requires architect signoff):**
```
risk_on
risk_off
choppy
trending_up
trending_down
volatility_expansion
volatility_contraction
```

### 2.5 invest-backtest output -- `raw/backtests/YYYY-MM-DD_<slug>_backtest.md` (per Q1 below)

Each backtest run = one immutable file in `raw/backtests/`. Strategy file `wiki/strategies/strategy_<slug>.md` is NOT touched (Bundle 3 cycle 4 Check D content-immutability holds).

```yaml
---
tags: [backtest, <slug>, raw]
date: YYYY-MM-DD
type: backtest
origin: k2bi-generate
up: "[[backtests/index]]"
strategy_slug: <slug>
strategy_commit_sha: <sha of strategy file at backtest-run time>
backtest:
  window:
    start: 2024-01-01
    end: 2026-04-18
  source: yfinance
  source_version: 0.2.40
  symbol: SPY
  reference_symbol: SPY    # if same as symbol, no reference benchmark
  metrics:
    sharpe: 1.42
    sortino: 1.86
    max_dd_pct: -8.5
    win_rate_pct: 58.0
    avg_winner_pct: 2.3
    avg_loser_pct: -1.8
    total_return_pct: 34.5
    n_trades: 87
    avg_trade_holding_days: 9.2
  look_ahead_check: passed     # passed | suspicious
  look_ahead_check_reason: ""  # populated if suspicious
  last_run: 2026-04-19T10:15:00Z
---

> [!robot] K2Bi analysis -- yfinance sanity-check backtest

## Strategy Reference
- Slug: <slug>
- Commit SHA at backtest time: `<sha>`
- Strategy file: [[strategy_<slug>]]

## Sanity Gate Result
[passed | suspicious -- if suspicious, list which thresholds tripped]

## Metrics
[Human-readable table]

## Trade Distribution
[Optional: histogram or first/last 5 trades]

## Limitations
- yfinance returns what it has today (no point-in-time)
- mid-price fills (no slippage modeling)
- single strategy in isolation (no portfolio context)
```

**Sanity gate (NOT overridable):** `total_return_pct > 500 OR max_dd_pct > -2 OR win_rate_pct > 85` → `look_ahead_check: suspicious` + `look_ahead_check_reason: <which thresholds tripped>`. Skill writes the file regardless (audit trail), but the strategy approval flow MUST refuse `status: approved` if any backtest for that slug has `look_ahead_check: suspicious` and no Keith-written override note in the strategy body.

**Atomic-write concurrency policy (MiniMax R3 -- HIGH):** invest-backtest MUST write the per-run capture file via the canonical atomic pattern: tempfile in the SAME directory as the target, write all content to tempfile, `f.flush()` + `os.fsync(f.fileno())`, then `os.replace(tmp, final)`. This guarantees the approval-gate scan never sees a partial file. Multi-write() sequences without fsync are NOT safe -- the scan can interleave with a partial write and misread `look_ahead_check`. `scripts/lib/strategy_frontmatter.py`'s atomic-write helper already implements this; invest-backtest MUST use it (NOT roll a bare-write equivalent). Concurrent backtest + approval is otherwise unsafe until Bundle 6 file locks land per Q11.

---

## 3. Per-skill architecture

### 3.1 invest-thesis (m2.11)

**Implementation language: Python (LOCKED -- MiniMax R2 HIGH).** Python via `scripts/lib/invest_thesis.py` calling `scripts/lib/strategy_frontmatter.py` for atomic write. The "try bash-first" recommendation that appeared in v1 of this spec was removed: the skill's surface (Ahern body assembly + scorecard math + EV-weighted asymmetry table + atomic YAML serialization + Teach Mode conditional + glossary append) makes bash fragile and convergence-prone. Same lesson as Bundle 3 cycle 4 hooks (Python helper, not bash parsers). Skill body is `.claude/skills/invest-thesis/SKILL.md` (Claude orchestration); compute is `scripts/lib/invest_thesis.py` (Python).

**Mandatory original-source reads (NOT just the reference pass synthesis):** the cycle 1 implementer MUST read these three files at `~/Desktop/trading-skills/` directly, not only the K2Bi-side trade-skills-reference-pass.md condensation:

1. `~/Desktop/trading-skills/skills/trade-thesis/SKILL.md` (352 lines) -- 10-section structure including Entry Strategy (§4), Exit Strategy (§5), Asymmetry Assessment (§8), Thesis Scorecard presentation weights (§9), Action Plan Summary code-block format (§10), Edge Cases, Quality Standards, Error Handling. The reference pass condenses these; the originals carry the depth.
2. `~/Desktop/trading-skills/agents/trade-thesis.md` (233 lines) -- locked JSON output schema + per-sub-dim score band definitions (catalyst_clarity 17-20 / 13-16 / 9-12 / 5-8 / 0-4 with conditions). Use these EXACT bands in invest_thesis.py's scorecard module.
3. `~/Desktop/trading-skills/agents/trade-fundamental.md` (183 lines) -- 5-dim fundamental sub-scoring with band definitions (valuation / growth / profitability / financial_health / moat_strength @ 0-20 each). Plugs into Ahern phase 3 (financial quality) and phase 2 (competitive position / moat) per spec §2.1's `fundamental_sub_scores` schema.

**Source-gathering path (Phase 2 MVP):** `/research` with explicit `--sources <urls>` (10-K, 10-Q, last 4 earnings transcripts, investor deck). Per the existing stub note about `/research deep` source-gathering gap. Phase 4 may promote to NBLM-grounded path if 5-criterion exit gate passes.

**Pipeline:**
1. Read `<SYMBOL>` argument (uppercase, support `0700.HK` style).
2. Check `wiki/tickers/<SYMBOL>.md` for `thesis-last-verified:` within 30 days. If fresh + no `--refresh` flag, skip with informational message + exit 0.
3. Gather sources (Phase 2: explicit URLs passed to `/research`).
4. Run Ahern 4-phase analysis on gathered material (4 H2 sections).
5. Compute 5-dimension thesis scorecard using EXACT band definitions from `agents/trade-thesis.md`: catalyst_clarity, asymmetry, timeline_precision, edge_identification, conviction_level (each 0-20). Composite `thesis_score` 0-100 = sum.
6. Compute 5-dimension fundamental sub-scoring using EXACT band definitions from `agents/trade-fundamental.md`: valuation, growth, profitability, financial_health, moat_strength (each 0-20). Stored as `fundamental_sub_scores` enrichment; NOT recomposed into thesis_score.
7. Derive `conviction_band` from thesis_score: high (80-100) | good (65-79) | watchlist (50-64) | pass (35-49) | avoid (0-34).
8. Build asymmetry block (EV-weighted scenario table) + Asymmetry Score 1-10 per `trade-thesis SKILL.md §8`.
9. Build Entry Strategy section (primary/secondary entries + entry_triggers list + entry_invalidation list) per `trade-thesis SKILL.md §4`. Entry triggers + invalidation also stored in frontmatter.
10. Build Exit Strategy section (profit targets table with sell_pct per T1/T2/T3 + stop loss + time_stop + exit_signals list) per `trade-thesis SKILL.md §5`. All also stored in frontmatter.
11. Build Catalyst Timeline section (multi-event table) per `trade-thesis SKILL.md §3`. Stored as `catalyst_timeline` array in frontmatter; `next_catalyst` is the soonest-dated row.
12. Build Action Plan Summary as code-block format per `trade-thesis SKILL.md §10`. Position field always reads "validator-owned (see config.yaml position_size cap)" -- NEVER computes a position size (Q3 architectural constraint).
13. Apply Teach Mode preamble (novice/intermediate prepend; advanced skip).
14. First-occurrence terms → `[[glossary#term]]`; missing terms → append pending stub to `wiki/reference/glossary.md` in same run.
15. Atomic write `wiki/tickers/<SYMBOL>.md` via `scripts/lib/strategy_frontmatter.py` (reuse Bundle 3 cycle 4 helper for serialize-with-NFC).
16. Append to `wiki/log.md` via `scripts/wiki-log-append.sh`.

**Edge cases (lock these from /trade-thesis):**
- ETF: thesis on sector/thematic, not single-company financials. Frontmatter `ticker_type: etf`.
- Pre-revenue: substitute runway + TAM + pipeline for traditional growth/margin metrics. Frontmatter `ticker_type: pre_revenue`.
- Penny stock (<$300M market cap): liquidity warning + adjusted-sizing comment in body. Frontmatter `ticker_type: penny`.

### 3.2 invest-bear-case (m2.12)

**Pipeline:**
1. Read `<SYMBOL>` argument. Optional `--thesis <path>` to target a specific thesis version.
2. Read `wiki/tickers/<SYMBOL>.md`. If no `thesis_score` field present, refuse with "run /thesis first; bear-case requires existing thesis" + exit 1.
3. Construct adversarial prompt (template lives in skill body):
   ```
   Here is a bull thesis on <SYMBOL> (thesis_score: <N>, sub-scores: <breakdown>):
   <thesis body excerpts: phases 1-4 + asymmetry + bull_case reasons>
   
   Strategy proposing the trade: [if invoked from /invest-ship pre-approval; otherwise "general thesis review"]
   
   Your job: build the strongest case AGAINST this thesis. 
   - Use only verifiable claims (cite sources or mark as inference).
   - Identify the strongest structural reasons the thesis is wrong.
   - Identify scenarios that invalidate it.
   - Rate your conviction 0-100 on how strong the bear case is.
   - If conviction > 70: return VETO with top-3 strongest counterpoints.
   - Else: return PROCEED with top-3 counterpoints Keith should monitor.
   ```
4. Parse Claude's structured response into the schema (§2.2).
5. Atomic update `wiki/tickers/<SYMBOL>.md` frontmatter (add bear_* fields; preserve all existing thesis fields) + append `## Bear Case (YYYY-MM-DD)` body section.
6. Apply Teach Mode footer (novice/intermediate: dollar-impact translation against current portfolio state in HKD).
7. Append to `wiki/log.md`.

**Engine integration (read-only contract; engine code already shipped Bundle 3):** the engine's strategy approval flow MUST refuse `status: approved` if the strategy's primary ticker has `bear_verdict: VETO` from a bear-case run within last 30 days. Override = fresh bear-case run returning PROCEED. This logic lives in `/invest-ship --approve-strategy` Step A validation; Bundle 4 doesn't add code there, only documents the contract.

### 3.3 invest-screen (m2.13)

**Phase 2 MVP scope:** manual ticker intake + structured rubric + Quick Score composite. NO automated screening from feed data (Phase 4 conditional).

**Pipeline:**
1. Read `<SYMBOL>` + optional `--reason <text>` + optional sub-score overrides.
2. If `wiki/watchlist/<SYMBOL>.md` exists: read existing scores, set `previous_score` from current `quick_score`, recompute, set `score_delta`. If new: `previous_score: null`, `score_delta: null`.
3. Multi-turn if reason or sub-scores not provided: ask Keith for the 14 sub-factors (or accept "use defaults" with documented anchors).
4. Compute `quick_score` = sum of three category totals (technical 0-40 + fundamental 0-35 + catalyst 0-25).
5. Determine `rating_band`: A 80-100, B 65-79, C 50-64, D 35-49, F <35.
6. Set boolean flags: `earnings_within_7d` (requires Keith input or earnings calendar feed -- Phase 2 manual), `short_interest_over_20`, `sub_300m_market_cap`.
7. Atomic write `wiki/watchlist/<SYMBOL>.md`.
8. Append to `wiki/log.md`.

**Cross-skill consistency:** the 14 sub-factors are scored on ABSOLUTE bands (e.g. "trend_alignment 8/10 = 50MA above 200MA AND price above 50MA"), NOT relative-to-other-tickers. This makes scores comparable across tickers without anchoring drift. Skill body includes a one-line band definition for each sub-factor.

**Band definition versioning (MiniMax R4 MEDIUM -- closes the band-drift sub-anchoring problem):** band definitions live in skill body AND get a `band_definition_version` integer that bumps on ANY revision. The output frontmatter records `band_definition_version: <N>` at scoring time. When invest-screen scores an existing watchlist entry, it compares `previous_score_band_version` (from the existing file) to current `band_definition_version`:

- Same version → `score_delta` computed normally.
- Different version → `score_delta: null` + warning emitted in body: "Band definitions changed from v<old> to v<new>; this score is on a fresh measurement instrument, not directly comparable to previous_score".

This makes the measurement instrument explicit and prevents silent band-definition drift from masquerading as genuine score changes. Cycle 3 (invest-screen) authors `band_definition_version: 1` as the initial value + documents the bump policy in skill body.

**Phase 4 deferred (stub a section in SKILL.md citing target):**
- 5 pre-built screen criteria sets (growth/value/momentum/dividend/earnings).
- WebSearch cross-reference pattern.
- Auto-screen against feed data.

### 3.4 invest-regime (m2.14)

**Pipeline:**
1. Read `wiki/regimes/current.md` if exists; show Keith current state.
2. Optional argument: `<classification>` to skip multi-turn. If absent, multi-turn:
   - Ask about VIX level, SPY 200MA position, credit spreads, rate direction, geopolitical temperature.
   - Distill into one of the 7 LOCKED enum values (§2.4).
3. If classification CHANGED from previous: archive previous state to `wiki/regimes/<date>_<previous_class>.md` (read previous current.md, write to archive).
4. Atomic write new `wiki/regimes/current.md` via tempfile + os.replace.
5. Append to `wiki/log.md`.
6. Body's "Why this matters for your strategies" section: scan `wiki/strategies/strategy_*.md` for `regime-required:` frontmatter, list ALIGNED vs MISMATCH per strategy. Phase 2 advisory; Phase 4 may promote to deterministic gate.

**Multi-turn discipline:** keep questions short, one at a time. Distillation logic in skill body, not embedded in prompts.

### 3.5 invest-backtest (m2.15)

**Pipeline:**
1. Read `<strategy-slug>` argument; resolve to `wiki/strategies/strategy_<slug>.md`. If not found, refuse + exit 1.
2. Capture `strategy_commit_sha` = current `git rev-parse HEAD` of K2Bi repo (NOT parent sha; we want the sha at backtest run time so the audit trail can find the exact strategy state).
3. Parse strategy frontmatter via `scripts/lib/strategy_frontmatter.py` to extract: symbol, entry_rules, exit_rules, sizing_logic.
4. Pull 2 years of daily yfinance bars for the symbol + SPY (reference benchmark).
5. Simulate strategy day-by-day (vectorized pandas; no loop-of-loops):
   - Apply entry rules; track position open/close; compute daily P&L.
   - Mid-price fills (Phase 2 MVP; Phase 4 adds slippage).
6. Compute metrics per §2.5 schema.
7. Apply sanity gate (500%/2%/85% trigger). Set `look_ahead_check: passed | suspicious` + reason.
8. Write `raw/backtests/YYYY-MM-DD_<slug>_backtest.md` (immutable per-run capture).
9. Append to `wiki/log.md`.

**Strategy approval gate integration -- LOCKED schema (MiniMax R1 -- HIGH; closes the cycle-3 spec-gap pattern):**

`/invest-ship --approve-strategy` Step A MUST scan `raw/backtests/*_<slug>_backtest.md` files via the following exact algorithm. This is the addition to `scripts/lib/invest_ship_strategy.py`'s `handle_approve_strategy` that ships in cycle 5 alongside invest-backtest itself.

```
def scan_backtests_for_slug(slug: str) -> ScanResult:
    """Returns ApprovalGate verdict for backtest scan."""
    # 1. Glob: K2Bi-Vault/raw/backtests/<YYYY-MM-DD>_<slug>_backtest.md
    pattern = f"K2Bi-Vault/raw/backtests/*_{slug}_backtest.md"
    files = sorted(glob.glob(pattern), reverse=True)  # filename descending = ISO-date descending
    
    # 2. Empty case: no backtest = HARD REFUSE
    if not files:
        return ScanResult.REFUSE("no backtest found for strategy; run /backtest <slug> first")
    
    # 3. Skip incomplete files defensively (atomic-write protection)
    files = [f for f in files if os.path.getsize(f) > 0]
    if not files:
        return ScanResult.REFUSE("backtest files exist but all are empty (interrupted writes?); re-run /backtest <slug>")
    
    # 4. Parse the most recent file's frontmatter via strategy_frontmatter.py helper
    most_recent = files[0]
    try:
        frontmatter = parse_frontmatter(most_recent)
    except YAMLError as e:
        return ScanResult.REFUSE(f"backtest {most_recent} unparseable ({e}); re-run /backtest <slug>")
    
    # 5. Check the look_ahead_check field
    look_ahead = frontmatter.get("backtest", {}).get("look_ahead_check")
    if look_ahead == "passed":
        return ScanResult.PROCEED()
    elif look_ahead == "suspicious":
        # 6. Look for Keith-written override in strategy body
        strategy_body = read_strategy_body(slug)
        if has_section(strategy_body, "## Backtest Override"):
            return ScanResult.PROCEED()  # override accepted (Keith made the call)
        else:
            return ScanResult.REFUSE(
                f"backtest {most_recent} has look_ahead_check: suspicious "
                f"({frontmatter['backtest'].get('look_ahead_check_reason')}); "
                f"add '## Backtest Override' section to strategy body explaining why this is acceptable, then retry"
            )
    else:
        return ScanResult.REFUSE(f"backtest {most_recent} has unknown look_ahead_check value: {look_ahead}")
```

**Locked decisions (do NOT relitigate during cycle 5):**

1. **"Most recent" = filename-date descending sort.** ISO date prefix in filename guarantees lexicographic sort = chronological sort. Faster than parsing every file's `last_run` frontmatter; tie-breaking on same-day runs is filename-suffix-deterministic.
2. **No backtest = HARD REFUSE** (not silent skip). Approval requires at least one backtest run for the slug. This forces the discipline of "backtest before approve".
3. **Empty file = HARD REFUSE** (defensive against interrupted writes from concurrency window per §2.5).
4. **YAML-unparseable = HARD REFUSE** with re-run prompt.
5. **`look_ahead_check: suspicious` + no override section = HARD REFUSE.** `look_ahead_check: suspicious` + `## Backtest Override` section in strategy body = PROCEED.
6. **Unknown `look_ahead_check` value = HARD REFUSE.** Forces the enum to stay locked to {passed, suspicious}.

**Override section format (also locked):**

```markdown
## Backtest Override

Backtest run: <ISO date> at `raw/backtests/YYYY-MM-DD_<slug>_backtest.md`
Suspicious flag reason: <copy from backtest's look_ahead_check_reason>
Why this is acceptable: <Keith's text, must be non-empty>
```

`scripts/lib/strategy_frontmatter.py` extends with `has_section(body, heading)` helper if not already present (cycle 5 verifies + adds if absent).

This contract requires a small Bundle 4 addition to `scripts/lib/invest_ship_strategy.py` `handle_approve_strategy` (insert `scan_backtests_for_slug` call between existing thesis-VETO check and frontmatter-validation step); ship as part of m2.15's cycle 5, NOT a separate cycle.

---

## 4. Cross-cutting patterns (apply to ALL Bundle 4 skills)

These come from `trade-skills-reference-pass` cross-cutting section + Bundle 3 retro learnings.

1. **Numeric rubric scorecards.** Every analyst skill emits a 0-100 composite + 0-20 sub-dim breakdown (or equivalent for the skill domain). Forces objective bands over prose.
2. **Action Plan Summary blocks (or equivalent).** Every user-facing analyst output ends with a one-screen handoff structured block. invest-thesis = full Action Plan; invest-bear-case = VETO/PROCEED + counterpoints; invest-screen = rating band + flags; invest-regime = classification + valid_until; invest-backtest = passed/suspicious gate + key metrics.
3. **Edge-case handling.** Each skill has an explicit edge-cases section (ETF / pre-revenue / penny / data-limited / first-run / refresh-of-existing).
4. **"No vague language" quality standard.** Every claim = number + date + source. "Strong growth" → "revenue grew 23% YoY to $4.2B in Q3 2025." Codify in skill body.
5. **JSON output contracts (Routines-Ready principle #4).** All frontmatter fields are YAML-serializable. Schemas above are the locked contracts; Bundle 4 cycles MUST NOT silently add or rename fields.
6. **Teach Mode integration.** Every skill reads `learning-stage:` from `K2Bi-Vault/System/memory/active_rules.md`. Apply per `[[trade-skills-reference-pass]]` Teach Mode rules:
   - novice: prepend plain-English preamble (2-3 sentences); first-occurrence terms → `[[glossary#term]]`.
   - intermediate: drop preamble on routine outputs; keep on first-time concepts.
   - advanced: skip preamble entirely; keep glossary linking.
7. **AI vs Human Ideas.** Every output uses `> [!robot] K2Bi analysis` callout. NO "buy SPY" directives. Signals only.
8. **Frontmatter via shared helper.** Use `scripts/lib/strategy_frontmatter.py` for ALL frontmatter parse + serialize. Do NOT author parallel YAML helpers per skill.
9. **Atomic writes.** Tempfile + os.replace pattern (NEVER partial writes). Use the helper's atomic-write mode.
10. **Single-writer log appends.** ALL `wiki/log.md` appends go through `scripts/wiki-log-append.sh`. Direct `>>` appends are pre-commit blocked.

---

## 5. Open architect questions -- ANSWERED before implementation

### Q1: Where does invest-backtest persist results given Bundle 3 Check D content-immutability?

**Answer:** `raw/backtests/YYYY-MM-DD_<slug>_backtest.md` (immutable per-run capture in raw/, NOT writes to strategy file).

**Why:** Bundle 3 cycle 4 Check D blocks any edit to approved-status strategy files except retire transitions. The original invest-backtest stub said "Write backtest block to wiki/strategies/<slug>.md frontmatter" -- that breaks Check D. Resolution: backtest results live in raw/ (immutable captures), referenced from strategy file via wiki link, never embedded in strategy frontmatter.

**Approval gate impact:** `/invest-ship --approve-strategy` Step A scans `raw/backtests/*_<slug>_backtest.md` files for the most recent run + checks `look_ahead_check`. This adds one validation step to Step A in Bundle 4 cycle 5 (invest-backtest's ship). Document this as a small Bundle 4 addition, not a separate cycle.

**Deviation cost of writing to strategy file:** weakens Check D for one field, opens a precedent for "well, this OTHER field is also safe to update in-place", strategy files become mutable in practice. Reject.

### Q2: invest-bear-case dependency on invest-thesis -- what about stale theses?

**Answer:** invest-bear-case refuses if no `thesis_score` field exists; runs against whatever thesis IS present (may be stale). invest-bear-case does NOT auto-trigger thesis refresh.

**Why:** keeps the skill single-purpose. If the thesis is stale (>30 days old per `thesis-last-verified`), the skill emits a warning in its output ("WARNING: thesis last verified <date>, consider /invest thesis <SYMBOL> --refresh first") but proceeds. Forcing a thesis re-run would couple the two skills' invocation patterns and break the parallel-shippable property.

**Engine impact:** the strategy approval flow already requires fresh bear-case (within 30 days). Engine doesn't care about thesis freshness directly -- it cares about bear-case verdict. So a stale-thesis + fresh-bear-case combination still gates approvals correctly.

### Q3: invest-regime archive cleanup -- when do old archives expire?

**Answer:** never auto-archived. Keith manual `git mv wiki/regimes/<old>_<class>.md wiki/regimes/archive/` if clutter becomes an issue.

**Why:** regime archives are an audit trail. The classification at any historical date is needed to validate strategy decisions in retrospect ("this trade was made during a `risk_off` regime; is that consistent with the strategy?"). Auto-deletion destroys audit. Low file volume means this is not a real clutter problem.

### Q4: invest-screen multi-ticker scoring consistency -- is the scorecard reproducible across tickers?

**Answer:** YES, by design. Each of the 14 sub-factors gets an absolute band definition in the skill body (e.g. "trend_alignment: 10 = price > 50MA > 200MA, both rising; 7 = price > 50MA but 50MA flat; 3 = price below 50MA; 0 = below 200MA"). NO relative-to-other-tickers scoring.

**Why:** anchoring is the failure mode here. If "growth: 8" means "high relative to other tickers I've scored this week", scoring shifts as the watchlist changes shape. Absolute bands keep the score meaningful over time.

**Implementation:** the band definitions live in skill body as a `## Sub-factor band definitions` section. Bundle 4 cycle for invest-screen MUST author this section before scoring any tickers.

### Q5: Teach Mode "How This Works" gate -- does it apply to invest-thesis output?

**Answer:** NO -- the "How This Works" gate is a STRATEGY spec requirement (Bundle 3 cycle 4 commit-msg + pre-commit hook enforcement). Thesis pages are NOT strategy specs.

**Why:** the Bundle 3 hook enforces "How This Works" only on `wiki/strategies/strategy_*.md` files, not on `wiki/tickers/*.md`. Confirmed by reading cycle 4 hook code.

**What thesis pages DO have:** the Teach Mode plain-English preamble (when learning-stage is novice/intermediate). Different gate, lighter enforcement, no hook involvement.

### Q6: Per-skill vs per-bundle Codex final-gate?

**Answer:** **Per-skill Codex final-gate + ONE cumulative-bundle MiniMax sweep before bundle close.**

**Why:** the 5 Bundle 4 skills are truly independent (except bear-case → thesis). Per-skill final-gate keeps each skill's audit trail clean + lets parallel sessions proceed without coordination. The cumulative MiniMax sweep at bundle close (per Bundle 3 retro learning #2) catches whole-bundle drift that per-skill reviews miss.

**Bundle close criterion:** all 5 skills shipped + cumulative MiniMax sweep clean (or only-P3) + Bundle 4 closure commit updates milestones.md / phase-2-bundles.md / roadmap.md / index.md.

### Q7: invest-bear-case PROCEED at low-conviction borderline -- 65? 69?

**Answer:** verdict = `VETO` if `conviction > 70`; `PROCEED` otherwise. Conviction value surfaced in frontmatter (`bear_conviction: 65`) for human review, but the VETO/PROCEED enum is the engine-consumable signal.

**Why:** locked threshold avoids per-call subjective drift. 70 is the AI Investing Lab pattern from agent-topology.md research. Keith can read conviction value to decide whether a borderline PROCEED warrants caution.

### Q8: invest-thesis on existing thesis -- refresh logic

**Answer:** skip if `thesis-last-verified:` within 30 days AND no `--refresh` flag. The `--refresh` flag forces re-run regardless of freshness.

**Why:** matches the existing stub. 30 days is a reasonable freshness window for fundamental-driven thesis (10-K cadence is annual; quarterly 10-Q drops within 90 days; thesis stale in 30 days mostly captures macro/competitive shifts).

### Q9: invest-backtest "look_ahead_check: suspicious" gate

**Answer:** advisory in Phase 2 (Keith reads + decides); deterministic gate in `/invest-ship --approve-strategy` Step A in Bundle 4 cycle 5 (small addition, not separate cycle). If suspicious AND no Keith-written `## Backtest Override` section in strategy body: refuse approval.

**Why:** the 500%/2%/85% thresholds catch the "11,000% P&L look-ahead cheat" pattern from research. Allowing approval without explicit override makes the gate cosmetic. Requiring an override note creates a paper trail when Keith chooses to ship anyway.

### Q10: Cross-skill JSON schema field consistency -- thesis `recommended_action` vs bear-case `verdict`?

**Answer:** different fields with different consumers; do NOT unify.

- `recommended_action: bull|neutral|bear` = invest-thesis direction signal. Consumed by Keith's decision-making (and Phase 4+ scoring algorithms).
- `bear_verdict: VETO|PROCEED` = invest-bear-case approval gate. Consumed by /invest-ship --approve-strategy logic.

Different purposes (signal vs gate), different value spaces (3-way enum vs 2-way), different consumers. Unification would force false equivalence.

---

## 6. Test matrix per skill

Each skill needs unit tests covering:

| Test class | invest-thesis | invest-bear-case | invest-screen | invest-regime | invest-backtest |
|---|---|---|---|---|---|
| Happy path | thesis written, all schema fields populated | bear block appended, verdict + conviction set | watchlist file written, quick_score correct | current.md updated, archive written if class changed | backtest file in raw/, all metrics computed |
| Refresh skip | thesis fresh-within-30d skips | bear-case fresh-within-30d skips | screen overwrites previous_score correctly | regime same-class no archive | backtest re-runs always (each = new file) |
| Edge case 1 | ETF ticker (no traditional financials) | thesis missing → refuse + exit 1 | first ticker (no previous_score) | classification unchanged → no archive | strategy not found → refuse + exit 1 |
| Edge case 2 | Pre-revenue | low-conviction PROCEED at 65 | sub-300M-cap flag set | classification not in enum → refuse | yfinance returns no data → refuse |
| Edge case 3 | Penny stock | high-conviction VETO at 85 | rating-band boundary (79 vs 80) | valid_until past → warn but allow | sanity gate trips → file written with suspicious flag |
| Schema | all frontmatter fields YAML-valid | bear_* fields don't clobber thesis_* | all 14 sub-factors present | enum value valid | all metrics fields populated |
| Teach Mode | novice prepend present | novice footer present | (no Teach Mode body) | (no Teach Mode body) | (no Teach Mode body) |
| Atomic write | tempfile + replace not partial | append doesn't lose existing thesis | watchlist file not partial | current.md atomic + archive atomic | backtest file atomic |
| Hook integration | thesis file commit doesn't trigger Check D (tickers/ not strategies/) | bear append doesn't trigger Check D | watchlist commit doesn't trigger Check D | regime commit doesn't trigger Check D | backtest commit (raw/) doesn't trigger Check D; strategy file untouched |

Plus per-bundle integration tests (cycle 7 of Bundle 4 e.g.):
- invest-thesis SPY → invest-bear-case SPY → both files coherent + bear references thesis_score correctly
- invest-screen SPY → invest-thesis SPY → thesis enriches the watchlist entry's catalysts list
- invest-regime risk_on → strategy with regime-required: [risk_on] shows ALIGNED in regime body
- invest-backtest spy-rotational → /invest-ship --approve-strategy spy-rotational → backtest gate consulted, approval proceeds (or refuses if suspicious without override)

---

## 7. Ship order (parallel-shippable per spec)

5 skills + 1 bundle close. Bear-case has soft dependency on thesis (skill-level, not ship-cycle: bear-case can SHIP before thesis is RUN against any specific ticker).

**Recommended sequencing:**

| Cycle | Skill | Parallel? | Dependencies |
|---|---|---|---|
| 1 | invest-thesis | Can ship in own session | None |
| 2 | invest-bear-case | Can ship in own session, parallel with cycle 1 | None at ship time (depends on thesis at INVOCATION time, not ship time) |
| 3 | invest-screen | Can ship in own session, parallel | None |
| 4 | invest-regime | Can ship in own session, parallel | None |
| 5 | invest-backtest + /invest-ship Step A addition | Sequential after the others | Touches scripts/lib/invest_ship_strategy.py (Bundle 3 cycle 5 file); coordinate to avoid merge conflicts with parallel-shipped skills if any of them ALSO touch it (none should -- only m2.15) |
| 6 | Cumulative-bundle MiniMax sweep + Bundle 4 closure | Sequential after all 5 skills shipped | All shipped |

**Parallelization strategy options:**

A) **Maximum parallel (3 sessions):** Session A ships cycles 1+2 (thesis + bear-case). Session B ships cycles 3+4 (screen + regime). Session C ships cycle 5 (backtest). Closure cycle 6 in any of the 3 sessions or a 4th. Highest velocity; coordination overhead = none if each session's commits don't touch the same file.

B) **Sequential one session per skill (5 sessions):** safer audit trail, no coordination concern, slower. Each session = one cycle. Recommended for first-time-applying-the-new-discipline.

C) **Hybrid (2 sessions):** Session A ships cycles 1-4 sequentially (4 LLM-reasoning skills). Session B ships cycle 5 (backtest needs Python/yfinance focus). Closure in Session A. Balances velocity + isolation.

**Architect recommendation: Option C.** Bundle 3 used single-session sequential discipline; Option C extends that with one parallelized session for the Python-heavy backtest skill. Matches the "decide-don't-ask" pattern + minimizes context switching + keeps audit clean.

Per-cycle commit subjects:
- Cycle 1: `feat(invest-thesis): MVP -- Ahern 4-phase + asymmetry + scorecard + Action Plan to wiki/tickers/`
- Cycle 2: `feat(invest-bear-case): MVP -- single Claude call, VETO/PROCEED gate appended to thesis`
- Cycle 3: `feat(invest-screen): MVP -- Quick Score composite + 14 sub-factors + rating band + flags to wiki/watchlist/`
- Cycle 4: `feat(invest-regime): MVP -- 7-class enum + multi-turn classification + atomic current.md + archive`
- Cycle 5: `feat(invest-backtest): MVP -- yfinance sanity-check + per-run capture in raw/backtests/ + approval-gate hook`
- Cycle 6: `chore(bundle-4): cumulative MiniMax sweep + closure (10 of 22 → 15 of 22 milestones)`

---

## 8. Review discipline (per spec §10 from Bundle 3, refined post-Bundle-3 retro)

Per cycle (each of cycles 1-5):

1. MiniMax-M2.7 iterative via `scripts/minimax-review.sh` until clean. Expect 2-4 rounds per skill (smaller surface than Bundle 3 cycles).
2. ONE Codex pass as final cross-vendor gate via codex-companion.mjs in background + Monitor tail (NEVER codex:rescue agent per L-2026-04-19-001).
3. Stop rule: 3+ Codex rounds on same surface = STOP + escalate to architect (the spec is ambiguous on that surface).
4. Accept/defer heuristics:
   - P1 always fix.
   - P2 fix if semantic/safety-related; defer if cosmetic (unless cluster on same pattern).
   - P3 defer with reason logged.

**Mid-bundle drift check (MiniMax R-next-step -- recommended addition):** after cycles 3-4 ship (when 2-3 skills have landed and cross-skill drift becomes possible), run ONE MiniMax-M2.7 review on `git diff <bundle-4-base-commit>..HEAD` -- mid-bundle scope. Label findings `R<N>-mid-bundle-sweep`. This catches cross-skill drift earlier than the cycle-6 close sweep would. Cheap (~$0.06), high-yield. Insert as a no-cycle-number checkpoint between cycles 4 and 5, takes ~5 minutes.

Cycle 6 closure (cumulative-bundle sweep):

1. ONE MiniMax-M2.7 review on `git diff <bundle-4-base-commit>..HEAD` -- all of Bundle 4 in one diff. Label findings `R<N>-bundle-sweep`.
2. Catches whole-bundle drift the per-cycle reviews missed (and any drift that surfaced after the mid-bundle check).
3. Fix P1/P2 inline; defer P3.
4. NO Codex pass on the sweep itself unless P1 is found that has cross-skill semantic implications.

Expected total Bundle 4 review counts:
- MiniMax: ~12-20 rounds across 5 skills + 1 sweep
- Codex: ~5-7 final-gate calls (1 per skill + maybe 1 on sweep findings)
- vs Bundle 3 totals (~13 Codex / ~19 MiniMax) -- Bundle 4 should burn slightly less Codex due to smaller per-skill surfaces.

---

## 9. Dependencies + prerequisites

### 9.1 Must hold before Bundle 4 cycle 1

- Bundle 3 closed (verified at `9f14dca`, 10/22 milestones, mailbox empty, Mini synced).
- `scripts/lib/strategy_frontmatter.py` available (Bundle 3 cycle 4 ship).
- `K2Bi-Vault/wiki/insights/insight_bundle-3-retro.md` read.
- `K2Bi-Vault/wiki/planning/trade-skills-reference-pass.md` read by sessions drafting m2.11 + m2.13.
- `K2Bi-Vault/wiki/reference/glossary.md` exists (Phase 2 supplementary; if not, cycle 1 creates it as a stub).
- yfinance pinned in `requirements.txt` (cycle 5 verifies; if not, ship as a small commit in cycle 5).

### 9.2 Inherited Bundle 3 surfaces (do NOT duplicate)

- Frontmatter parsing/serialization
- Deploy config + preflight
- Hook trio (pre-commit + commit-msg + post-commit)
- Sentinel scheme (`.retired-<sha16>.json`)
- /invest-ship subcommands (--approve-strategy etc.)

### 9.3 New Bundle 4 surfaces

- 5 new skill bodies in `.claude/skills/invest-{thesis,bear-case,screen,regime,backtest}/SKILL.md`.
- Possibly: `scripts/lib/invest_thesis.py` if invest-thesis needs Python beyond bash. Recommendation: try bash-first (existing stub feels bash-friendly); only Python if scoring math gets unwieldy.
- `scripts/lib/invest_backtest.py` (Python definite -- yfinance + pandas + numpy).
- New folder: `K2Bi-Vault/raw/backtests/` (cycle 5 creates, with `index.md`).
- Updated `wiki/reference/glossary.md` (cycle 1 may auto-stub new terms).
- Small addition to `scripts/lib/invest_ship_strategy.py` --approve-strategy Step A: bear-case freshness check + backtest sanity-gate check (cycle 2 + cycle 5).

### 9.4 Deferred (Phase 4+)

- NBLM-grounded thesis path (5-criterion exit gate from nblm-mvp).
- Auto-screening from feed data.
- Multi-round adversarial debate.
- Walk-forward backtest.
- Auto-detection of regime.
- Multi-strategy portfolio backtest.
- invest-earnings, invest-sector standalone skills.

---

## 10. Kickoff prompt template for Bundle 4 cycle sessions

Each Bundle 4 cycle session gets its own kickoff prompt, similar to Bundle 3's pattern. Architect produces per-cycle prompts on demand. The general template:

```
You are starting Bundle 4 cycle <N> of 6 -- <skill name> (m2.<XX>).

State coming in:
- Bundle 3 closed at `9f14dca`. 10 of 22 Phase 2 milestones shipped. 618 tests baseline.
- Bundle 4 spec at `~/Projects/K2Bi/proposals/2026-04-19_k2bi-bundle-4-decision-support-spec.md`.
- Architect Q1-Q10 pre-answered. Do not re-ask.
- Cycles already shipped in Bundle 4: <list with commit SHAs>

Read these sections of the Bundle 4 spec:
1. §0 Prerequisites (esp. §0.3 architectural constraints)
2. §2.<N> data contract for this skill (LOCKED -- match exactly)
3. §3.<N> per-skill architecture
4. §4 cross-cutting patterns (apply ALL)
5. §6 test matrix row for this skill
6. §8 review discipline

Then read (mandatory before drafting):
- ~/Projects/K2Bi-Vault/wiki/insights/insight_bundle-3-retro.md
- ~/Projects/K2Bi-Vault/wiki/planning/trade-skills-reference-pass.md  (mandatory for m2.11 + m2.13; recommended for others)
- The existing stub at .claude/skills/invest-<skill>/SKILL.md
- scripts/lib/strategy_frontmatter.py (use this; don't author parallel YAML helpers)

CYCLE SCOPE:
- Implement the skill per §3.<N>.
- Cover every test matrix row from §6.
- Apply all 10 cross-cutting patterns from §4.

Preemptive design decisions:
- [skill-specific: e.g. for invest-thesis: Ahern 4-phase body skeleton + 5-dim scorecard + Action Plan; NO position-sizing in prompts; NBLM is Phase 4]

Review discipline: MiniMax iterative + ONE Codex final via codex-companion.mjs background + Monitor tail. Stop rule: 3+ Codex rounds on same surface = escalate.

Ship criteria:
- All test matrix rows have passing tests.
- MiniMax clean (or only-P3) + 1 Codex pass clean/P2/P3-accepted.
- Full suite green (618 baseline + new tests).

Commit subject: <per-cycle subject from §7>
Trailer: Co-Shipped-By: invest-ship

Do NOT start the next cycle in this session. Handoff after ship.

Escalate to K2B-side architect on any P1 not pre-answered by Q1-Q10.

First action: read the spec sections above + the prior-art pass. Then implement.
```

Architect produces specific cycle prompts on Keith's request.

---

## 11. Self-review (architect's own)

**Spec coverage check:** Bundle 4 = m2.11 + m2.12 + m2.13 + m2.14 + m2.15. Each has §2 contract + §3 architecture + §6 test matrix row + §7 ship cycle + §10 prompt template hook.

**Placeholder scan:** no "TBD", "implement later", "appropriate handling". Every data shape locked. Every cross-cutting pattern named.

**Type consistency:** scorecard composites consistently 0-100 with sub-dimensions. Enums consistent: `bull|neutral|bear` (thesis), `VETO|PROCEED` (bear-case), `A|B|C|D|F` (screen rating), 7-class regime enum, `passed|suspicious` (backtest gate). NO field-name overlap across skills.

**Bundle 3 retro learnings applied:**
- ✅ Lock data contracts in §2 (5 schemas, all locked).
- ✅ Cumulative MiniMax sweep at close (cycle 6).
- ✅ Stop-rule 3+ Codex rounds (in §8 + every cycle prompt template).
- ✅ MiniMax-primary + Codex final-gate (§8).
- ✅ Plan-scoped MiniMax review BEFORE paste-to-implementer (this spec gets one before any cycle ships).
- ✅ Per-skill Codex final-gate (Q6 answer).
- ✅ trade-skills-reference-pass mandatory pre-read (§0.2).

**Known gaps + intentional deferrals:**
- yfinance dep verification (cycle 5 picks this up; if missing from requirements.txt, single-line addition during cycle 5).
- glossary.md may not exist; cycle 1 auto-stubs if absent (no separate cycle).
- File-lock guard for concurrent backtest + approval-gate scan deferred to Bundle 6 (per Bundle 3 Q11). Bundle 4 mitigates via atomic-write enforcement on backtest output (§2.5) + defensive empty-file skip in approval scan (§3.5).

**Bundle 4 closure adds Phase 2 progress: 10/22 → 15/22.** After Bundle 4: Bundle 5 (m2.9 + m2.10 + m2.18, comms + journaling) unblocks; Bundle 6 (m2.19 + m2.20 + m2.21 + m2.22, ops + quality gates including pm2 daemons + IB Gateway on Mac Mini) follows.

---

## Ready-state check before paste-to-K2Bi

- [x] §0 prerequisites identified (Bundle 3 closure verified, prior-art pre-read mandatory).
- [x] §2 data contracts locked (5 schemas).
- [x] §3 per-skill architecture (5 sections).
- [x] §4 cross-cutting patterns (10 enforced).
- [x] §5 architect questions Q1-Q10 pre-answered.
- [x] §6 test matrix per skill (zero empty cells).
- [x] §7 ship order with parallelization options.
- [x] §8 review discipline (per-cycle + cumulative-bundle sweep).
- [x] §9 dependencies (inherited Bundle 3 surfaces explicit; do-not-duplicate list).
- [x] §10 kickoff prompt template.
- [x] **Plan-scoped MiniMax review of this spec passed.** Run 2026-04-19 via `scripts/minimax-review.sh --scope plan`. 4 findings: 3 HIGH + 1 MEDIUM. All integrated:
    - R1 (backtest approval-gate underspecified) → §3.5 LOCKED schema with full algorithm + 6 locked decisions + override section format
    - R2 (invest-thesis bash-vs-Python deferred) → §3.1 LOCKED to Python via `scripts/lib/invest_thesis.py`
    - R3 (race condition on backtest scan) → §2.5 atomic-write concurrency policy (tempfile + fsync + os.replace via existing helper) + §3.5 defensive empty-file skip
    - R4 (band-definition stability not versioned) → §2.3 schema adds `band_definition_version: 1` + `previous_score_band_version` + `score_delta: null` on version mismatch + body warning
    - Next-step recommendation (mid-bundle drift check) → §8 added as no-cycle-number checkpoint between cycles 4 and 5
    - Archive: `.minimax-reviews/2026-04-19T11-56-36Z_plan.json`
- [ ] **Per-cycle prompts written** -- next, on Keith's request per cycle.
