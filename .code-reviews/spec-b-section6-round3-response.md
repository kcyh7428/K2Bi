# Spec B Section 6 Round 3 Kimi Disposition

Review log: `.code-reviews/2026-05-11T07-23-37Z_005511.log`

Kimi verdict: NEEDS-ATTENTION.

Iteration status: round 3 cap reached. Codex self-judged the remaining findings per operator authorization.

Codex disposition: release-failure observability accepted and fixed at `64e6d10`; malformed/legacy lease cleanup tightened at `64e6d10`; PID-0 inversion claim rejected as stated because `_pid_is_alive()` already returned `False` for `pid <= 0`, but the branch was simplified to make the intended fallback explicit.

## Finding 1

Status: ACCEPTED - fixed at `64e6d10`

Code anchor: `scripts/gateway-query.sh:67` defines `release_clientid_lease()`. The release command now runs inside an `if ! ...; then` block at `scripts/gateway-query.sh:69`, and `scripts/gateway-query.sh:72` emits `clientId lease release failed` to stderr before returning non-zero.

Code anchor: `scripts/lib/clientid_allocator.py:69` treats malformed JSON lease files as stale so a corrupt lease file cannot permanently occupy a clientId slot.

Test anchor: `tests/test_engine_gateway_discipline.py:102` verifies a malformed `clientId-93.json` lease file is reclaimed. `tests/test_engine_gateway_discipline.py:120` requires the gateway wrapper to retain the release-failure warning string.

Safety reasoning: Kimi was correct that silent cleanup failure was too quiet for an operator broker helper. The fix makes release failure observable and makes corrupt lease files reclaimable on the next allocation attempt.

## Finding 2

Status: REJECTED as stated; simplified at `64e6d10`

Code anchor: `scripts/lib/clientid_allocator.py:52` defines `_pid_is_alive()`, and `scripts/lib/clientid_allocator.py:53` returns `False` for `pid <= 0` before any `os.kill()` call. The claimed `os.kill(0, 0)` path was not reachable.

Code anchor: `scripts/lib/clientid_allocator.py:79` now makes the branch explicit: positive owner PIDs use process liveness, and `scripts/lib/clientid_allocator.py:81` sends zero, missing, legacy, or malformed owner PIDs through the TTL fallback.

Test anchor: `tests/test_engine_gateway_discipline.py:102` covers malformed lease reclamation. The older stale-dead-owner and fresh-dead-owner tests at `tests/test_engine_gateway_discipline.py:50` and `tests/test_engine_gateway_discipline.py:76` cover the positive-owner-PID paths.

Safety reasoning: The reviewer identified a confusing branch, not an executable PID-0 bug. The code already refused `pid <= 0` inside `_pid_is_alive()`. The simplification still improves maintainability and makes the TTL fallback explicit for legacy records.
