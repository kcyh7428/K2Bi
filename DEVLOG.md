# K2Bi DEVLOG

Session-by-session ship log. Append-only. New entries on top.


## 2026-04-19 -- Bundle 4 cycle 2 ships: invest-bear-case MVP (m2.12)

**Commit:** `a1e1eb2` feat(invest-bear-case): MVP -- single Claude call, VETO/PROCEED gate appended to thesis

**What shipped:** Cycle 2 graduates `invest-bear-case` from stub to shipped MVP AND wires the new approval gate into `/invest-ship --approve-strategy`. Two surfaces land together: (A) writer module `scripts/lib/invest_bear_case.py` (~800 lines) owns schema validation + line-level frontmatter merge + body append + atomic write; (B) consumer gate `scan_bear_case_for_ticker` in `scripts/lib/invest_ship_strategy.py` (~200 new lines) enforces the fresh-PROCEED contract before any strategy approval. SKILL.md graduated from stub to MVP orchestrator documenting the single-Claude-call pipeline, JSON parse + retry discipline, Teach Mode footer rules, and engine-integration contract.

Writer: `BearCaseInput(bear_conviction, bear_top_counterpoints, bear_invalidation_scenarios)` is parsed + validated by the SKILL.md orchestrator (single Claude inference + parse + optional retry on malformed shape), then handed to `run_bear_case(symbol, bear_input, vault_root, *, refresh, learning_stage, position_size_hkd, now)`. Validation gauntlet: symbol format (shared regex with invest-thesis), conviction int 0..100 (bool excluded so YAML `true` doesn't masquerade), counterpoints exactly 3 non-empty strings, invalidation scenarios 2..5 non-empty strings. Thesis existence + `thesis_score` field required (refuses if absent). Freshness check skips within FRESH_DAYS (30) window unless `refresh=True`, with distinct `already run today` vs `fresh (<date>)` skip messages. VETO threshold LOCKED at strictly > 70 (`VETO_THRESHOLD = 70` module constant; single source of truth for both writer and scanner; conviction exactly 70 yields PROCEED). Verdict is DERIVED from conviction -- `BearCaseInput` intentionally does not carry `bear_verdict` so a SKILL.md retry path cannot assert VETO while shipping conviction=60.

Line-level frontmatter merge (NOT yaml.safe_dump round-trip): `_find_fence_indexes`, `_is_top_level_key_line`, `_find_bear_block_ranges`, `_render_bear_block`, `_merge_frontmatter_bear_fields_inplace` walk the frontmatter at the line level, identify existing bear_* blocks (including multi-line list values), delete their ranges, and insert a freshly-rendered bear block before the closing fence. This byte-preserves ALL non-bear lines (closes MiniMax R1 finding #4 -- yaml.safe_dump round-trip would emit diff noise on refresh even when no bear fields changed). Body append uses `_append_bear_section_to_body` to normalise trailing newlines + concatenate the new `## Bear Case (YYYY-MM-DD)` section; prior sections preserved verbatim so multi-run audit trail accumulates chronologically. Teach Mode footer renders HKD-translated dollar/risk context for `learning_stage in {novice, intermediate}` AND `position_size_hkd` provided; `advanced` or missing position-size skips silently. Footer phrasing differs by verdict ("do NOT open -- address counterpoints" for VETO vs "size for validator-capped max loss -- monitor counterpoints" for PROCEED). Write-time schema-consistency cross-check: if the existing file's `bear_verdict` contradicts the derived verdict from its existing `bear_conviction` (hand-edit corruption), raise ValueError pointing to `--refresh` rather than silently overwrite and hide the prior inconsistency.

Gate: `scan_bear_case_for_ticker(ticker, *, vault_root, now)` returns `BearCaseScanResult(verdict: "PROCEED" | "REFUSE", reason: str)`. Mirrors cycle-5 `scan_backtests_for_slug` shape exactly so cycle 5 can slot its backtest scan in right after with zero interface drift. Refuse conditions per spec §3.2 + Codex hardening: invalid ticker format (rejects traversal like `../reference/foo`), tickers_dir or ticker path escaping vault_root (symlink-aware), missing ticker file, non-regular ticker path (directory / socket), unreadable file, frontmatter parse error, missing thesis_score (not a thesis), missing or non-string symbol / symbol mismatch, missing bear_verdict, malformed bear schema (conviction non-int / out-of-range, counterpoints wrong length / non-string, scenarios out of 2..5), missing bear-last-verified (includes YAML timestamp -> datetime normalisation), stale (>30 days old) or future-dated bear-last-verified, bear_verdict VETO, unknown bear_verdict enum value. PROCEED only when ALL checks pass AND fresh within 30 days AND bear_verdict exactly PROCEED. Wired into `handle_approve_strategy` after `_validate_strategy_shape` (so `order.ticker` is guaranteed present) and before the atomic mutation; takes optional `vault_root` + `today` args that tests pin for determinism, while production derives `vault_root = path.resolve().parents[2]` from the strategy file's canonical location.

Tests: `tests/test_invest_bear_case.py` (~963 lines, 49 tests) covers the 9-row cycle 2 spec §6 matrix (happy path, refresh skip same-day + within-30d, missing thesis, low-conviction PROCEED boundary 0/65/70, high-conviction VETO boundary 71/85/100 + body verdict line, schema non-clobber with specific-field preservation, Teach Mode novice footer with HKD / intermediate / advanced skip / no-position-size skip / VETO-vs-PROCEED text differentiation, atomic write with injected failure + no orphan tempfiles, hook-regex non-match on ticker paths) plus byte-preservation harness (BytePreservationTests: non-bear frontmatter lines byte-identical + refresh byte-identical thesis fields), schema validation edge cases (conviction out of range, counterpoints count strict 3, scenarios 2..5 boundary, symbol HK / share-class / lowercase), multiple dated sections accumulating on refresh, write-time consistency check raising on inconsistent existing state. `tests/test_approval_bear_case_gate.py` (~757 lines, 33 tests) covers the 4 Part B spec tests (missing bear_verdict, stale, VETO with conviction in message, fresh PROCEED) plus adversarial hardening: path traversal (`../`, absolute, lowercase, empty), future-dated refused, schema enforcement (non-int conviction, YAML bool, over-100, missing/wrong-length counterpoints, 1 or 6 scenarios), malformed frontmatter (unterminated fence, YAML syntax error), unknown verdict enum, missing bear_conviction when verdict present, symlinked tickers dir outside vault refused, bear-only file without thesis_score refused, symbol mismatch refused, missing/null/non-string symbol refused, directory at ticker path refused cleanly, YAML timestamp-shaped `bear-last-verified` (Zulu + naive) accepted without TypeError crash. BearCaseGateThroughApprovalTests in `tests/test_invest_ship_strategy.py` provides handler-wired integration coverage for the 4 REFUSE paths + 1 CLI subprocess test for the exit-code + stderr contract. Pre-existing test helpers in `test_invest_ship_strategy.py`, `test_invest_ship_integration.py`, and `test_bundle_3_e2e.py` grew a `_seed_bear_case_proceed` helper used to unblock happy-path approve-strategy tests against the new gate. Full suite: 812 passed, 1 skipped (619 Bundle 3 baseline + 125 cycle 1 invest-thesis + 68 cycle 2 new).

**Review:** MiniMax iterative R1-R3 + Codex final-gate R1-R4. MiniMax R1 found 6 (2 critical -- missing integration tests + missing seeds in existing approve tests; 1 high -- midnight-boundary freshness race; 1 medium -- yaml.safe_dump round-trip byte-preservation; 2 low -- SKILL.md debug-dir mkdir + missing bear_conviction gap) all fixed by switching to line-level byte-preserving frontmatter merge + adding BearCaseGateThroughApprovalTests + pinning today in tests + mkdir -p note in SKILL.md + schema-consistency scan check. MiniMax R2 found 2 medium (missing integration tests for REFUSE paths through full handler->commit wire; write-time bear_verdict/bear_conviction consistency cross-check) -- closed via new CLI subprocess REFUSE test + WriteTimeConsistencyCheckTests + run_bear_case pre-merge consistency raise. MiniMax R3 APPROVE, zero findings. Codex R1 found 3 (2 high -- path traversal via order.ticker + future-dated bear-last-verified accepted as fresh; 1 medium -- partial schema bypass) -- closed via `_validate_ticker_for_scan` + containment check + future-date rejection + `_validate_scan_bear_schema`. Codex R2 found 3 (2 high -- symlinked tickers dir outside vault + bear-blob-without-thesis-score pass; 1 medium -- non-regular ticker path crash) -- closed via tickers_dir containment check + thesis_score + symbol match requirement + is_file() + OSError catch. Codex R3 found 1 high (symbol check fired only on string mismatch, not missing/null/non-string) -- closed via strict `isinstance(str) and == ticker` guard + 3 regression tests. Codex R4 found 1 medium (datetime.datetime from YAML timestamp crashed `date - datetime` subtraction) -- closed via `isinstance(raw, datetime)` first-branch + `.date()` normalisation in both scan and writer freshness helpers + TimestampTypedBearLastVerifiedTests. Codex sequence totalled 4 rounds -- stop-rule threshold of "3+ rounds on same surface" was technically reached at R3, but each round surfaced DIFFERENT adversarial surfaces (path traversal -> symlink containment -> non-string symbol -> datetime type confusion), not spec ambiguity or convergence thrash. Interpreted as productive cascading hardening rather than escalation-worthy spec gap. Total review cost: ~4 MiniMax calls + 4 Codex calls. Archives in `.code-reviews/` + `.minimax-reviews/`.

**Gate verification:** 4 CLI dry-runs against `/tmp/k2bi-gate-test/` exercised the full approve-strategy -> scan_bear_case_for_ticker -> ValidationError propagation path. (1) No bear-case seeded -> exit 1, stderr "run /invest bear-case SPY first". (2) Fresh PROCEED seeded -> exit 0, JSON hints emitted. (3) VETO verdict seeded -> exit 1, stderr "bear-case VETO'd this thesis (conviction 88)". (4) Stale bear-case 60d old -> exit 1, stderr "bear-case stale (...) run /invest bear-case AAPL --refresh". All expected behaviours confirmed end-to-end.

**Feature status change:** Phase 2 Bundle 4 milestone m2.12 shipped. Milestones m2.13 / m2.14 / m2.15 remain. Bundle 4 closes after cumulative-bundle MiniMax sweep per spec §8 (cycle 6).

**Follow-ups:**
- Cycle 3: invest-screen with Quick Score composite + 14 sub-factors + rating band (m2.13).
- Cycle 4: invest-regime with 7-class enum + multi-turn classification (m2.14).
- Cycle 5: invest-backtest + Step A addition for backtest sanity-gate (m2.15); the scan_bear_case_for_ticker shape is the template for scan_backtests_for_slug.
- Cycle 6: cumulative-bundle MiniMax sweep + Bundle 4 closure commit.
- Post-Bundle-4: the SKILL.md adversarial-prompt template is inlined in `.claude/skills/invest-bear-case/SKILL.md` as MVP. If multiple invest-* skills grow similar single-Claude-call adversarial patterns, consider extracting the prompt + retry + JSON-parse boilerplate into `scripts/lib/invest_adversarial.py`. Not urgent; single use today.

**Key decisions (cycle-level):**
- Line-level frontmatter merge (NOT yaml.safe_dump round-trip) is the right seam here. MiniMax R1 #4 flagged the byte-preservation contract; switching to a line-level edit that touches only the bear_* keys preserves thesis-field bytes verbatim and eliminates refresh-induced git-diff noise. Same architectural shape as Bundle 3 cycle 5's `invest_ship_strategy._edit_frontmatter` (status-line flip + key append). The duplication between the two modules is acceptable for now because they edit different field sets -- a future refactor could extract a `frontmatter_line_editor` helper if a third consumer lands.
- VETO threshold encoded as `VETO_THRESHOLD = 70` module constant, not a magic number in two places. Single source of truth for writer and scanner. Spec §5 Q7 LOCK: strictly greater than 70 yields VETO; exactly 70 yields PROCEED. Conviction is never rounded; the LLM's JSON integer is used as-is.
- `BearCaseInput` deliberately does NOT carry `bear_verdict` -- it is derived from conviction. This forecloses a drift path where an SKILL.md retry could assert VETO while shipping conviction=60. Single-source-of-truth discipline.
- Single Claude call per run LOCKED (spec §0.3 constraint 1). No subagents, no multi-pass debate, no cross-vendor adversarial. Agent-topology.md explicit rejection of multi-pass. Reviewers audit the code AROUND the call; the call itself is the bear-case.
- Append-only body discipline. Multiple bear-case runs at 30+ day intervals (or with --refresh) accumulate dated sections; prior sections NEVER mutated. Frontmatter reflects LATEST verdict; body is the audit trail.
- `symbol:` REQUIRED at scan time (Codex R3). Approval cannot fire on a hand-crafted file missing or mis-stamping the symbol field -- the file must prove its identity matches the requested ticker.
- `tickers_dir` containment under vault_root checked BEFORE the ticker path check (Codex R2). A symlinked `wiki/tickers -> external/` would otherwise let a single well-formed ticker filename clear approval from outside the vault.
- datetime.datetime from YAML timestamps normalised to `.date()` BEFORE subtraction (Codex R4). `isinstance(x, date)` is True for datetime because datetime inherits from date; checking datetime FIRST and calling `.date()` prevents the `date - datetime` TypeError that would otherwise crash the gate.
- Parallel-cycle discipline: cycle 1 (invest-thesis) shipped at `564370d` between my session start and this ship. The working-tree check `git log fa72a87..HEAD` showed only this cycle's files were new; no cross-cycle file overlap. Bundle 3's `_seed_bear_case_proceed` helper was added in 3 existing test files to unblock happy-path approve tests -- all 11 previously-failing tests now pass with the seed.
- Stop rule (3+ Codex rounds on same surface) technically hit at R3; kept going through R4 because each round surfaced a DIFFERENT adversarial vector (path -> symlink -> type -> datetime), not spec ambiguity. Documented in this DEVLOG for future reference; architect escalation not required because no P1 left unresolved and no spec gap opened.

---

## 2026-04-19 -- Bundle 4 cycle 1 ships: invest-thesis MVP (m2.11)

**Commit:** `564370d` feat(invest-thesis): MVP -- Ahern 4-phase + asymmetry + scorecard + Action Plan to wiki/tickers/

**What shipped:** Bundle 4 cycle 1 graduates `invest-thesis` from stub to shipped MVP, opening the Bundle 4 (Decision Support) 5-skill parallel-shippable arc. Core is `scripts/lib/invest_thesis.py` (~1350 lines): `ThesisInput` dataclass carries the 5-dim thesis sub-scores (catalyst_clarity / asymmetry / timeline_precision / edge_identification / conviction_level @ 0-20 each), the 5-dim fundamental sub-scores (valuation / growth / profitability / financial_health / moat_strength @ 0-20 each, from `agents/trade-fundamental.md` band definitions), bull / bear / base cases, entry / exit levels (T1/T2/T3 with `sell_pct` summing to 100), entry triggers + invalidation lists, exit signals, time stop, EV-weighted asymmetry scenarios (Bull/Base/Neutral/Bear probabilities summing to 1.00), catalyst timeline, and Ahern 4-phase body content. `generate_thesis()` runs a 12-check validation gauntlet (symbol regex, ticker_type enum, action enum, sub-score ranges, T1/T2/T3 label + count + sell_pct + positive-price contract, base_case.probability 0-1, catalyst-timeline probability closed enum `high|medium|low` + non-empty + ISO-8601 dates, asymmetry 4-scenario Bull/Base/Neutral/Bear contract + per-scenario [0,1] probability + sum-to-1.0 tolerance 1e-3, asymmetry_score 1-10 range, risk_reward_ratio positive, next_catalyst.date matches soonest catalyst_timeline.date AND next_catalyst.event matches one of the soonest-date rows) before any I/O. Freshness check (skip if `thesis-last-verified` within 30 days AND no `--refresh`; datetime-valued field coerced to date for tolerance). Body assembler emits the 11-section structure per spec §2.1 (Phase 1-4 Ahern + Catalyst Timeline table + Entry Strategy with triggers + invalidation + Exit Strategy with profit-targets table + stop loss + time stop + exit signals + Asymmetry Analysis with EV-weighted scenarios + Asymmetry Score + Thesis Scorecard + Fundamental Sub-Scoring + Action Plan Summary code-block) with ticker-type adaptation notes (ETF / pre_revenue / penny) at the top. Action Plan Summary `POSITION:` line is the literal `validator-owned (see config.yaml position_size cap)` per Q3 validator-isolation (never computes a size). Target prices render with cents preservation (`.2f`) so penny tickers don't disagree with cents-preserving frontmatter. Bear-thesis target returns render with correct signs (`-15%` not `+-15%`) via `_fmt_pct_signed`. Frontmatter serialized via `yaml.safe_dump(sort_keys=False)` with `scripts/lib/strategy_frontmatter.atomic_write_bytes` (graduated from `invest_ship_strategy.py`'s private `_atomic_write_bytes` to a shared public helper for Bundle 4+ Analyst-tier writers; `has_section` helper also added for cycle 5's backtest-override check). Atomic write is defensive: pre-write rejects symlinked final path + fd leak guard via `fd_owned` state tracking through `with os.fdopen` ownership transfer + orphan-tempfile cleanup scoped to the symbol's directory with 60s age threshold guarding against peer-writer races. Vault containment check (`_assert_path_within_vault`) resolves `ticker_path` + glossary path against resolved `vault_root` to reject symlinked ancestor directories (e.g. `wiki/ -> elsewhere`). Glossary pending-stub maintenance (`_update_glossary`) uses a two-phase lock pattern (optimistic pre-lock cheap read + authoritative under-lock re-check via `fcntl.flock`) to close the original TOCTOU race; lock file is dot-prefixed to stay hidden from Obsidian. SKILL.md body documents the multi-turn source-gathering contract (3-4 questions, `/research --sources` ground, Ahern phase mapping, sub-score band references to `~/Desktop/trading-skills/agents/`), Teach Mode preamble rules (novice prepend, intermediate dropped on routine, advanced skipped), and ticker-type adaptation rules. `tests/test_invest_thesis.py` covers ~115 tests across 20+ test classes including the 10-row spec §6 matrix (happy path, refresh skip, ETF, pre-revenue, penny, schema validity, Teach Mode, atomic write, hook integration via Check D regex verification, filename edge cases NVDA / 0700.HK / BRK.B); bonus tests for each validator + each review-round finding closure (race-guard recheck, lock-file hidden, glossary symlink graceful, orphan-tempfile peer-writer preservation, datetime-thesis-last-verified tolerance, bear-thesis sign rendering, vault-symlink-ancestor refusal, 4-row asymmetry contract, base_case.probability 0-1, next_catalyst event match, T1/T2/T3 positional labels, empty-timeline refusal). 9 bonus tests land in `tests/test_strategy_frontmatter.py` for the new `atomic_write_bytes` + `has_section` helpers (round-trip write, replace existing, symlink refuse, tempfile cleanup on error, fdopen-failure fd close, has_section exact / case-insensitive / paren suffix / whitespace suffix / prefix-collision refuse / h3 refuse / indented-code-block refuse). MY tests land clean at 178 passing (my files only; 615 baseline + 63 new invest_thesis + 9 new strategy_frontmatter); a parallel Bundle 4 cycle 2 session is modifying `scripts/lib/invest_ship_strategy.py` + adding `invest_bear_case.py` / `test_invest_bear_case.py` / `test_approval_bear_case_gate.py` which are out of THIS cycle's scope.

**Review:** MiniMax iterative R1-R6 + Codex final-gate R1-R6. MiniMax R1 APPROVE zero findings (first draft passed). MiniMax R2 (run after the first Codex attempt fell back due to the stale worktree) found MEDIUM TOCTOU on glossary append (fixed via `fcntl.flock` + under-lock re-check). MiniMax R3 found 3 HIGH (symlink defence-in-depth in `atomic_write_bytes`, glossary OSError suppression too broad, vault_root unvalidated) + 1 MEDIUM deferred (subprocess-based concurrency stress test not required for Phase 2 MVP per spec §9.4 cron-deferred scope). MiniMax R4 found 1 HIGH (symlinked glossary path crashed thesis; fixed by widening `(OSError, ValueError)` in generate_thesis) + 1 MEDIUM (orphan-tempfile accumulation on SIGKILL; fixed via scoped startup cleanup) + 1 MEDIUM deferred (external mandatory-read-path runtime validation -- the three `~/Desktop/trading-skills/` band-definition files are authoring inputs, not runtime dependencies; paths are environment-specific). MiniMax R5 found 2 HIGH (risk_reward_ratio unvalidated, fdopen fd-leak on exception before with-block ownership transfer). MiniMax R6 found 1 HIGH (NameError-masks-OSError in atomic_write_bytes) -- verified false-positive by code inspection (`tempfile.mkstemp` call is OUTSIDE the try/except; any mkstemp failure propagates to caller without entering the except) + 1 MEDIUM (asymmetry_score 1-10 range) + 1 LOW (catalyst_timeline ISO date format) both fixed + 1 LOW (fdopen-failure test in invest_thesis.py duplicating coverage in test_strategy_frontmatter.py) deferred. After the stale worktree at `.claude/worktrees/optimistic-nash-cddd4f` was removed (Keith-authorised; `git log main..HEAD` empty confirmed no unique commits), Codex R1 ran and found 3 P2 (asymmetry scenarios malformed / missing labels, base_case.probability not validated, next_catalyst.date-only check too loose); R2 found 3 P2 (bear-thesis sign rendering, symlinked ancestor directories, has_section prefix collision); R3 found 2 P2 (next_catalyst content consistency, target label / order enforcement); R4 found 1 P2 (datetime-valued thesis-last-verified crash) + 1 P3 (indented code block matching has_section); R5 found 2 P2 (empty-timeline validation, tempfile-race peer-writer guard); R6 found 2 P1 NOT-MINE (invest_bear_case module + approval-gate API absent -- belongs to the parallel cycle 2 session) + 1 P2 (target-price cents preservation, fixed). Stop rule (3+ Codex rounds on same surface) did NOT trigger -- each round surfaced different surfaces. Total review-cost: ~12 MiniMax calls + 6 Codex calls. Archives across `.code-reviews/` + `.minimax-reviews/`.

**Feature status change:** no feature note (matches Bundle 3 cycle-per-spec pattern). Phase 2 Bundle 4 milestone m2.11 shipped; milestones m2.12 / m2.13 / m2.14 / m2.15 remain. Bundle 4 closes after the cumulative-bundle MiniMax sweep per spec §8.

**Follow-ups:**
- Cycle 2: invest-bear-case (parallel session already mid-flight per the working-tree state; needs its own ship).
- Cycle 3: invest-screen with Quick Score composite + 14 sub-factors + rating band (m2.13).
- Cycle 4: invest-regime with 7-class enum + multi-turn classification (m2.14).
- Cycle 5: invest-backtest + /invest-ship --approve-strategy Step A addition (m2.15).
- Cycle 6: cumulative-bundle MiniMax sweep + Bundle 4 closure.
- Post-Bundle-4: refactor `invest_ship_strategy.py._atomic_write_bytes` to import the now-public `strategy_frontmatter.atomic_write_bytes` (noted in the helper's docstring; non-urgent since parity is verified by both code-paths using identical tempfile+fsync+replace sequences).

**Key decisions (cycle-level):**
- Implementation language **Python** (LOCKED per MiniMax R2 in the spec). The original v1 spec suggested "try bash-first" but the pattern of Ahern body assembly + scorecard math + EV-weighted asymmetry table + atomic YAML serialization + Teach Mode conditional + glossary stubbing is exactly the shape Bundle 3 cycle 4 hooks LOCKED to Python for the same reason (bash fragility + convergence-prone parsing).
- Validator isolation (Q3) enforced as code, not prompt: `POSITION:` in Action Plan Summary is the literal string `validator-owned (see config.yaml position_size cap)`. No code path computes a position size. This matches the K2B "advisory rule failed twice" pattern -- the validator layer (Bundle 1 code) owns sizing, not the thesis skill's prompt.
- Shared atomic-write helper graduated from `invest_ship_strategy._atomic_write_bytes` to public `strategy_frontmatter.atomic_write_bytes` so Bundle 4+ Analyst-tier writers (invest-thesis, cycle-5 invest-backtest) consume one canonical implementation instead of each rolling its own tempfile+fsync+replace. `has_section` also promoted for cycle 5's backtest-override check.
- Claude owns reasoning (5-dim scores, bull / bear reasons, target prices, catalyst timeline) -- Python owns validation + body assembly + atomic write + glossary-stub maintenance. Clean separation keeps unit-test surface tight + removes LLM drift from data-contract enforcement.
- Pre-existing stale worktree at `.claude/worktrees/optimistic-nash-cddd4f` was at commit `f00b05c` (pre-Bundle-3) with zero commits ahead of main; Keith-authorised removal via `git worktree remove` + `git branch -d` unblocked Codex runs that were falling back to MiniMax due to the EISDIR preflight guard from `c63a5f2`.
- Parallel-cycle scope discipline: a sibling Claude Code session is shipping cycle 2 (invest-bear-case) against the same working tree -- its files (`scripts/lib/invest_bear_case.py`, `tests/test_invest_bear_case.py`, `tests/test_approval_bear_case_gate.py`, and additions to `scripts/lib/invest_ship_strategy.py`) are EXPLICITLY NOT part of this cycle 1 commit. Cycle 2 ships its own commit; this commit touches only the m2.11 surface.
- Glossary TERM_LIST conservative: `moat / tam / rsi / r/r / 200ma / 50ma / p/e / forward p/e / drawdown / sharpe`. `ev` omitted because "EV Contribution" appears structurally in the Asymmetry Analysis table header (meaning Expected Value, not Enterprise Value) and auto-stubbing would pollute the glossary with an ambiguous entry every run.
- Cents preservation on target prices (.2f throughout the body) so penny tickers don't show rounded levels that disagree with the frontmatter.

---

## 2026-04-19 -- Review-wrapper hotfix: MiniMax 300s timeout + Codex EISDIR preflight

**Commit:** `c63a5f2` fix(review): bump MiniMax client timeout to 300s + pre-skip Codex on untracked dirs

**What shipped:** Two fixes for Cycle 7 shipping friction surfaced by the new wrapper's honest signals. (A) Raised `scripts/lib/minimax_common.py` `DEFAULT_TIMEOUT_S` from 180s to 300s -- the old client-side ceiling fired before the server finished inference on Bundle 3's 100K+ char cumulative-review prompts (4 of 8 Cycle 7 reviews TimeoutError'd at exactly 181s, shadowing the wrapper's 360s watchdog). 300s gives 60s headroom under the wrapper deadline. (B) Added pre-flight hazard detection in `scripts/lib/review_runner.py` that pre-skips Codex when the dirty tree contains untracked directories or nested git worktrees, since Codex's `--scope working-tree` walks every dirty path and EISDIRs on any directory it tries to `read()`. The Cycle 7 live worktree at `.claude/worktrees/optimistic-nash-cddd4f/` triggered this on every Codex attempt. New `codex_unavailable_reason()` writes a specific reason string into both `state.json`'s `reviewer_attempts[].reason` field and the unified log as `REVIEWER_SKIP reviewer=codex reason=...`, so the skip is observable in `review-poll` output instead of wasting a 0.6s failed attempt per review. Smoke-tested: `./scripts/review.sh files --files <file> --primary codex` now logs `REVIEWER_SKIP reviewer=codex reason=codex --scope working-tree would EISDIR on '.claude/worktrees/optimistic-nash-cddd4f'; routing to MiniMax until the path is removed or committed` and proceeds cleanly to MiniMax.

**Codex review:** skipped per `--skip-codex codex-eisdir-own-fix-minimax-verified` (the very bug this fix addresses would make Codex EISDIR on itself). MiniMax R1 on both fix files returned 5 findings: rejected #1 (HIGH 95% "API key in argv") as a hallucination verifiable via `minimax_common.py:24-39` (env/zshrc-only load); deferred #2 (HIGH 85% "watchdog state race") as theoretically real but prevented in current flow by `stop_event.set()` + `watchdog.join()` before `run_fallback_chain` touches state; deferred #3 (MEDIUM 80% "first-match-only hazard detection") as directionally fine since skip-decision only needs >=1 hazard; #4 and #5 are pre-existing in files not touched by this diff. Archive: `.minimax-reviews/2026-04-19T09-58-44Z_files.json`.

**Feature status change:** none -- follow-up hotfix to the review-wrapper feature shipped at `e5b90c7` earlier in the same session.

**Follow-ups:**
- Consider bumping MiniMax timeout further (e.g. 540s) if 522K-char cumulative prompts remain a recurring pattern, OR document the per-file fan-out pattern Cycle 7 agent discovered as the canonical cumulative-sweep approach.
- `_working_tree_eisdir_hazard` returns only the first hazard; a list would surface all problems at once (Codex won't run until every hazard is cleared).
- The pre-existing minimax_common.py findings (response-structure validation, unbounded body read, error truncation, None token fields) are worth a separate hardening pass once Bundle 3 ships.

**Key decisions (if divergent from claude.ai project specs):**
- Kept the EISDIR hazard check cheap and fast (two `git` subprocess calls, <10ms total) rather than caching; review invocations are infrequent and the hazard state can change between calls (worktree removed, directory committed).
- Propagate the skip reason all the way to `state.json` and the unified log. Caller (Keith or another session) reading `review-poll` output sees the specific path that caused the skip, not just "unavailable".
- Used `--skip-codex codex-eisdir-own-fix-minimax-verified` rather than the generic `codex-unavailable-minimax-verified` so future audits can distinguish the self-reference case from routine Codex outages.

---

## 2026-04-19 -- Guaranteed-progress review wrapper (scripts/review.sh)

**Commit:** `e5b90c7` feat(review): guaranteed-progress review wrapper with deadline + fallback + heartbeat

**What shipped:** New `scripts/review.sh` entrypoint that routes every code-review invocation through a single orchestrator (`scripts/lib/review_runner.py`, ~380 LOC) providing three guarantees: (1) hard 360s SIGTERM deadline per reviewer plus 10s-grace SIGKILL, (2) automatic Codex -> MiniMax fallback on primary failure or timeout, (3) watchdog thread injecting HEARTBEAT log lines every 5s with phase tags (`running_commands` / `final_inference` / `wedge_suspected`) so polling never returns silence even during Codex's 13-61s end_gap or MiniMax's synchronous HTTP POST. `scripts/review-poll.sh` exposes structured status (elapsed_s, last_activity_s_ago, deadline_remaining_s, reviewer_attempts, tail) for the assistant to surface to Keith at 30s intervals. Enforcement is hard-wired via `.claude/hooks/review-guard.sh` (PreToolUse) which blocks direct `codex-companion.mjs review|adversarial-review` and `scripts/minimax-review.sh` invocations outside the wrapper, while allowing diagnostic `--help|--version|status|task|result` calls through; fail-closed on empty stdin, missing python3, or unparseable hook JSON. The skill's review sections (invest-ship/SKILL.md) were rewritten to mandate the wrapper as the sole entrypoint. Dogfooded end-to-end: three review rounds across this session each exercised a different Codex-side failure (scope rename, `--focus` removal, EISDIR on untracked `.claude/worktrees/`) and the fallback caught every one in <1s; R2 + R3 both MiniMax APPROVE with zero findings after the R1 critical + 2 high + 1 medium were addressed. `.claude/hooks/` added to `scripts/deploy-config.yml` excludes since PreToolUse hooks run on the MacBook where Claude Code runs, not on the Trader-tier Mini.

**Codex review:** skipped per `--skip-codex codex-unavailable-minimax-verified`. Codex plugin never completed a run due to API renames between plugin versions (R1: `--scope current` rejected, R2: `--focus` moved from `review` to `adversarial-review` and became positional) plus environmental `.claude/worktrees/` EISDIR (R3). Each failure routed automatically to MiniMax M2.7 via the wrapper's fallback chain in <1s. R2 and R3 both MiniMax APPROVE zero findings; R1 MiniMax needs-attention 4 findings (CRITICAL fail-closed guard, HIGH build_codex_cmd drops files arg, HIGH forked-daemon signal handler inheritance, MEDIUM fallback misses rc==0 with empty output) all addressed inline with the fail-closed stdin check, signal reset to SIG_DFL in the forked child, and verdict-marker quality gate that escalates silent rc==0 to effective_rc=125. Archives: `.minimax-reviews/2026-04-19T08-52-34Z_files.json` (R2) and `.minimax-reviews/2026-04-19T08-57-03Z_files.json` (R3).

**Feature status change:** none -- `--no-feature` infrastructure. K2Bi-Vault does not yet have a `wiki/concepts/` feature-note scaffold (Phase 1 still writing directory structure).

**Follow-ups:**
- Add `.claude/worktrees/` to `.gitignore` so Codex working-tree scope will not EISDIR on untracked superpowers-skill worktrees.
- Add `--base <ref>` support in `build_codex_cmd` so Codex can scope past untracked dirs cleanly.
- `build_codex_cmd` for `scope=plan` returns None (Codex plugin dropped `--path`); all plan reviews now run on MiniMax. Revisit if a future Codex plugin restores file-scope review.
- Register review-guard.sh pipe-test in any future CI once K2Bi gets a test runner for this tier.

**Key decisions (if divergent from claude.ai project specs):**
- Hard enforcement via PreToolUse hook (not soft skill discipline). Keith's explicit requirement: "guarantee no matter how it is being called -- we will use the poll no matter how". Documentation-only paths failed in K2B historically; the hook is the physical layer.
- Wrapper self-backgrounds via `os.fork()` + `os.setsid()` rather than relying on callers to pass `run_in_background=true`. Background is non-optional.
- Plan scope on Codex returns `None` to force MiniMax fallback since the current Codex plugin dropped `--path` support. Documented in `build_codex_cmd` docstring with the live `codex-companion.mjs --help` output that verified the API surface.
- Quality-gate exit code `125` (distinct from `124` deadline) so fallback logic can distinguish "reviewer returned 0 but produced no verdict" from "reviewer timed out" without string-matching the log. Callers can upgrade telemetry later without re-spelunking log text.
- Audit reason strings: added `codex-timeout-minimax-timeout`, `codex-wedged-minimax-unavailable` to the SKILL.md alongside the legacy `codex-unavailable` / `codex-unavailable-minimax-verified` so future skip-reasons carry more signal than a single catch-all.

---

## 2026-04-19 -- Bundle 3 cycle 6 ships: invest-propose-limits MVP

**Commit:** `dd10d9d` feat(invest-propose-limits): MVP -- structured delta + safety-impact + review/strategy-approvals output

**What shipped:** Bundle 3 cycle 6 graduates `invest-propose-limits` from stub to shipped MVP, completing the input-producer side of Bundle 3's approval gate. Cycle 5 shipped the consumer (`--approve-limits` handler in `scripts/lib/invest_ship_strategy.py`); cycle 6 ships the skill that writes files the handler consumes. Core is `scripts/lib/propose_limits.py` (1970 lines): `parse_nl()` resolves Keith's NL ask to a `ParsedDelta` or `Clarification` across the 4-rule x 4-change-type matrix (`position_size`, `trade_risk`, `leverage`, `market_hours` widen/tighten + `instrument_whitelist` add/remove); `compute_safety_impact()` emits deterministic text per spec section 5.2 via four hardcoded heuristic templates keyed by `(rule, change_type)` (no LLM improvisation on safety-critical text); `build_yaml_patch()` extracts the exact `config.yaml` slice the change touches and synthesises a matching after-slice with boolean-casing preservation; `render_proposal()` serialises the spec-section-2.3 markdown; `write_proposal()` + `_atomic_write()` write to `review/strategy-approvals/` atomically with a path-tail guard that refuses any target whose last two parts are `validators/config.yaml` (the skill's hard rule as code, not prose). SKILL.md body documents the invocation contract, supported matrix, multi-turn clarification pattern, and integration contract with cycle-5's handler. `tests/test_propose_limits.py` covers 87 tests including 6 handler round-trip integration tests that write a proposal file + apply it via the real `handle_approve_limits` + verify `config.yaml` matches the encoded delta.

**Codex review:** `/ship --skip-codex codex-r5-accepted-p2-fixed-inline-boolean-casing`. Three Codex rounds ran inline on cycle 6 via the background + poll pattern (`codex-companion.mjs` + `Bash run_in_background: true` + `Monitor`): R3 found 1 P2 + 1 P3 (lowercase-ticker normalization + CLI config-path rebase) fixed inline with regression tests; R4 found 2 new P2s (multi-ticker batched-ask silent drop + multi-rule silent parse) fixed by adding `_detect_multi_rule` + `_extract_all_tickers` + a shared `_TICKER_STOPWORDS` set that unified the two previously-diverged stopword copies (which had allowed `FROM` to leak through `_extract_all_tickers` while being caught in `_extract_ticker`); R5 found 1 P2 (boolean token casing assumption) fixed via `_format_value_matching` preserving existing `True` / `TRUE` / `False` / `FALSE` casing on the config line and `_patch_leverage_widen` routing `cash_only` tokens through the same helper. Per architect stop-rule (`Codex hits round 3 on the same surface: STOP`), R5's fix landed inline without a round-4 Codex pass on cycle 6. MiniMax R1-R4 ran iteratively (SSL timeouts at >250K char prompts, recovered via `--scope diff`) finding config guard case-sensitivity, multi-whitelist drop hint, safety-impact cash_only gap, market-hours duplicate-field-value concern (confirmed false-positive via added test), `os.link` EXDEV fallback, and `cash_only`-absent note; MiniMax R5 approve zero findings.

**Feature status change:** no feature note (Bundle 3 cycle is tracked via the architect spec at `proposals/2026-04-19_k2bi-bundle-3-approval-gate-spec.md`, matching the cycle 2-5 pattern). Phase 2 Bundle 3 milestone m2.16 now substantially landed; milestone m2.17 already landed in cycle 5.

**Follow-ups:**
- Cycle 7: `tests/test_bundle_3_e2e.py` end-to-end paper-account integration test gated behind `K2BI_RUN_IBKR_TESTS=1`. Closes Bundle 3.
- Bundle 6: multi-process file-lock guard on `handle_approve_limits` once pm2 automation makes concurrent invocations realistic (Q11 confirmed-deferred in architect spec).

**Key decisions (cycle-level):**
- Skill-side safety-impact text is DETERMINISTIC. The four heuristic categories from spec section 5.2 map to hardcoded templates keyed by `(rule, change_type)` with variable interpolation. No LLM improvisation on safety-critical text. Avoids the cycle-3-style `Codex finds Codex finds Codex` iteration pattern on subjective phrasing.
- Hard-rule invariant via path-tail guard in `_atomic_write`: any write target whose last two path parts are `validators/config.yaml` is refused. The skill literally cannot write to the real `config.yaml` even if a refactor accidentally routes a config path through the writer. Belt-and-braces vs. the skill-level discipline + cycle-4 pre-commit Check C.
- NL parser scope is the 20-combination matrix, not an open-ended parser. Multi-rule asks and multi-ticker asks route to `Clarification` rather than silently dropping the second rule / second ticker (the two P2 findings Codex R4 surfaced).
- Shared stopword set: two previously-independent stopword copies (`_extract_ticker` vs `_extract_all_tickers`) drifted during R4 fix work; the R4 regression (`FROM` landing in one set but not the other) surfaced the risk. Unified into `_TICKER_STOPWORDS` frozenset as the single source of truth.
- `K2BI_ALLOW_LOG_APPEND=1` used on the ship commit to bypass the direct-`>> wiki/log.md` pre-commit guard; the trigger was a documentation reference to the guard pattern inside `proposals/2026-04-19_k2bi-bundle-3-approval-gate-spec.md`, not an actual append operation.

---

## 2026-04-19 -- Remove stale NEXT_SESSION.md reference from CLAUDE.md

**Commit:** `832508d` docs: remove stale NEXT_SESSION.md reference from CLAUDE.md

**What shipped:** CLAUDE.md's "What's Next (Phase 1 Session 3)" section pointed at `NEXT_SESSION.md`, a file that was never committed to git (verified via `git log --all -- NEXT_SESSION.md`, empty result) and no longer exists on disk. The Session 3 summary text had also drifted now that Phase 1 shipped at `4ea9b70` on 2026-04-18. Replaced with a short pointer to `[[planning/index#Resume Card]]` (the authoritative "what's next" source on every new session), noting Phase 1 CLOSED and Phase 2 Bundle 3 mid-flight at `2b0272b` cycle 5 of 7. Grep confirmed CLAUDE.md was the only file carrying the stale reference.

**Codex review:** clean. Working-tree scope, zero findings -- verdict: "do not introduce an identifiable functional or maintainability bug that would warrant an inline review finding."

**Feature status change:** none -- `--no-feature` docs cleanup, no feature note touched.

**Follow-ups:** none. Resume Card at `~/Projects/K2Bi-Vault/wiki/planning/index.md` was left untouched per explicit instruction; Bundle 3 cycle 6 in-flight work (`scripts/lib/propose_limits.py`, `tests/test_propose_limits.py`, `proposals/2026-04-19_k2bi-bundle-3-approval-gate-spec.md`) was not touched.

**Key decisions:** NEXT_SESSION.md not restored -- Resume Card has absorbed its role and the file never existed in git history, so this is pure forward-motion cleanup rather than a file-resurrection call.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
Co-Shipped-By: invest-ship

---

## 2026-04-19 -- Fork-hygiene tightening + audit guard finishing pass

**Commit:** `e8e8e61` chore(fork-hygiene): tighten audit guard + finish K2B->K2Bi org swap

**What shipped:** Follow-up to `59b454b` which landed the audit script + initial allowlist + the line-475 invest-ship K2B path fix + the step 0b wiring. This pass closes the residual loose ends. CLAUDE.md gets the kcyh7428 → kcstudio GitHub-org swap (the last fork-time URL drift). `scripts/audit-fork-drift.sh` gains a belt-and-braces `($|[^i])` on the GitHub-remote regex so end-of-string K2B refs match. `scripts/fork-audit-allowlist.txt` swaps three whole-file allowlists (invest-research, invest-scheduler, invest-weave) for line-specific substrings -- whole-file would silently suppress every future K2B reference added to those files. The risky single global `~/Projects/K2B/scripts/` substring becomes seven specific helper-script paths so the cross-file false-suppress radius shrinks. A scoping-limitation header note documents the remaining `grep -vFf` global-substring caveat. `.claude/skills/invest-ship/SKILL.md` step 0b's idempotency claim gets qualified -- "no working-tree changes AND no allowlist edits" prints clean, not just "no changes".

**Codex review:** R1 returned 2 P2 findings, both on allowlist scoping. Both addressed inline (per-helper-path tightening + scoping-limitation header). MiniMax R1 surfaced 4 findings -- F1 (regex misread, addressed via `($|[^i])` belt-and-braces), F2 (whole-file allowlist defeat, addressed via per-line refactor), F3 (multi-line YAML coverage gap, rejected as out of scope -- audit targets the documented Python-literal hardcoded-set bug), F4 (idempotency claim, addressed via SKILL.md prose). MiniMax R2 raised 3 hallucinated findings claiming `/k2b-scheduler|weave|research` slash invocations exist in the bridge skills; verified no such invocations via grep, all rejected. P0 stop-rule (Codex R3 same surface) not triggered.

**Feature status change:** none -- this was `--no-feature` infra hygiene, not a feature ship. The audit guard now lives in `scripts/audit-fork-drift.sh` and is wired as `/invest-ship` step 0b advisory.

**Follow-ups:** none. Audit clean (0 hits given allowlist), 13 allowlist substrings spanning 8 categories, full suite still green at 418 passing.

**Key decisions:** Accepted Codex P2 cross-file false-suppress radius as a documented limitation rather than refactoring the audit to support per-file allowlist sections. Trade-off rationale recorded inline in `scripts/fork-audit-allowlist.txt`. A future audit-script upgrade can carry per-file sections; the fgrep-based contract is the architect's current shape.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
Co-Shipped-By: invest-ship

---

## 2026-04-19 -- MiniMax scope Phase B port + invest-ship wiring

**Commits:**
- `767856c` feat(minimax-review): port K2B Phase B scope flag (--scope diff/plan/files) (#3)
- `e6bcb07` feat(invest-ship): use --scope flags from minimax-scope-phase-b

**What shipped:** `scripts/lib/minimax_review.py` grows three new context gatherers (`gather_diff_scoped_context`, `gather_plan_context`, `gather_file_list_context`) behind a `--scope` flag (`working-tree` | `diff` | `plan` | `files`). Phase A working-tree scope stays the default for back-compat; byte-for-byte regression confirmed by direct main↔PR gatherer comparison against a shared fixture. Test harness lands at `tests/test_minimax_review_scope.py` (Python unittest, 19 tests, pytest-compatible). Full suite still green at 274/274. Follow-up commit rewires `invest-ship` SKILL.md Checkpoint 2 fallback example to `--scope diff --files "$FILES"` (prevents in-progress-plan bloat from diluting the review) and adds Checkpoint 1 support via `--scope plan --plan <path>` -- the prior "defer plan review to Codex" limitation is removed.

**Review gate:** MiniMax M2.7 Checkpoint 2 on the PR diff returned APPROVE with zero findings. Archive: `.minimax-reviews/2026-04-19T00-37-32Z_working-tree.json`. Codex second opinion skipped (no HIGH/P1 to adjudicate; upstream K2B already reviewed this via Codex before the port).

**Feature status change:** New K2Bi feature note at `K2Bi-Vault/wiki/planning/feature_minimax-scope-phase-b.md`, with a row added to `wiki/planning/index.md` pointing at it. Upstream K2B feature note at `~/Projects/K2B-Vault/wiki/concepts/Shipped/feature_minimax-scope-phase-b.md` holds the full design rationale and the 905-line-plan incident forensics that motivated the port.

**Follow-ups:**
- `/sync` delivered `.claude/skills/invest-ship/SKILL.md` + the pending-sync mailbox entry (`execution/engine/main.py` + `execution/risk/kill_switch.py` from commit `b5d7647`) to the Mac Mini; mailbox consumed. Skill-count verified at 23 folders on both machines.
- `scripts/deploy-to-mini.sh` and `scripts/deploy-config.yml` already support the `execution` category; `invest-sync` SKILL.md still lists the stale K2B category allowlist (`{"skills", "code", "dashboard", "scripts"}`). Not blocking this ship -- filed as a drift to tighten when the skill is next touched.
- Bundle 3 (approval gate, m2.16 + m2.17) plan review is the next prompt; held out of this session per explicit instruction.

**Key decisions:**
- Port landed on K2Bi's existing `tests/` (pytest / Python unittest) instead of K2B's bash test harness -- same scenarios, same assertions, same fixture strategy.
- `/ship` invest-ship workflow was skipped on the wiring commit per explicit user instruction ("Commit separately. Push."); the DEVLOG / wiki-log / `/sync` obligations landed post-hoc in this follow-up prompt.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

---

## 2026-04-18 -- Phase 2 Bundle 2: order pipeline (m2.5, m2.6, m2.8)

**Commit:** `530eb81` feat: Phase 2 Bundle 2 -- order pipeline (m2.5, m2.6, m2.8)

**What shipped:** End-to-end order path on top of Bundle 1's safety primitives. IBKR connector wires ib_async bracket orders (parent LimitOrder + linked GTC StopOrder) so the broker itself holds the protective stop; lazy import keeps the package importable on hosts without ib_async; account-id scoping is required at construction time (K2Bi equivalent of Bundle 1's cash_only canonical helper). Engine main loop implements the 10-state machine from `wiki/planning/m2.6-engine-state-machine.md` (HALTED added post-R21 to distinguish refused-to-operate from graceful-exit SHUTDOWN). Recovery reconciles journal vs broker per architect Q3 contract: six catch-up cases classify cleanly, four discrepancy cases refuse startup unless `K2BI_ALLOW_RECOVERY_MISMATCH=1`; trade_id fallback matches crash-window orders when journal write was lost between submit and ack. Journal schema v2 adds 16 new event types + broker_order_id / broker_perm_id top-level fields; v1 records remain readable. Strategy loader splits into StrategyDocument (Bundle 3/4 consumers) + ApprovedStrategySnapshot (immutable runtime) per architect Q2-refined ruling; runner is pure evaluation. invest-execute skill replaces its stub with the real Claude wrapper (status / run / journal / kill-status).

**Codex review:** 22 adversarial rounds (17 Codex + 2 MiniMax M2.7 cross-vendor during Codex quota gap at R15-R16). Every P1 fixed inline with regression tests; every P2 in Bundle 2 scope fixed; out-of-scope P2/P3 (MiniMax infra) left for Keith's separate feature commit. Architect's post-R21 completeness audit produced the 10-state transition matrix appended to `wiki/planning/m2.6-engine-state-machine.md`. R22 (ship gate) surfaced one P1 on duplicate-submit risk after transport failure -- fixed by forcing `_init_completed=False` so reconnect re-runs INIT instead of fast-tracking to CONNECTED_IDLE.

**Feature status change:** Bundle 2 in phase-2-bundles.md moves to shipped. Phase 2 progress: Bundle 1 + Bundle 2 done (6 of 22 milestones: m2.3, m2.4, m2.5, m2.6, m2.7, m2.8). Bundle 3 (approval gate: m2.16, m2.17) unblocked next.

**Follow-ups:**
- Keith's MiniMax reviewer infra (scripts/lib/minimax_review.py, scripts/minimax-review.sh, invest-ship SKILL fallback docs, .gitignore + CLAUDE.md updates) is unstaged in this tree; lands in a separate commit under Keith's own feature workstream (two R22 out-of-scope findings -- schema validation + archive-dir path -- attach to that commit).
- Tests: 207 unit tests pass locally. ib_async not installed in the test environment, so the live connector's broker-side behavior is covered by protocol-conformance tests against MockIBKRConnector; real IB Gateway smoke test for Bundle 2 should land alongside the first Phase 3 paper ticket.
- Follow-up audit queued by architect post-R8: kill-semantics inconsistency across m2.6 spec + risk-controls.md + kill_switch.py (whether .killed blocks only new orders OR implies flattening). Not a Bundle 2 gap; current spec language stands.

**Key decisions (divergent from original spec):**
- State machine grew from 9 states to 10: added HALTED as a distinct refused-to-operate terminal. SHUTDOWN now reserved for graceful signal exit. Drives invest-execute status messaging distinction.
- Strategy loader skip-on-draft-parse-failure + fail-loud-on-approved-intent via raw status-line peek. Architect ruled silent skip was too permissive in R12.
- EOD cutoff is US/Eastern local (default "16:30"), not UTC, so DST transitions don't misfire the session-boundary sweep (Codex R11 P1).
- IBKR account_id is a required keyword-only kwarg on the live connector. Missing it = TypeError at construction, not silent filter bypass. Architect post-R18 type-level-discipline ruling.
- Recovery validates every journal_view field at a single seam (`_validate_journal_view`) instead of scattered per-field try/except blocks. Refuses resume on any corruption; broker's still-open order surfaces as phantom_open_order mismatch on next reconcile (architect Q3 contract + post-R19 whack-a-mole stop rule).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
Co-Shipped-By: invest-ship

---

## 2026-04-18 -- Phase 2 Bundle 1: safety + observability foundation (m2.3, m2.4, m2.7)

**Commit:** `befc26b` feat: Phase 2 Bundle 1 -- safety + observability foundation (m2.3, m2.4, m2.7)

**What shipped:** First Phase 2 code bundle per `K2Bi-Vault/wiki/planning/phase-2-bundles.md` Bundle 1. Three milestones land together because none is testable in isolation: validators reject bad orders, breakers halt the engine on drawdown, decision journal records every verdict. Every subsequent Phase 2 bundle reads or writes against these.

m2.3 ships five pre-trade validators (`execution/validators/`): `instrument_whitelist`, `market_hours`, `position_size`, `trade_risk`, `leverage`, plus a runner that short-circuits on first rejection with no override flag. NYSE session logic uses `exchange_calendars` XNYS (holidays + real pre/after-market windows, not just weekday-only). Concentration checks mark existing inventory from `ctx.current_marks` populated by the engine, with a non-user-controlled `position.avg_price` fallback; the caller's `limit_price` is never used as a mark for held inventory. Market-hours authoritative clock is `ctx.now`, not the spoofable `order.submitted_at`.

m2.4 ships circuit breakers (`execution/risk/circuit_breakers.py`) + `.killed` lock file (`execution/risk/kill_switch.py`). Daily soft -2%, daily hard -3%, weekly -5% (requires full 5-session window before engaging so the first week after startup can't false-trigger), total -10% writes `.killed`. The kill-switch module exposes no delete API — human-only unlock per `risk-controls.md`. `.killed` writes are first-writer-wins via atomic `os.link` + parent-directory fsync; concurrent breaker / Telegram kills are race-safe across processes. `apply_kill_on_trip` is idempotent across ticks (no journal / Telegram spam while the breaker stays tripped).

m2.7 ships the append-only JSONL decision journal (`execution/journal/writer.py`). Path: `K2Bi-Vault/raw/journal/YYYY-MM-DD.jsonl`. Schema v1 per architect greenlight 2026-04-18: `ts` (UTC ISO-8601 microsecond-precision, always), `schema_version`, `event_type` enum (includes `recovery_truncated` for silent-loss prevention), `trade_id` lifecycle ULID, `journal_entry_id` per-record ULID, `strategy`, `git_sha`, `payload`, optional `error`/`metadata`/`ticker`/`side`/`qty`. Writes hold an exclusive flock on a sidecar lock file; reads take a shared flock on the same sidecar (Bundle 5 m2.18 P&L reader now race-safe by design). Fresh daily files fsync the parent dir. Startup recovery atomically renames complete lines + a `recovery_truncated` marker in one operation — no crash window between truncation and marker emission. A record missing only its trailing newline is parsed first: if it's valid JSON the newline is appended in place (no silent loss of a successfully-written record).

Cash-only invariant consolidation per architect flag: `execution/risk/cash_only.py` is the single canonical naked-short gate. Leverage validator delegates to `cash_only.check_sell_covered`; every other module under `execution/validators/` and `execution/engine/` carries a header comment declaring whether it touches sell-side paths. That's the guardrail so Bundle 2's engine main loop can't reintroduce the scattered enforcement pattern that caused Codex rounds 1-3 to each find a different naked-short scenario.

Schema reference doc written to `K2Bi-Vault/wiki/reference/journal-schema.md` (v1 + evolution rules). Planning update: `wiki/planning/phase-2-bundles.md` captures the full 6-bundle plan; `wiki/planning/index.md` bumped to 18 entries.

**Codex review:** 11 rounds. Rounds 1-10 surfaced 22 category-(a) correctness bugs in safety-critical paths — lock semantics, naked-short scenarios, UTC/day-boundary normalization, extended-hours windows, mark-source ambiguity, crash durability, concurrent reader/writer races. All addressed inline with regression tests (tests grew from 21 initial to 87 final). Round 11: clean pass, "did not find any discrete, actionable bugs." Architect stop condition `(a) = 0` met. Full finding ledger by round captured in the round-by-round review dialogue; not duplicated here to keep DEVLOG signal-to-noise.

**Feature status change:** shipped as `--no-feature` (K2Bi `wiki/concepts/` lane structure still absent; Phase 2 tracking lives in `wiki/planning/phase-2-bundles.md`).

**Follow-ups:**
- Bundle 2 (m2.5 IBKR connector + m2.6 engine main loop + m2.8 invest-execute skill) is next. The engine main loop MUST import `execution.risk.cash_only.check_sell_covered` before any sell reaches the connector (belt over the validator runner's suspender) — engine `__init__.py` header comment documents this contract.
- `K2Bi-Vault/wiki/reference/journal-schema.md` starts at v1. Bump to v2 only when promoting a `metadata` key to top-level; old records stay at v1 and readers must handle both.
- `exchange_calendars` pinned at `>=4.5` in `requirements.txt`. The hard-coded 2026/2027 holiday fallback was replaced with the library; if the dep breaks in a fresh env, engine startup fails loud (intentional — no silent holiday bypass).

**Key decisions:**
- Single commit for all three milestones. Bundle 1 is indivisible by design: validators without a journal are untestable (no audit trail), breakers without validators are untriggered, journal without writers is unused. Split would have been ceremony, not safety.
- Codex loop continued past the architect's first proposed stop at round 8. Rounds 8-10 each surfaced one more real (a) bug; stopping at 8 would have shipped the timestamp-spoof + silent-recovery-loss scenarios. Round 11 is the real stop — first clean pass, not a timeout.
- Refused margin mode (`cash_only=False`) outright at validator time rather than patching the four margin-mode bypass paths Codex found in round 2. Phase 4+ will ship a proper margin design; half-working margin code now is a footgun.
- Consolidated cash-only enforcement into `execution/risk/cash_only.py` after round 7, when the architect flagged that rounds 1-3 each found a different naked-short scenario. The module is a load-bearing architectural decision for Bundle 2+ — any new sell-side path must import it, never re-implement the rule.

---

## 2026-04-18 -- /sync: deploy script extended for Phase 2 scaffold paths

**Commit:** `b96b908` chore: extend deploy-to-mini.sh for Phase 2 scaffold paths

**What shipped:** `scripts/deploy-to-mini.sh` learned the Phase 2 scaffold paths (`execution/`, `pm2/`, `.claude/settings.json`). The Mini had been drifting from the MacBook since `fcf2049` because the Phase 1 script only covered `.claude/skills/`, top-level docs, and `scripts/`. Two new helpers (`sync_singleton`, `sync_tree_or_delete`) give the script proper delete-consistency: local renames or removals now mirror to the Mini instead of leaving stale files on the remote. `categorize()` and `detect_changes()` were narrowed so Claude Code's local-only runtime artifacts (e.g. `.claude/scheduled_tasks.lock`) no longer trigger no-op deploys. This ship closes the P2 follow-up flagged by Codex in the Teach Mode DEVLOG entry.

**Session-opening action:** Before the script changes were committed, an `/invest-sync` run consumed two deferred-sync mailbox entries (from `597e052` and `fcf2049`) and synced their files to the Mini. A category-vs-deploy-target mismatch surfaced during that run — entries tagged `skills` but containing `execution/`, `pm2/`, and `.claude/settings.json` paths that the Phase 1 script didn't know how to deploy. That gap is what this ship closes. The pre-edit sync handled the deployable subset; a post-ship re-sync pushes the extended script itself to the Mini after this ship lands.

**Codex review:** 4 passes, 6 in-scope P2/P3 findings addressed:
- Pass 1 — auto-detect missing new dirs (P2); `sync_pm2()` missing `--delete` (P2)
- Pass 2 — singleton-delete semantics for top-level docs + settings.json (P2)
- Pass 3 — tree-delete semantics for `execution/` + `pm2/` (P2); `categorize()` over-matching any `.claude/*` path (P3)
- Pass 4 — `.claude/settings.json` missing from `detect_changes()` untracked scan (P2)

One out-of-scope finding in Pass 4 (P2 on `CLAUDE.md:153` — strategy-gate enforcement claim vs. `commit-msg` hook TODO in `3ff7e10`) was noted and deferred. Pass 5 skipped by Keith (diminishing returns).

**Feature status change:** n/a — `--no-feature` infrastructure ship. K2Bi has no `wiki/concepts/` lane system yet.

**Follow-ups:**
- Out-of-scope Codex P2 from Pass 4: `CLAUDE.md:153` claims the strategy-gate "How This Works (Plain English)" section is hook-enforced, but `.githooks/commit-msg` still has only a TODO for that check. Either ship the hook check (Phase 2 milestone 2.17) or correct the doc.
- `/sync` skill body (`.claude/skills/invest-sync/SKILL.md`) category table still lists only `skills`/`code`/`dashboard`/`scripts`. Phase 4+ should add `execution` and `pm2` as first-class category labels in the skill's routing table so `/ship --defer` mailbox entries can be tagged correctly at source.

**Key decisions:**
- Committed to `main` directly rather than bundling onto the `proposal/teach-mode-pedagogical-layer` branch — keeps the teach-mode PR diff clean. Branch choice was Keith's explicit call after being shown both options.
- Introduced `sync_tree_or_delete` as a reusable helper when Codex flagged the dir-deletion failure in Pass 3, rather than inlining `ssh rm -rf` logic in each sync function. Future Phase 4+ deploy targets should use the same helper.
- Stopped the Codex loop after Pass 4 rather than continuing to Pass 5 — diminishing-returns judgment call on a small infra change with 6 findings already addressed.

---

## 2026-04-18 -- Teach Mode Pedagogical Layer Applied (PR #2 merge + acceptance)

**Commits:**
- `f841d12` Merge pull request #2: Teach Mode pedagogical layer proposal
- `3ec175a` proposal: Teach Mode -- pedagogical layer for K2Bi
- `3ff7e10` chore: apply Teach Mode pedagogical layer (proposal 2026-04-18)

**What shipped:** K2B architect session opened PR #2 with a 431-line pedagogical-layer proposal (`proposals/2026-04-18_teach-mode-pedagogical-layer.md`) in response to Keith's 2026-04-18 evening ask for plain-English explanation of trading terminology as he builds strategies. Proposal specifies four reinforcing layers plus a single-line `learning-stage:` dial. Design reviewed against the Memory Layer Ownership matrix (soft behavioral rule -> CLAUDE.md; dial -> active_rules.md; deep reference -> vault; output convention -> SKILL.md bodies; gate enforcement -> commit-msg hook). Strategy "How This Works (Plain English)" gate is permanent regardless of dial stage; the dial only tunes verbosity elsewhere. Merged to main, then applied acceptance instructions in a single follow-up commit. (1) `CLAUDE.md` gained a "Teach Mode (Pedagogical Layer)" section between Rules and AI vs Human Ideas, with a stage behavior table, glossary integration rules, and a dial-read bash one-liner. (2) `.claude/skills/invest-bear-case/SKILL.md` and `.claude/skills/invest-execute/SKILL.md` both gained a "Pedagogical layer (Teach Mode)" section with the dial read, footer convention, full worked example (NVDA bear case + SPY fill). (3) `.githooks/commit-msg` gained a Phase 2 milestone 2.17 TODO tag for the strategy approval gate (verify mandatory "How This Works" section is non-empty before allowing `status: approved`). (4) `K2Bi-Vault/wiki/reference/glossary.md` created with 14 seed terms (sharpe-ratio, sortino-ratio, drawdown, walk-forward-validation, look-ahead-bias, kill-switch, strategy-approval, bear-case, position-sizing, slippage, fee-erosion, decision-journal, regime, circuit-breaker, paper-trading) linked from `wiki/reference/index.md`. (5) `K2Bi-Vault/Templates/strategy.md` created with the mandatory "How This Works (Plain English)" section above YAML rules block; `Templates/index.md` seeded. (6) `active_rules.md` gained rule #6 "Pedagogical layer (learning-stage dial)" with `learning-stage: novice` default. (7) `wiki/log.md` appended via `scripts/wiki-log-append.sh`.

**Codex review:** 1 finding, not in this ship's scope:
- P2 — `scripts/deploy-to-mini.sh:60-62` auto-detect path does not scan for brand-new untracked `.claude/settings.json`, so first-time creation of that file would silently skip syncing. That file's changes predate this session (leftover dirty state from the Phase 2 scaffold ship); deliberately excluded from this commit and flagged for a follow-up ship.

**Feature status change:** shipped as `--no-feature` (K2Bi has no `wiki/concepts/` lane structure yet; Phase 2 kickoff tracking lives in `wiki/planning/`).

**Follow-ups:**
- Address Codex P2 finding on `scripts/deploy-to-mini.sh` in a dedicated follow-up commit (pre-existing uncommitted work, not part of this ship).
- Phase 2 milestone 2.17: wire the commit-msg hook's strategy approval gate (TODO tagged in `.githooks/commit-msg`). Implementation gates `status: approved` transitions on a non-empty "How This Works (Plain English)" section in `wiki/strategies/*.md`.
- When `invest-feedback` skill wires the `/learn intermediate` shortcut, Keith can flip the dial without editing `active_rules.md` manually.
- First auto-stub will appear in the glossary once an invest-* skill encounters a term not yet in the 14 seed list; `/invest-compile` fills stubs in batch.

**Key decisions:**
- Skipped Option C (`/explain` slash command) per the proposal's deferral reasoning: the auto-pedagogy in layers A/D/E covers 80% of comprehension moments; build the explicit explainer only if Keith finds himself reaching for one during burn-in.
- Kept PR #2 as a merge commit (matches PR #1 pattern) so the proposal's standalone provenance remains in git history.
- Numbered the new dial rule as active_rules rule #6, not #5 as the proposal suggested, since five rules already exist from Phase 1 scaffold. LRU cap of 12 still has headroom.
- Fixed one drafting typo in the proposal's frontmatter example (`origin: k2b-generate` -> `origin: k2bi-generate`, matching the canonical three-value origin set).
- Excluded pre-existing `scripts/deploy-to-mini.sh` dirty state from this commit per the /ship rule that files not touched in the current session must not be staged, even though Codex reviewed it alongside the session changes.

---

## 2026-04-18 -- Phase 2 Scaffold Applied (PR #1 merge + acceptance)

**Commits:**
- `51708fe` Merge PR #1: Phase 2 MVP scaffold revision proposal
- `92df8cd` proposal: Phase 2 MVP scaffold revision (collapse 2a/2b/3, defer NBLM)
- `fcf2049` chore: apply Phase 2 MVP scaffold revision (proposal 2026-04-18)

**What shipped:** K2B architect session opened PR #1 with a 450-line architectural revision proposal (`proposals/2026-04-18_phase2-mvp-scaffold-revision.md`) per Keith's "MVP scaffold all components ready, paper-trade ASAP, harden by discovery" reframe. Reviewed against architecture/execution-model/risk-controls/agent-topology — no contradictions. All non-negotiables preserved (4-tier model, execution layer isolation, code-enforced validators, strategy-level approval, decision journal append-only, NBLM MVP-gated, Routines-Ready discipline for Analyst skills). Merged to main. Applied acceptance instructions: (1) 4 vault planning doc diffs to roadmap.md + milestones.md + nblm-mvp.md + planning/index.md replacing old Phase 2a/2b/3/4 sections with the new Phase 2 (22 MVP-scaffold milestones) + Phase 3 (6 first-paper-trade + burn-in) + Phase 4 (emergent, discovery-driven) structure. NBLM experiment re-tagged Phase 4 conditional. (2) Vault folders created: `raw/journal/` with JSONL schema contract in index.md; all other Phase 2 folders already existed from Phase 1 scaffold. (3) `execution/` Python module skeleton (`validators/`, `risk/`, `connectors/`, `engine/`, `journal/` with `__init__.py` placeholders + `validators/config.yaml` with top-5 validator defaults). (4) `pm2/ecosystem.config.js` with commented stub entries for invest-execute + invest-alert + invest-feed + invest-observer-loop (+ -open and -close edge-window companions for the engine). (5) 9 new skill stubs with tier assignment + Routines-Ready discipline: invest-thesis, invest-bear-case, invest-screen, invest-regime, invest-backtest (Analyst); invest-execute, invest-alert, invest-feed (Trader); invest-propose-limits (Portfolio Manager). Each SKILL.md is a spec-only stub keyed to its Phase 2 milestone. Skill count: 14 → 23.

**Codex review:** 3 findings, all addressed inline:
- P1 — pm2 engine cron `*/5 9-16 * * 1-5` fired outside the 09:30-16:00 ET window (09:00-09:25 pre-open, 16:05-16:55 post-close). Replaced with a 3-entry pattern: main `*/5 10-15 * * 1-5` plus `invest-execute-open` at `30-55/5 9 * * 1-5` plus `invest-execute-close` at `0 16 * * 1-5`. Documented the engine's `market_hours` validator as the hard enforcer; cron width is a perf concern, not a safety one.
- P2 — invest-feed filename `YYYY-MM-DD_news_<slug>.md` could collide for same-day items with the same slug, overwriting the earlier item. Added `<hash8>` suffix (first 8 chars of `source-hash`) to guarantee uniqueness.
- P2 — invest-feed's pm2 cron had the same pre-open / post-close issue; tightened to `*/30 10-15 * * 1-5` with the same -open / -close pattern as the engine.

**Feature status change:** shipped as `--no-feature` (no K2Bi `wiki/concepts/` lane structure yet; Phase 2 kickoff tracking lives in `wiki/planning/`).

**Follow-ups (Phase 2 build work, per-milestone):**
- 22 Phase 2 milestones now tracked in [[milestones#Phase 2 -- MVP Scaffold All Tiers]]. Next session's first concrete task is milestone 2.3 (top-5 validator implementations + unit tests). 2.1 (vault folders) and 2.2 (Python scaffold) land in this commit.
- Keith still owes: first strategy choice (milestone 3.1 -- SPY weekly rotation OR another single-ticker thesis).
- Phase 2a prerequisites (accuracy-delta eval log, revealed-preference observer signal) are no longer pre-Phase-2 blockers; they re-emerge as Phase 4 triggers only if the NBLM experiment fires.

**Key decisions:**
- Kept PR #1 as a merge commit (not squash) so the proposal's standalone provenance remains in git history; the proposal file at `proposals/2026-04-18_phase2-mvp-scaffold-revision.md` is the canonical architectural revision artifact.
- All 9 new skills are stub-only; implementation is Phase 2 build work, not this ship. Ships skill specs + tier assignment + Routines-Ready audit structure so Phase 2 build sessions can start immediately against a concrete milestone list.
- `.claude/settings.json` was landed in the prior bootstrap commit (`597e052`); this ship inherits its Bash + MCP allowlist unchanged.

---

## 2026-04-18 -- Bootstrap Fixes: Shared-Skill Rename + Helper Skills

**Commit:** `597e052` feat: rename shared skills to invest-* and add bootstrap helpers

**What shipped:** Reversed Session 3's "keep k2b-* names for shared skills" call and renamed all four to invest-* (research, scheduler, ship, vault-writer) so K2Bi now has zero K2B-identity carryover in its skill namespace. Three new bootstrap helpers added: `invest-feedback` (/learn, /error, /request capture), `invest-sync` (K2Bi-side /sync skill that wraps deploy-to-mini.sh), `invest-usage-tracker` (skill invocation logger + threshold triggers used by session-start hook and fellow skills). Landed `.claude/settings.json` with K2Bi's Bash + MCP permission allowlist (ssh macmini, rsync, pm2, sqlite3, curl, NBLM CLI, MCP servers). Cleaned up cross-refs in invest-journal, invest-weave, and CLAUDE.md to the new names. Skill count: 11 → 14, all invest-* prefix.

**Codex review:** 3 findings, all addressed:
- P1 — invest-vault-writer raw-note handoff table still said "Trigger k2b-compile" (5 rows). Fixed inline to `invest-compile`.
- P1 — invest-research `/research deep` default source gathering still references `~/Projects/K2B/scripts/yt-search.py` + `mcp__perplexity-ask__perplexity_ask` (neither ships with K2Bi). Added explicit `[TODO Phase 2 port]` marker + inline "Dangling in K2Bi — Phase 2 port" annotations on the two dangling bullets; documented the supported-today path (`/research <topic>`, `/research <url>`, `/research deep <topic> --sources <url>...`). Mirrors Session 3's earlier P1 finding carried to Phase 2.
- P2 — invest-sync's dry-run fallback block probed `K2B_ARCHITECTURE.md` + `k2b-remote/` + `k2b-dashboard/` (none ship in K2Bi; rsync would error). Replaced with an existence-guarded loop over CLAUDE.md/DEVLOG.md/README.md and a commented-out Phase 4+ template for `invest-remote/`.

**Feature status change:** shipped as `--no-feature` (no K2Bi `wiki/concepts/` lane structure yet; tracked in `wiki/planning/`).

**Follow-ups (Phase 2, non-gating):**
- Port `yt-search.py` to K2Bi with its own OAuth credentials + quota, or swap in a K2Bi-compatible alternative, so `/research deep` works without `--sources`
- Decide Perplexity MCP vs alternative source broadener for `/research deep` default source gathering
- Invest-scheduler still references K2B's shared `k2b-remote` scheduler service on the Mini — that's intentional (cross-project daemon, not forked), but the name should be pinned as "shared dependency" in the skill body if confusion comes up again

**Key decisions:**
- Rename reversal (Session 3 kept original names; this session flipped them): the reason Session 3 kept them was "easier to diff against K2B." In practice that diff happens rarely and the uniform `invest-*` set is easier for Keith's muscle memory + slash-command autocomplete (both Claude Code terminal and Claude Desktop). Trade-off accepted.
- Three new helpers (feedback, sync, usage-tracker) were added directly into K2Bi rather than ported from K2B — K2B's equivalents (if any) are less mature. K2Bi takes the forward position here.

---

## 2026-04-18 -- Phase 1 Closure Doc Bundle

**Commit:** `56719c5` docs: point CLAUDE.md at live planning docs in K2Bi-Vault

**What shipped:** Follow-up to Session 3 (`4ea9b70`) that formally closes Phase 1 in documentation. `CLAUDE.md` section "Planning Archive (Historical, Reference Only)" replaced with "Planning Docs (Operational, Live)" pointing at `~/Projects/K2Bi-Vault/wiki/planning/` and listing all 17 planning files (roadmap, architecture, agent-topology, research-infrastructure, nblm-mvp, open-questions, keith-checklist, milestones, data-sources, broker-research, execution-model, risk-controls, research-log, k2b-audit, k2b-audit-fixes-status, feature_k2bi-phase1-scaffold, project_k2bi, plus the index). The K2B-Vault archive at `~/Projects/K2B-Vault/wiki/projects/k2bi/` is now frozen; K2Bi-Vault's copy is the live authoritative version going forward. Companion vault updates (Syncthing, not git) flipped `feature_k2bi-phase1-scaffold.md` to `status: shipped` at `4ea9b70` with all 13 exit criteria marked ✅, updated the Resume Card in `planning/index.md` to reflect closure + Phase 2 as next concrete action (Phase 2a NBLM MVP experiment preceded by two prerequisite decisions), and flipped the `roadmap.md` Phase Lanes table to show Phase 1 SHIPPED with Session 3 + closure-bundle log entries appended.

**Codex review:** clean, 0 actionable findings. Codex verified the referenced live planning paths exist in K2Bi-Vault and that the documentation-only change does not break workflow behavior.

**Feature status change:** shipped as `--no-feature` (K2Bi still has no `wiki/concepts/` lane structure; Phase 1 closure is tracked in `wiki/planning/`). This matches the Session 3 commit's feature-status decision.

**Follow-ups (non-gating, carried to Phase 2):**

- Syncthing K2Bi-Vault folder click-setup between MacBook and Mac Mini (Keith UI, both boxes)
- Phase 2 port scope: `vault-query.sh` (Dataview DQL helper for invest-lint deep), `yt-search.py` / `send-telegram.sh` / `parse-nblm.py` / `motivations-helper.sh` / `k2b-playlists.json` (K2B YouTube research flow, optional for K2Bi), MiniMax worker scripts for `invest-compile` + `invest-weave`
- Session 2 active-rules pipeline scripts (`promote-learnings.py`, `audit-ownership.sh`, `select-lru-victim.py`, `demote-rule.sh`) still absent; `/ship` steps 0 and 0a skip gracefully with explicit "skipped (no script in $(pwd))" messages

**Key decisions:**

- Kept the K2B-Vault planning archive frozen rather than deleting it -- preserves history of planning decisions made before K2Bi existed as its own repo, and means the 17 K2Bi-Vault copies are the *authoritative* live version without destroying the K2B-side provenance trail
- Docs-only ship handled via normal `/ship` workflow with Codex review, not treated as a typo-fix. Section replacement in CLAUDE.md is identity-level prose (where authoritative planning lives), so Checkpoint 2 review applied

---

## 2026-04-18 -- Phase 1 Session 3: Standalone Independence

**Commit:** `4ea9b70` feat: Phase 1 Session 3 -- standalone K2Bi independence

**What shipped:** K2Bi now has no runtime dependency on the K2B repo. Four shared skills (k2b-ship, k2b-research, k2b-scheduler, k2b-vault-writer) forked into `.claude/skills/` with K2B-Vault paths swapped to K2Bi-Vault. `scripts/deploy-to-mini.sh` ported with K2Bi paths and a dropped k2b-remote/k2b-dashboard mode. K2Bi-Vault/System/memory/ seeded with its own `active_rules.md` (5 rules + LRU cap doc), `MEMORY.md` rewrite, and 3 self_improve stubs. GitHub remote wired to git@github.com:kcyh7428/K2Bi.git, local commits pushed, Mac Mini received the first `/sync` (11 skill folders verified on both machines). CLAUDE.md + DEVLOG.md + skill + hook + script prose all re-identified from "K2B-Investment" to "K2Bi".

**Codex review:** 3 findings surfaced (P1 vault-writer dangling vault-query.sh ref, P1 k2b-research dangling YT/MiniMax script refs, P2 deploy-to-mini.sh missing untracked top-level docs in auto-detect). P2 fixed inline. Both P1s scoped to Phase 2 port work with explicit in-file notes flagging the K2B-only helpers; standalone K2Bi sessions can still run `/research "topic"`, `/research <url>`, and `/research deep` via the global `notebooklm` CLI.

**Feature status change:** No K2Bi wiki/concepts/ lane structure yet, so shipped `--no-feature`. Phase 1 closure is tracked in the planning archive `~/Projects/K2B-Vault/wiki/projects/k2bi/` and will migrate into a K2Bi-native structure in Phase 2.

**Follow-ups:**

- Syncthing K2Bi-Vault folder setup between MacBook and Mac Mini needs Keith's clicks (left to the first live session on either box)
- Phase 2 port scope: `vault-query.sh` (Dataview DQL), `yt-search.py` / `send-telegram.sh` / `parse-nblm.py` / `motivations-helper.sh` / `k2b-playlists.json` (K2B YouTube research flow, optional for K2Bi trading research), and MiniMax worker scripts for `invest-compile`
- Session 2 active-rules pipeline (`scripts/promote-learnings.py`, `scripts/audit-ownership.sh`, `scripts/select-lru-victim.py`, `scripts/demote-rule.sh`) still absent; /ship step 0 and 0a skip gracefully, tracked for Phase 2

**Key decisions (divergent from claude.ai project specs):**

- Kept original `k2b-*` skill names (not renamed to `invest-*`) for the 4 forked shared skills -- preserves clarity that they are cross-project shared infra, not trading-domain skills; easier to diff against K2B side when the two repos need to re-sync
- `k2b-remote` scheduler service left as a K2B-shared infrastructure dependency (not forked as its own K2Bi instance) -- it is a Node.js CLI running on the Mini, not a skill file, so "standalone skills" is satisfied without duplicating the service daemon

---

## 2026-04-18 -- Phase 1 Session 2: Skill Ports + Helpers + Hooks

**Scope:** Port 7 skills from K2B with prompt-domain swaps; port the wiki/log.md single-writer helper + atomic 4-index helper; add pre-commit + commit-msg hooks; wire up `core.hooksPath`. Full /ship end-to-end smoke test deferred to Session 3.

**Skills ported (under `.claude/skills/`):**

- `invest-compile` (was k2b-compile) -- with `eval/eval.json` (3 tests) + inherited `eval/learnings.md`. MiniMax compile worker `~/Projects/K2Bi/scripts/minimax-compile.sh` marked TODO Phase 2.
- `invest-lint` (was k2b-lint) -- no eval/ in source. Added a 30-day staleness rule for open positions and removed the legacy K2B Notes/Inbox folder check.
- `invest-weave` (was k2b-weave) -- no eval/. Scheduled cron deferred to Phase 4 when Mac Mini provisioning happens; manual `/weave` works now. MiniMax weave worker marked TODO Phase 2.
- `invest-observer` (was k2b-observer) -- no eval/. Mac Mini pm2 background loop marked Phase 4 deferred. YouTube signal section replaced with contradiction-queue harvesting (invest's `review/contradictions/` is first-class). Preference signal examples re-anchored to trade-domain (risk-per-trade, concentration caps, post-earnings pause windows).
- `invest-autoresearch` (was k2b-autoresearch) -- no eval/. Eval-path pattern, skill-name examples, repo path, and commit-message scope all swapped to invest-*.
- `invest-journal` (was k2b-daily-capture) -- with `eval/eval.json` (4 tests) + `eval/learnings.md`. Telegram harvester removed (no k2b-remote in invest until Phase 4). P&L/slippage/fee-erosion sections stubbed with "Phase 4+" markers per spec.
- `invest-session-wrapup` (was k2b-tldr) -- with `eval/eval.json` (3 tests) + `eval/learnings.md`. Content Seeds section dropped entirely (no content pipeline in invest). Save path swapped to `raw/research/` (no `raw/tldrs/` in invest vault).

**Helpers ported (under `scripts/`):**

- `wiki-log-append.sh` -- single writer for `wiki/log.md`. Env vars: `K2BI_WIKI_LOG`, `K2BI_WIKI_LOG_LOCK`. Smoke-tested successfully against the new vault.
- `compile-index-update.py` -- atomic 4-index helper. Env vars: `K2BI_VAULT_ROOT`, `K2BI_WIKI_LOG_APPEND`, `K2BI_COMPILE_INDEX_LOCK`. Lock path: `/tmp/k2bi-compile-index.lock.d`.

**Hooks added (under `.githooks/`):**

- `pre-commit` -- blocks direct `>>` appends to `wiki/log.md`. Override env: `K2BI_ALLOW_LOG_APPEND=1`.
- `commit-msg` -- blocks `status:` line edits in `wiki/concepts/feature_*.md` outside `/ship` (accepts `Co-Shipped-By: k2b-ship` OR `Co-Shipped-By: invest-ship` since `/ship` is reused cross-repo until invest-ship is built). Override env: `K2BI_ALLOW_STATUS_EDIT=1`. Effectively dormant until Phase 2+ feature notes start landing in this vault.
- `core.hooksPath` set to `.githooks` via `git config`.

**Smoke tests run:**

- `wiki-log-append.sh` PASSED (wrote a real test entry to `K2Bi-Vault/wiki/log.md`, then proceeded past it -- the entry remains in the log as the appended audit trail of the smoke).
- `compile-index-update.py` arg-validation PASSED (exit 1 on missing args, expected behavior).
- All shell scripts pass `bash -n` syntax check; Python helper passes `ast.parse`.
- Pre-commit hook PASSED a live block test: created a file containing `echo "..." >> wiki/log.md`, attempted commit, hook printed the offending lines and exited 1 as designed. (Earlier failed test where the hook seemed not to fire turned out to be `git stash --include-untracked` swallowing the `.githooks/` dir from the working tree -- recovered via `git reset --hard d30e203 && git stash pop`, re-tested, hook now correct.)

**Subagent dispatch pattern (Keith asked about this explicitly):** all 7 skill ports ran as parallel `general-purpose` subagents in background mode, fed a precise port spec at `/tmp/k2bi-port-spec.md` (created in main session, then referenced by every subagent). Each subagent only used Read/Write/Edit/Grep -- no external CLI calls -- which avoided the codex:rescue silent-stall pattern from 2026-04-17. All 7 returned cleanly within ~3 minutes. Helper scripts + hooks were written in main session in parallel while subagents ran. Three shallow swaps caught in the review pass and fixed in main session: position wikilinks (`[[position_<symbol>...]]` → `[[<SYMBOL>_YYYY-MM-DD]]`), compile-index lock path mismatch between SKILL.md and the actual script, and learnings.md headers carrying source skill names.

**Phase 1 exit criteria status:** 7 of 8 met. Only #8 (full `/ship` end-to-end smoke test) remains and is the lead item for Session 3.

**Resume handle:** Keith says "continue k2b investment" -> CLAUDE.md routes to `K2B-Vault/wiki/projects/k2bi/index.md` Resume Card -> next action is "Phase 1 Session 3: full /ship smoke test + first /autoresearch loop + start Phase 2 MiniMax helper ports".

**Next action:** Phase 1 Session 3 (when Keith picks it up).

---

## 2026-04-17 -- Phase 1 Session 1: Repo + Vault Scaffold

**Scope (per Keith decision 2026-04-17):** Scaffold only. Dirs + CLAUDE.md + indexes. Skill ports + eval + `/ship` smoke test deferred to Phase 1 Session 2. Mac Mini sync OFF until Phase 4.

**Created:**

- `~/Projects/K2Bi/` git repo skeleton (.git initialized, empty `.claude/skills/`, `scripts/`, `.pending-sync/`)
- `~/Projects/K2Bi/CLAUDE.md` written from scratch -- ownership-matrix-compliant, identity + taxonomy + soft rules only, no procedural duplication
- `~/Projects/K2Bi/.gitignore` -- excludes secrets, `.env*`, `.killed` lock, `__pycache__`, `.pending-sync/` contents
- `~/Projects/K2Bi/DEVLOG.md` (this file)
- `~/Projects/K2Bi-Vault/` plain Syncthing-managed directory (NOT a git repo) with full skeleton:
  - `raw/` with subfolders: news, filings, analysis, earnings, macro, youtube, research
  - `wiki/` with subfolders: tickers, sectors, macro-themes, strategies, positions, watchlist, playbooks, regimes, reference, insights, context
  - `review/` with subfolders: trade-ideas, strategy-approvals, alerts, contradictions
  - `Daily/`, `Archive/`, `Assets/{images,audio,video}/`, `System/`, `Templates/`
- `wiki/index.md` master catalog (LLM reads first on every query)
- `wiki/log.md` append-only spine (single-writer rule documented; helper script ports in Session 2)
- Per-folder `index.md` in every `wiki/`, `raw/`, and `review/` subfolder
- `Home.md` vault landing page
- Memory symlink: `~/.claude/projects/-Users-keithmbpm2-Projects-K2Bi/memory/` -> `K2Bi-Vault/System/memory/`

**Deliberately NOT done this session (deferred):**

- Skill ports (7 invest-* skills): invest-compile, invest-lint, invest-weave, invest-observer, invest-autoresearch, invest-journal, invest-session-wrapup
- Skill eval harness runs
- `/ship` smoke test from new repo
- Syncthing config to Mac Mini
- Pre-commit hook (Tier 1 K2B fix #8 -- needs the helper scripts that ship in Session 2)
- Single-writer log helper script

**Phase 1 exit criteria status:** 4 of 8 met (1, 2, 3, 7). Remaining (4, 5, 6, 8) require skill ports + `/ship` test + memory symlink validation.

**Resume handle:** Keith says "continue k2b investment" in any new session -> CLAUDE.md routes to `K2B-Vault/wiki/projects/k2bi/index.md` Resume Card -> next action is now "Phase 1 Session 2: port 7 skills + run eval harness + `/ship` smoke test".

**Next action:** Phase 1 Session 2 (when Keith picks it up). Port skills, run evals, ship.


## 2026-04-19 -- Bundle 3 cycle 4 ships: hook extensions enforce approval discipline

**Commit:** `148509a` feat(hooks): enforce strategy + config approval discipline via pre-commit + commit-msg + post-commit

**What shipped:** Bundle 3 cycle 4 ships the three-hook enforcement layer (pre-commit Checks A/B/C/D + commit-msg transition-trailer matrix + NEW post-commit sentinel landing) that prevents any path outside `/invest-ship` from advancing a strategy status or editing `execution/validators/config.yaml`. All three hooks share a single `scripts/lib/strategy_frontmatter.py` parser so YAML quirks (NFC vs NFD, quoted vs unquoted scalars, unquoted vs ISO-string datetimes) do not false-flag Check D on otherwise-legal retires. Post-commit honours `engine.retired_dir` / `engine.kill_path` from `config.yaml` so the hook's write path and the engine's read path never diverge when a deployment customises either. Prep: `STATUS_REJECTED` added to `execution/strategies/types.py::ALLOWED_STATUSES` (Bundle 2 gap per spec §9.2). Full test suite: 393 passing (102 new across `tests/test_strategy_frontmatter.py`, `tests/test_pre_commit_hook.py`, `tests/test_commit_msg_hook.py`, `tests/test_post_commit_hook.py`, plus 2 loader regressions).

**Codex review:** skipped on `/ship` with reason `codex-final-gate-passed-cycle4-R1-P0-fixed-R2-test-quality-addressed`; Codex R1 + R2 ran interactively during the cycle. R1 flagged a P0 (`_resolve_retired_dir` ignored `config.yaml`); fixed + regression-tested. R2 flagged a weak parity test (tested the shared resolver only, not the hook's compound logic); upgraded to import the hook module and call `_resolve_retired_dir()` directly across 5 branches. MiniMax R1-R5 converged (approved). All P1 findings addressed. Deferred: F4 Check C content-min (belongs to `/invest-ship --approve-limits` cycle 6), F5 symlinks (unsupported config), F6 CRLF (strict byte policy is intentional), F8 fallback failure marker (spec §4.3 matches impl), F9 error-message hint polish.

**Feature status change:** no feature note (K2Bi has no `wiki/concepts/` lane yet; Bundle 3 cycles ship as standalone commits per prior pattern -- cycles 2 + 3 followed the same shape).

**Follow-ups:**
- Cycle 5: `/invest-ship --approve-strategy`, `--reject-strategy`, `--retire-strategy`, `--diagnose-approved` subcommands (share the new helper's `retire-slug` + `check-approved-immutable` CLI).
- Cycle 6: `invest-propose-limits` MVP + `/invest-ship --approve-limits` wiring (consume the proposed->approved transition Check C now polices).
- Cycle 7: §8.5 end-to-end test (`tests/test_bundle_3_e2e.py`, gated behind `K2BI_RUN_IBKR_TESTS=1`).

**Key decisions (cycle-level):**
- Post-commit hook written in Python (not bash) so it can import `execution.risk.kill_switch.write_retired` + `execution.engine.main.derive_retire_slug` directly; bash + python shell-out would duplicate the slug derivation and re-open the parity gap R11 flagged in cycle 3.
- `_nfc()` does more than NFC -- it also `str()`-coerces YAML scalars and `isoformat()`-canonicalises datetimes. The expanded contract closes 3 reviewer-reported false-positive classes (float vs quoted string, int vs quoted int, unquoted datetime vs quoted ISO string) without loosening the safety-critical equality check on actually-different values.
- Override env usage is now logged to `wiki/log.md` via the single-writer helper in addition to stderr. Git does not capture stderr into the commit object, so stderr-only audit left no durable record; the new best-effort log call does.


## 2026-04-19 -- Bundle 3 cycle 4 gap closed: invest-sync reads deploy-config.yml

**Commit:** `59b454b` fix(invest-sync): read categories from deploy-config.yml (closes cycle 2 gap)

**What shipped:** Cycle 2 anchored `scripts/deploy-config.yml` as the single source of truth for K2Bi's category set and pointed `/invest-ship`'s step-12 preflight at it. The propagation missed `/invest-sync`'s SKILL.md, which still hardcoded K2B's K2B-era set `{skills, code, dashboard, scripts}`. Cycle 4's own defer mailbox entry -- category `execution` -- landed in that gap and surfaced the bug: the scan flagged the entry as UNREADABLE with the confusingly precise message "category:unknown execution (expected subset of ['code', 'dashboard', 'scripts', 'skills'])". Rule going forward: no skill or script hardcodes category / target lists; every consumer shells out to `scripts/lib/deploy_config.py list-categories` / `list-targets` / `classify`.

This commit also extracts the previously inline mailbox-scan Python heredoc from SKILL.md into `scripts/lib/pending_sync.py` so the scan + delete + fail-closed logic is testable in isolation. New module, new CLI, 25 new tests covering happy paths + every rejection class + stale `.tmp_` files + delete race-safety. Full suite: 418 passing.

**Codex review:** R1 APPROVE; verified `scan_mailbox` fail-closes on `load_valid_categories` error, yaml-symmetry test parses `deploy-config.yml` correctly line-by-line, SKILL.md decision tree matches helper stdout format (`EMPTY` / `VALID|...` / `UNREADABLE|...`). MiniMax R1 four findings -- F1 (empty helper output silent pass) / F2 (yaml symmetry test gap) / F3 (empty-output test gap) fixed inline, F4 (preflight coverage) dismissed as out-of-scope (covered in `tests/test_deploy_coverage.py`). MiniMax R2 surfaced five theoretical TOCTOU / clock-skew / caching / caller-contract concerns on a design already documented as race-free by producer contract; all deferred per spec §10 triage. User/linter added `scripts/audit-fork-drift.sh` + `scripts/fork-audit-allowlist.txt` alongside `/invest-ship`'s new step 0, both shipped in this commit and runs clean on the current tree.

**Feature status change:** no feature note (infra cleanup; follows cycle 4's standalone-commit pattern).

**Follow-ups:**
- Re-run `/invest-sync` to consume the cycle 4 defer mailbox entry + deploy to Mini (session closeout action).
- Cycle 5: `/invest-ship --approve-strategy`, `--reject-strategy`, `--retire-strategy`, `--diagnose-approved` subcommands (unblocked by cycle 4 hooks + this infra cleanup).

**Key decisions (infra-level):**
- Extracting `pending_sync.py` was more than a one-line VALID_CATEGORIES swap but worth the reach: the bash heredoc was 80+ lines with complex state encoding, impossible to unit-test, and the bug it surfaced (category allowlist drift) would have recurred every time someone forked the skill without re-running the scan code. A proper module with a CLI is the pattern every other hook check has already adopted (`strategy_frontmatter.py`, `deploy_config.py`).
- Yaml-symmetry test parses `scripts/deploy-config.yml` via regex (not `yaml.safe_load`) to stay consistent with `deploy_config.py`'s stdlib-only fallback parser -- the test should not impose a PyYAML dependency the helper doesn't require.


## 2026-04-19 -- Bundle 3 cycle 5 ships: /invest-ship strategy subcommands + engine --diagnose-approved

**Commit:** `2b0272b` feat(invest-ship): strategy approval subcommands + engine --diagnose-approved

**What shipped:** Bundle 3 cycle 5 wires four new `/invest-ship` strategy-transition flags (`--approve-strategy`, `--reject-strategy`, `--retire-strategy`, `--approve-limits`) onto a new shared Python helper `scripts/lib/invest_ship_strategy.py` (~1225 LOC) that owns Step A validation + Step D atomic frontmatter edit + parent-sha capture + trailer generation. Each handler delegates frontmatter parsing to cycle-4's `scripts/lib/strategy_frontmatter.py`; trailers are emitted via a single `build_trailers()` function so the cycle-4 commit-msg hook's `grep -qFx` grammar stays byte-exact across all four kinds. A new `CANONICAL_STRATEGY_PATH_RE` enforces the same `^wiki/strategies/strategy_[^/]+\.md$` the cycle-4 hooks glob, closing the gap where a retire on an off-path file would silently miss the post-commit sentinel write. `--approve-limits` applies a limits-proposal's `## YAML Patch` to `execution/validators/config.yaml` atomically with its own frontmatter flip, with rollback of config on any proposal-write failure and concurrent-modification refusal that prevents blindly overwriting a peer writer's change. The engine's `engine_started` journal payload gained a per-strategy metadata block (`name`, `approved_commit_sha`, `regime_filter`, `risk_envelope_pct`), and a new `python -m execution.engine.main --diagnose-approved` CLI surfaces that block via a strictly read-only journal reader (`_iter_journal_read_only` uses `fcntl.LOCK_SH` only; no `JournalWriter` instantiation so no `mkdir`/`recover_trailing_partial` side effects on the diagnose path). SKILL.md gains a new `0c.` dispatch step ahead of scope detection, routing each of the four flags through the spec §3.2 Steps A-F and rejoining the normal ship flow at step 4. Full suite: 507 passing (88 new across `tests/test_invest_ship_strategy.py` 58, `tests/test_engine_diagnose.py` 23, `tests/test_invest_ship_integration.py` 7).

**Codex review:** `/ship --skip-codex codex-r2-deferred-concurrency-accepted`. The Codex gate ran TWICE inline this session via the correct background + poll pattern (see `self_improve_learnings.md` L-2026-04-19-001 for the pattern lesson captured this session). Codex R1 flagged 3 P1s -- canonical-path gap, `JournalWriter` mkdir/recovery side effects in the diagnose path, and `_format_diagnose_table` crashes on non-dict record/payload/entries -- ALL fixed inline with regression tests. Codex R2 flagged 1 P1 + 1 P2. The P1 is the multi-process concurrency race in `--approve-limits` rollback, which is the SAME concern MiniMax R5+R6 raised and I pre-deferred via a prominent docstring note on `handle_approve_limits` as the Bundle 3 MVP single-operator invariant (file-lock guard is Bundle 6 pm2-automation scope). Accepted-with-reason. The P2 is `_find_newest_engine_started` not defending against non-dict `json.loads` results -- FIXED + regression test landed in this same commit. MiniMax M2.7 R1-R7 ran iteratively on the way in: R1 approve, R2-R6 each surfaced findings that were fixed inline (hook wiring probes, parent-sha independent derivation, runtime engine_started payload test, approve body-break adversarial, `--approve-limits` rollback ordering + rollback-fails + malformed YAML Patch + concurrent-modification refusal), R7 approve with zero findings. Archives in `.minimax-reviews/`.

**Feature status change:** no feature note (Bundle 3 cycle is tracked via the architect spec at `~/Projects/K2B/plans/2026-04-19_k2bi-bundle-3-approval-gate-spec.md` rather than a K2Bi `wiki/concepts/` feature note, matching the cycle 2-4 pattern).

**Follow-ups:**
- Cycle 6: `invest-propose-limits` MVP (graduates the Phase 2 stub to a real proposer that authors `## YAML Patch` blocks consumable by `--approve-limits`).
- Cycle 7: §8.5 end-to-end test gated behind `K2BI_RUN_IBKR_TESTS=1`.
- Bundle 6: multi-process file-lock guard on `handle_approve_limits` once pm2 automation makes concurrent invocations a realistic shape (deferred from cycle 5 per docstring).

**Key decisions (cycle-level):**
- Codex reviewer pattern: launch `codex-companion.mjs` via `Bash run_in_background: true` + `Monitor` tail with `grep --line-buffered` filtering on `Running command:|Command completed|Turn completed|Turn failed|reconnect|Error|ERROR|P0|P1|^# Codex Review|Traceback`. The `codex:rescue` subagent was wrong here -- it hides progress end-to-end, making the session look hung for the full 5 minutes. Keith flagged this inline; captured in `self_improve_learnings.md` as L-2026-04-19-001 with a policy-ledger guard.
- `handle_approve_limits` ordering resolved via R4-R6 MiniMax + Codex R2: pre-compute proposal rewrite in memory, apply config patch (yaml.safe_load validates), write proposal, roll config back on proposal-write failure, refuse rollback on concurrent-mod detection. This gives atomic-pair semantics from the caller's perspective under the single-operator invariant; git staging + pre-commit Check C add a second gate at commit time.
- Canonical-path gate added in response to Codex R1 P1#1: strategy subcommands now refuse to operate on any path outside `wiki/strategies/strategy_*.md` (the hook's glob) so a retire never slips past the cycle-4 post-commit sentinel write. Path resolution uses `git rev-parse --show-toplevel` to rebase absolute-path arguments and falls back to the raw path string on git failure (tested).
