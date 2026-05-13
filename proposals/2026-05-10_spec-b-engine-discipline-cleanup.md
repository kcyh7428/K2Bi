---
tags: [k2bi, spec-b, engine-discipline, post-mortem, architect-spec]
date: 2026-05-10
type: architect-spec
origin: k2b-architect
up: "[[Home]]"
for-repo: K2Bi
target-bundle: spec-b
incident-trigger: E-2026-05-09-001
milestones: [spec-b]
plan-review-required: true
---

# Spec B -- Engine Discipline Cleanup -- Architect Spec

**Status:** SPEC-ONLY. This document defines the Spec B implementation work; it does not ship any §1-§6 engine defenses by itself.

**Goal:** Land four P0 engine defenses that prevent E-2026-05-09-001 (G-strategy 11x cascade) from ever recurring, plus seven deferred review findings and the MasterClientId follow-up. After Spec B ships with red-then-green tests for all four defects, the operator runs `sudo systemctl enable --now k2bi-engine` to re-enable the engine on the Hostinger VPS. **NO new strategy approvals are processed by the engine until Spec B lands.**

**Trigger:** [[self_improve_errors]] /error E-2026-05-09-001 (DEVLOG `bbfb611`). On 2026-05-08 14:55:59-14:56:24 UTC, post-IBKR-credential-swap recovery, the engine fired 11 BUY orders for 71 shares of G in 25 seconds (all filled, 781 shares total at avg cost $32.8295) AND 11 standalone STP SELL 71 G @ $30 GTC orders bound to clientId=1. The orphan STPs were invisible to per-client `openTrades()` queries and were only surfaced by `reqAllOpenOrders()` after the architect cross-check. Real-money cascade-short risk on a $30 trigger: 71-share close + 11 orphan SELLs = SHORT 781 shares (~$23,400 USD).

**Architecture:** Four engine defenses layered into the existing cycle in `execution/engine/main.py` and the strategy-runner path in `execution/strategies/runner.py` (or wherever the BUY+STP fire path lives). Defenses are checked in this order on every cycle: position-aware skip → order dedup via journaled order_id → rapid-fire circuit breaker. The fourth defense (child-stop attachment via parentId) is a structural change to the order-fire path: STPs are no longer standalone; they bind to the parent BUY's `orderId`. A separate explicit verb handles "attach protective stop to existing position" for the engine-recovery path.

**Tech stack:** Python 3.12 (existing), `ib_async` for IBKR connectivity (existing), append-only JSONL journal (existing), pytest + unittest hybrid (existing test conventions). No new runtime deps.

**Bundle estimation:** 4 P0 engine modules + 1 IB Gateway config change (MasterClientId) + 7 deferred review findings (mostly doc + small code fixes per [[feature_k2bi-discipline-cleanup]]). Treat as Bundle 2-level care: this is engine code that placed live paper trades and has a documented P0 incident. Codex full review on every commit, no doc-only exception. Expected 6-10 Codex rounds across the bundle.

---

## 0. Prerequisite: re-verify before code lands

Before any Spec B code lands, operator runs `scripts/gateway-query.sh -f <snippet.py>` (clientId=99) and confirms:

- G qty = 71 shares
- Exactly 1 G open order by durable identity: SELL STP 71 @ $30 GTC, permId 499958748, side SELL, stop_price 30, qty 71, status PreSubmitted or Submitted (verified via `reqAllOpenOrders()` to catch orphans bound to other clientIds)
- No other G orders visible
- Engine systemd unit (`k2bi-engine.service`) reports `inactive` AND `disabled` at the moment of verify (not just "supposed to be")

**Cost basis tolerance (corporate-action sentinel):**

- avgCost change ≤ 0.5% from baseline ($31.3340875 ± $0.16/share; new window: $31.17 to $31.49): informational only, recorded in the §0 log line, does not block
- avgCost change > 0.5% with qty unchanged: STOP. File incident note. Investigate corporate action (split, reverse split, spin-off) before any code lands. The 0.5% threshold catches material economic exposure changes while ignoring routine T+2 commission settlement (~14bps observed on this position)

**Baseline re-anchor history:**

- $32.8295 from 2026-05-08 pre-incident state through 2026-05-11 regression test
- $32.0540875 from 2026-05-12 02:30 HKT through the 2026-05-12 regression round-trip (re-anchored by K2B-architect after planned regression-test round-trip; rationale in `K2Bi-Vault/wiki/insights/2026-05-12_spec-b-section8-1-section0-cascade-blockers.md` + K2B PM session transcript)
- $31.3340875 from 2026-05-13 onward (re-anchored after the 2026-05-12 regression round-trip changed G avgCost; tolerance remains ± $0.16/share)

**Known §0 limitations:**

IBKR `parentId` is a session/client-scoped `orderId` reference, not a durable broker-side identity. After IBC `ClosedownAt` daily reauth, the parent BUY's `orderId` is no longer live in any session, so the STP's parent pointer can appear as `0`. Cross-clientId reads from master clientId 99 can also return `parentId=0` because order access and modification are bound to the submitting clientId. `parentId` binding is verified at submission time only (§4 unit tests + Phase C of the regression test). The durable post-submission identity is permId + stop_price + qty + side + status.

Sources: [IBKR Order class reference](https://interactivebrokers.github.io/tws-api/classIBApi_1_1Order.html) defines `parentId` as the parent order's `orderId`, `orderId` as the API client's order id, and `permId` as the host order identifier; [IBKR active orders docs](https://interactivebrokers.github.io/tws-api/open_orders.html) state API orders are bound to the clientId that submitted them and describe permId to API orderId mapping; [IBKR bracket order docs](https://interactivebrokers.github.io/tws-api/bracket_order.html) show child orders setting `ParentId` to the parent orderId at submission; [ib_async order docs](https://ib-api-reloaded.github.io/ib_async/api.html#ib_async.order.Order) expose separate `orderId`, `clientId`, `permId`, `parentId`, and `parentPermId` fields.

**Freshness requirement:**

- §0 verification is valid for 24 hours from the verify timestamp, OR until the next NYSE regular-session open, whichever is sooner
- NYSE regular-session open means 09:30 America/New_York on the next exchange trading day. Extended-hours sessions do not extend §0 validity. If the next exchange trading day is a holiday, use the next regular-session open
- If §0 is older than the validity window at the time of any Spec B code commit, a fresh §0 re-verify is required before that commit lands
- Each fresh §0 verify writes a new `wiki/log.md` line via `scripts/wiki-log-append.sh` -- one line per verify, append-only audit trail. The line must include the broker state, observed avgCost, explicit no-orphans result, and `k2bi-engine.service inactive AND disabled` in the same line

**Verification timing:**

- Allowed any time after 2026-05-09 market close while engine remains disabled
- Re-runs are encouraged daily during Spec B implementation to keep the freshness window alive

**Failure handling:**

- If verification fails or the gateway is unreachable, pause Spec B work, file an incident note, resolve drift before code lands
- TODO (operational runbook, out of scope this spec): define escalation procedure when §0 cannot be resolved within 24h (owner: operator + architect; content: secondary operator + architect contact list). Track in [[feature_k2bi-discipline-cleanup]]. If §0 is unresolved for more than 24h, no further Spec B code commit lands until the incident is resolved or the runbook is written and invoked

**Scope:** operator session only, no code. Verification-only.

**Design rationale: operator-driven §0.** §0 remains operator-driven by architecture (L-2026-05-08-002). Do not replace it with a pre-commit hook, CI gate, or automated broker query. Codex must verify the operator-recorded `wiki/log.md` evidence before each Spec B code commit. Cost basis remains a prose-based corporate-action sentinel, not a config or JSON contract; the operator judges it in context with IBKR notices and incident history.

**2026-05-10 operator override on Kimi review.** Kimi review artifact `.code-reviews/2026-05-10T14-21-45Z_d21139.log` objected that §0 lacks a programmatic gate and that cost-basis tolerance is not machine-enforced. Operator override authorizes shipping this §0 amendment with those objections documented because both conflict with locked architect guardrails G1 and G2:

> Finding 1 (programmatic §0 gate via script/pre-commit/CI): conflicts with G1 (§0 stays operator-driven per L-2026-05-08-002). The §0 trust anchor is human-in-the-loop by design; replacing it with a machine creates circular trust (machine validates machine). The existing append-only audit trail in wiki/log.md is sufficient for downstream tooling without forcing the operator step itself to be automated.
>
> Finding 6 (automated cost-basis enforcement in gateway-query.sh): conflicts with G2 (cost-basis stays prose-based, operator-judged). Threshold is judgment-laden (corporate-action awareness, IBKR settlement context); JSON-config removes that layer. The §0 design rationale section already documents this.

**Spec vs implementation scope (Kimi round 4 clarification).** This commit is a spec-only amendment. Findings that request the actual `schema_version` field, `JournalReplayError` hierarchy, rapid-fire clear implementation, recovery-context token implementation, or tests are implementation-scope findings for their owning Spec B commits. They are not blockers for this §0 amendment commit, but the requirements are now explicitly written into §2, §3, and §4 and must land with the red-then-green code commits for those sections.

---

## 1. P0 Defense 1 -- Position-Aware Skip

### Defect

The engine cycle does not check "do I already have a position at or above the strategy's target qty?" before submitting the next BUY. After ~3 hours of rejected orders during the credential-swap session, the queued/repeated submissions drained as ACCEPTs once the gateway recovered. There was no "I already have 71 shares; do not buy more" gate.

### Fix

Before submitting any BUY for a strategy, query `ib.positions()` for the symbol. If existing qty is non-zero, do NOT submit and journal a `cycle_skipped_existing_position` event with `{strategy_id, symbol, current_qty, target_qty, position_state, cycle_id}`. `symbol` is normalized to uppercase for journal consumers. `position_state` is `at_target` when `current_qty >= target_qty` and `partial` when `0 < current_qty < target_qty`.

### Implementation

- Add `_check_position_at_target(strategy, ib_connector) -> bool` helper in `execution/strategies/runner.py` (or wherever `submit_order_for_strategy` lives).
- Call helper in cycle at the top of the BUY-fire path and again immediately before `ib.placeOrder()`.
- Journal a new event type `cycle_skipped_existing_position` (append to `journal/schema.py` + `journal/writer.py` allowed-events list).
- Position query uses the same connector already wired for validators -- no new connection.
- If `ib.positions()` returns an error, fail-closed: skip the submission, journal `cycle_skipped_position_query_failed` with `abort_phase` set to `decision` or `pre_submit_recheck`, and surface to alert pipeline as a Tier 1 alert.

### Known §1 limitations

Residual TOCTOU window (~50-100ms between second `get_positions()` and broker `placeOrder()`) is qualitatively different from the 5/8 incident root cause. The 5/8 incident was the ABSENCE of any position check, not a race condition. §1 closes the absence. The residual window is closed by Spec B's defense-in-depth: §2 (journaled order_id dedup) + §3 (rapid-fire circuit breaker). Hardening the residual window inside §1 alone (e.g. via client_order_id idempotency token) would either duplicate §2's dedup mechanism or force ib_async-side broker-API features that are out of §1 scope. §1 ship discipline: close the named bug, leave defense-in-depth to layered defenses. Architect override of Kimi finding 2; reviewer was technically correct but scope-bounded to §1, finding belongs to §2.

### Tests

`tests/test_engine_position_aware_skip.py`:

1. **G1 red-then-green: existing position blocks BUY.** Mock `ib.positions()` returns `[Position(symbol='G', qty=71)]`; strategy target qty=71. Assert `submit_order_for_strategy` returns without calling `ib.placeOrder()`. Assert journal contains `cycle_skipped_existing_position` event with `position_state=at_target`. Pre-fix this test must FAIL (the current path fires regardless of position).
2. **G2: zero position permits BUY.** Mock `ib.positions()` returns `[]`; assert `ib.placeOrder()` is called.
3. **G3: partial position still skips (STRICT semantics, locked).** Skip-at-or-above-target, NOT top-up-to-target. Locked because top-up creates a silent-recovery path: a partial fill + cancel becomes an invisible re-fire under the same approval. STRICT forces partial fills to surface as state requiring operator review. Defense-in-depth (§1+§2+§3+§4) prevents the 11x cascade either way, so STRICT loses nothing on safety and gains explicit lifecycle. Top-up is a separate future feature behind its own approval gate. Assert: `ib.positions()` returns `[Position(symbol='G', qty=30)]`, target=71 -> no submit, journal `cycle_skipped_existing_position` with `current_qty=30, target_qty=71, position_state=partial`. Note in test docstring: STRICT semantics, not top-up.
4. **G4: position query failure fails closed.** Mock `ib.positions()` raises during the decision-phase check; assert no `ib.placeOrder()` call, journal contains `cycle_skipped_position_query_failed` with `abort_phase=decision`.
5. **G4b: pre-submit position query failure fails closed.** Mock `ib.positions()` to return `[]` during the decision-phase check and raise during the pre-submit recheck; assert no `ib.placeOrder()` call, journal contains `cycle_skipped_position_query_failed` with `abort_phase=pre_submit_recheck` and the same trade_id as the proposal, and engine state is not poisoned.

---

## 2. P0 Defense 2 -- Order Dedup via Journaled order_id

### Defect

Each cycle submits independently with no awareness of prior in-flight submissions. The 11x cascade was 11 cycles each firing without checking whether the previous cycle's order was still pending broker reply.

### Fix

On every cycle, before submitting a BUY for a strategy, scan the journal (or an in-memory cycle-state cache rebuilt from journal at startup) for any prior submission for the same `(strategy_id, symbol)` whose `broker_order_id` has not yet received a terminal status (`Filled`, `Cancelled`, `Rejected`, `Inactive`). If any such order exists, skip this cycle and journal `cycle_skipped_pending_prior_submission` with `{strategy_id, symbol, pending_order_id, cycle_id}`.

### Implementation

- Add `_pending_orders_for_strategy(strategy_id, symbol, journal_reader) -> list[str]` helper in `execution/strategies/runner.py`.
- On engine startup, replay journal once to build `_pending_orders` in-memory map keyed by `(strategy_id, symbol) -> set[broker_order_id]`. Update on every `order_submitted` (add) and `order_terminal` (remove) event.
- Call helper at top of BUY-fire path, after position-aware skip (§1) but before validators (cheaper to skip earlier).
- New journal event types: `cycle_skipped_pending_prior_submission` and (if not already present) `order_terminal` with `{broker_order_id, terminal_status}` for clean state-machine reconstruction.
- **Journal replay validation (added per Kimi finding 6):** Add `CURRENT_SCHEMA_VERSION = 2` in `execution/journal/schema.py`. Every new journal line written by Spec B code includes `schema_version`. Pre-Spec-B legacy lines without `schema_version` must be migrated or explicitly normalized to `schema_version=1` before engine re-enable; unknown missing-version lines fail closed. On replay, each journal line must parse as valid JSON matching the schema in `journal/schema.py`. Unknown event types fail closed -- engine refuses to start, operator must confirm before adding the event type to the allowed list. Truncation detection (unexpected EOF mid-line) raises an alert and fails closed. Schema-version mismatch, missing version after the migration boundary, or version greater than `CURRENT_SCHEMA_VERSION` fails closed. The map rebuild test (D4) extends to cover: D4a malformed JSON line, D4b unknown event type, D4c truncated final line, D4d schema-version mismatch -- all four must fail closed with a specific exception (`JournalReplayError` with subclass per failure mode).

### Tests

`tests/test_engine_order_dedup.py`:

1. **D1 red-then-green: pending order blocks new submit.** Seed journal with `order_submitted` event for `(strategy_id=g-2026-05, symbol=G, broker_order_id=42)` and no terminal event. Run cycle; assert no `placeOrder` call, journal contains `cycle_skipped_pending_prior_submission`. Pre-fix this test must FAIL.
2. **D2: terminal-filled order does NOT block new submit.** Seed journal with `order_submitted` + `order_terminal{terminal_status=Filled}` for `broker_order_id=42`. Run cycle; assert `placeOrder` IS called (subject to position-aware skip in §1; for this test mock `ib.positions()` returns `[]`).
3. **D3: terminal-rejected order does NOT block new submit.** Same as D2 with `terminal_status=Rejected`.
4. **D4: in-memory map rebuild on engine startup.** Write a journal with mixed submit + terminal events; instantiate `_pending_orders` from journal replay; assert correct map state.
5. **D5: cross-strategy isolation.** Strategy A has pending order for G; strategy B's cycle for G is independent. Assert strategy B's path is NOT blocked by strategy A's pending order. (This means dedup is per-strategy, not per-symbol. Defect (a) position-aware skip is the per-symbol gate.)

---

## 3. P0 Defense 3 -- Rapid-Fire Circuit Breaker

### Defect

The engine submitted orders at ~2/second during the cascade, far above the documented 31-second cycle baseline. Either a tight retry loop on gateway-online detection or queue-drain semantics produced the rapid fire. There was no rate gate.

### Fix

Track order submission timestamps in an in-memory rolling window per `(strategy_id, symbol)`. If more than 3 orders are submitted within 60 seconds for the same key, halt strategy submission for that symbol and journal `circuit_breaker_tripped_rapid_fire` with `{strategy_id, symbol, submission_timestamps, cycle_id}`. Operator must clear via documented re-arm procedure (write `.rapid-fire-cleared.json` sentinel listing the `(strategy_id, symbol)` key + operator-signed timestamp; engine reads sentinel, removes entry from rapid-fire halt list, journals `circuit_breaker_cleared` event, deletes sentinel).

### Implementation

- Add `_rapid_fire_window: dict[tuple[str,str], deque[float]]` to engine state in `execution/engine/main.py`.
- Add `_rapid_fire_halted: set[tuple[str,str]]` (persisted across restarts via journal replay of `circuit_breaker_tripped_rapid_fire` minus `circuit_breaker_cleared`).
- On every `order_submitted`, append timestamp to deque; trim entries older than 60s; if `len > 3`, add to `_rapid_fire_halted`, journal trip event.
- At top of BUY-fire path, if `(strategy_id, symbol) in _rapid_fire_halted`, skip and journal `cycle_skipped_rapid_fire_halt`.
- Re-arm sentinel path: scan `~/Projects/K2Bi-Vault/System/.rapid-fire-cleared.json` once per cycle. A valid sentinel contains a unique `clear_nonce`, the target `trip_id`, operator timestamp, and the `(strategy_id, symbol)` keys to clear. Each `circuit_breaker_tripped_rapid_fire` event creates a monotonic `trip_id` per key. If a well-formed sentinel targets the active `trip_id`: (1) journal `circuit_breaker_cleared` FIRST with the same `trip_id` and `clear_nonce` (atomic write via existing journal helpers), (2) replay semantics remove matching keys from `_rapid_fire_halted` when a `circuit_breaker_cleared` event for the active `trip_id` is present, (3) remove listed keys from live halted state, (4) delete sentinel via `os.unlink`. Ordering matters: if engine crashes between (1) and (4), restart replay sees the cleared event and removes the halt; the next cycle sees the leftover sentinel nonce already consumed for that `trip_id`, journals `circuit_breaker_cleared_stale_sentinel_ignored`, and deletes it. If a sentinel's nonce or `trip_id` mismatches the active halt, fail closed, leave the halt in place, journal `circuit_breaker_cleared_stale_sentinel_rejected`, and alert the operator. The pre-Spec-B "os.unlink as atomic delete" framing was incorrect -- two-phase commit risk is mitigated by trip-scoped nonce correlation and journal-first ordering, not by atomicity of unlink.
- **Threshold rationale:** 3-orders-per-60s is conservative. Normal engine cadence is one submission per ~31s per strategy; even back-to-back same-symbol cycles cannot reach 3 in 60s without a defect. False-positive rate is ~zero in normal operation. Threshold is configurable in `execution/validators/config.yaml` under new `rapid_fire_circuit_breaker:` block (`max_orders_per_window: 3`, `window_seconds: 60`).

### Tests

`tests/test_engine_rapid_fire_breaker.py`:

1. **R1 red-then-green: 4 orders in 10s trips breaker.** Submit 4 orders for `(g-2026-05, G)` with timestamps 0, 1, 2, 3 seconds. Assert breaker trips on the 4th (or after; tight semantics in code), journal `circuit_breaker_tripped_rapid_fire`, subsequent cycle for same key journals `cycle_skipped_rapid_fire_halt`. Pre-fix this test must FAIL.
2. **R2: 3 orders in 60s does NOT trip breaker.**
3. **R3: 4 orders spread over 90s does NOT trip breaker** (rolling-window correctness).
4. **R4: cross-strategy isolation.** Strategy A trips breaker for G; strategy B's path for G is unaffected (different `strategy_id`).
5. **R5: re-arm sentinel clears halt.** Trip breaker; write valid sentinel; run cycle; assert halt cleared, journal `circuit_breaker_cleared`, sentinel deleted. Also assert the journal `circuit_breaker_cleared` event is written before the sentinel file is deleted (mock filesystem + journal in test order).
6. **R6: malformed sentinel is rejected.** Write garbage JSON; run cycle; assert halt NOT cleared, journal `circuit_breaker_cleared_malformed_sentinel`.
7. **R7: halt persists across engine restart.** Trip breaker; restart engine (fresh process); replay journal; assert `_rapid_fire_halted` rebuilt with the trip and the post-restart cycle still skips.
8. **R8: crash between journal-write and sentinel-delete is recoverable.** Simulate engine crash after journal write but before unlink. Restart engine; assert: (a) replay sees `circuit_breaker_cleared`, (b) the leftover sentinel is detected on next cycle and ignored as stale, (c) journal contains a `circuit_breaker_cleared_stale_sentinel_ignored` event, (d) sentinel file is then deleted.
9. **R9: consumed clear nonce cannot clear a new trip.** Trip breaker, clear `trip_id=1` with `clear_nonce=A`, then trip again for the same `(strategy_id, symbol)` as `trip_id=2`. Recreate the old sentinel with `trip_id=1` and `clear_nonce=A`; assert it is rejected as stale, the new halt remains, journal contains `circuit_breaker_cleared_stale_sentinel_rejected`, and the operator alert path is called.

---

## 4. P0 Defense 4 -- Child-Stop Attachment via parentId

### Defect

When the engine fired each BUY, it ALSO fired a fresh standalone STP SELL @ trigger price. The STP was NOT a child order on the parent BUY -- no `parentId` binding. Cumulative outcome: 11 BUYs + 11 standalone STPs targeting the same $30 trigger on the same 71-share quantity. If G triggered $30: first STP closes the 71-share position; remaining 10 STPs SELL into flat = SHORT 710 shares.

### Fix

The BUY-fire path no longer creates a standalone STP. Instead:

- BUY parent order is submitted with `transmit=False` (broker holds for child).
- STP child order is built immediately with `parentId = parent_buy.orderId`, `transmit=True` (broker now releases parent + child as a bracket).
- IBKR auto-cancels the child STP if the parent BUY cancels.
- The STP is bound 1:1 to the position created by that BUY. Cascade-short impossibility: if N parent BUYs fire (defense §1+§2+§3 prevent this, but defense in depth), only the corresponding N child STPs exist, each tied to its parent's filled qty.

For the engine-recovery path (where a pre-existing position needs a protective stop because the prior STP was cancelled mid-recovery), introduce an explicit verb `attach_protective_stop_to_existing_position(symbol, qty, stop_price, strategy_id, *, recovery_context)` in `execution/strategies/runner.py`. The `recovery_context` argument MUST be a module-private token object created for the recovery path. Preferred implementation: define `_RECOVERY_CONTEXT_TOKEN = object()` in `execution/engine/recovery.py`; if import cycles require it, place the token in a dedicated recovery-context module that only `execution/engine/recovery.py` imports for production use. Boolean flags are forbidden. Any missing, wrong, or forged token raises `RecoveryContextError`. Normal cycle path (`submit_order_for_strategy`) cannot call this verb accidentally; it would need an explicit import of the recovery token, which C5 static analysis must flag. This verb:

1. Asserts `ib.positions()` returns exactly `qty` for the symbol (refuse if drift).
2. Submits a standalone STP with `parentId=0` and `transmit=True`.
3. Journals `protective_stop_attached_to_existing_position` with `{strategy_id, symbol, qty, stop_price, broker_order_id}`.
4. Is invoked ONLY by the engine-recovery code path (`execution/engine/recovery.py`) or by an operator-side script -- never by the normal cycle.

### Implementation

- Modify `submit_order_for_strategy` (or equivalent) in `execution/strategies/runner.py` to use parent+child bracket pattern. Do not submit standalone STPs from this path.
- Add `attach_protective_stop_to_existing_position` helper in same file.
- Add `protective_stop_attached_to_existing_position` to journal schema.
- Engine-recovery code path (`execution/engine/recovery.py`): identify the call sites that previously submitted standalone STPs and migrate them to the new explicit verb. Each migration must include a position-qty assertion.
- Update `execution/engine/main.py` if it directly fires STPs anywhere (likely not, but audit).

### Tests

`tests/test_engine_child_stop_attachment.py`:

1. **C1 red-then-green: BUY fires with child STP bound by parentId.** Mock IBKR connector; trigger BUY-fire path for strategy with `entry=MKT` + `stop=30`. Assert two `placeOrder` calls: parent BUY with `transmit=False`, child STP with `parentId=<parent.orderId>, transmit=True`. Assert no third standalone STP is fired. Pre-fix this test must FAIL (the current path fires standalone STP).
2. **C2: parent BUY cancellation propagates to child STP.** Mock the connector to cancel the parent; assert IBKR's auto-cancel-on-parent-cancel behavior is what we rely on (test documents the assumption explicitly via comment + reference to IBKR API docs). This test asserts our code does NOT explicitly cancel the child -- broker handles it.
3. **C3: explicit verb refuses on position drift.** Call `attach_protective_stop_to_existing_position(symbol='G', qty=71, ...)`; mock `ib.positions()` returns 70; assert verb raises `PositionDriftError`, no `placeOrder` call, journal contains `protective_stop_attach_refused_drift`.
4. **C4: explicit verb succeeds on exact match.** Mock `ib.positions()` returns 71; assert standalone STP submitted, journal contains `protective_stop_attached_to_existing_position`.
5. **C5: normal cycle path does NOT call the explicit verb.** Static-grep test asserts `submit_order_for_strategy` source does NOT reference `attach_protective_stop_to_existing_position`. C5 + C6 together provide compile-time + runtime defense.
6. **C6: recovery-context runtime guard.** Call `attach_protective_stop_to_existing_position(...)` with no token and with a forged `object()` token; assert `RecoveryContextError` raised, no `placeOrder` call, journal contains `protective_stop_attach_refused_no_recovery_context`. Call from the recovery path with the real token; assert the guard permits the exact-qty attachment path. Pre-fix this test must FAIL (the current proposed signature has no token guard).

---

## 5. P0 Defense 5 - MasterClientID=99 for Cross-Client Visibility (docs + config only)

### Original spec assumption (FALSIFIED 2026-05-11 by live paper test)

The original §5 assumed MasterClientID=99 grants clientId=99 cross-client `cancelOrder()` authority. Live paper test on 2026-05-11 disproved the cancel half: clientId 88 placed a non-marketable order; clientId 99 saw it via `reqAllOpenOrders()` ✓ but `cancelOrder()` from clientId 99 failed with IBKR error 10147. Cleanup via the original placing clientId 88 succeeded. IBKR API docs confirm: `cancelOrder` is bound to the placing clientId; `reqGlobalCancel` cancels all open orders (too blunt for per-order use). Sources: https://interactivebrokers.github.io/tws-api/cancel_order.html + https://interactivebrokers.github.io/tws-api/open_orders.html + https://www.interactivebrokers.com/campus/ibkr-api-page/twsapi-doc/.

### Corrected scope (what §5 actually ships)

MasterClientID=99 is a **visibility-only** facility, not a cleanup facility. It gives a privileged read connection that sees orders placed by ALL clientIds - which is what's needed to detect the 5/8-style orphan condition (11 standalone STPs bound to clientId=1 that were invisible to per-client `openTrades()` queries). Cancellation of detected orphans MUST be done via the original placing clientId, via a temporary `ib_async` connection on that clientId.

### Fix

1. Set `MasterClientID=99` on VPS IB Gateway: edit `/home/ibgateway/ibc/config.ini`, restart `ib-gateway.service`. On the current IBC-managed VPS install, the config key that sets the Gateway Master Client ID is `OverrideTwsMasterClientID=99`.
2. Document the visibility-only behavior + per-clientId-cancel reality in this spec section + in `wiki/concepts/feature_k2bi-discipline-cleanup.md` "Known §5 limitations" section.
3. Engine code change: NONE. §5 is config + docs only.
4. Cleanup tooling for detected orphans: DEFERRED to post-Spec-B follow-up (`wiki/concepts/feature_orphan-order-cleanup-tool.md`, status: backlog). The follow-up tool will accept an orphan's placing clientId from `reqAllOpenOrders()` output, spawn a temporary connection on that clientId, cancel surgically, disconnect.

### Operator-side cleanup procedure (interim, until follow-up tool ships)

If an orphan order is detected via `reqAllOpenOrders()`:

1. Do not use `reqGlobalCancel()` for normal cleanup.
2. Do not use clientId 99 for targeted cancellation.
3. Record the orphan's symbol, action, order type, quantity, stop or limit price, permId, orderId, orderRef, and placing clientId from `reqAllOpenOrders()`.
4. Use the deferred cleanup tool once it ships. Until then, cleanup is an operator-run incident procedure that must include exact permId or orderId confirmation, dry-run review, `try/finally` disconnect handling, and a `wiki/log.md` audit line.
5. If the cleanup cannot be performed surgically, stop and keep the engine disabled.

### Tests

`tests/test_engine_master_client_id.py`:

M1 (config-presence, operator-verified): verified by §7 engine-re-enable-checklist via `scripts/ssh-vps.sh 'grep MasterClientID /home/ibgateway/ibc/config.ini'`. No CI pytest assertion (config lives on VPS, not in repo).

M2 (visibility-confirmation contract): inline comment in `execution/connectors/ibkr.py` documents that clientId 99 connections see all orders via `reqAllOpenOrders()` but can only cancel their own orders. Behavior contract documented in code, not test-enforced.

DROP the cross-client cancel red test from commit `da63664` - it tested a falsified assumption.

---

## 6. Seven Deferred Review Findings ([[feature_k2bi-discipline-cleanup]])

Address all seven findings tracked in `wiki/concepts/feature_k2bi-discipline-cleanup.md`. Per-finding plan:

| # | Finding | Plan |
|---|---|---|
| F1 | clientId 90-99 has no allocator (CRITICAL) | `flock`-based allocator (~30 lines) in `scripts/lib/clientid_allocator.py`; `gateway-query.sh` calls it on entry + releases on exit. Pair with §5 MasterClientId. |
| F2 | T10 share_count NAV divergence (HIGH) | Add divergence pre-condition to `feature_engine-vault-snapshots` build session (NOT this Spec B; document handoff in [[feature_k2bi-discipline-cleanup]]). Spec B writes the pointer; engine-vault-snapshots ships the check. |
| F3 | Codex-skip exception abuse (HIGH) | Amend `invest-ship/SKILL.md` Checkpoint 2 exception clause: explicitly exclude "new architectural principles, conventions, or invariants." This amendment commit MUST go through full Codex review (no doc-only exception on the meta-rule). |
| F4 | review/ gitignore vs Check C (HIGH) | **Locked 2026-05-10: option A.** Drop `review/` from `.gitignore` and stage proposals normally so Check C can verify same-commit proposal + config changes. |
| F5 | Dependency inversion (MEDIUM) | Same handoff as F2 to engine-vault-snapshots build session. Document in [[feature_k2bi-discipline-cleanup]]. |
| F6 | gateway-query.sh runtime caller-context guard (MEDIUM) | Add an `assert_invoked_from_macbook()` runtime check at top of `scripts/gateway-query.sh` (test `uname -n` against allow-list, or check for a sentinel env var the engine NEVER sets). Pair with F1 in the gateway-discipline pass. |
| F7 | L-2026-05-08-002 'operator-driven liveness' (MEDIUM) | **Locked 2026-05-10: augment, do not demote.** Add two examples: IBKR migration liveness and Syncthing liveness. |

---

## 7. Sequencing & Test Discipline

**Order of work:**

1. §0 pre-open re-verify (Monday 2026-05-11 21:30 HKT, operator-only).
2. §1 position-aware skip (red-then-green tests; Codex review).
3. §2 order dedup (red-then-green tests; Codex review).
4. §3 rapid-fire circuit breaker (red-then-green tests; Codex review).
5. §4 child-stop attachment (red-then-green tests; Codex review).
6. §5 MasterClientID=99 visibility config (operator-verified config + docs; no cross-client cancel test).
7. §6 deferred findings F1, F3, F4, F6, F7 (each its own commit; F2 + F5 are pointers; F4 path is locked to option A).
8. Engine re-enable: `sudo systemctl enable --now k2bi-engine` on Hostinger VPS, with ~/Projects/K2Bi-Vault/System/.killed verified absent and `wiki/log.md` line written.

**Test discipline (every defense):**

- Write the failing test FIRST, commit it as `test(spec-b): red test for §N defect`.
- Implement the fix; commit as `feat(engine): §N <defense name>`.
- Verify the red test now passes; full suite still green.
- Codex review on every code commit (no doc-only exception). Kimi-backed `scripts/minimax-review.sh` is the documented fallback.

**Engine re-enable pre-conditions checklist (operator runs before `systemctl enable --now`):**

- [ ] All §1-§4 red-then-green tests pass in CI.
- [ ] Full pytest suite green (`pytest tests/`).
- [ ] Before any `ib-gateway.service` restart for §5 config maintenance: verify `k2bi-engine.service` is inactive AND disabled, and verify `~/Projects/K2Bi-Vault/System/.killed` is absent.
- [ ] §5 MasterClientID=99 visibility config verified via `scripts/ssh-vps.sh 'grep MasterClientID /home/ibgateway/ibc/config.ini'` (expected active IBC key: `OverrideTwsMasterClientID=99`) and `systemctl status ib-gateway.service` active with uptime since the config edit.
- [ ] `wiki/concepts/feature_k2bi-discipline-cleanup.md` has an accurate "Known §5 limitations" section.
- [ ] §0 pre-open re-verify state has not drifted (re-run `gateway-query.sh` clientId=99 on the day of re-enable).
- [ ] `wiki/log.md` line written via `scripts/wiki-log-append.sh`.
- [ ] DEVLOG.md entry appended.

---

## 8. Out of Scope

- Phase 3.8b first paper trade via invest-coach (resumes AFTER engine re-enable).
- Phase 3.10 5-day burn-in (gated on 3.8b completion).
- New strategy approvals (blocked until Spec B ships).
- Changes to validators (`execution/validators/`) -- the four defenses sit BEFORE validators in the cycle, not inside them.
- Changes to kill-switch semantics (current behavior is correct per Bundle 3 prerequisite).
- Auto-flatten on circuit-breaker trip (separate Phase 4+ command per Bundle 3 §0 resolution).
- Engine snapshot pipeline ([[feature_engine-vault-snapshots]]) -- Spec B writes pointers for F2 + F5 but does not implement.

---

## 9. Operator Decisions

All kickoff questions are resolved.

1. ~~**F4 path:**~~ **Locked: option A.** Drop `review/` from `.gitignore` and stage proposals normally. This restores Check C's same-commit audit path instead of adding an out-of-repo Syncthing read path.
2. ~~**F7 disposition:**~~ **Locked: augment.** Keep L-2026-05-08-002 as a rule and add two concrete examples: IBKR migration liveness and Syncthing liveness.
3. ~~**MasterClientID value:**~~ **Locked: 99.** `gateway-query.sh` already defaults to 99; operator muscle memory uses 99; switching to 90 is churn for zero safety benefit. See §5.
4. ~~**Position-aware skip semantics (§1 G3):**~~ **Locked: STRICT skip-at-or-above-target.** Top-up creates a silent-recovery path; STRICT forces partial fills to surface as operator-reviewable state. Defense-in-depth ensures cascade prevention either way. See §1 G3.

---

## 10. Cross-References

- [[self_improve_errors]] /error E-2026-05-09-001 (post-mortem)
- [[strategy_g-2026-05_2nd-wave-paper-trade]] (engine_bug_recovery_note frontmatter)
- [[feature_k2bi-discipline-cleanup]] (7 deferred findings tracker)
- `~/Projects/K2Bi-Vault/raw/research/2026-05-08_session-wrapup_gateway-credential-swap-first-fill.md` (first surface)
- DEVLOG.md commit `bbfb611` (incident landing)
- Bundle 3 spec (`proposals/2026-04-19_k2bi-bundle-3-approval-gate-spec.md`) for the §0-prerequisite + red-then-green pattern this spec mirrors.
- 2026-05-10 spec amendment: §0 deterministic freshness + cost-basis tiers (Kimi findings 1, 2), §2 journal replay validation + schema versioning (finding 6), §3 nonce-correlated journal-first sentinel ordering (finding 4), §4 recovery-context token guard (finding 5). Finding 3 (escalation runbook) remains a TODO tracked in [[feature_k2bi-discipline-cleanup]] with owner and 24h unresolved-state trigger.
