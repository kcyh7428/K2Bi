# Spec B Section 2 Round 4 Review Response

Reviewer artifact: `.code-reviews/2026-05-11T00-41-13Z_bdd995.log`

Codex disposition: one genuine §2 defect accepted, three findings rejected as either scope expansion or unsafe relaxation of fail-closed replay.

## Finding 1

Status: REJECTED.

Kimi's duplicate-submission claim is not supported by the current code. `_submit()` journals `order_submitted` before mutating `_pending_orders`, and startup replay rebuilds `_pending_orders` from the strict journal reader. The review text also acknowledges that the journal-first interleaving is safe on restart.

The concrete risk Kimi eventually describes is stale journal-pending liveness after a crash that missed a later terminal broker event. That is not a §2 duplicate-submit bug. It is a broker reconciliation and recovery liveness concern. Clearing journal-pending state from broker absence would expand §2 beyond "journaled order_id dedup" and would require a separate recovery contract for absent, cancelled, inactive, filled, and partially filled broker states.

§2 remains fail-closed: if the journal has a submitted order with no terminal journal evidence, the engine skips a new submit. That is the intended safety direction.

## Finding 2

Status: REJECTED.

The recommendation would clear pending state from a legacy `order_filled` event that lacks both `remaining_qty` and usable `cumulative_filled_qty`. That is unsafe. A fill event without terminal quantity evidence can be a partial fill, and clearing pending state would allow a duplicate order while the prior order may still be live or partially open.

Round 3 already fixed malformed `order_submitted` replay by failing closed when `qty` is missing or invalid. For legacy fill replay, the current behavior is correct: clear only when `remaining_qty` is zero or when `cumulative_filled_qty` proves a full fill against the submitted quantity.

## Finding 3

Status: REJECTED.

The compatibility field `pending_order_id` remains one string. Round 3 added `pending_order_ids` and `pending_order_count`, so the full pending set is now visible in the skip payload. Chronological sorting would be nicer for operator ergonomics, but string ordering of the compatibility field is not an execution safety defect, and it does not hide the full set.

## Finding 4

Status: ACCEPTED.

Kimi correctly identified that startup recovery and §2 pending replay could use different clock reads near a UTC day boundary. The implementation now captures `startup_now` once in `_run_init()` and passes the same timestamp into recovery lookbacks, `_refresh_pending_orders_from_journal(now=startup_now)`, and `reconcile(now=startup_now)`.

Regression test: `test_d4h_startup_pending_replay_uses_init_clock`.
