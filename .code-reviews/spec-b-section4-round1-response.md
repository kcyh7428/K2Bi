# Spec B §4 Round 1 Kimi Disposition

Round 1 review log: `.code-reviews/2026-05-11T02-48-47Z_3d3c79.log`

Round 2 review log: `.code-reviews/2026-05-11T03-03-46Z_54f4c3.log`

Round 3 review log: `.code-reviews/2026-05-11T03-11-44Z_b297a8.log`

Round 4 review log: `.code-reviews/2026-05-11T03-24-09Z_14005e.log`

Kimi round 1 verdict: NEEDS-ATTENTION.

Codex disposition: five findings accepted and fixed at `7fcee67`; one finding rejected as a process invariant outside the recovery-stop helper.

## Finding 1

Status: ACCEPTED - fixed at `7fcee67`

Claim: Standalone recovery STP timeout could leave an unconfirmed live stop that a retry duplicates.

Concrete code anchor:

- `execution/connectors/ibkr.py:973`: `if not getattr(trade.order, "orderId", 0):`
- `execution/connectors/ibkr.py:975`: `self._ib.cancelOrder(trade.order)`
- `execution/connectors/ibkr.py:992`: `if not getattr(trade.order, "permId", 0):`
- `execution/connectors/ibkr.py:994`: `self._ib.cancelOrder(trade.order)`

Safety reasoning:

The accepted fix cancels a partially submitted standalone stop on orderId or permId timeout before raising. That keeps the recovery-only path from silently leaving an orphan STP when IBKR assigns partial state too late for the connector's confirmation loop.

Test coverage:

- `tests/test_engine_child_stop_attachment.py:208` asserts a permId timeout calls `cancelOrder()` for the partially submitted standalone stop.

## Finding 2

Status: ACCEPTED - fixed at `7fcee67`

Claim: The recovery token was exported via `__all__`.

Concrete code anchor:

- `execution/engine/recovery_context.py:6`: `_RECOVERY_CONTEXT_TOKEN = object()`
- `execution/engine/recovery_context.py:15`: `__all__ = ["is_recovery_context_token"]`
- `execution/strategies/runner.py:353`: `if not is_recovery_context_token(recovery_context):`

Safety reasoning:

The token remains module-private by convention and is no longer in `__all__`. The normal runner imports only the validator function, not the token object. The recovery module imports the token for the recovery path.

Test coverage:

- `tests/test_engine_child_stop_attachment.py:351` asserts `_RECOVERY_CONTEXT_TOKEN` is not in `recovery_context.__all__` and not imported by `strategy_runner`.

## Finding 3

Status: ACCEPTED - fixed at `7fcee67`

Claim: Summing multiple broker positions for the same symbol could pass drift check on a blended position.

Concrete code anchor:

- `execution/strategies/runner.py:375`: `matching_positions = [`
- `execution/strategies/runner.py:381`: `if len(matching_positions) != 1 or actual_qty != qty:`
- `execution/strategies/runner.py:392`: `"protective_stop_attach_refused_drift",`

Safety reasoning:

The helper now requires exactly one nonzero broker position record for the symbol and exact qty match. Multiple same-symbol records are treated as drift even when their sum equals the requested qty.

Test coverage:

- `tests/test_engine_child_stop_attachment.py:295` asserts two G position records totaling 71 are refused and journal `matching_position_count: 2`.

## Finding 4

Status: REJECTED

Claim: The recovery-only helper should assert the systemd engine is inactive/disabled or inspect `.killed`.

Concrete code anchor:

- `execution/strategies/runner.py:353`: `if not is_recovery_context_token(recovery_context):`
- `execution/strategies/runner.py:370`: `raise RecoveryContextError(`
- `execution/engine/recovery_context.py:6`: `_RECOVERY_CONTEXT_TOKEN = object()`
- `scripts/wiki-log-append.sh` recorded §0 after `7fcee67`: `k2bi-engine inactive+disabled`

Safety reasoning:

The engine-OFF and `.killed` rules are operator/process invariants for Spec B shipping, not responsibilities of a pure broker repair helper in `execution/strategies/runner.py`. Baking systemd or vault kill-file reads into this helper would create a host-specific dependency in a strategy module and would not be testable in the same connector harness. The runtime guard for this helper is the private recovery-context token; process verification is handled by the required §0 gateway-query audit before and after commits. No code in §4 re-enables the engine or touches `.killed`.

Existing test coverage:

- `tests/test_engine_child_stop_attachment.py:251` and `tests/test_engine_child_stop_attachment.py:333` assert missing or forged recovery context refuses before any broker call.
- `tests/test_engine_child_stop_attachment.py:345` asserts the normal engine cycle methods do not call `attach_protective_stop_to_existing_position`.

Why this is not a §4 named-bug gap:

§4's named bug is standalone STPs from normal BUY submit and unsafe recovery stop repair. Engine service state is governed by §0 and the operator-only engine re-enable rule, not by this helper.

## Finding 5

Status: ACCEPTED - fixed at `7fcee67`

Claim: C2 lacked an explicit IBKR bracket-order documentation reference.

Concrete code anchor:

- `tests/test_engine_child_stop_attachment.py:193`: `def test_c2_parent_cancel_relies_on_broker_child_auto_cancel`
- `tests/test_engine_child_stop_attachment.py:199`: `https://interactivebrokers.github.io/tws-api/bracket_order.html`
- `tests/test_engine_child_stop_attachment.py:203`: `self.assertIn("child.parentId = parent_trade.order.orderId", source)`

Safety reasoning:

C2 is intentionally a documented-assumption test because IBKR child auto-cancel behavior is broker-side bracket semantics. The test now cites the IBKR bracket-order reference and pins our code to parentId plus final child transmit behavior while asserting we do not manually cancel child orders in the normal bracket path.

## Finding 6

Status: ACCEPTED - fixed at `7fcee67`

Claim: New §4 journal event payloads had no field-level validation.

Concrete code anchor:

- `execution/journal/schema.py:400`: `def validate_protective_stop_attached_payload`
- `execution/journal/schema.py:412`: `def validate_protective_stop_attach_refused_drift_payload`
- `execution/journal/schema.py:427`: `def validate_protective_stop_attach_refused_no_context_payload`
- `execution/strategies/runner.py:361`: `validate_protective_stop_attach_refused_no_context_payload(payload)`
- `execution/strategies/runner.py:390`: `validate_protective_stop_attach_refused_drift_payload(payload)`
- `execution/strategies/runner.py:421`: `validate_protective_stop_attached_payload(payload)`

Safety reasoning:

The runner now validates all three §4 payload shapes before appending them. This preserves the journal's cheap top-level `validate()` contract while keeping field-level validation at the event construction site, matching the existing `orphan_stop_adopted` pattern.

Test coverage:

- `tests/test_engine_child_stop_attachment.py:361` asserts malformed attached payloads are rejected by the new schema helper.

## Current Verification

Focused verification after fixes: `pytest tests/test_engine_child_stop_attachment.py tests/test_order_type_e2e.py tests/test_ibkr_timeout.py tests/test_journal.py tests/test_journal_v2.py -q` -> `89 passed`.

Post-fix §0 recheck after `7fcee67`: `2026-05-11T02:57:02.615023+00:00`, G qty 71, avgCost 32.7840873, exactly one G open STP SELL qty 71 @ 30, `k2bi-engine` inactive and disabled.

## Round 2 Finding 1

Status: ACCEPTED - fixed at `3175590`

Claim: Fractional broker position qty could be truncated by `int(position.qty)` in the recovery drift guard.

Concrete code anchor:

- `execution/strategies/runner.py:375`: `matching_position_qtys = [`
- `execution/strategies/runner.py:376`: `Decimal(str(position.qty))`
- `execution/strategies/runner.py:381`: `actual_qty = sum(matching_position_qtys, Decimal("0"))`
- `execution/strategies/runner.py:383`: `if len(matching_position_qtys) != 1 or actual_qty != expected_qty:`

Safety reasoning:

The guard now compares exact `Decimal` quantities and journals `actual_qty` as a decimal string. Fractional broker quantities are drift, not silently narrowed to an integer.

Test coverage:

- `tests/test_engine_child_stop_attachment.py:318` asserts `qty=Decimal("70.9")` refuses a requested `qty=70` and journals actual_qty `"70.9"`.

## Round 2 Finding 2

Status: ACCEPTED - fixed at `3175590`

Claim: `_require_int` should only accept native Python `int` values for §4 journal payloads.

Concrete code anchor:

- `execution/journal/schema.py:375`: `def _require_int`
- `execution/journal/schema.py:379`: `if type(value) is not int:`
- `execution/journal/schema.py:382`: `return value`

Safety reasoning:

The schema helper now rejects `bool` and non-native integer scalar types by exact type. This keeps §4 payload integers JSON-safe before append.

Test coverage:

- Existing §4 tests exercise valid native ints through attach/refusal payload construction.
- `tests/test_engine_child_stop_attachment.py:384` keeps malformed attached payload validation pinned.

## Round 2 Verification

Focused verification after round-2 fixes: `pytest tests/test_engine_child_stop_attachment.py tests/test_order_type_e2e.py tests/test_ibkr_timeout.py tests/test_journal.py tests/test_journal_v2.py -q` -> `90 passed`.

Post-round-2 §0 recheck after `3175590`: `2026-05-11T03:06:58.584855+00:00`, G qty 71, avgCost 32.7840873, exactly one G open STP SELL qty 71 @ 30, `k2bi-engine` inactive and disabled.

## Round 3 Finding 1

Status: ACCEPTED - fixed at `574b881`

Claim: The recovery-only helper should explicitly reject non-positive requested qty and short broker positions before submitting a SELL stop.

Concrete code anchor:

- `execution/strategies/runner.py:374`: `if qty <= 0:`
- `execution/strategies/runner.py:405`: `if len(matching_position_qtys) != 1 or actual_qty != expected_qty or actual_qty <= 0:`
- `execution/strategies/runner.py:416`: `"protective_stop_attach_refused_drift",`
- `execution/journal/schema.py:437`: `if expected == 0:`

Safety reasoning:

The helper now refuses a non-positive requested qty before broker position lookup, and it refuses any matching broker position whose exact Decimal qty is non-positive. The recovery-only path therefore cannot submit a SELL stop for a short or flat position.

Test coverage:

- `tests/test_engine_child_stop_attachment.py:346` asserts a broker short position `qty=-71` is refused and no stop is submitted.
- `tests/test_engine_child_stop_attachment.py:366` asserts a requested `qty=-71` is refused and no stop is submitted.

## Round 3 Verification

Focused verification after round-3 fix: `pytest tests/test_engine_child_stop_attachment.py tests/test_order_type_e2e.py tests/test_ibkr_timeout.py tests/test_journal.py tests/test_journal_v2.py -q` -> `92 passed`.

Post-round-3 §0 recheck after `574b881`: `2026-05-11T03:17:06.001417+00:00`, G qty 71, avgCost 32.7840873, exactly one G open STP SELL qty 71 @ 30, `k2bi-engine` inactive and disabled.

## Round 4 Finding 1

Status: REJECTED

Claim: Standalone stop orderId/permId confirmation uses fragile 50 x 0.1s polling loops, can falsely timeout under event-loop pressure, may double-wrap timeout exceptions, and should add recovery idempotency before retries.

Concrete code anchor:

- `execution/connectors/ibkr.py:967`: `try:`
- `execution/connectors/ibkr.py:969`: `for _ in range(50):`
- `execution/connectors/ibkr.py:972`: `await asyncio.sleep(0.1)`
- `execution/connectors/ibkr.py:981`: `await self._await_parent_terminal(`
- `execution/connectors/ibkr.py:984`: `raise BrokerRejectionError(`
- `execution/connectors/ibkr.py:1000`: `await self._await_parent_terminal(`
- `execution/connectors/ibkr.py:1003`: `raise BrokerRejectionError(`
- `execution/connectors/ibkr.py:1069`: `def _classify_and_raise(self, exc: Exception, *, phase: str) -> None:`
- `execution/connectors/ibkr.py:1075`: `if isinstance(exc, ConnectorError):`
- `execution/connectors/ibkr.py:1079`: `raise exc`
- `execution/strategies/runner.py:429`: `ack = await connector.submit_standalone_stop_order(`
- `execution/strategies/runner.py:437`: `payload = {`
- `execution/strategies/runner.py:446`: `journal.append(`

Safety reasoning:

The stated event-loop-pressure failure mechanism is not how this code behaves. These loops are iteration-count loops with `await asyncio.sleep(0.1)`, not wall-clock deadline loops. If the event loop is congested or paused, the coroutine does not burn through the 50 iterations while paused. It resumes later, checks the broker-mutated `trade.order.orderId` or `trade.order.permId`, and succeeds if the ID has appeared. Event-loop delay lengthens real elapsed time; it does not make the loop expire before the coroutine resumes.

The double-wrap claim is also false for current code. `_classify_and_raise()` explicitly preserves existing `ConnectorError` subclasses, including `BrokerRejectionError`, by checking `isinstance(exc, ConnectorError)` and re-raising the original typed exception.

The retry/idempotency recommendation is a broader recovery redesign, not a §4 named-bug gap. §4's named bug is preventing normal BUY submits from creating standalone STPs and adding a guarded recovery-only verb for exact broker-position repair. Existing architect-scoped connector policy already documents write-path polling as an explicit deferred limitation in `execution/connectors/ibkr.py:100-109`; §4 did not reopen a general write-path wait/callback/idempotency redesign.

Existing test coverage:

- `tests/test_engine_child_stop_attachment.py:208` asserts a standalone stop permId timeout cancels the partial order before propagating `BrokerRejectionError`.
- `tests/test_engine_child_stop_attachment.py:386` asserts exact-position recovery attachment journals success only after `submit_standalone_stop_order()` returns an ack.
- `tests/test_engine_child_stop_attachment.py:447` asserts malformed attached payloads are rejected by schema validation.

Why this is not a §4 named-bug gap:

The current §4 implementation fails closed on standalone stop confirmation timeout by cancelling the partial order and raising a typed connector error. The remaining question of replacing write-path polling with callback-driven waits or adding retry idempotency spans the connector's broader write path and recovery orchestration. That is outside §4's scoped child-stop attachment defense and should not block §4 closure.

## Round 4 Verification

Kimi round-4 verdict: NEEDS-ATTENTION on one rejected tactical finding. The rejection above is Codex self-judged under the operator-authorized round-cap process.

Latest focused verification remains the round-3 post-fix run: `pytest tests/test_engine_child_stop_attachment.py tests/test_order_type_e2e.py tests/test_ibkr_timeout.py tests/test_journal.py tests/test_journal_v2.py -q` -> `92 passed`.

Latest full verification remains the round-3 post-fix run: `pytest tests/ -q` -> `1569 passed, 1 skipped, 2 warnings, 33 subtests passed`.

Post-round-4 §0 state will be refreshed after the closure commit because this disposition document will land as a new commit.
