# Spec B §1 Round 6 Review Response

Source review: `.code-reviews/2026-05-10T15-40-38Z_eeb77b.log`

## Finding 1 [HIGH]

Resolution: REJECT AS WRITTEN; REAL ADJACENT GAP FIXED.

Rationale: The finding says the pre-submit catch path has no journal entry. That is false. `test_g4b_pre_submit_position_query_fails_closed` calls `tick_once()` through the full `_submit()` caller path and asserts exactly one `cycle_skipped_position_query_failed` journal event with `abort_phase: "pre_submit_recheck"`, same trade_id as the proposal, no `order_submitted`, no pending order, and no broker submit. Adding a second journal append in the caller would double-count the same failure.

Adjacent fix: while checking the "alert pipeline" part of the finding, Codex found a real §1 gap: `scripts/invest_alert_lib.py` did not classify `cycle_skipped_position_query_failed`, so the event was journaled but not surfaced. Added Tier 1 alert classification and `test_position_query_failure_fires_alert`.
