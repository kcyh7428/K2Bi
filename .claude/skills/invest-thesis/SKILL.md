---
name: invest-thesis
description: Produce a ticker-level thesis using the Dave Ahern 4-phase framework (business model, competitive moat, financial quality, risks + valuation) plus the 5-dim thesis scorecard + fundamental sub-scoring + EV-weighted asymmetry block + Action Plan Summary. MVP runs one-shot via /research (NBLM-grounded variant lands in Phase 4 only if the experiment passes its 5 exit criteria). Output lands in wiki/tickers/<SYMBOL>.md frontmatter + body. Use when Keith says /thesis <SYMBOL>, "write a thesis on X", "generate thesis for X", "update thesis on X", or before a strategy is drafted that depends on a ticker.
tier: Analyst
routines-ready: true
phase: 2
status: mvp
---

# invest-thesis

Ticker-level research thesis with structured signals for the approval flow. Reads `/research` source material, applies the Dave Ahern 4-phase framework, scores the 5 thesis sub-dimensions + the 5 fundamental sub-dimensions from `~/Desktop/trading-skills/agents/trade-thesis.md` + `trade-fundamental.md`, and writes `wiki/tickers/<SYMBOL>.md` atomically.

**Input:** `<SYMBOL>` (required). Optional `--refresh` flag to force re-run against the 30-day freshness window. Optional `--type <equity|etf|pre_revenue|penny>` (defaults to `equity`).

**Output:** one file at `wiki/tickers/<SYMBOL>.md` with full frontmatter schema (spec §2.1) + body (Ahern 4-phase + Catalyst Timeline table + Entry Strategy + Exit Strategy + Asymmetry Analysis + Thesis Scorecard + Fundamental Sub-Scoring + Action Plan Summary).

## Activation

- `/thesis <SYMBOL>` or "write a thesis on <SYMBOL>"
- "refresh the thesis on <SYMBOL>" (adds `--refresh` flag)
- Auto-invoked before an order ticket when `invest-execute` detects a missing or stale thesis for the strategy's primary ticker.

## Pipeline

1. **Validate symbol + ticker_type**. Uppercase alphanumeric, optional `.exchange` or `.class` suffix. Special cases: `0700.HK`, `BRK.B`, `BRK.A`. Digits-only rejected.

2. **Check freshness.** Read existing `wiki/tickers/<SYMBOL>.md`. If `thesis-last-verified` is within 30 days of today AND `--refresh` is not set, exit 0 with an info message. No rewrite.

3. **Multi-turn source gathering (3-4 questions max).** Ask Keith for:
   - Latest 10-K URL.
   - Latest 10-Q URL.
   - Last 4 earnings-call transcript URLs.
   - Any investor deck URL or analyst rebuttal Keith wants in scope.
   Then invoke `/research --sources <urls>` to pull the content. `/research` is the Phase 2 MVP data ground -- NBLM promotion is Phase 4 conditional per [[nblm-mvp]].

4. **Run Ahern 4-phase analysis** on the gathered material:
   - **Phase 1: Business Model** -- revenue split, segments, pricing power, customer concentration, TAM.
   - **Phase 2: Competitive Position / Moat** -- moat type (network effects / switching costs / IP / brand / scale), market share, challengers.
   - **Phase 3: Financial Quality** -- revenue trajectory, margin trajectory, FCF conversion, balance sheet, capital allocation. Use company-type key-metric selector (tech/SaaS: ARR/NRR; retail: SSSG/inventory; banks: NII/provisioning; pharma: pipeline; industrial: backlog).
   - **Phase 4: Risks + Valuation** -- Forward P/E vs sector + 5Y history, bear-case drivers, geopolitical / regulatory / customer-concentration risks.

5. **Score the 5 thesis sub-dimensions** using the EXACT band definitions from `~/Desktop/trading-skills/agents/trade-thesis.md`:
   - `catalyst_clarity` (0-20) -- specificity + timing of the next catalyst.
   - `asymmetry` (0-20) -- R/R from current price.
   - `timeline_precision` (0-20) -- how soon the thesis resolves.
   - `edge_identification` (0-20) -- informational / analytical / behavioral / structural edge.
   - `conviction_level` (0-20) -- overall confidence after all factors considered.
   Composite `thesis_score` = sum (0-100).

6. **Score the 5 fundamental sub-dimensions** using the EXACT band definitions from `~/Desktop/trading-skills/agents/trade-fundamental.md`:
   - `valuation`, `growth`, `profitability`, `financial_health`, `moat_strength` (each 0-20).
   Stored as enrichment; NOT recomposed into `thesis_score`.

7. **Build the structured cases:**
   - `bull_reasons` (2-5 entries): each has `reason` + `evidence` (cite the source) + `impact_estimate` (quantified).
   - `bear_reasons` (2-5 entries): same shape.
   - `base_case`: scenario + probability + target_price.

8. **Build the EV-weighted asymmetry scenarios** (Bull / Base / Neutral / Bear). Probabilities MUST sum to 1.00. Targets are price levels. Asymmetry Score 1-10 with rationale.

9. **Build Entry Strategy:**
   - Primary entry (price + rationale: MA support / volume shelf / breakout level).
   - Optional secondary aggressive + conservative entries.
   - `entry_triggers` (list) -- conditions that MUST be met.
   - `entry_invalidation` (list) -- conditions that MUST NOT hold.

10. **Build Exit Strategy:**
    - `targets` = T1/T2/T3 with `level` + `price` + `sell_pct` + `reasoning`. `sell_pct` MUST sum to 100; default split 33/33/34 if you cannot distill distinct targets.
    - `stop`: initial price + rationale. Trailing rationale (common pattern: "after T1, move to breakeven").
    - `time_stop`: max hold period + reassessment triggers.
    - `exit_signals`: sell-regardless-of-price conditions.

11. **Build Catalyst Timeline:** multi-event table with `date` + `event` + `expected_impact` + `probability`. `next_catalyst` is the soonest-dated row.

12. **Call Python:** hand the structured input to `scripts/lib/invest_thesis.generate_thesis(thesis_input, vault_root, refresh, learning_stage, now)`. Python validates + writes the file atomically + appends any missing glossary stubs.

13. **Append to wiki/log.md** via `scripts/wiki-log-append.sh /thesis wiki/tickers/<SYMBOL>.md "thesis_score <N> band <band>"`. Single-writer discipline (direct `>>` is pre-commit blocked).

## Invocation shape for Claude

The skill is Python-backed; the bash caller is:

```bash
# 1. Collect sources from Keith (inline conversation)
# 2. Invoke /research --sources <urls>  and pipe output to Claude
# 3. Claude reasons the 5-dim scores + bull/bear/base + targets etc.
# 4. Claude invokes Python with the structured input:

python3 -c "
from pathlib import Path
from scripts.lib import invest_thesis as it
ti = it.ThesisInput(
    symbol='NVDA',
    ticker_type='equity',
    sub_scores=it.SubScores(16, 14, 15, 12, 16),
    fundamental_sub_scores=it.FundamentalSubScores(13, 16, 17, 15, 13),
    bull_reasons=[...],
    bear_reasons=[...],
    base_case=it.BaseCase('scenario', 0.55, 850.0),
    entry_exit_levels=it.EntryExitLevels(entry=700, stop=630, targets=[...], risk_reward_ratio=4.3),
    entry_triggers=[...],
    entry_invalidation=[...],
    exit_signals=[...],
    time_stop=it.TimeStop('6 months', [...]),
    recommended_action='bull',
    next_catalyst=it.NextCatalyst('Q4 2025 earnings', '2026-02-15', '...'),
    catalyst_timeline=[...],
    asymmetry_scenarios=[...],
    asymmetry_score=8,
    asymmetry_score_rationale='...',
    plain_english_summary='...',
    phase_1_business_model='...',
    phase_2_competitive_moat='...',
    phase_3_financial_quality='...',
    phase_4_risks_valuation='...',
    primary_entry_rationale='\$700 -- 50MA support + volume shelf',
    secondary_entry_aggressive='\$720 -- breakout',
    secondary_entry_conservative='\$660 -- 200MA pullback',
    initial_stop_rationale='below 200MA',
    trailing_stop_rationale='after T1, move to breakeven',
)
result = it.generate_thesis(ti, Path.home() / 'Projects/K2Bi-Vault')
print(result)
"

# 5. Log the action
if [[ <written> == True ]]; then
    scripts/wiki-log-append.sh /thesis "wiki/tickers/NVDA.md" "thesis_score 73 band good"
fi
```

In practice Claude composes the Python invocation inline rather than through a bash script; the above shows the shape of the call.

## Teach Mode

Reads `learning-stage:` from `K2Bi-Vault/System/memory/active_rules.md` (default: `novice` per CLAUDE.md default). Unknown values fall back to `advanced`.

- **novice**: prepend a 2-3 sentence plain-English preamble above `## Phase 1`.
- **intermediate**: no preamble for routine thesis runs (invest-thesis is routine; first-time concepts are rare after first run).
- **advanced**: no preamble.

The "How This Works" strategy-spec gate does NOT apply to thesis pages per spec Q5 (wiki/strategies/ scope only).

## Edge-case ticker types

- `--type etf`: body prepends an ETF Adaptation note. Phase 2 interpretation shifts to index methodology; Phase 3 interpretation shifts to aggregate underlying holdings.
- `--type pre_revenue`: body prepends a Pre-Revenue Adaptation note. Phase 3 substitutes runway + TAM + pipeline for traditional financial metrics.
- `--type penny`: body prepends a Penny Stock Warning (liquidity + bid-ask spreads + manipulation risk). Sizing is still validator-owned.

## Validator isolation (Q3 architectural constraint)

The Action Plan Summary `POSITION:` line is ALWAYS the literal string:

```
POSITION:      validator-owned (see config.yaml position_size cap)
```

Never compute a position size anywhere. Sizing is owned by `execution/validators/config.yaml`, enforced by the Python validator layer. The thesis proposes R/R + stop + targets only.

## Routines-Ready discipline (Analyst tier)

- **Stateless:** each run reads from vault + sources, writes to vault. No process-local state.
- **Vault-in / vault-out:** input is `wiki/tickers/<SYMBOL>.md` (if exists) + source URLs; output is the same file + glossary stubs.
- **Schedulable:** cron-safe (Phase 4+ "refresh-stale-theses" job -- scan for theses >30 days old and re-run).
- **JSON I/O:** frontmatter is fully YAML-serializable; all fields are structured.
- **Self-contained prompt:** no cross-context Claude state.

## Non-goals (Phase 2)

- NBLM-grounded thesis generation (Phase 4 conditional per [[nblm-mvp]]).
- Peer-comparison auto-generation (Phase 4 if needed).
- Auto-refresh on 10-Q drop (Phase 4, tied to `invest-feed` coverage).
- Earnings-cycle refresh mode (folded into this skill when Phase 3 surfaces the need; for now, add the earnings-cycle language manually during a `--refresh` run).

## Sources + references

- `~/Desktop/trading-skills/skills/trade-thesis/SKILL.md` -- 10-section structure, Edge Cases, Quality Standards.
- `~/Desktop/trading-skills/agents/trade-thesis.md` -- 5-dim scorecard with band definitions.
- `~/Desktop/trading-skills/agents/trade-fundamental.md` -- 5-dim fundamental scorecard.
- `K2Bi-Vault/wiki/planning/trade-skills-reference-pass.md` -- K2Bi-side adopt/adapt/skip mapping.
- Spec: `proposals/2026-04-19_k2bi-bundle-4-decision-support-spec.md` §2.1 + §3.1 + §4 + §5 (Q1-Q10) + §6 test matrix.
