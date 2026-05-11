# Spec B Section 6 Round 1 Kimi Disposition

Review log: `.code-reviews/2026-05-11T07-00-38Z_ecad61.log`

Kimi verdict: NEEDS-ATTENTION.

Codex disposition: one finding accepted and fixed at `197dd56`; two findings rejected as outside the Section 6 named-bug surface.

## Finding 1

Status: ACCEPTED - fixed at `197dd56`

Code anchor: `scripts/lib/clientid_allocator.py:64` defines `_lease_is_stale()`. The load-bearing checks are `owner_pid` liveness at `scripts/lib/clientid_allocator.py:77` and the stale cutoff at `scripts/lib/clientid_allocator.py:79`: `return now - created_at > ttl_seconds`.

Code anchor: `scripts/lib/clientid_allocator.py:115` now reclaims stale lease files under the same flock used for allocation. The load-bearing branch is `path.unlink()` at `scripts/lib/clientid_allocator.py:121`.

Code anchor: `scripts/gateway-query.sh:107` passes `--owner-pid "$$"` so the lease owner is the shell process that remains alive for the remote query, not the short-lived allocator subprocess.

Test anchor: `tests/test_engine_gateway_discipline.py:50` exercises a dead-owner stale lease and verifies the same preferred clientId can be reclaimed.

Safety reasoning: Kimi was correct. A clean-exit trap alone is not enough for a 10-slot operator clientId pool. The fix keeps active owners protected, but lets a dead-owner lease older than the default TTL stop blocking the gateway-query path.

## Finding 2

Status: REJECTED

Code anchor: `proposals/2026-05-10_spec-b-engine-discipline-cleanup.md:267` defines F6 as `gateway-query.sh runtime caller-context guard`, specifically an `assert_invoked_from_macbook()` check using host allow-listing or a sentinel env var the engine never sets.

Code anchor: `scripts/gateway-query.sh:41` implements `assert_invoked_from_macbook()`. The load-bearing accidental-skill guard is `CLAUDE_CODE_SKILL_INVOCATION` at `scripts/gateway-query.sh:46`, and the manual override is `K2BI_GATEWAY_QUERY_OPERATOR_OVERRIDE` at `scripts/gateway-query.sh:42`.

Test anchor: `tests/test_engine_gateway_discipline.py:76` pins the F1/F6 source contract by requiring the allocator, `assert_invoked_from_macbook`, the skill sentinel, the override sentinel, `clientId=1` refusal, and lease release path.

Safety reasoning: Kimi's requested remote `systemctl is-active/is-enabled k2bi-engine` gate is outside F6. F6 is about caller context and accidental skill misuse, not engine-state gating. Section 0 and the Section 7 re-enable checklist own engine inactive/disabled verification before Spec B code and before re-enable. After operator re-enable, `gateway-query.sh` remains the intended operator one-off broker-query path, and forcing it to fail whenever the engine is active would break that path. Hostname spoofing is also not a meaningful threat in this repo's operator-local threat model because a local operator who can spoof host identity can also set the explicit override env var. The named bug is closed by blocking skill invocations and refusing engine clientId 1.

## Finding 3

Status: REJECTED

Code anchor: `.githooks/pre-commit:165` gathers staged `review/strategy-approvals/*_limits-proposal_*.md` files only when `execution/validators/config.yaml` is staged. The load-bearing validation is at `.githooks/pre-commit:197`, which requires HEAD `status=proposed`, and `.githooks/pre-commit:205`, which rejects if no same-commit proposed-to-approved transition exists.

Code anchor: `scripts/lib/invest_ship_strategy.py:2322` applies the proposal's `## YAML Patch` only if the before-block appears exactly once in current `config.yaml`. The stale-proposal rejection text is at `scripts/lib/invest_ship_strategy.py:2324`: `before-block not found in config`.

Test anchor: `tests/test_pre_commit_hook.py:231` rejects a config edit paired with an already-approved proposal from a prior commit. `tests/test_pre_commit_hook.py:272` rejects a forged new approved proposal. `tests/test_invest_ship_strategy.py:1331` rejects an approval patch whose before-block is not found in current config.

Safety reasoning: Kimi's concern is not a Section 6 F4 gap. F4's named bug was that `.gitignore` hid `review/`, making Check C structurally unsatisfiable. The locked fix was option A: drop `review/` from `.gitignore` and stage proposals normally. That is done. The repo already rejects the stale paths Kimi described: Check C rejects config edits paired with prior-approved proposals, and `/invest-ship --approve-limits` rejects stale YAML patches because the before-block must match current config exactly once. Adding a new approved_commit_sha ancestor/signature gate would be a new invest-ship design change, not the F4 cleanup.
