---
tags: [milestones, k2bi, planning]
date: 2026-04-26
type: design
origin: test-fixture
up: "[[project_k2bi]]"
---

# Synthetic Milestones Fixture

Used by `tests/test_propagate_planning_status.py` to exercise the
propagation handlers against a known table state.

## Phase 2 -- MVP Scaffold All Tiers

### Bundle 5 -- Go Live Prep (synthetic)

| Milestone | Status | Artifact | Verification |
|---|---|---|---|
| m2.9 | ✅ SHIPPED Bundle 5a 2026-04-25 at K2Bi `aaaaaaa` + cron-env hotfix `bbbbbbb`. Bundle 5 follow-ups (z.4) classifier branch + (bb) bootstrap ✅ SHIPPED 2026-04-26 at K2Bi `ccccccc`. | invest-alert + Telegram | covered in tests |
| m2.19 | ✅ SHIPPED Bundle 5 2026-04-26 at K2Bi `ddddddd` (synthetic ops-verification ship). | systemd hardening | tests cover this |
| m2.20 | ✅ SHIPPED Bundle 5 2026-04-26 at K2Bi `eeeeeee` | tier frontmatter audit | tests cover this |
| m2.22 | LAST Bundle 5 item; gates on m2.13. | Codex full-stack review | runs after m2.13 |

## Phase 3 -- First Paper Trade + Smoke Test + First Real Thesis

Sequential, not bundled.

| # | Milestone | Verification |
|---|---|---|
| 3.1 | ✅ SHIPPED 2026-04-20 at `1111111`. Smoke-test commit. | Tests synthetic verification. |
| 3.2 | ✅ SHIPPED 2026-04-20 at `2222222`. | Tests synthetic verification. |
| 3.3 | ✅ SHIPPED 2026-04-20 at `3333333`. | Tests synthetic verification. |
| 3.4 | ✅ SHIPPED 2026-04-20 at `4444444`. | Tests synthetic verification. |
| 3.5 | ✅ SHIPPED 2026-04-20 at `5555555`. | Tests synthetic verification. |
| 3.6 | ✅ SHIPPED 2026-04-23 at `6666666`. | Tests synthetic verification. |
| 3.9 | ✅ FULLY CLOSED 2026-04-25 at `9999999`. | Tests synthetic verification. |
| 3.6.5 | ✅ SHIPPED 2026-04-26 at `7777777`. | Tests synthetic verification. |
| Q42 | ✅ SHIPPED 2026-04-26 at `8888888`. | Tests synthetic verification. |
| 3.7 | 🟡 NEXT 2026-04-26 evening -- invest-screen MVP. Spec drafted; awaiting paste. | m2.13 promoted from Bundle 4b. |
| 3.8 | First domain-driven thesis end-to-end on VPS | Pipeline verification. |
| 3.10 | 10-trading-day full unattended burn-in | Genuinely unattended. |
| 3.11 | Burn-in retro committed | Insights doc. |

## Related

- [[roadmap]] -- phase framing
