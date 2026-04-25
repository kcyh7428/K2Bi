# Q42 Implementation Plan — orphan-STOP adoption workflow

**Goal:** Adopt Phase 3.6 Day 1 Portal STOP `permId=1888063981` as a first-class journal event so VPS cold-starts no longer require `K2BI_ALLOW_RECOVERY_MISMATCH=1` for THIS specific orphan. The general override stays as escape hatch for OTHER unknown broker state.

**Architecture:** New env var `K2BI_ADOPT_ORPHAN_STOP=<permId>:<justification>` triggers a write of a new `orphan_stop_adopted` journal event during recovery. Future cold-starts read that event from `journal_tail` and treat the permId as KNOWN, so the orphan check at `recovery.py:548-622` matches it and skips it. Adoption is per-permId; OTHER unknown broker state still fails closed (`MISMATCH_REFUSED`).

**Tech stack:** Python 3 (existing journal+engine modules), unittest harness, IBKR `ib_async` open-orders feed (already wired into recovery.py). No new dependencies.

**Capital-path review bucket:** aggressive. Stop rule: P1=0 + P2 isolated. NO ship until both Codex Checkpoint 1 (this plan) and Checkpoint 2 (pre-commit) clear.

---

## Architect rulings (already confirmed; record for review context)

- **D1 = D1.a (no SCHEMA_VERSION bump).** Follow existing docstring rule + m2.23 precedent: additive event types do NOT bump SCHEMA_VERSION. Stays at 2.
- **D3 = bounded 48h lookback acceptable.** VPS engine runs continuously; cold-starts are within lookback in practice. Long-tail mitigation (engine_recovered checkpoint carry-forward of `adopted_orphan_perm_ids`) is Phase 4+ scope and is explicitly OUT OF SCOPE this ship. DEVLOG must include the 48h boundary caveat with explicit pointer to Phase 4+ home.

---

## Files touched

| Path | Mode | Reason |
|---|---|---|
| `execution/journal/schema.py` | additive | new event type + payload validator |
| `execution/engine/recovery.py` | additive + extension | env parser, `reconcile()` arg, seen_broker_ids pre-populate, adoption write path, `_adopted_orphan_perm_ids()` helper |
| `execution/engine/main.py` | additive | read env var, fatal on parse error (`sys.exit(78)`), pass through to `reconcile()` |
| `tests/test_engine_recovery.py` | additive | +7 tests (1002 → 1009 suite total) |
| `DEVLOG.md` | additive | Q42 entry (handled by `/invest-ship`) |
| `K2Bi-Vault/wiki/log.md` | additive | auto-appended by `/invest-ship` |

NO changes to: `tests/test_engine_main.py` (existing override-env assertions still hold), `tests/test_invest_alert_lib.py` (no event-classifier change in this ship; Bundle 5 z.4 follow-up).

---

## Step 1 — `execution/journal/schema.py` (D1.a; no version bump)

Add `orphan_stop_adopted` event type via a new frozenset (kept separate so docs trace which session each addition came from, mirroring the m2.23 `EVENT_TYPES_V2_ADDITIVE` pattern in spirit):

```python
# Q42 (2026-04-26): orphan-STOP adoption workflow. operator-portal-submitted
# STOPs that pre-date the engine's awareness can be adopted as first-class
# journal events so future recovery passes recognize them as KNOWN broker
# state. Required payload fields: permId (int), ticker (str), qty (int),
# stop_price (Decimal as str), source (enum), adopted_at (ISO8601 UTC),
# justification (non-empty str). Additive-only; SCHEMA_VERSION unchanged
# per the evolution rule documented above.
EVENT_TYPES_V2_ADDITIVE_Q42 = frozenset({"orphan_stop_adopted"})

EVENT_TYPES = EVENT_TYPES_V1 | EVENT_TYPES_V2_ADDITIONS | EVENT_TYPES_V2_ADDITIVE_Q42
```

Update the v2-additive section of the module docstring with a Q42 entry mirroring the m2.23 entry's prose.

Add payload validator (separate from `validate()` to preserve its cheap-checks-only contract):

```python
ORPHAN_STOP_ADOPTED_SOURCES = frozenset({"operator-portal", "operator-tws", "external-api"})

def validate_orphan_stop_adopted_payload(payload: dict[str, Any]) -> None:
    """Field-level validation for orphan_stop_adopted event payloads.

    Called by the writer caller (recovery.py) when constructing the event.
    Raises JournalSchemaError on any violation. NOT called from validate()
    above -- that helper stays cheap-checks-only per its contract.
    """
    required = ("permId", "ticker", "qty", "stop_price", "source",
                "adopted_at", "justification")
    missing = [k for k in required if k not in payload]
    if missing:
        raise JournalSchemaError(
            f"orphan_stop_adopted payload missing fields: {missing}"
        )
    perm = payload["permId"]
    if not isinstance(perm, int) or isinstance(perm, bool) or perm <= 0:
        raise JournalSchemaError(
            f"orphan_stop_adopted permId must be positive int, got {perm!r}"
        )
    qty = payload["qty"]
    if not isinstance(qty, int) or isinstance(qty, bool) or qty == 0:
        raise JournalSchemaError(
            f"orphan_stop_adopted qty must be non-zero int, got {qty!r}"
        )
    try:
        stop = Decimal(str(payload["stop_price"]))
        if not stop.is_finite() or stop <= 0:
            raise JournalSchemaError(
                f"orphan_stop_adopted stop_price must be finite > 0, "
                f"got {payload['stop_price']!r}"
            )
    except (InvalidOperation, ValueError, TypeError):
        raise JournalSchemaError(
            f"orphan_stop_adopted stop_price not parseable as Decimal: "
            f"{payload['stop_price']!r}"
        )
    src = payload["source"]
    if src not in ORPHAN_STOP_ADOPTED_SOURCES:
        raise JournalSchemaError(
            f"orphan_stop_adopted source must be one of "
            f"{sorted(ORPHAN_STOP_ADOPTED_SOURCES)}, got {src!r}"
        )
    adopted = payload["adopted_at"]
    if not isinstance(adopted, str):
        raise JournalSchemaError(
            f"orphan_stop_adopted adopted_at must be str, "
            f"got {type(adopted).__name__}"
        )
    try:
        parsed = datetime.fromisoformat(adopted)
        if parsed.tzinfo is None:
            raise JournalSchemaError(
                f"orphan_stop_adopted adopted_at must include timezone, "
                f"got {adopted!r}"
            )
    except ValueError:
        raise JournalSchemaError(
            f"orphan_stop_adopted adopted_at not ISO8601: {adopted!r}"
        )
    just = payload["justification"]
    if not isinstance(just, str) or not just.strip():
        raise JournalSchemaError(
            "orphan_stop_adopted justification must be non-empty string"
        )
```

`bool` exclusion guard: in Python `isinstance(True, int) is True`, so without the explicit `bool` reject the validator would accept `permId=True` as `permId=1`. Defense in depth.

Imports added at top of `schema.py` if not already present: `from datetime import datetime`, `from decimal import Decimal, InvalidOperation`.

---

## Step 2 — `execution/engine/recovery.py` (env parser + reconcile signature + adoption write path)

### 2.1 New constants + parser (top of file, near `RECOVERY_OVERRIDE_ENV`):

```python
ADOPT_ORPHAN_STOP_ENV = "K2BI_ADOPT_ORPHAN_STOP"


@dataclass(frozen=True)
class OrphanStopAdoptionRequest:
    perm_id: int
    justification: str


def _parse_adopt_orphan_stop(raw: str | None) -> OrphanStopAdoptionRequest | None:
    """Parse K2BI_ADOPT_ORPHAN_STOP=<permId>:<justification>.

    Returns None if env var is unset/empty. Raises ValueError if set but
    malformed -- engine main treats this as fatal at startup, same way
    a corrupt config would (reject loud, do not silently fall through
    and start the engine in a state inconsistent with operator intent).
    """
    if not raw or not raw.strip():
        return None
    if ":" not in raw:
        raise ValueError(
            f"{ADOPT_ORPHAN_STOP_ENV} format is <permId>:<justification>; "
            f"got {raw!r} (no colon)"
        )
    perm_str, _, just = raw.partition(":")
    perm_str = perm_str.strip()
    just = just.strip()
    try:
        perm_id = int(perm_str)
    except ValueError:
        raise ValueError(
            f"{ADOPT_ORPHAN_STOP_ENV} permId must be int, got {perm_str!r}"
        )
    if perm_id <= 0:
        raise ValueError(
            f"{ADOPT_ORPHAN_STOP_ENV} permId must be positive, got {perm_id}"
        )
    if not just:
        raise ValueError(
            f"{ADOPT_ORPHAN_STOP_ENV} justification must be non-empty"
        )
    return OrphanStopAdoptionRequest(perm_id=perm_id, justification=just)
```

Use `partition(":")` (split on FIRST colon) so a justification containing colons is preserved verbatim.

### 2.2 Extend `reconcile()` signature (mirror existing `override_env` pure-function pattern at `recovery.py:162-163`):

```python
def reconcile(
    *,
    journal_tail: list[dict[str, Any]],
    broker_positions: list[BrokerPosition],
    broker_open_orders: list[BrokerOpenOrder],
    broker_order_status: list[BrokerOrderStatusEvent],
    now: datetime,
    override_env: str | None = None,
    override_env_name: str = RECOVERY_OVERRIDE_ENV,
    adopt_orphan_stop: OrphanStopAdoptionRequest | None = None,
    adopt_orphan_stop_env_name: str = ADOPT_ORPHAN_STOP_ENV,
) -> ReconciliationResult:
```

Engine main reads `os.environ[ADOPT_ORPHAN_STOP_ENV]` via `_parse_adopt_orphan_stop()` and passes the result through. Tests pass it directly (matching the `override_env=""` test pattern at lines 233/260/287/310).

### 2.3 Pre-populate `seen_broker_ids` from journaled adoptions (BEFORE Phase A matching loop, near `recovery.py:206`):

```python
seen_broker_ids: set[str] = set()

# Q42: orphan_stop_adopted events from prior recovery passes pre-mark
# their permIds as known so the orphan loop at line ~548 skips them.
# Same mechanism the existing Phase A perm/oid matching uses. Bounded
# by journal_tail's lookback (DEFAULT_LOOKBACK = 48h); long-tail
# carry-forward via engine_recovered checkpoint is Phase 4+ scope
# (see DEVLOG Q42 caveat).
adopted_perm_ids = _adopted_orphan_perm_ids(journal_tail)
for perm in adopted_perm_ids:
    seen_broker_ids.add(f"perm:{perm}")
```

With helper:

```python
def _adopted_orphan_perm_ids(records: Iterable[dict[str, Any]]) -> set[str]:
    """Permanent IDs adopted by any prior orphan_stop_adopted journal event.

    The orphan check treats these permIds as KNOWN broker state so future
    cold starts don't re-flag them after the architect has explicitly
    adopted them. NOTE: bounded by journal_tail's lookback window
    (currently 48h via DEFAULT_LOOKBACK). Long-tail carry-forward via
    engine_recovered checkpoint is Phase 4+ work; in practice the VPS
    engine restarts continuously so adopted permIds stay in tail.
    """
    out: set[str] = set()
    for rec in records:
        if rec.get("event_type") != "orphan_stop_adopted":
            continue
        payload = rec.get("payload") or {}
        perm = payload.get("permId")
        if perm is not None:
            out.add(str(perm))
    return out
```

### 2.4 Adoption write path (after Phase B mismatch collection, BEFORE status assembly at `recovery.py:1058`)

Logic:

```python
# Q42: adoption resolves a SPECIFIC permId mismatch when the architect
# has explicitly invoked K2BI_ADOPT_ORPHAN_STOP. It does NOT bypass
# OTHER mismatches; defense in depth.
if adopt_orphan_stop is not None:
    target_perm = str(adopt_orphan_stop.perm_id)
    matched_open = next(
        (o for o in broker_open_orders if str(o.broker_perm_id or "") == target_perm),
        None,
    )
    if matched_open is None:
        mismatches.append({
            "case": "adopt_orphan_stop_perm_not_found",
            "requested_perm_id": adopt_orphan_stop.perm_id,
            "env": adopt_orphan_stop_env_name,
            "reason": (
                "K2BI_ADOPT_ORPHAN_STOP requested adoption of permId "
                f"{adopt_orphan_stop.perm_id} but no matching broker "
                "open order was found. Refusing to silently swallow the "
                "request -- operator must verify the permId or unset "
                "the env var."
            ),
        })
    else:
        # Verify the broker order is actually a STOP order. Adopting
        # a non-STOP would let an arbitrary order type sneak past the
        # invariant check.
        is_stop_order = (
            (matched_open.order_type or "").upper() in ("STP", "STOP")
            or (
                matched_open.client_tag
                and CLIENT_TAG_STOP_SUFFIX in matched_open.client_tag
            )
        )
        if not is_stop_order:
            mismatches.append({
                "case": "adopt_orphan_stop_not_a_stop",
                "requested_perm_id": adopt_orphan_stop.perm_id,
                "matched_order_type": matched_open.order_type,
                "matched_client_tag": matched_open.client_tag,
                "reason": (
                    "Broker order at permId "
                    f"{adopt_orphan_stop.perm_id} is not a STOP order "
                    "(adoption is restricted to STOP orders this ship)."
                ),
            })
        else:
            # Remove the phantom mismatch entry that flagged THIS permId
            # so adoption resolves it cleanly. Other mismatches stay.
            mismatches = [
                m for m in mismatches
                if str(m.get("broker_perm_id") or "") != target_perm
            ]
            stop_trigger = _stop_trigger_of(matched_open)  # reuse existing helper
            adoption_payload = {
                "permId": adopt_orphan_stop.perm_id,
                "ticker": matched_open.ticker,
                "qty": matched_open.qty,
                "stop_price": str(stop_trigger) if stop_trigger is not None else "",
                "source": "operator-portal",
                "adopted_at": now.isoformat(),
                "justification": adopt_orphan_stop.justification,
            }
            # Programmer-error guard: if we built an invalid payload, fail
            # loud rather than write a malformed event.
            from ..journal.schema import validate_orphan_stop_adopted_payload
            validate_orphan_stop_adopted_payload(adoption_payload)
            events.append(
                ReconciliationEvent(
                    event_type="orphan_stop_adopted",
                    payload=adoption_payload,
                    ticker=matched_open.ticker,
                    broker_order_id=matched_open.broker_order_id,
                    broker_perm_id=str(adopt_orphan_stop.perm_id),
                )
            )
```

`_stop_trigger_of()` already exists in `recovery.py` (used by Q31 invariants). Reuse rather than reinvent.

### 2.5 Status assembly UNCHANGED (existing logic at `recovery.py:1058-1098`)

After step 2.4 above:
- If we successfully adopted the only orphan, `mismatches` is now empty → `status = CATCH_UP`, no override needed.
- If the request was malformed (perm_not_found, not_a_stop), the new mismatch entry keeps `mismatches` non-empty → `MISMATCH_REFUSED` (or `MISMATCH_OVERRIDE` if `K2BI_ALLOW_RECOVERY_MISMATCH=1`).
- If there are OTHER orphans/mismatches alongside an adopted one, `mismatches` stays non-empty → still REFUSE.

### 2.6 Update `__all__` at `recovery.py:1883`

Export `ADOPT_ORPHAN_STOP_ENV`, `OrphanStopAdoptionRequest`, `_parse_adopt_orphan_stop`, `_adopted_orphan_perm_ids` (the last for tests + future Phase 4+ engine_recovered carry-forward work).

---

## Step 3 — `execution/engine/main.py` (env wire-up)

Locate the existing `RECOVERY_OVERRIDE_ENV` read at startup (will be near the engine's startup configuration block; grep for `K2BI_ALLOW_RECOVERY_MISMATCH` or `RECOVERY_OVERRIDE_ENV`). Add adoption-env read with fatal-on-parse-error:

```python
try:
    adopt_request = recovery._parse_adopt_orphan_stop(
        os.environ.get(recovery.ADOPT_ORPHAN_STOP_ENV)
    )
except ValueError as e:
    LOG.error("FATAL: %s", e)
    sys.exit(78)  # config-error convention used elsewhere in this module

result = recovery.reconcile(
    ...,
    adopt_orphan_stop=adopt_request,
)
```

Use `sys.exit(78)` (sysexits.h `EX_CONFIG`) consistent with other fatal-config-error paths. Engine refuses to start if the env var is malformed; operator must fix the env var before retry. This is exactly the same fail-closed posture as the existing `RECOVERY_OVERRIDE_ENV` semantics (refuse-to-start on mismatch unless explicitly bypassed).

---

## Step 4 — Tests (`tests/test_engine_recovery.py`; +7 tests)

All tests use the existing pure-function pattern: pass `adopt_orphan_stop` and `override_env` directly; build `journal_tail` + `broker_open_orders` as fixtures.

```
test_q42_adoption_writes_event_and_clears_mismatch
    Cold start: journal has SPY position from prior fill, broker has
    matching position + an unknown STOP at permId=42 client_tag stop.
    adopt_orphan_stop=OrphanStopAdoptionRequest(perm_id=42,
    justification="test justification"). Assert
      result.status == RecoveryStatus.CATCH_UP
      one event with event_type="orphan_stop_adopted", permId=42,
        source="operator-portal", justification="test justification"
      result.mismatch_reasons == []
      override_env="" passed (unused)

test_q42_adoption_perm_not_found_rejects
    Same fixture but adopt_orphan_stop.perm_id=999 while broker only has
    permId=42. Assert
      result.status == RecoveryStatus.MISMATCH_REFUSED
      mismatch_reasons contains case="adopt_orphan_stop_perm_not_found"
        with requested_perm_id=999
      NO orphan_stop_adopted event in result.events

test_q42_adoption_not_a_stop_rejects
    Broker has permId=42 but order_type="LMT" (limit, not stop) and
    client_tag does NOT contain stop suffix. adopt targets permId=42.
    Assert
      result.status == RecoveryStatus.MISMATCH_REFUSED
      mismatch_reasons contains case="adopt_orphan_stop_not_a_stop"
      NO orphan_stop_adopted event written

test_q42_subsequent_cold_start_recognizes_adopted_perm_id
    Two-pass test:
      Pass 1: run reconcile with adopt_orphan_stop set. Capture the
        emitted orphan_stop_adopted event into a synthesized journal_tail
        (simulating it having been journaled).
      Pass 2: run reconcile WITHOUT adopt_orphan_stop AND WITHOUT
        override_env. Pass-1 event included in journal_tail.
    Assert pass 2:
      result.status == RecoveryStatus.CATCH_UP
      result.mismatch_reasons == []
      no new orphan_stop_adopted event written in pass 2's events

test_q42_multiple_orphans_only_one_adopted_still_refuses
    Broker has TWO unknown STOPs (permId=42, permId=43; both with
    client_tag stop suffix and matching position tickers).
    adopt_orphan_stop targets permId=42 only.
    Assert
      result.status == RecoveryStatus.MISMATCH_REFUSED
      events contain orphan_stop_adopted for permId=42
      mismatch_reasons contain phantom_open_order entry for permId=43
        (and NOT for permId=42)

test_q42_env_var_parse_validation
    Direct test of _parse_adopt_orphan_stop():
      None / "" / "  " -> returns None (no env set)
      "42" -> raises ValueError mentioning "no colon"
      "abc:reason" -> raises ValueError mentioning "permId must be int"
      "0:reason" -> raises ValueError mentioning "permId must be positive"
      "-1:reason" -> raises ValueError mentioning "permId must be positive"
      "42:" -> raises ValueError mentioning "justification must be non-empty"
      "42:   " -> raises ValueError mentioning "justification must be non-empty"
      "42:reason: with: colons" -> returns
        OrphanStopAdoptionRequest(perm_id=42,
          justification="reason: with: colons")
      "  42  :  reason  " -> returns
        OrphanStopAdoptionRequest(perm_id=42, justification="reason")

test_q42_orphan_stop_adopted_payload_validation
    Direct test of validate_orphan_stop_adopted_payload():
      missing permId -> JournalSchemaError mentioning fields
      permId=True -> JournalSchemaError (bool guard)
      non-int permId -> JournalSchemaError
      permId=0 / negative -> JournalSchemaError
      qty=True -> JournalSchemaError (bool guard)
      qty=0 -> JournalSchemaError
      stop_price="abc" -> JournalSchemaError
      stop_price="0" -> JournalSchemaError
      stop_price="-1.5" -> JournalSchemaError
      stop_price="Infinity" -> JournalSchemaError
      source="invented" -> JournalSchemaError
      adopted_at="2026-04-26 not iso" -> JournalSchemaError
      adopted_at without tz -> JournalSchemaError
      justification="" -> JournalSchemaError
      justification="   " -> JournalSchemaError
      all valid -> returns None
```

Note: spec's "test #6 SCHEMA_VERSION 2 -> 3 lenient reader" is REMOVED because D1.a chose no version bump. Replaced with `test_q42_env_var_parse_validation` to keep the count at 7. Architect approved D1.a in the kickoff message.

Suite count: 1002 → 1009.

---

## Step 5 — Production validation sequence (AFTER all tests pass + Codex Pre-commit Review APPROVE)

This runs only after `/invest-ship --no-feature` lands the Q42 commits AND `/sync` deploys to VPS. (Order: ship locally → sync → run Step 5.)

```
# 1. Confirm Q42 code is on VPS
ssh hostinger 'cd /opt/k2bi && git log -1 --oneline'
# Expected: shows the Q42 commit short-sha

# 2. Inject the adoption env var into systemd unit
ssh hostinger
sudo systemctl edit k2bi-engine.service
# In the editor, add:
[Service]
Environment="K2BI_ADOPT_ORPHAN_STOP=1888063981:Phase-3.6-Day-1-Portal-submitted-STOP-permId-1888063981-broker-safe-no-duplicate-risk-confirmed-by-3-day-shakedown-2026-04-20-to-04-22"

# 3. Restart engine -- this writes the orphan_stop_adopted event during recovery
sudo systemctl restart k2bi-engine.service

# 4. Verify journal contains the adoption event
sudo journalctl -u k2bi-engine.service -n 200 --no-pager | grep -i "orphan_stop_adopted\|recovery_state\|engine_started"
# Expected: orphan_stop_adopted event with permId=1888063981
# AND engine_started event (engine running cleanly)
# AND recovery_reconciled or recovery_state events that show the
#   adoption resolved the mismatch

# 5. Tail the actual journal file directly to confirm the event landed
sudo tail -50 /var/lib/k2bi/journal/decisions.jsonl | grep "orphan_stop_adopted"

# 6. Remove the K2BI_ADOPT_ORPHAN_STOP override (one-shot use)
sudo systemctl edit k2bi-engine.service
# delete the K2BI_ADOPT_ORPHAN_STOP line; ALSO delete K2BI_ALLOW_RECOVERY_MISMATCH
# if it was previously set in this systemd unit

# 7. Restart engine WITHOUT either override
sudo systemctl restart k2bi-engine.service

# 8. Verify clean start
sudo systemctl status k2bi-engine.service
sudo journalctl -u k2bi-engine.service -n 100 --no-pager | grep -i "recovery_\|engine_started\|engine_recovered\|FATAL\|exit"
# Expected:
#   engine_started + engine_recovered events
#   NO recovery_state_mismatch with refuse-to-start
#   NO K2BI_ADOPT_ORPHAN_STOP in environment (verified by ps)
#   ps -p $(pgrep -f k2bi-engine) o environ | tr '\0' '\n' | grep K2BI
#     -- should NOT show either override env var
```

**Abort condition:** if step 8 shows `recovery_state_mismatch` again, abort and investigate. Likely causes:
1. A SECOND orphan we didn't know about (broker has another unknown order)
2. The `_adopted_orphan_perm_ids()` lookup is failing (event not actually in journal_tail)
3. Lookback boundary edge (very unlikely on immediate restart, but possible if clock drift)

Do NOT re-add the override blindly — diagnose first via the journal contents and broker open-orders snapshot.

**Rollback (if Step 5 production validation fails after Step 7's override-removal):**
- Re-add `K2BI_ALLOW_RECOVERY_MISMATCH=1` to systemd unit
- Restart engine (returns to known-good pre-Q42 behavior)
- File a follow-up to debug the `_adopted_orphan_perm_ids()` lookup
- Q42 ship is NOT reverted — the journal event is permanent — but the override remains in place until the bug is fixed

---

## Step 6 — DEVLOG entry (handled by `/invest-ship`)

Title: "Q42 orphan-STOP adoption SHIPPED -- Phase 3.6 Day 1 STOP permId=1888063981 now first-class journal event; K2BI_ALLOW_RECOVERY_MISMATCH=1 no longer required on VPS cold-start"

Mandatory caveat (D3 architect ruling):

> Adoption persists via `journal_tail`'s 48h lookback. Sufficient for continuous-engine operation (VPS reality); a >48h cold-start gap would re-flag the orphan as if Q42 was never shipped. Long-tail mitigation tracked for Phase 4+: extend `engine_recovered` checkpoint to carry forward `adopted_orphan_perm_ids` so they survive lookback expiry. Out of scope this ship.

---

## Review checkpoints

- **Now (before code):** Codex Plan Review on this file. Capital-path = aggressive bucket. Stop rule: P1=0 + P2 isolated.
- **After implementation, before commit:** Codex Pre-commit Review on the working tree via `scripts/review.sh diff --files <list> --primary codex`.
- **Cross-model rule:** Kimi-backed reviewer is the wrapper's auto-fallback if Codex wedges/times out. Acceptable for plan-review (this file). For pre-commit review of capital-path code, force `K2B_LLM_PROVIDER=minimax` if fallback fires (cross-model self-review forbidden — Kimi cannot review Kimi-built code, and even though this is Codex-led work, capital-path binds extra).

---

## Out of scope (explicitly per kickoff)

- Generalizing adoption to support arbitrary external STOPs beyond a single env-var-targeted permId
- Auto-discovery of orphan STOPs on cold start (manual env-var only; no auto-adopt)
- Modifying Q31/Q32 invariant logic itself
- m2.9 alert classifier changes for `orphan_stop_adopted` events (Bundle 5 z.4 follow-up)
- engine_recovered carry-forward of `adopted_orphan_perm_ids` (Phase 4+; mitigates D3 long-tail)
