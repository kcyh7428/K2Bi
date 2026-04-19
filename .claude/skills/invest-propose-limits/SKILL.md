---
name: invest-propose-limits
description: Draft a proposed validator config change (position_size, trade_risk, leverage, market_hours, instrument_whitelist) and write it to review/strategy-approvals/ for Keith's explicit approval. Claude CANNOT edit execution/validators/config.yaml directly -- only this skill produces the delta, and only Keith lands it via /invest-ship. Use when Keith says "propose new limits", "widen position size", "allow ticker X", "tighten risk", "/propose-limits".
tier: Portfolio Manager
phase: 2
status: shipped
---

# invest-propose-limits

Draft a validator-config change and queue it in `review/strategy-approvals/` for Keith's explicit approval. You produce the proposal file; `/invest-ship --approve-limits <path>` applies it to `execution/validators/config.yaml`.

## When to trigger

Explicit: `/propose-limits`, "propose new limits", "propose a limits change", "draft a limits proposal", "/invest propose-limits".

Natural language:
- "widen position size cap to 25%"
- "tighten daily risk to 3%"
- "widen per-trade risk to 2%"
- "widen leverage to 2x"
- "allow AAPL on the whitelist"
- "remove SPY from the whitelist"
- "drop market_hours guard" / "allow pre-market trading" / "allow after-hours"

## Hard rule (spec §5.4)

**This skill NEVER writes to `execution/validators/config.yaml`.** The module's `_atomic_write` refuses any target path whose last two path parts are `validators/config.yaml`. Do not try to work around this. Only `/invest-ship --approve-limits <path>` lands config edits, and only after Keith confirms.

Pre-commit Check C (cycle 4) is a backstop, not a primary gate: any commit that touches `config.yaml` without a paired `proposed -> approved` limits-proposal in the same commit is rejected at the hook layer. This skill's refusal to write config.yaml is the first line of defence.

## Supported matrix

| rule                  | change_types    | target field(s)                                  |
| --------------------- | --------------- | ------------------------------------------------ |
| `position_size`       | widen, tighten  | `max_trade_risk_pct`, `max_ticker_concentration_pct` (auto-picked by magnitude or explicit keyword) |
| `trade_risk`          | widen, tighten  | `max_open_risk_pct`                              |
| `leverage`            | widen, tighten  | `max_leverage` (widen past 1.0 also flips `cash_only` to `false`) |
| `market_hours`        | widen, remove   | `allow_pre_market` and/or `allow_after_hours`    |
| `instrument_whitelist`| add, remove     | `symbols` list                                   |

Anything outside this matrix returns a `Clarification` -- surface the question to Keith, wait for his restatement, re-parse.

## Pipeline (what this skill does)

All steps live inside `scripts/lib/propose_limits.py`. The skill body just drives the CLI.

### Step 1 -- Parse Keith's natural-language ask

```bash
python3 -m scripts.lib.propose_limits \
  --repo "$(git rev-parse --show-toplevel)" \
  parse \
  --text "<Keith's exact words>"
```

Output JSON with `"kind"`:

- `"parsed"` -- resolved to a unique (rule, change_type, field, before, after). Continue to Step 2.
- `"clarification"` -- ambiguous or out-of-matrix. Surface `question` + `options` to Keith verbatim, wait for his restatement, re-invoke Step 1 with the clarified text.

Common clarifications:
- `"trade risk"` without qualifier → position_size.max_trade_risk_pct vs. trade_risk.max_open_risk_pct
- "tighten" / "widen" without a target value → ask for the number
- "widen" with a smaller target than current → confirm direction
- "add/remove" without a ticker → ask for the ticker

Re-prompt pattern: prepend Keith's clarification to the original text and re-parse. Example: original `"tighten risk"`, clarification `"daily"`, re-parse as `"tighten daily risk to 3%"` (you also need the magnitude from Keith).

### Step 2 -- Confirm rationale

Before writing the file, ask Keith for the rationale in one short sentence. This goes under `## Rationale (Keith's)` in the proposal. Example prompt:

> Widening position_size.max_ticker_concentration_pct 20% → 25%. What's the rationale? (one short sentence for the proposal file)

Keep the rationale concise (one sentence, < 200 chars). Longer reasoning can live in the daily journal.

### Step 3 -- Write the proposal file

```bash
python3 -m scripts.lib.propose_limits \
  --repo "$(git rev-parse --show-toplevel)" \
  write \
  --text "<Keith's exact words>" \
  --rationale "<Keith's one-sentence reason>" \
  --date "$(date -u +%Y-%m-%d)"
```

The `write` subcommand:
1. Re-parses the NL (same logic as Step 1) -- deterministic.
2. Reads current `execution/validators/config.yaml` (read-only).
3. Derives the filename slug: `<rule>-<change_type>[-<ticker>]` (spec §5 preemptive decision #3).
4. Computes the deterministic safety-impact paragraph per §5.2 (no LLM improvisation -- the four heuristic templates are hardcoded in `compute_safety_impact`).
5. Extracts the exact config.yaml slice that needs to change, builds the `## YAML Patch` before/after blocks that `/invest-ship --approve-limits` will consume.
6. Renders the full markdown per spec §2.3.
7. Atomically writes to `review/strategy-approvals/YYYY-MM-DD_limits-proposal_<slug>.md`.

Output JSON:

```json
{
  "kind": "written",
  "path": "/abs/path/.../2026-04-19_limits-proposal_position_size-widen.md",
  "slug": "position_size-widen",
  "rule": "position_size",
  "change_type": "widen",
  "field": "max_ticker_concentration_pct",
  "before": 0.2,
  "after": 0.25,
  "summary": "max_ticker_concentration_pct 0.2 -> 0.25",
  "safety_impact": "Increases per-ticker concentration cap from 20.00% to 25.00%..."
}
```

### Step 4 -- Log to wiki/log

```bash
scripts/wiki-log-append.sh \
  "/invest-propose-limits" \
  "<relative path to the proposal file>" \
  "proposed <slug>"
```

The helper is single-writer (one lock per call); don't `>>`-append directly.

### Step 5 -- Report to Keith

Report path + the safety_impact text + the next step. Example:

> Wrote `review/strategy-approvals/2026-04-19_limits-proposal_position_size-widen.md`.
>
> Safety impact: Increases per-ticker concentration cap from 20.00% to 25.00% (1.25x larger). A single-ticker adverse move now impacts a larger share of NAV; diversification across tickers is correspondingly reduced.
>
> Next step: review the file in Obsidian, then run `/invest-ship --approve-limits review/strategy-approvals/2026-04-19_limits-proposal_position_size-widen.md` to apply. Engine restart is required after the approval commit -- validators load at engine startup only (no hot-reload).

Do NOT commit the file. The commit lands during `/invest-ship --approve-limits` (spec §5.3 step D).

## Safety-impact heuristics (§5.2)

The four categories locked by the architect, each rendering deterministic text from the Python module:

1. **Widening size / risk / leverage caps** -- computes post-change max exposure vs. pre-change, frames in NAV %. For leverage specifically: flags that widening past 1.0 flips `cash_only` to `false` (MVP-safety departure).
2. **Adding to instrument_whitelist** -- framed as neutral on aggregate risk ("only ENABLES trading X; strategy approval still gates whether any order fires").
3. **Dropping / widening market_hours guard** -- RISKY framing: overnight / extended-hours fills on gap-ups or thin liquidity can blow through `stop_loss` levels.
4. **Tightening limits** -- safer-by-definition; flags that existing open positions are NOT force-closed by a tightening change (validators reject top-ups instead).

Two additional templates cover:
- **Removing from instrument_whitelist** -- tightens access; flags that existing strategy specs referencing the ticker will fail at next engine load.
- **Tightening numeric limits** -- generic "safer by definition" plus the same non-force-close caveat.

These are rule-based, NOT LLM-generated. The skill emits the text as-is from `compute_safety_impact(delta)`.

## Staleness

The `## YAML Patch` before-block is a byte-exact snapshot of the current `execution/validators/config.yaml` at propose-time. If Keith (or any process) edits `config.yaml` between propose and approve, the before-block no longer matches and the cycle-5 handler rejects the approval with a clear "before-block not found" error.

When that happens, the proposal file is stale and must be regenerated: re-run `/propose-limits` with the same ask; the new proposal snapshots the current config, and Keith approves that one instead. Do not hand-edit the stale proposal's YAML Patch -- rewriting the slice outside the skill loses the auto-derived before/after alignment.

Tell Keith this up-front when reporting the written proposal: "if config.yaml changes before you approve, the proposal goes stale -- re-run /propose-limits to regenerate."

## Multi-turn clarification pattern

When Step 1 returns `"kind": "clarification"`, the UX is:

1. Show Keith the `question` verbatim. Bullet the `options` if non-empty.
2. Wait for his one-line answer.
3. Construct a restatement:
   - If his answer is a rule name (e.g. `trade_risk`), compose `"<change_type> <answer>"`.
   - If his answer is a field name (e.g. `max_ticker_concentration_pct`), keep the rule and append the field keyword.
   - If his answer is a number or ticker, append to the original text.
4. Re-invoke `parse`. If still ambiguous, loop at most twice more; on a third ambiguous round, report to Keith that the ask is out-of-matrix and show him the supported matrix.

Never silently pick a default. If you can't resolve, say so and surface the supported matrix.

## Non-goals (out of scope, per stub + spec §5)

- Auto-rollback proposals (Phase 4 if limits adjustments prove error-prone)
- Proposal history view (Phase 4; each proposal is a self-contained file)
- Back-test of the proposed change against recent data (Phase 4 nice-to-have)
- Proposals that span multiple rules in one file (Phase 4 if batched-ops prove useful)
- Custom safety-impact text (Phase 4; MVP uses the four §5.2 templates only)

## File shape reference (spec §2.3)

```
---
tags: [review, strategy-approvals, limits-proposal]
date: YYYY-MM-DD
type: limits-proposal
origin: keith
status: proposed
applies-to: execution/validators/config.yaml
up: "[[index]]"
---

# Limits Proposal: <one-line summary>

## Change

```yaml
rule: position_size | trade_risk | leverage | market_hours | instrument_whitelist
change_type: widen | tighten | add | remove
field: <sub-field>          # populated by skill
ticker: <SYM>               # instrument_whitelist only
before: <current value>
after: <proposed value>
```

## Rationale (Keith's)

<one-sentence reason>

## Safety Impact (skill's assessment)

<deterministic paragraph from compute_safety_impact>

## YAML Patch

before:

```yaml
<exact bytes from current config.yaml>
```

after:

```yaml
<proposed replacement bytes>
```

## Approval

Pending Keith's review. Apply via `/invest-ship --approve-limits <path>`.
```

## Reference: cycle 5 handler integration

The file this skill produces is consumed by `scripts/lib/invest_ship_strategy.handle_approve_limits` (cycle 5). The handler validates:

- `type: limits-proposal` (exact)
- `status: proposed` (at approval time)
- `applies-to: execution/validators/config.yaml` (exact)
- `## Change` block is valid YAML with `rule`, `change_type`, `before`, `after`
- `rule` in `{position_size, trade_risk, leverage, market_hours, instrument_whitelist}`
- `change_type` in `{widen, tighten, add, remove}`
- `## YAML Patch` has exactly two fenced yaml blocks preceded by `before:` / `after:` labels
- Before-block appears exactly once in current `config.yaml`
- Before-block != after-block
- Patched config.yaml parses as valid YAML

This skill's `build_and_write` produces files that pass every one of these checks. The integration contract is covered by `tests/test_propose_limits.py::HandlerIntegrationTests` -- a regression there means the skill's output drifted from what the handler expects.

## Summary for Claude

1. Parse Keith's NL via `python3 -m scripts.lib.propose_limits parse --text "..."`.
2. If clarification, surface the question; loop on Keith's reply.
3. Ask Keith for a one-sentence rationale.
4. `python3 -m scripts.lib.propose_limits write --text "..." --rationale "..."` to generate the file.
5. `scripts/wiki-log-append.sh /invest-propose-limits <rel-path> "proposed <slug>"`.
6. Report path + safety_impact + "run /invest-ship --approve-limits <path>".
7. Do NOT commit the file. Do NOT touch `execution/validators/config.yaml`.
