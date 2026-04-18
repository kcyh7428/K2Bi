---
name: invest-propose-limits
description: Draft a proposed validator config change (position_size, trade_risk, leverage, market_hours, instrument_whitelist) and write it to review/strategy-approvals/ for Keith's explicit approval. Claude CANNOT edit execution/validators/config.yaml directly — only this skill produces the delta, and only Keith lands it via /invest-ship. Use when Keith says "propose new limits", "widen position size", "allow ticker X", "tighten risk", "/propose-limits".
tier: Portfolio Manager
phase: 2
status: stub
---

# invest-propose-limits (STUB -- Phase 2 build work)

Stub skill. Implementation is Phase 2 milestone 2.16. Specs below.

## MVP shape

**Input (natural language from Keith):** "widen position size cap to 25%", "allow AAPL on the whitelist", "tighten daily risk to 3%", etc. Multi-turn clarification if needed.

**Pipeline:**
1. Read current `execution/validators/config.yaml` via read-only.
2. Parse Keith's requested change into a structured delta:
   ```yaml
   rule: position_size | trade_risk | leverage | market_hours | instrument_whitelist
   change-type: widen | tighten | add | remove
   before: <current value>
   after: <proposed value>
   rationale: "<Keith's stated reason>"
   safety-impact: "<skill's honest assessment of what this does to the risk envelope>"
   ```
3. Write the proposal to `review/strategy-approvals/YYYY-MM-DD_limits-proposal_<slug>.md`:
   ```yaml
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
   <delta block>

   ## Rationale (Keith's)
   ...

   ## Safety Impact (skill's assessment)
   ...

   ## Approval

   Keith approves by running /invest-ship with this file staged. The ship
   workflow writes the validator change, restarts the engine, and flips
   this file's status to `approved`. Until then, no config change.
   ```
4. Append via `scripts/wiki-log-append.sh`.
5. Report to Keith the file path + next step: "Run /invest-ship to apply, or edit the proposal file to adjust before applying."

## Hard rule

This skill NEVER writes to `execution/validators/config.yaml`. Only `/invest-ship` does, and only after the proposal is staged. The pre-commit hook (Phase 0 fix #8 pattern extended) refuses any commit that edits `config.yaml` without a matching approved proposal in the same commit.

## Non-goals (not in Phase 2)

- Auto-rollback proposals (Phase 4 if limits adjustments prove error-prone)
- Proposal history view (Phase 4; for now each proposal is a self-contained file)
- Back-test of the proposed change against recent data (Phase 4 nice-to-have)

## Safety-impact heuristics

The skill emits its own assessment, not just echoes Keith's rationale. Examples:
- Widening position_size max_trade_risk_pct from 1% to 2% -- "Doubles max loss per trade. Combined with 5 open positions, max total risk at any time rises from 5% to 10% of NAV."
- Adding AAPL to instrument_whitelist -- "Neutral; this only ENABLES trading the ticker. Strategy approval still gates whether any order fires."
- Dropping market_hours guard -- "RISKY. Removes regular-hours enforcement. Overnight fills on gap-ups would be allowed. Phase 2 default is cash-only regular hours for a reason."
