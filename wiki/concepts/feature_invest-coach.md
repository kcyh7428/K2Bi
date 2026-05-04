---
tags: [k2b-feature, k2bi, portfolio-manager-tier, coaching, phase-3.8a]
date: 2026-05-04
type: k2b-feature
status: spec-approved
origin: k2b-architect
priority: high
effort: M
impact: high
mvp: "Given a simulated lived signal, invest-coach walks through T0-T12 (or T0-T13 on refusal recovery), invokes existing skills without modifying them, and produces atomically-written vault artifacts: context_<sigid>-lived-signal.md, theme_<slug>.md, watchlist entry, wiki/tickers/<SYMBOL>.md, wiki/strategies/<slug>.md. The 11 binary MVP gates are: (1) synthetic end-to-end produces all artifacts without skipping turns; (2) CALX un-grounded info set produces verification refusal with no thesis written; (3) bucket-rule inside guide range surfaces contradiction at T10 and MVP-3 refuses pass; (4) every override at MVP-2 or MVP-3 surfaces in T12 summary with structured text; (5) T0 resume reads partial state and jumps to correct turn without re-asking T0-T5; (6) T8 invokes invest-bear-case as exactly one adversarial call; (7) approval refusal triggers clean T13 recovery walk-back without restart; (8) operator rejection at T2 writes coach-feedback file via invest-feedback with sigid, turn_id, rejected_framing, operator_correction; (9) spot-check fires only on operator command with vendor-must-differ and single-call; (10) stage-advancement suggestion updates active_rules.md with operator confirmation under flock concurrency guard; (11) T5.5 operator-elected bulk-research-handoff writes vendor_provenance frontmatter, T7 surfaces vendor warning, un-grounded claim is refused, and T12 names vendor explicitly. Gates 11a (no auto-invocation), 11b (vendor-must-differ enforcement), and 11c (T12 vendor section present iff T5.5 elected) are sub-tests of gate 11."
proposed-by: "Phase 3.8a spec 2026-05-03; D1-D10 resolved 2026-05-04"
accepted-by: "Keith via K2B architect session 2026-05-04"
priority-reason: "Closes the operator-fit gap identified in 2026-05-03 UX audit. Makes the existing analytically-rigorous pipeline reachable for a novice operator without weakening any gate."
depends-on: [phase-3.8.6-mvp-2-thesis-verification-gate, phase-3.8.6-mvp-3-forward-guidance-gate]
up: "[[index]]"
---

# Feature: invest-coach -- Multi-turn coaching skill for the K2Bi pipeline

## Goal

Build a multi-turn coaching skill that walks Keith through producing the structured inputs the existing K2Bi pipeline expects. The coach generates analytical drafts; Keith steers with judgment, plain-English questions, and primary-source verification. The coach NEVER bypasses any gate.

## What this is

A Portfolio Manager tier skill that plugs in front of the existing pipeline:

- `invest-narrative` for top-of-funnel discovery
- `invest-screen` for Stage-2 enrichment
- `invest-thesis` for Ahern 4-phase + 5-dim scoring
- `invest-bear-case` for the single adversarial call
- `invest-backtest` for the yfinance sanity check
- Strategy spec drafting + MVP-3 forward-guidance check
- `/invest-ship --approve-strategy` for approval

## What this is NOT

- Does NOT bypass any gate (MVP-2, MVP-3, /invest-ship validators)
- Does NOT auto-verify on Keith's behalf
- Does NOT substitute un-grounded LLM output for primary sources
- Does NOT author thesis directly bypassing invest-thesis
- Does NOT submit orders to the engine
- Does NOT auto-promote candidates
- Does NOT auto-invoke deep-research vendors at T5.5
- Does NOT run from Telegram in MVP
- Does NOT auto-flip the learning-stage dial

## The 11 binary MVP gates

### Gate 1: Synthetic lived-signal end-to-end test
Pass: Simulated lived signal -> T0 picks NEW -> coach turns T1 through T12 -> existing skills invoke -> approval succeeds; vault has populated context_<sigid>-lived-signal.md, theme_<slug>.md, watchlist entry, wiki/tickers/<SYMBOL>.md, wiki/strategies/<slug>.md (all atomically written).
Fail: Any gate refuses unintentionally or coach skips a turn.

### Gate 2: CALX info-set re-run test
Pass: Same un-grounded info set as 2026-04-30 shadow re-run -> coach -> MVP-2 fires REFUSE -> no thesis written; same verdict as standalone validator (verdict='refuse'); coach NEVER auto-marks claims `verified`; T12 (if reached via override path) surfaces the override.
Fail: Coach somehow rationalizes the un-grounded claims into verified status.

### Gate 3: Bucket-rule contradiction test
Pass: Strategy spec with bucket-4 EXIT inside the Q2 GM guide range -> coach T10 surfaces contradiction in plain English -> coach T11 + MVP-3 refuses `status='pass'` -> operator recalibrates to outside-guide thresholds; D4 dual-layer fires (T10 question + MVP-3 lock).
Fail: Coach allows the contradiction to ship without T10 surfacing OR MVP-3 catching.

### Gate 4: Override-path visibility test (D5)
Pass: Operator elects override at MVP-2 OR MVP-3 with valid reason >= 20 chars -> coach T12 final summary lists every override taken in the pipeline with structured text (claim_id / threshold name + original verdict + override_reason + categorical reason).
Fail: Override hidden in frontmatter only.

### Gate 5: T0 resume from partial state test (D2)
Pass: Operator pauses at T6 phase 2 of 4, closes session. Reopens. Coach T0 reads context_<sigid>-lived-signal.md + scans theme + watchlist + draft thesis -> infers state -> presents resume summary -> jumps to T6 phase 3. Resumed cleanly without re-asking T0-T5 questions.
Fail: Coach restarts from T0 NEW path or asks T1-T5 again.

### Gate 6: Single-call bear-case discipline test
Pass: Coach T8 invokes invest-bear-case as a single adversarial call (not multiple); log shows one call per agent-topology decision.
Fail: Log shows multi-call orchestration.

### Gate 7: Refusal recovery test
Pass: Approval gate refuses (e.g. MVP-3 catches a bucket-1 above the guide ceiling) -> operator re-engages coach -> coach T13 reads refuse message -> diagnoses failed turn -> walks back to T10 -> re-runs through approval. Clean refusal-recovery cycle.
Fail: Coach restarts from T0 NEW or gets confused.

### Gate 8: invest-feedback auto-capture test (D7)
Pass: Operator rejects a coach-generated framing at T2 (narrative restate). Coach writes K2Bi-Vault/raw/coach-feedback/<sigid>_T2_rejected.md via invest-feedback with {sigid, turn_id=T2, rejected_framing, operator_correction}. File exists post-rejection; structure matches schema.
Fail: Rejection event silently dropped.

### Gate 9: Operator-elected spot-check test (D3)
Pass: Without "spot-check this claim" command, T7 verification proceeds with manual click-through only; no spot-check call fires. With "spot-check this claim" command on a paywalled source, coach picks a vendor different from the curated info set's producer and makes ONE call. Spot-check fires only on operator command; vendor-must-differ holds; single-call.
Fail: Spot-check auto-invokes OR vendor-match violates compound-bias mitigation OR multiple calls per spot-check.

### Gate 10: Stage advancement suggestion test (D8) WITH flock concurrency guard
Pass: End-of-session reflection counts concepts the operator explained back without coach explanation. With >=3 distinct concepts, coach suggests dial flip; operator confirms; active_rules.md updated single-writer. Concurrent-session simulation results in exactly ONE dial flip.
Fail: Dial flips without operator confirmation OR multiple writers race AND lose updates OR the second session's suggestion contradicts the first's just-written value.

### Gate 11: T5.5 bulk-research-handoff test (D10)
Pass: Operator elects T5.5 at T5 close, coach drafts research prompt, operator pastes synthetic vendor response with exactly one un-grounded load-bearing claim. T6 ingests vendor draft and presents section-by-section; thesis frontmatter writes vendor_provenance block atomically. T7 entry surfaces explicit vendor-warning surface. T7 identifies un-grounded claim as un-verified by default; operator's manual click-through finds no primary source match; operator marks `refused` with note >= 20 chars. T7 aggregate refuses thesis (verdict='refuse'); generate_thesis raises ValueError; no wiki/tickers/<SYMBOL>.md written. If operator takes override path, thesis writes; T12 final summary names vendor explicitly and lists the override.
Fail: T5.5 auto-invokes OR vendor-source hidden OR T7 lets vendor claims pass without per-claim verification OR vendor-must-differ silently allows match OR T12 omits vendor when elected.

#### Gate 11a: Auto-invocation refusal
Pass: Without operator's explicit T5.5 election at T5 close, coach proceeds directly to T6 from-scratch drafting; zero vendor calls when T5.5 not elected.
Fail: Coach calls a vendor without operator election.

#### Gate 11b: vendor-must-differ enforcement at T7
Pass: With T5.5 vendor recorded as Kimi DR, operator-elected spot-check at T7 with vendor=Kimi DR is REJECTED by enforce_vendor_must_differ(). Spot-check with vendor=Perplexity (different from T5.5 record) is accepted.
Fail: vendor-must-differ silently allows match.

#### Gate 11c: T12 vendor visibility
Pass: When T5.5 was elected and the thesis ships through approval, T12 final summary renders the vendor name + claim count + override list. When T5.5 was skipped, T12 omits the vendor section entirely (no empty stub).
Fail: Vendor section hidden when T5.5 elected, OR present with empty values when T5.5 skipped.

## Known follow-ups (deferred from 2026-05-04 Codex Agent re-review)

- **Schema-layer validation for verification claims (LOW from Codex Agent re-review, 2026-05-04).**
  Currently `scripts/lib/invest_coach_schemas.py` covers lived-signal + vendor_provenance schemas only.
  The verification claim schema has function-layer defense (`build_verification_result` + `validate_verification`)
  which is sufficient for Phase 3.8a MVP gates.
  Schema-layer addition would be defense-in-depth, not closing a live gap.
  Track for a Phase 4 hardening ship; not blocking Phase 3.8a.

- **Advisory-only flock semantics (MEDIUM from cycle-9 Kimi review, 2026-05-04).**
  `write_learning_stage_dial` uses `flock(LOCK_EX)` on a separate lock file.
  Advisory locks require all participants to cooperate; a non-cooperating process
  can still read/write `active_rules.md` without acquiring the lock.
  This is a fundamental property of POSIX flock, not a bug in the implementation.
  The K2Bi engine is the only writer of `active_rules.md`; all writers use the
  same lock protocol. Documented for Phase 4 if multi-process non-Python writers
  ever enter the picture.

- **atomic_write_bytes scope verification (HIGH from cycle-9 Kimi review, 2026-05-04 — verified FALSE).**
  Reviewer claimed `sf.atomic_write_bytes` might re-resolve the target path
  independently, bypassing the flock scope. Inspection of `strategy_frontmatter.py:97-136`
  confirms it uses the passed `Path` object directly (`path.parent`, `path.name`)
  without `resolve()` or independent path re-resolution. Finding is factually
  incorrect; no action required.

- **Lock file truncate(0) crash window (MEDIUM from cycle-9 Kimi review, 2026-05-04).**
  If a process crashes between `truncate(0)` and the data-file read, the lock
  file is left empty. This is harmless for flock (empty files work fine).
  The next caller reads the actual data file state under the lock and proceeds
  correctly. No live gap; defer to Phase 4 if crash-recovery observability
  becomes a priority.

## Tier assignment

**Portfolio Manager.** Does not migrate to Routines.

## Linked notes

- `K2Bi-Vault/proposals/2026-05-03_invest-coach-spec.md` -- full spec with D1-D10
- `K2Bi-Vault/proposals/2026-05-03_k2bi-ux-audit-operator-fit.md` -- motivating audit
- `K2Bi-Vault/wiki/insights/2026-04-30_calx-shadow-verification-rerun.md` -- failure mode the coach must respect
- `K2Bi-Vault/System/memory/self_improve_learnings.md` L-2026-04-27-001, L-2026-04-27-004, L-2026-04-27-005, L-2026-04-30-001
- `K2Bi-Vault/wiki/context/policy-ledger.jsonl` -- executable guards
