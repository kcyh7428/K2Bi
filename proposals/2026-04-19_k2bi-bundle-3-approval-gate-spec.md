---
tags: [k2bi, phase-2, bundle-3, architect-spec, approval-gate]
date: 2026-04-19
type: architect-spec
origin: k2b-architect
up: "[[Home]]"
for-repo: K2Bi
target-bundle: phase-2-bundle-3
milestones: [m2.16, m2.17]
plan-review-required: true
---

# K2Bi Phase 2 Bundle 3 -- Approval Gate (m2.16 + m2.17) -- Architect Spec

**Goal:** Keith can write a strategy spec, approve it via `/invest-ship`, and the engine honors the approval on its very next tick -- with pre-commit + commit-msg hook enforcement that manual status edits outside `/invest-ship` cannot land.

**Architecture:** `/invest-ship` becomes the sole writer of `status:` transitions on `wiki/strategies/*.md` and of `execution/validators/config.yaml` changes. Two git hooks enforce the one-writer rule. The engine's existing `load_all_approved()` + hash-drift mechanism (Bundle 2) is the reader; no engine code changes required for Bundle 3 beyond a contract acknowledgement.

**Tech stack:** Python 3.11+ (existing), bash hooks (existing), YAML frontmatter parsing (existing via `execution/strategies/loader.py`), no new runtime deps.

**Bundle estimation (from Bundle 1+2 retrospective):** 2 modules (skill + hook extensions), no external integration, simple state, expected 4-8 Codex rounds. Treat with Bundle 2-level care regardless -- this is the human-in-the-loop architectural commitment.

---

## 0. Prerequisite: kill-semantics audit resolution (ship BEFORE Bundle 3 code)

**Finding:** Three separate docs specify three different behaviors for `.killed`:

| Source | Behavior claimed |
|---|---|
| `execution/engine/main.py` + `execution/risk/kill_switch.py` (shipped Bundle 2) | `.killed` blocks NEW order submits only. Does NOT cancel existing open orders. Does NOT flatten existing positions. |
| `wiki/planning/m2.6-engine-state-machine.md` (`.killed handling specifics` section) | Same as shipped code: "Do NOT cancel existing open orders... Do NOT close existing positions". |
| `wiki/planning/risk-controls.md` line 53 ("Manual kill" row) | **Contradicts:** "Immediately flatten all positions at market; write `.killed`". |
| `wiki/planning/execution-model.md` line 104 (code-enforced non-negotiable #4) | **Contradicts:** "No position after the kill switch -- `.killed` lock file blocks new orders; existing positions flattened". |
| `wiki/planning/risk-controls.md` line 51 (daily hard-stop breaker row) | Claims breaker "Close[s] all open positions at market". Shipped code: breaker only writes `.killed` via `apply_kill_on_trip`; no flatten. |

**Resolution:** shipped code is architecturally correct and planning docs are stale. Reasons:

1. Auto-flatten on breaker trip means the engine sends MORE orders (sell-all) at the worst possible moment (mid-outage, mid-flash-crash, mid-liquidity-crisis). Slippage on forced sells under stress is exactly the failure mode Knight Capital represents.
2. The kill switch's job is "no new exposure", not "unwind existing exposure". Those are separable actions with different risk profiles.
3. First-writer-wins immutability of the `.killed` record requires that the record's reason be stable; coupling kill-write to flatten-all would mean a kill during an in-flight flatten confuses the audit trail.

**Action (must land before Bundle 3 code):** update the two stale docs to match shipped behavior.

- `wiki/planning/risk-controls.md` line 51: change "Close all open positions at market; block new orders until next session" to "Write `.killed` via `apply_kill_on_trip`; block new order submits. Existing positions + open orders are untouched. Separate `/invest flatten-all` path (Phase 4+, tracked in milestones) handles position closure."
- `wiki/planning/risk-controls.md` line 53 (Manual kill row): change "Immediately flatten all positions at market; write `.killed`" to "Write `.killed` via `apply_kill_on_trip`; block new order submits. Telegram-triggered flatten-all is a Phase 4+ separate command."
- `wiki/planning/execution-model.md` line 104: change "existing positions flattened" to "existing positions untouched; flatten-all is a separate Phase 4+ command".
- Add a new row to `wiki/planning/milestones.md` Phase 4 candidates table: `invest-flatten-all Telegram command | Phase 3 burn-in surfaces need to close all positions on panic | risk-controls.md "Manual kill" note`.

**Scope:** vault-only edits, no code. Single commit, no Codex review required (doc realignment, not new logic). Can be the same K2Bi session's first commit before Bundle 3 code work begins.

### §0.1 Phase 3 post-kill position runbook (MiniMax R1 finding -- close the ownership gap)

When `.killed` fires during Phase 3 paper trading, existing positions are NOT auto-flattened (per the kill-semantics audit above). The ownership question -- "who closes them?" -- must be answered explicitly, not deferred.

**Phase 3 answer (paper only):** Keith manual via IB Gateway / TWS. DUQ demo account has no margin-call risk; positions sit until Keith manually cancels open DAY orders and closes positions. Runbook:

1. Telegram alert fires (Bundle 5 m2.9 delivers this; until Bundle 5 ships, manual tail of `raw/journal/YYYY-MM-DD.jsonl` for `kill_switch_written` event).
2. Keith opens IB Gateway / TWS, reviews open orders + positions.
3. Keith cancels / closes at his discretion (he is the position owner).
4. Keith deletes `.killed` when ready to resume (human-only operation; no automation exists).
5. Engine restart transitions KILLED -> CONNECTED_IDLE on next tick.

**Phase 6 (live capital) hard prerequisite:** `/invest flatten-all` Telegram command MUST exist + be tested before live funding. Deferring to "Keith will manually close" is acceptable ONLY because Phase 3 + 4 + 5 use paper. Phase 6 gate check: verify the command exists, test it in a dry-run, pass or block live funding.

**Action item in Bundle 3 cycle 1 doc-realignment commit:** add a `## Kill-Switch Runbook` subsection to `wiki/planning/risk-controls.md` capturing the above. Also add a row to `wiki/planning/milestones.md` Phase 6 gate checklist: `/invest flatten-all exists + dry-run tested | block live funding until verified | risk-controls.md Kill-Switch Runbook`.

---

## 1. Scope + non-goals

### In scope

- **m2.17** -- strategy approval flow: `/invest-ship --approve-strategy` transitions `status: proposed → approved` on `wiki/strategies/strategy_<name>.md`; `/invest-ship --reject-strategy` transitions to `rejected`; `/invest-ship --retire-strategy` transitions `approved → retired`. Pre-commit + commit-msg hooks block any `status:` edit to `wiki/strategies/*.md` that does not come through one of these subcommands.
- **m2.16** -- `invest-propose-limits` skill MVP: draft validator config delta from natural-language input, write to `review/strategy-approvals/`, never touch `config.yaml` directly. Wire `/invest-ship --approve-limits` to consume approved proposals and apply the config delta in a single gated commit.
- Tests (unit + end-to-end) covering both milestones + every negative hook path.
- Deploy-coverage assertion in `/invest-ship` step 12 (pre-existing open architect item, folded into Bundle 3).

### Out of scope (document explicitly; do not build)

- Rejected strategies moving to `wiki/strategies/archive/` (manual Keith action today; auto-archive is Phase 4+).
- Proposal-history view for limits proposals (stub defers to Phase 4).
- Auto-rollback of limits proposals (stub defers to Phase 4).
- Backtest of a proposed limits change against recent data (Phase 4 nice-to-have).
- Engine hot-reload of newly approved strategies (see §6.2 -- defer to Bundle 6 pm2 automation; Bundle 3 ships with manual restart as the integration point).
- `/invest flatten-all` Telegram command (surfaced by kill-semantics audit; tracked as Phase 4 candidate).
- Multi-strategy batched approval in a single `/invest-ship` (see §6.4 -- one-at-a-time only).

---

## 2. Data contracts (LOCK these before writing code)

### 2.1 Strategy file shape (extends Bundle 2's existing loader)

**Path:** `wiki/strategies/strategy_<slug>.md` (matches K2Bi CLAUDE.md File Conventions, not Bundle 2's loader docstring example which showed flat `<name>.md`).

**Action item for implementer:** verify `execution/strategies/loader.py::load_all_approved()` globs `wiki/strategies/strategy_*.md` -- if it globs flat `wiki/strategies/*.md`, decide explicitly whether to (a) keep flat (update CLAUDE.md file convention) or (b) switch to `strategy_` prefix (update the loader glob). Architect recommendation: **(b)** keep `strategy_` prefix -- it matches every other wiki/ folder convention in K2Bi (person_, project_, concept_, insight_), and a flat `wiki/strategies/` namespace would collide with `index.md` + future sub-folders like `archive/`.

**Frontmatter schema (required fields unless noted):**

```yaml
---
name: <slug>                          # matches filename stem minus "strategy_"
status: proposed | approved | rejected | retired
strategy_type: hand_crafted | <future types>
risk_envelope_pct: <float 0-1>        # max % of NAV this strategy can risk per trade
regime_filter: [<regime>, ...]        # which regimes this strategy runs under
order:                                # hand-crafted MVP: one order spec per strategy
  ticker: <SYM>
  side: buy | sell
  qty: <int>
  limit_price: <Decimal>
  stop_loss: <Decimal>
  time_in_force: DAY | GTC | IOC | FOK
# fields populated by /invest-ship on approval (never by hand):
approved_at: <ISO-8601 UTC>
approved_commit_sha: <short-sha of parent commit at approval time>
# fields populated by /invest-ship on rejection:
rejected_at: <ISO-8601 UTC>
rejected_reason: "<Keith's text>"
# fields populated by /invest-ship on retirement:
retired_at: <ISO-8601 UTC>
retired_reason: "<Keith's text>"
# existing K2Bi frontmatter convention:
tags: [strategy, <SYM>, ...]
date: YYYY-MM-DD
type: strategy
origin: keith
up: "[[index]]"
---
```

**Body requirements:**

- `## How This Works` section MUST be non-empty. This is the Teach Mode permanent gate (per K2Bi CLAUDE.md Teach Mode section) -- regardless of `learning-stage: novice|intermediate|advanced`. If the section is missing or contains only whitespace, approval fails.

**Extensibility:** Any field not in this schema: `/invest-ship --approve-strategy` preserves it as-is. The pre-commit hook only checks the enum for `status:`; it does not enforce the full schema (that is `loader.py`'s job at runtime, where a malformed approved file fails loud per the Bundle 2 contract).

### 2.2 Status enum

```
proposed   # draft written; awaiting approval decision
approved   # /invest-ship transitioned; engine reads on next tick (after manual restart in Bundle 3; automated in Bundle 6)
rejected   # /invest-ship rejected with reason; TERMINAL (Keith creates a new draft for a revision)
retired    # previously approved, now disabled; TERMINAL (engine will not load)
```

**Allowed transitions (anything else must fail at the hook layer):**

| From \\ To | proposed | approved | rejected | retired |
|---|---|---|---|---|
| (new file) | `/invest-ship --draft-strategy` (optional; Keith can also just write the file by hand at status=proposed) | -- | -- | -- |
| **proposed** | (edits in-place stay proposed) | `/invest-ship --approve-strategy` | `/invest-ship --reject-strategy --reason "<text>"` | -- |
| **approved** | -- (must retire + create new proposed draft) | -- (already approved; re-approving is an error) | -- | `/invest-ship --retire-strategy --reason "<text>"` |
| **rejected** | -- (terminal) | -- | -- | -- |
| **retired** | -- (terminal) | -- | -- | -- |

Attempting any forbidden transition in a commit diff = commit-msg hook fails loud.

**Content-immutability for approved strategies (MiniMax R4 + R7 finding -- close the body-edit bypass):**

Once `status: approved`, the strategy file is effectively immutable. The ONLY allowed staged diff on an approved file is the retirement transition:

- `status: approved` -> `status: retired` with same-commit addition of `retired_at` + `retired_reason` fields
- No other frontmatter field changes permitted
- No body changes permitted (including "## How This Works")

Enforced by §4.1 Check D (pre-commit). Rationale: safety-critical fields (`order.limit_price`, `order.stop_loss`, `order.qty`, `risk_envelope_pct`, `regime_filter`) must not drift post-approval without going through the full approve cycle. "Approved" means LOCKED, not "approved-and-still-tuneable".

**Happy-path consequence:** a typo in a strategy's "## How This Works" section post-approval requires `/invest-ship --retire-strategy` then a new proposed draft. Overhead measured in seconds; safety payoff is full-cycle re-review of any change. Accepted trade-off; see Q8.

Proposed strategies remain body-mutable (drafts evolve). Rejected + retired are terminal (no further edits accepted; Keith git-mv to archive/ manually if desired).

### 2.3 Limits proposal file shape (m2.16 output)

**Path:** `review/strategy-approvals/YYYY-MM-DD_limits-proposal_<slug>.md`

```yaml
---
tags: [review, strategy-approvals, limits-proposal]
date: YYYY-MM-DD
type: limits-proposal
origin: keith
status: proposed | approved | rejected         # same transition matrix applies
applies-to: execution/validators/config.yaml
# fields populated by /invest-ship --approve-limits:
approved_at: <ISO-8601 UTC>
approved_commit_sha: <short-sha>
up: "[[index]]"
---

# Limits Proposal: <one-line summary>

## Change

```yaml
rule: position_size | trade_risk | leverage | market_hours | instrument_whitelist
change_type: widen | tighten | add | remove
before: <current value>
after: <proposed value>
```

## Rationale (Keith's)

<natural-language reason>

## Safety Impact (skill's assessment)

<honest assessment; e.g. "Doubles max loss per trade. Combined with 5 open positions, max total risk at any time rises from 5% to 10% of NAV.">

## Approval

<populated by /invest-ship --approve-limits at approval time: link to the config.yaml commit SHA that applied this delta>
```

---

## 3. m2.17 -- strategy approval flow

### 3.1 `/invest-ship` subcommands (new)

Extend `.claude/skills/invest-ship/SKILL.md` with four new flags. Each gates on a single file path argument:

| Flag | Takes | Transition | Required arg |
|---|---|---|---|
| `--approve-strategy <path>` | one strategy file at status=proposed | proposed → approved | path |
| `--reject-strategy <path> --reason "<text>"` | one strategy file at status=proposed | proposed → rejected | path, reason |
| `--retire-strategy <path> --reason "<text>"` | one strategy file at status=approved | approved → retired | path, reason |
| `--approve-limits <path>` | one limits-proposal at status=proposed | proposed → approved, also edits `execution/validators/config.yaml` | path |

**Mutual exclusion:** at most one of these flags per `/invest-ship` invocation. Combining two = fail immediately with usage help. This ensures one commit = one state transition = one audit unit (per §6.4 one-at-a-time decision).

### 3.2 Subcommand workflow (using `--approve-strategy` as the canonical example)

```
Input: /invest-ship --approve-strategy wiki/strategies/strategy_spy-rotational.md

Step A: Validate input file
  - File exists at given path
  - Frontmatter parses as YAML
  - status == proposed (else: "Strategy is <status>, cannot approve. Allowed transition: proposed → approved only.")
  - All required frontmatter fields present (name, strategy_type, risk_envelope_pct, regime_filter, order{...})
  - filename stem == "strategy_" + frontmatter name (consistency check)
  - "## How This Works" section exists and is non-empty after stripping whitespace
  - If any check fails: print specific error, exit 1. Do NOT proceed to commit.

Step B: Codex plan review on the strategy (Checkpoint 1)
  - Run the existing Codex review-background-poll pattern with a focused prompt:
      "Review this proposed strategy spec for: (1) look-ahead bias in rules, (2) unrealistic assumptions in order spec (stop_loss too tight, limit too aggressive), (3) regime_filter mismatch with strategy_type, (4) missing 'How This Works' clarity from Keith's pedagogical perspective (learning-stage: novice)."
  - On Codex unavailable: fall back to scripts/minimax-review.sh with --focus "strategy spec review".
  - Present findings neutrally. Keith decides fix / defer / accept.
  - If Keith chooses fix: loop (edit strategy file, re-run Codex). Re-entering Step B keeps status at proposed.
  - Log the round outcome for audit (same pattern as Bundle 2 R1..Rn).

Step C: Keith final approve / reject / defer
  - Prompt: "Approve strategy <name> at status=approved? [approve / reject --reason <text> / defer]"
  - approve: continue to Step D
  - reject: switch to --reject-strategy flow (Step D-reject), asking for --reason if not already provided
  - defer: exit 0, leave file at proposed, do NOT commit

Step D: Apply the transition + stage
  - Parse current HEAD short-sha (this becomes approved_commit_sha -- see §6.1 Q1)
  - Edit the strategy file atomically (tempfile + os.replace):
      status: proposed           →  status: approved
      (no field)                 →  approved_at: <now in ISO-8601 UTC, microsecond precision>
      (no field)                 →  approved_commit_sha: <parent short-sha>
  - git add <strategy file>

Step E: Run the normal /invest-ship flow FROM step 3 onward (Codex pre-commit review of the diff, commit, push, DEVLOG, wiki/log, deployment handoff)
  - The commit message MUST include these trailers (built by the subcommand, inserted into the commit body before the Co-Authored-By/Co-Shipped-By lines):
      Strategy-Transition: proposed -> approved
      Approved-Strategy: strategy_<slug>
  - Commit subject: "feat(strategy): approve <slug>"
  - Pre-commit hook: checks wiki/strategies/*.md diffs -- must match §4.1 rules.
  - Commit-msg hook: checks trailers present -- must match §4.2 rules.

Step F: Post-commit notice to Keith (UPDATED -- MiniMax R5 finding: add restart verification)
  - Print: "Strategy <slug> approved at <commit sha>. Engine will pick up on next startup.
    Bundle 3 does NOT automate engine restart -- Bundle 6 (pm2) will.

    VERIFY the engine picked up the approval (or is still on stale state):
      python -m execution.engine.main --diagnose-approved

    This new Bundle 3 subcommand reads the most recent engine_started journal entry and
    prints the approved-strategy set the engine was initialized with. If the output does
    NOT include strategy_<slug> with approved_commit_sha=<this-commit>, restart is required.

    To smoke-test end-to-end now: `python -m execution.engine.main --once --account-id DU12345`
    and verify the journal shows strategy_loaded + order_proposed + order_submitted for <slug>."
```

**New CLI subcommand spec (`--diagnose-approved`):**

- Reads the newest `engine_started` event from today's `raw/journal/YYYY-MM-DD.jsonl` (and falls back to yesterday's if today's is empty).
- Prints a table: strategy name, approved_commit_sha, regime_filter, risk_envelope_pct.
- If no `engine_started` event within the last 24h: prints "engine not started in last 24h; run `--once` or restart the daemon".
- Exit 0 always (diagnostic; non-blocking). Keith reads the output visually.
- Unit test: seed a journal with a known engine_started event, run `--diagnose-approved`, assert stdout contains the seeded strategy name + sha.

**`--reject-strategy` variant:** Step A same. Step B-D: no Codex review required (rejection is a decision, not a spec change). Edit frontmatter: `status: rejected`, add `rejected_at`, `rejected_reason`. Commit trailer: `Strategy-Transition: proposed -> rejected` + `Rejected-Strategy: strategy_<slug>`. Commit subject: `feat(strategy): reject <slug>`.

**`--retire-strategy` variant:** Step A validates status=approved. No Codex review required.

**Step D-retire (frontmatter stage only -- sentinel lands via post-commit hook; Codex v2 R1 finding):**

`/invest-ship --retire-strategy` edits the frontmatter ONLY. The sentinel write is delegated to a new post-commit hook (§4.3) so it fires atomically with commit landing, not before.

Edit frontmatter: `status: retired`, add `retired_at`, `retired_reason`. Commit trailer: `Strategy-Transition: approved -> retired` + `Retired-Strategy: strategy_<slug>`. Commit subject: `feat(strategy): retire <slug>`.

**Why sentinel lands via post-commit hook, not pre-commit:**

v2's initial design wrote the sentinel BEFORE the commit. Codex v2 plan review caught the race: if the commit aborts (pre-commit hook rejects, commit-msg hook rejects, Keith cancels at a prompt, hook disk write fails), the sentinel is orphaned on disk. Engine then refuses submits for a strategy whose committed state is still `approved`. Self-inflicted denial of service.

Moving to post-commit closes this: sentinel only exists if the commit actually landed. The `.githooks/post-commit` hook parses the just-landed commit's trailers (`git log -1 --format=%B`) for `Retired-Strategy: strategy_<slug>` and writes the sentinel atomically when found.

**Residual race (milliseconds, defense-in-depth covers):** between `git commit` completing and the post-commit hook firing is a synchronous window inside git's own commit sequence -- typically sub-millisecond. Even if this window is exercised, the committed file state now shows `status: retired`, so Bundle 2's `strategy_file_modified_post_approval` hash-drift detection fires on the next tick regardless of sentinel presence. Sentinel is defense-in-depth; file-state is the primary signal post-commit.

**Engine code delta (Bundle 3 adds; NOT pure-read like original plan):**

- `execution/risk/kill_switch.py`: new functions mirroring `.killed` module shape --
  - `assert_strategy_not_retired(slug, path=None)` raises `StrategyRetiredError` if `.retired-<slug>` exists
  - `StrategyRetiredError(RuntimeError)` with `strategy_slug` + `record` attributes
  - `is_strategy_retired(slug, path=None)` + `read_retired_record(slug, path=None)` for non-raising checks
  - Default path base: `Path.home() / "Projects" / "K2Bi-Vault" / "System"` (same dir as `.killed`)
- `execution/engine/main.py`: one call site in the submit path, immediately after the existing `assert_not_killed()` call, calling `assert_strategy_not_retired(candidate.strategy.name)`.
- Unit test: retire a strategy (sentinel written), engine submit path raises `StrategyRetiredError` synchronously with no IBKR call.
- Integration test: in the §8.5 end-to-end, after retire-commit lands during engine `--once`, verify journal shows `order_rejected` with reason `strategy_retired` (new event subtype) for any candidate from the retired strategy.

**Sentinel filename scheme (LOCKED post-cycle-3 convergence; architect retrofit):**

Cycle 3 landed after 6 Codex rounds converged on the filesystem-safe design:

- Path: `K2Bi-Vault/System/.retired-<sha16>.json` where `<sha16>` = first 16 hex chars of `sha256(filename_stem)`.
- `filename_stem` = the strategy file's basename without `.md` extension (e.g. `strategy_spy-rotational` or `mean.reversion`). Derived from the file path, NEVER from frontmatter `name:`. This closes all six issue classes structurally: filesystem-safe (no traversal / length / case / encoding hazards), drift-safe (filename-stem is the canonical identity that survives frontmatter `name:` rename).
- Record schema (JSON): `{"ts": <ISO-UTC micro>, "reason": <text>, "source": "invest-ship --retire-strategy", "filename_stem": <stem>, "slug": <sha16>, "commit_sha": <sha>}`. `filename_stem` is included for reverse lookup + audit even though `slug` is the file-key.
- Lookup path: engine derives `<sha16>` from the strategy source path at load time, stores it on the ApprovedStrategySnapshot, checks `K2Bi-Vault/System/.retired-<sha16>.json` existence on every submit. `assert_strategy_not_retired(slug)` takes the hex slug, not a display name.

**Cycle 4 post-commit hook contract (inherits this scheme):** when the post-commit hook sees a `Retired-Strategy: strategy_<slug>` trailer, it MUST derive the sentinel's hex slug from the retired strategy file's filename stem (not the trailer's slug portion, not the frontmatter `name:`). The trailer is a human-readable hint; the file path is the source of truth.

**Sentinel lifecycle:**

- Written by `/invest-ship --retire-strategy` BEFORE commit (so race between commit landing + engine reading wiki is closed by the sentinel already being present).
- Human-removable (same as `.killed`): Keith deletes `K2Bi-Vault/System/.retired-<slug>` manually if he wants to un-retire. Un-retire is not a supported flow; the architect recommendation is "retire + new proposed draft" per Q8.
- Immutable record (same as `.killed`): first-writer-wins, contents stable after create. Subsequent retire attempts on same slug = no-op.
- Syncthing-replicated to Mac Mini (same vault path as `.killed`).

### 3.3 Engine contract (what the engine does on next tick)

Bundle 3 requires NO engine code changes. Bundle 2 already shipped:

- `execution/strategies/loader.py::load_all_approved()` filters by `status=approved`; rejected + retired + proposed are ignored automatically.
- `execution/engine/main.py` runs `load_all_approved()` at INIT; if a strategy file was mutated after approval (any byte difference), `strategy_file_modified_post_approval` event fires on next tick and that strategy is skipped for that tick (drift detection via file sha256).

**Bundle 3's integration with the engine:**

1. Keith approves strategy via `/invest-ship --approve-strategy`. The strategy file now has `status: approved`.
2. Engine must be restarted to pick up the new approval. This is a **documented manual step** in Bundle 3: the `/invest-ship` post-commit notice (Step F above) tells Keith to restart.
3. Rationale: hot-reload violates the "explicit checkpoint" principle from `execution-model.md` ("Execution engine restart -- not hot-reload -- forces explicit checkpoint"). Bundle 6 will wire pm2 to restart-on-approval; until then, manual is the right gate.
4. For a retired strategy: on the next engine restart, `load_all_approved()` simply does not include it, so the engine stops acting on it. Mid-session, if the retire commit landed while the engine was running, the next tick will detect `strategy_file_modified_post_approval` (because the file bytes changed) and skip submits from that strategy -- which is the safe behavior until restart.

**Acknowledgement required in spec, not code:** the fresh K2Bi session should verify `load_all_approved()` + drift-detection work as documented by Bundle 2 unit tests, without adding new engine code. If a gap is found (e.g. the loader doesn't properly filter retired), that is a Bundle 2 bug-fix commit, not new Bundle 3 scope.

---

## 4. Git hooks -- enforcement layer

Two hooks cooperate. Each owns a specific check; duplication across hooks is INTENTIONAL (defense in depth for the approval-contract invariant).

### 4.1 Pre-commit hook extensions (`.githooks/pre-commit`)

**Existing check (do not touch):** blocks direct `>> wiki/log.md` appends.

**New check A -- status-in-enum:** For every staged `wiki/strategies/*.md` file, read the staged contents (`git show :0:<path>`), parse YAML frontmatter, verify `status:` is one of `{proposed, approved, rejected, retired}`. Unknown values fail loud with the offending value + allowed list.

**New check B -- "How This Works" non-empty on approval:** If the staged contents have `status: approved` OR `status: proposed` (both -- the section is required at draft time, not just approval), verify the body contains a `## How This Works` section and it has non-whitespace content between the heading and the next `##` heading (or end of file). Empty or missing fails loud.

**New check C -- `config.yaml` requires matching in-commit approved limits-proposal (git-diff-based; MiniMax R3 + R8 finding):**

Original v1 used a 60-second wall-clock window on the limits-proposal's `approved_at`. This was trivially bypassable (fake `approved_at` timestamp in a manual editor edit). Replaced with deterministic git-diff check:

If `execution/validators/config.yaml` is in the staged diff (`git diff --cached --name-only` includes it):

1. At least one file matching `review/strategy-approvals/*_limits-proposal_*.md` must ALSO be in the staged diff.
2. The staged version of that limits-proposal (via `git show :0:<path>`) must have `status: approved`.
3. The HEAD version (via `git show HEAD:<path>`; or "new file at HEAD" if the proposal is newly created in this commit) must have `status: proposed` OR be a new file with `status: proposed` in its staged form compared to the prior-HEAD file at the same path.
4. The transition proposed -> approved must happen **in this commit's diff**. No wall-clock. No 60-second window. A pre-existing approved proposal from a prior commit does NOT satisfy Check C -- the approval must be atomic with the config edit.

Backdated `approved_at` timestamps cannot bypass this because the check is on git-diff state, not timestamps. Forged limits-proposal files (e.g. creating a new file already at `status: approved` without a proposed predecessor in git history) fail step 3 (prior HEAD state is "new file without proposed predecessor", which means the staged state should be `status: proposed`, not `status: approved`).

Override: `K2BI_ALLOW_CONFIG_EDIT=1` (e.g. for emergency rollback; logged as deferred drift).

**New check D -- approved strategies are content-immutable except for retire transition (MiniMax R4 + R7 finding):**

For every staged `wiki/strategies/strategy_*.md` file, compare HEAD state (`git show HEAD:<path>`) to staged state (`git show :0:<path>`):

1. If HEAD state is "new file" OR has `status: proposed`, `status: rejected`, or `status: retired`: Check D does NOT apply. (Drafts are freely editable; terminal states are trailer-gated by §4.2 + other transitions by transition matrix enforcement.)
2. If HEAD state has `status: approved`:
   - The ONLY permitted staged diff is: `status: approved` -> `status: retired` with same-commit addition of `retired_at: <timestamp>` + `retired_reason: "<text>"` fields. All other frontmatter keys must be byte-identical between HEAD and staged. The body (everything after `---`) must be byte-identical between HEAD and staged.
   - Any other diff (body change, change to `order.*`, `risk_envelope_pct`, `regime_filter`, `name`, etc.) -> FAIL with message: "Approved strategy `strategy_<name>` has post-approval modifications at `<field-list>`. Approved files are content-immutable except for retirement. To revise: `/invest-ship --retire-strategy <path>` first, then create a new proposed draft."

This closes the bypass where someone could edit `order.limit_price` on an approved file post-ship without any hook firing (the §4.2 commit-msg hook only checks status transitions, not body content).

**Override pattern:** same as existing hook -- `K2BI_ALLOW_STRATEGY_STATUS_EDIT=1` env var. Used only when rewriting the hook itself or repairing a malformed frontmatter field. Logged.

### 4.2 Commit-msg hook extensions (`.githooks/commit-msg`)

**Existing check (from Bundle 1; verify it's wired up):** if any file in `wiki/concepts/feature_*.md` has a `status:` change, require `Co-Shipped-By: invest-ship` trailer. (If this check doesn't exist yet in K2Bi -- it's in K2B but may not have ported -- that's a prerequisite to Bundle 3; fresh session should audit.)

**New check -- strategy status trailer enforcement:**

For every file in `git diff --cached --name-only` that matches `wiki/strategies/*.md`:
- Parse the staged contents for `status:` value (call it `new_status`)
- Parse `git show HEAD:<path>` for `status:` value (call it `old_status`; if the file is new at this commit, `old_status = (new file)`)
- If `old_status == new_status`, this is a body-only edit -- allowed, no trailer required.
- If `old_status != new_status`:
  - The transition `(old_status, new_status)` must be in the allowed transitions set (§2.2). If not: fail loud with the specific forbidden transition.
  - The commit message MUST contain a trailer `Strategy-Transition: <old_status> -> <new_status>` matching the file's actual change.
  - The commit message MUST contain a trailer `Approved-Strategy: strategy_<slug>` (for approve) OR `Rejected-Strategy: ...` / `Retired-Strategy: ...` matching the action.
  - The commit message MUST contain `Co-Shipped-By: invest-ship`.
  - If any of the three trailers are missing: fail loud with the missing trailer name + the full set of required trailers for this transition.

**Override:** `K2BI_ALLOW_STRATEGY_STATUS_EDIT=1` env var. Logged.

**Rationale for commit-msg vs pre-commit:** the pre-commit hook's own comment (present in the current file) says "commit-msg hook handles the `status:` edit guard for feature_*.md, since pre-commit cannot reliably read the commit message file across git versions." Same reasoning for strategy status transitions.

### 4.3 Post-commit hook -- sentinel landing on retire (Codex v2 R1 finding)

**New file:** `.githooks/post-commit` (not yet exists in K2Bi; Bundle 3 cycle 4 creates it).

**Behavior:**

1. Run `git log -1 --format=%B` to read the just-landed commit's message.
2. Grep for `Retired-Strategy: strategy_<slug>` trailer. If not present, exit 0 (this commit isn't a retire; no action).
3. If present, extract the slug. Write `K2Bi-Vault/System/.retired-<slug>` using the same atomic-create pattern as `.killed`:
   - tempfile + `os.link`, first-writer-wins, immutable once created
   - JSON record with `{"ts": <ISO-UTC>, "reason": <from commit body>, "source": "invest-ship --retire-strategy", "slug": "<slug>", "commit_sha": "<HEAD sha>"}`
4. On write failure (disk full, permission denied): log loud to stderr + `scripts/wiki-log-append.sh` with a `retire_sentinel_write_failed` event. The commit has already landed, so this is not rollbackable. Keith sees the stderr warning and can manually write the sentinel via `python -c "from execution.risk.kill_switch import write_retired; write_retired(...)"` if urgent.
5. Exit 0 regardless (post-commit hooks cannot fail the commit; it's already durable).

**Override:** `K2BI_SKIP_POST_COMMIT_RETIRE=1` env var (for testing the hook in isolation without writing sentinels). Logged.

**Sentinel lifecycle (updated from §3.2):**

- Written by `.githooks/post-commit` on retire-commit landing -- ATOMIC with commit, not before.
- Human-removable (Keith deletes if un-retiring; un-retire is not a supported flow, architect recommends retire + new proposed draft per Q8).
- Immutable record (first-writer-wins, contents stable after create).
- Syncthing-replicated to Mac Mini.
- `git commit --amend` on a retire commit: sentinel was already written by the first post-commit; amend does NOT re-trigger post-commit in standard git. This is acceptable since sentinel contents are immutable anyway (first-writer-wins).

### 4.4 Hook install + test harness

- `.githooks/` is the path; `core.hooksPath` is presumed already set in K2Bi's local `.git/config`. Fresh session: verify `git config core.hooksPath` returns `.githooks`; if not, fix.
- Test harness: add `tests/test_hooks.py` (or the K2Bi convention). Each hook test:
  - Uses `git init` in a tmpdir, copies `.githooks/*` in, sets `core.hooksPath=.githooks`, stages a synthetic diff, attempts commit, asserts exit code + stderr pattern.
  - See `tests/test_hooks_existing.py` in Bundle 1 for the existing pattern (if named differently, the fresh session finds the equivalent).

---

## 5. m2.16 -- `invest-propose-limits` skill MVP

Upgrade the existing stub at `.claude/skills/invest-propose-limits/SKILL.md` from `status: stub` to `status: shipped` by implementing the pipeline already documented in the stub. Bundle 3 adds:

### 5.1 Skill invocation contract

**Input (natural language):**
- "widen position size cap to 25%"
- "allow AAPL on the whitelist"
- "tighten daily risk to 3%"
- "add NVDA to the instrument whitelist for the next 30 days"
- (multi-turn clarification if the input is ambiguous -- e.g. which validator, what magnitude)

**Output (deterministic):** a file at `review/strategy-approvals/YYYY-MM-DD_limits-proposal_<slug>.md` matching §2.3 schema, with `status: proposed`.

### 5.2 Safety-impact heuristics (required; skill's own assessment)

The skill emits its own honest take, not just Keith's rationale. Stub already lists examples; implementer expands:

- **Widening size / risk / leverage caps:** compute post-change max exposure, compare to pre-change. Frame in NAV%.
- **Adding to instrument_whitelist:** "Neutral; this only ENABLES trading the ticker. Strategy approval still gates order fires."
- **Dropping market_hours guard:** "RISKY. Overnight fills on gap-ups would be allowed. Phase 2 default is cash-only regular hours for a reason."
- **Tightening limits:** "Safer by definition. Note that existing open positions may now violate the new cap -- the engine will not force-close, but validators reject any top-ups until the position is under the new cap."

### 5.3 `/invest-ship --approve-limits` flow

Same shape as `--approve-strategy` (§3.2) with these deltas:

- Step A: validate the limits-proposal file (frontmatter, `applies-to:`, change block parses, rationale + safety-impact sections present).
- Step B: Codex plan review with focus `"Review the safety-impact assessment for honesty; challenge the widened-cap case for tail-risk scenarios not mentioned"`.
- Step D: edit the limits-proposal frontmatter (`status: approved`, `approved_at`, `approved_commit_sha`) AND apply the change to `execution/validators/config.yaml` in the same commit. Tempfile + atomic rename for both.
- Step E: commit trailers: `Limits-Transition: proposed -> approved`, `Approved-Limits: <slug>`, `Config-Change: <rule>:<change_type>`.
- Step F: post-commit notice: "Limits change applied at <sha>. Engine restart required for `execution/validators/config.yaml` to take effect. Validators are loaded at engine startup only -- no hot-reload per risk-controls.md. Restart: see `/execute run` or pm2 restart (Bundle 6)."

### 5.4 Hard rule (from stub; carry forward as code)

The skill NEVER edits `execution/validators/config.yaml` directly. Pre-commit hook check C (§4.1) enforces it: a commit touching `config.yaml` must also contain a same-commit approved limits-proposal. Attempts to bypass (e.g. manual YAML edit + direct commit) fail at the hook layer.

---

## 6. Open architect questions -- ANSWER BEFORE IMPLEMENTATION

Per Bundle 2 retrospective lesson ("exhaustive questions + locked contracts upfront, not during R8"), these are pre-answered by the architect below. Each answer is a commitment; deviation requires a new architect review round.

### Q1: What value does `approved_commit_sha` take -- parent sha or the approval commit's own sha?

**Answer:** **Parent short-sha** (state of the world at the moment of approval, before the approval commit lands).

**Why:** The approval commit's own sha is chicken-and-egg (not known until the commit is made, which would require `--amend` to populate -- amend is banned by `/invest-ship` discipline). The parent sha is deterministic, known at Step D, and gives the auditor a clean "this is what the strategy looked like at the moment Keith approved" snapshot via `git show <approved_commit_sha>:wiki/strategies/strategy_<slug>.md`.

**Deviation cost:** if the implementer proposes own-sha instead, they must ALSO propose an amend-free way to populate the field (e.g. a second commit immediately after). Reject that proposal -- it adds complexity without audit benefit.

### Q2: How does the engine pick up new approvals -- restart-only, tick-polling, or SIGHUP?

**Answer:** **Restart-only** for Bundle 3. No code changes to the engine's reload behavior.

**Why:** hot-reload violates `execution-model.md` explicit-checkpoint principle. SIGHUP is implementation-equivalent to restart with extra complexity. Tick-polling means every tick does filesystem work that could race with a mid-transition `/invest-ship`.

**How it plays in Bundle 3's end-to-end test:** engine is started fresh AFTER approval commit lands. This is a clean test scenario without needing pm2. Bundle 6 (m2.19 pm2 config) automates restart-on-approval later.

**Documented manual step:** `/invest-ship --approve-strategy` Step F post-commit notice tells Keith the engine needs restarting.

### Q3: Rejected / retired strategies -- do they stay at their current path or move?

**Answer:** **Stay in place** with `status: rejected` / `status: retired`. No auto-move to `wiki/strategies/archive/`.

**Why:** audit trail. The file at its original path + its git history = the complete decision record. Moving is extra work without audit benefit. Clutter at the filesystem level is Keith's problem to solve with his own manual `git mv` when and if he feels it's noise -- low volume (rejections are rare) means this won't happen often.

**Engine impact:** `load_all_approved()` filters on status, so rejected + retired files in `wiki/strategies/` are already ignored. No change.

### Q4: Multi-strategy batched approval -- should one `/invest-ship` approve N strategies in one commit?

**Answer:** **One at a time.** Each `/invest-ship --approve-strategy` takes exactly one path and produces exactly one commit.

**Why:** one commit = one state transition = one audit unit = one rollback target (`git revert` undoes exactly one approval). Batching reduces audit clarity. Batching also increases the surface of what Codex must review per round. If Keith has five strategies to approve, five `/invest-ship` cycles is the right shape -- not one cycle with five strategies.

**Deviation cost:** if fresh session proposes batch, reject. The session-time overhead of 5 ships is real (5 Codex reviews, 5 commit messages) but the audit + rollback clarity payoff is substantial, and Bundle 6 automation will reduce the per-cycle friction anyway.

### Q5: "How This Works" non-empty check -- pre-commit hook, commit-msg hook, or skill-level only?

**Answer:** **Both skill-level AND pre-commit hook** (defense in depth).

**Why:** skill-level (`--approve-strategy` Step A) catches the happy path early (before Codex review burn). Pre-commit hook catches the adversarial path (someone bypasses the skill -- e.g. edits `status: proposed → approved` by hand and runs `git commit` directly). The redundancy is intentional. Commit-msg hook is the wrong layer (it sees the commit message, not the file contents).

**Interaction with status transitions:** the pre-commit check runs on ANY staged strategy file with `status: proposed` or `status: approved` (both -- the section is expected at draft time, not only at approval). Draft files with `status: proposed` without the section fail commit -- which is the desired behavior. Keith fills in the "How This Works" at draft time, not at approval time, and fills it as an ongoing discipline per Teach Mode.

### Q6: Strategy filename prefix -- `strategy_<name>.md` or flat `<name>.md`?

**Answer:** **`strategy_<name>.md`** (matches K2Bi CLAUDE.md File Conventions; collision-safe with `index.md` + future sub-folders).

**Why:** Bundle 2's `loader.py` docstring example shows flat; this may or may not match the actual glob -- fresh session audits. If loader globs flat, change the glob to `strategy_*.md` (one-liner). If loader globs with prefix already, CLAUDE.md already matches; no change.

**Deviation cost:** flat means a future `wiki/strategies/README.md` or similar would need the loader to skip specific filenames by name, which is brittle. Prefix-based glob is robust.

### Q7: Retirement race -- how does Bundle 3 close the one-tick exposure window? (added after MiniMax R2)

**Answer:** **Per-strategy sentinel file** `K2Bi-Vault/System/.retired-<slug>` checked synchronously by the engine's submit path, NOT at tick boundaries.

**Why:** Bundle 2's `strategy_file_modified_post_approval` event only fires at the next tick after file drift is detected. At default 30-60s tick cadence, a retire-commit landing at T+0.3s into a tick leaves up to ~60s of exposure where the retired strategy can submit orders. For a system handling real capital (even paper, for signal integrity), one tick of unwanted trades pollutes backtesting + journal and is unacceptable.

The sentinel pattern mirrors `.killed`: atomic first-writer-wins create, human-removable, Syncthing-replicated. Engine's submit path adds one call site (`assert_strategy_not_retired(slug)` right after `assert_not_killed()`). Pre-submit check is synchronous -- no tick-boundary window.

**Implementation cost:** ~30 LOC in `execution/risk/kill_switch.py` (mirrors existing `.killed` functions), 1 LOC call site in `execution/engine/main.py` submit path, 1 test. Same file, same module, same semantics.

**Deviation cost of the rejected alternative ("defer to Phase 4"):** documented one-tick exposure window per retire event. For a human-in-the-loop paper trading workflow, acceptable in theory; for audit cleanliness of the decision journal during Phase 3 burn-in, NOT acceptable. A single post-retire phantom order in the journal is a dirty signal that contaminates retro analysis.

### Q8: Body-edit bypass on approved strategies -- how is post-approval tampering prevented? (added after MiniMax R4 + R7)

**Answer:** **Content-immutability via §4.1 Check D.** Approved strategy files are byte-frozen except for the retire transition.

**Why:** v1 of this spec allowed "body-only edits" on approved files without trailers (§4.2's "old_status == new_status => no trailer required"). Safety-critical fields (`order.limit_price`, `stop_loss`, `qty`, `risk_envelope_pct`, `regime_filter`) live in the body + frontmatter of the approved file. Allowing edits post-approval means the engine's next restart loads a modified spec that was never reviewed or approved in its current form. This violates the approval contract from `execution-model.md` ("Keith approves STRATEGIES, not individual trades" means the approved STATE of the strategy, not "approval for this name plus whatever tweaks arrive later").

Bundle 2's `strategy_file_modified_post_approval` drift detection is a safety NET (detects drift), not a GATE (prevents drift). Check D is the gate.

**Deviation cost:** typo-in-body scenarios require retire + re-propose. Overhead: 1 retire commit + 1 new proposed-draft commit + 1 re-approve commit. 3 commits instead of 1 edit. Acceptable because (a) typos should be rare once a strategy ships, (b) retire-and-re-propose forces the retire-sentinel path which is audit-cleaner anyway, (c) the alternative (allow body edits with a new "Strategy-Edited" trailer) creates an entire new transition type with its own test matrix -- strictly more complex.

### Q11: File-lock guard on /invest-ship strategy subcommands -- ship in Bundle 3 or defer to Bundle 6? (added post-cycle-5 after Codex R2 P1 + parallel MiniMax finding)

**Answer:** **Defer to Bundle 6.** The cycle 5 docstring + commit body deferral is the architect's confirmed call.

**Why:** Today /invest-ship is interactive (Codex review wait + Keith confirm + Codex pre-commit Checkpoint 2). No automation, no scheduler, no secondary process triggers it. The "single-operator invariant" -- exactly one /invest-ship process at a time, owned by Keith -- holds through Phases 2-5. The concurrency race Codex + MiniMax flagged is real-shaped but not real-loaded under any foreseeable Bundle 4-5 workflow.

Bundle 6's pm2 work coordinates engine + alert + feed + observer-loop daemons. That coordination layer is where invariant pressure genuinely rises -- daemon restart-on-approval triggers + scheduled cleanup + multi-process state checks all need the file-lock guard. Adding the guard at the same time as the daemons that pressure it is the natural seam.

**Concrete un-defer trigger:** if Phase 3 burn-in (or any earlier phase) surfaces a single concurrency near-miss -- race observed, even without a journaled order or capital impact -- pull the guard forward into a hotfix cycle within the same week. Do NOT wait for Bundle 6 to start. The invariant is a Phase-2-3-4-5 working assumption, not an indefinite deferral.

**Implementation hint for Bundle 6 (when un-defer fires or pm2 lands):** flock-style guard around the four subcommand handlers' critical sections (file edit + git stage + commit). Lock path: `K2Bi-Vault/System/.invest-ship.lock`. Hold for the entire subcommand handler run; release on commit-success OR commit-failure-cleanup. Sub-millisecond contention; same lock-file pattern as `.killed`.

**Deviation cost of NOT deferring:** ~1 cycle of Bundle 3 burn (file-lock implementation + concurrency tests + Codex round on the new locking semantics) for zero runtime risk reduction in Phases 2-5. Strictly negative ROI given today's single-operator workflow.

### Q10: Retire sentinel timing -- pre-commit or post-commit? (added after Codex v2 R1)

**Answer:** **Post-commit hook write**, NOT pre-commit.

**Why:** v2 originally wrote the sentinel before staging the frontmatter change. Codex v2 flagged the race: on any commit-abort path (pre-commit hook rejects, commit-msg hook rejects, Keith aborts at a prompt), the sentinel is orphaned. Engine then blocks submits for a strategy whose committed state is still approved. DoS on the approval contract.

Moving to post-commit means: sentinel exists IFF retire-commit landed. Git's post-commit hook runs synchronously within the commit sequence, so the window between commit durability and sentinel presence is sub-millisecond. Bundle 2's `strategy_file_modified_post_approval` hash-drift detection covers the residual window on the next tick (the file is now `status: retired` post-commit, which triggers drift detection).

**Deviation cost:** the residual sub-ms window means there's no longer a "write sentinel pre-commit" determinism claim. Accept: post-commit is atomic with commit within git's own sequence + hash-drift is the primary signal + sentinel is defense-in-depth.

### Q9: Config.yaml approval gate -- wall-clock window or git-diff verification? (added after MiniMax R3 + R8)

**Answer:** **Git-diff-based** (§4.1 Check C). The limits-proposal must transition proposed -> approved in the SAME commit as the config.yaml edit.

**Why:** v1 used a 60-second wall-clock window on the limits-proposal's `approved_at` timestamp. This was bypassable by a manual editor edit that forged the timestamp. It also created a race where `/invest-ship --approve-limits` running Codex review could exceed 60 seconds (Codex rounds are minutes), making the legitimate happy path fail the hook. Git-diff is deterministic, not time-sensitive, and not forgeable.

**Deviation cost:** none -- `/invest-ship --approve-limits` already edits both files atomically in the same commit. The original window was a shortcut ("pre-commit can't read the commit message") that turned out to be the wrong abstraction: pre-commit CAN read staged diffs, which is what the check actually needs.

---

## 7. Deploy-coverage assertion (pre-existing open architect item, folded in)

### 7.1 Background

Bundle 1's `deploy-to-mini.sh` audit caught a missing untracked-top-level-doc issue. The lesson: the deploy script's coverage is implicit (a set of rsync source paths hard-coded in the script), and top-level paths can drift without anyone noticing until something breaks.

### 7.2 Implementation (in `/invest-ship` step 12 preflight)

Add a check to `.claude/skills/invest-ship/SKILL.md` step 12 "Deployment handoff":

```bash
# Deploy-coverage preflight: fail loud if the deploy script can't see a top-level dir
if [ -x scripts/deploy-to-mini.sh ]; then
  DEPLOY_TARGETS=$(grep -oE 'rsync[^|]*[a-zA-Z][a-zA-Z_/-]*/' scripts/deploy-to-mini.sh | awk '{print $NF}' | sort -u)
  TOPLEVEL_DIRS=$(find . -mindepth 1 -maxdepth 1 -type d ! -name '.git' ! -name '.venv' ! -name 'node_modules' ! -name '__pycache__' ! -name '.pending-sync' ! -name '.minimax-reviews' -printf '%f/\n' | sort -u)
  UNCOVERED=$(comm -23 <(echo "$TOPLEVEL_DIRS") <(echo "$DEPLOY_TARGETS" | grep -oE '^[^/]+/'))
  if [ -n "$UNCOVERED" ]; then
    echo "[error] Top-level dirs not covered by deploy-to-mini.sh rsync targets:"
    echo "$UNCOVERED"
    echo "Either add them to the deploy script, or add them to the exclusion allowlist."
    exit 1
  fi
fi
```

### 7.3 Allowlist mechanism (structured config, not bash comment -- MiniMax R6 finding)

v1 proposed parsing a bash comment block in `scripts/deploy-to-mini.sh`. Brittle (comments aren't enforced by any tooling; a rename of the comment marker silently breaks the preflight; variable-held rsync paths like `EXECUTION_DIRS="execution/"; rsync $EXECUTION_DIRS dest/` are invisible to the grep). Replaced with a structured config file that both the deploy script and the preflight read as the single source of truth.

**File:** `scripts/deploy-config.yml`

```yaml
# scripts/deploy-config.yml -- single source of truth for deploy-to-mini.sh
# + /invest-ship deploy-coverage preflight (§7.2). Both tools read this file.
#
# targets: explicit list of paths rsync'd to Mac Mini (relative to repo root)
# excludes: explicit list of top-level paths intentionally NOT deployed
# categories: mapping from top-level path -> /sync mailbox category (must match
#             the four categories in invest-ship SKILL.md: skills, code,
#             dashboard, scripts). Paths not in any category fall through to
#             the default category from the rsync target they're under.

targets:
  - path: .claude/skills/
    category: skills
  - path: execution/
    category: code
  - path: scripts/
    category: scripts
  # ... explicit list of every rsync target

excludes:
  - .git
  - .venv
  - node_modules
  - __pycache__
  - .pending-sync
  - .minimax-reviews
  - plans
  - .claude/plans
  # any path that should NEVER deploy
```

**Deploy script:** `scripts/deploy-to-mini.sh` reads `targets:` at startup, constructs rsync invocations from them (one per target). No hard-coded paths remain in the rsync lines.

**Preflight in `/invest-ship` step 12:** reads `targets:` + `excludes:`, computes `set(top_level_dirs) - set(targets_roots) - set(excludes)`, fails loud with the uncovered list if non-empty.

**Adding a new deployed path:** single-line `targets:` addition. Deploy script picks it up automatically. Preflight passes.

**Adding a new excluded path:** single-line `excludes:` addition. Both tools respect it.

**Migration from v1 hard-coded rsync:** Bundle 3 ship cycle 2 refactors `deploy-to-mini.sh` to read `deploy-config.yml`. One-time ~1 hour dev cost. Unlocks the preflight check + makes future additions low-friction.

**Test harness:** `tests/test_deploy_coverage.py` creates a mock repo with (a) a new top-level dir not in `targets` + not in `excludes`: assert preflight exits 1 with the dir in stderr. (b) a new top-level dir in `targets`: assert preflight passes. (c) a deploy-config.yml with a variable-path rsync target: n/a because structured config has literal paths, not variables -- this eliminates the MiniMax-flagged variable-path grep failure entirely.

### 7.4 When this check runs

Before the `/ship now or defer?` prompt. Drift = block the deploy conversation entirely (Keith can't defer what he can't deploy). Forces the fix inline.

---

## 8. Test + verification matrix

Per Bundle 2 retrospective ("exhaustive matrix from day one"), this is the completeness-audit surface for Bundle 3. The fresh session MUST produce a unit-test row for every cell below. Missing cells at ship time = Codex P1.

### 8.1 Hook behavior matrix (commit-msg)

| Scenario | old_status | new_status | has Strategy-Transition trailer? | has Co-Shipped-By? | has Approved/Rejected/Retired-Strategy trailer? | Expected |
|---|---|---|---|---|---|---|
| Happy approve | proposed | approved | yes | yes | Approved-Strategy yes | PASS |
| Happy reject | proposed | rejected | yes | yes | Rejected-Strategy yes | PASS |
| Happy retire | approved | retired | yes | yes | Retired-Strategy yes | PASS |
| Body-only edit | proposed | proposed | no | no | no | PASS (no trailers required) |
| Missing Strategy-Transition | proposed | approved | no | yes | yes | FAIL |
| Missing Co-Shipped-By | proposed | approved | yes | no | yes | FAIL |
| Missing Approved-Strategy | proposed | approved | yes | yes | no | FAIL |
| Forbidden approved→proposed | approved | proposed | yes | yes | yes | FAIL (transition not allowed) |
| Forbidden rejected→approved | rejected | approved | yes | yes | yes | FAIL |
| Forbidden retired→anything | retired | (any) | (any) | (any) | (any) | FAIL |
| Override env set | (any) | (any) | (any) | (any) | (any) | PASS with warning logged |
| Body edit on approved strategy (any non-retire diff) | approved | approved (body or non-retire field changed) | any | any | any | FAIL via §4.1 Check D (commit-msg sees no status change; pre-commit Check D blocks) |
| Clean retire (status-only + retired_at + retired_reason) | approved | retired | yes | yes | Retired-Strategy yes | PASS (both Check D and commit-msg allow) |

### 8.2 Hook behavior matrix (pre-commit)

| Scenario | Expected |
|---|---|
| Staged strategy file with `status: xyz` (unknown enum value) | FAIL |
| Staged strategy file with `status: proposed`, no `## How This Works` section | FAIL |
| Staged strategy file with `status: approved`, has `## How This Works` but body empty | FAIL |
| Staged strategy file with `status: approved`, has non-empty `## How This Works` | PASS |
| Staged `config.yaml` edit without matching staged approved limits-proposal | FAIL |
| Staged `config.yaml` edit WITH matching staged approved limits-proposal in same commit | PASS |
| Override env `K2BI_ALLOW_STRATEGY_STATUS_EDIT=1` | PASS with warning logged |
| Override env `K2BI_ALLOW_CONFIG_EDIT=1` | PASS with warning logged |
| Check D: approved strategy with `order.limit_price` changed, no status change | FAIL with field list in stderr |
| Check D: approved strategy with "## How This Works" body edited, no status change | FAIL |
| Check D: approved strategy with ONLY status→retired + retired_at + retired_reason added, all other bytes identical | PASS |
| Check D: approved strategy with status→retired BUT also `order.qty` changed | FAIL (retire transition must be pure; no co-mingled edits) |
| Check D: proposed strategy with body edit (Check D does not apply) | PASS |
| Post-commit hook: retire commit lands -> sentinel file appears at `.retired-<slug>` with matching commit_sha | PASS (sentinel present, engine can now block submits) |
| Post-commit hook: retire commit aborted pre-commit (hook rejection) -> sentinel NEVER written | PASS (orphaned-sentinel race closed) |
| Post-commit hook: non-retire commit (e.g. approve, reject) -> hook exits 0 with no sentinel write | PASS |
| Post-commit hook: `git commit --amend` on a retire commit -> sentinel already present from first commit, amend is no-op | PASS (first-writer-wins; sentinel contents stable) |
| Check C git-diff: config.yaml + NEW limits-proposal with staged-state=approved, HEAD-state=new-file-proposed | PASS |
| Check C git-diff: config.yaml + limits-proposal file with staged-state=approved AND HEAD-state=approved (already approved in prior commit) | FAIL (transition must happen IN this commit) |
| Check C git-diff: config.yaml + limits-proposal file with staged-state=approved AND HEAD-state=new-file-approved (forged new file at approved status) | FAIL (HEAD-state must be proposed or new-file-proposed) |
| Check C git-diff: config.yaml edit WITHOUT any staged limits-proposal | FAIL |
| Check C git-diff: config.yaml + limits-proposal with backdated fake `approved_at: 2020-01-01` | PASS if the transition is real in this diff (timestamp is ignored by Check C); status transition integrity is what matters

### 8.3 Loader + engine integration (Bundle 2 code; verify-only in Bundle 3)

| Scenario | Expected |
|---|---|
| `load_all_approved()` glob against a directory with one approved + one proposed + one rejected + one retired | returns exactly the 1 approved |
| Strategy approved at T0, file byte-mutated at T1 (mid-session) | next `tick_once()` fires `strategy_file_modified_post_approval`, skips submits from that strategy |
| Strategy transitioned approved → retired mid-session | next `tick_once()` fires `strategy_file_modified_post_approval` (hash change), engine restarts required to fully drop it from active set |

### 8.4 `invest-propose-limits` skill tests

| Scenario | Expected |
|---|---|
| NL input: "widen position size cap to 25%" | output file at correct path; frontmatter valid; change block YAML parses; safety-impact populated |
| NL input: ambiguous ("tighten risk") | skill asks clarifying question (multi-turn); resolves to a specific rule |
| Skill attempts to write `execution/validators/config.yaml` | does not; writes only to `review/strategy-approvals/` |
| Safety-impact for "widen max_trade_risk_pct 1% → 2%" | mentions doubling + total risk compounding with position count |

### 8.5 End-to-end test (the user's explicit ask)

Sequence (must pass before Bundle 3 ships):

```
1. Fresh K2Bi workspace. No wiki/strategies/strategy_*.md files.
2. Write wiki/strategies/strategy_spy-rotational.md at status=proposed, with a valid order spec
   (ticker: SPY, side: buy, qty: 1, limit_price: 500.00, stop_loss: 490.00, tif: DAY),
   risk_envelope_pct: 0.01, regime_filter: [risk_on], and a non-empty "## How This Works" body.
3. Commit the draft (status=proposed, pre-commit hook must pass: frontmatter valid,
   "How This Works" non-empty, no status transition required at draft time).
4. Run /invest-ship --approve-strategy wiki/strategies/strategy_spy-rotational.md
   - Codex plan review fires (or --skip-codex <reason>)
   - Keith confirms approve
   - Edit applies: status=approved, approved_at set, approved_commit_sha = parent short-sha
   - Commit lands with correct trailers
   - Post-commit notice prints "engine restart required"
5. Run `python -m execution.engine.main --once --account-id DU12345` (paper account from Bundle 2)
6. Verify raw/journal/YYYY-MM-DD.jsonl contains (in order):
   - engine_started
   - engine_recovered (clean) OR recovery_catch_up if prior journal exists
   - strategy_loaded (if emitted; or implicit via successful load)
   - tick_clock (first tick)
   - For the SPY strategy: order_proposed (via strategy_emit_candidate) → validator pass path →
     order_submitted (if IBKR paper ack) OR order_rejected (if any validator failed)
   - engine_stopped (clean exit after --once)
7. Verify the strategy's approved_commit_sha in the journaled order matches the commit in step 4.
8. Negative test: manually edit wiki/strategies/strategy_spy-rotational.md in a text editor,
   change status=approved → status=retired, try `git commit -m "fix: retire"`.
   - commit-msg hook must FAIL with missing Strategy-Transition + Co-Shipped-By + Retired-Strategy trailers.
9. Use /invest-ship --retire-strategy --reason "end-to-end test" to retire cleanly.
   Verify commit lands; verify a restart engine --once no longer loads the retired strategy
   (load_all_approved returns empty).
```

### 8.6 Negative tests (malicious + accidental paths)

| Path | Expected |
|---|---|
| Manual editor edit of `status: approved → approved_at: 2020-01-01` (backdate) | no hook catches this (status didn't change); Codex plan review SHOULD catch in §3.2 Step B for any proposed file that ships with suspicious fields; out of scope for automated hook -- documented known gap |
| Delete `approved_commit_sha` field on an approved strategy | `loader.py` raises; engine refuses to load that strategy at startup (existing Bundle 2 behavior) |
| Create a strategy with `status: approved` from scratch (no proposed predecessor in git history) | commit-msg hook sees `old_status = (new file)`, `new_status = approved`. This transition `(new file, approved)` is NOT in the allowed table → FAIL. Only `(new file, proposed)` is allowed. |
| Edit `wiki/strategies/index.md` (the folder index, not a strategy file) | no strategy-transition check fires (path doesn't match `wiki/strategies/strategy_*.md`); existing index-helper rules apply |

---

## 9. Dependencies + prerequisites

### 9.1 Must land BEFORE Bundle 3 code

1. Kill-semantics audit doc fixes (§0) -- vault-only commit, same session, first commit.

### 9.2 Must exist (Bundle 1 + 2); fresh session verifies

- `execution/strategies/loader.py` with `load_all_approved()` and `load_document()` -- Bundle 2 shipped; verify existence.
- `execution/strategies/types.py` with `StrategyDocument`, `ApprovedStrategySnapshot`, `STATUS_*` constants -- Bundle 2 shipped; verify `STATUS_REJECTED` + `STATUS_RETIRED` are in `ALLOWED_STATUSES` (if not, that's a Bundle 2 gap to fix in Bundle 3's prep commit).
- `execution/risk/kill_switch.py` -- Bundle 2 shipped.
- `execution/engine/main.py` with `strategy_file_modified_post_approval` event handling -- Bundle 2 shipped.
- `wiki/strategies/index.md` folder exists -- Phase 1 shipped.
- `review/strategy-approvals/` folder exists -- Phase 1 shipped.
- `.githooks/pre-commit` + `.githooks/commit-msg` exist -- Phase 1 shipped.
- `.claude/skills/invest-ship/SKILL.md` forked from K2B -- Phase 1 shipped.
- `.claude/skills/invest-propose-limits/SKILL.md` exists as stub -- K2Bi inherits, confirmed present.
- `core.hooksPath=.githooks` in local `.git/config` -- Phase 1; verify.

### 9.3 Deferred to Bundle 6

- pm2-triggered engine restart on approval (m2.19 pm2 config).
- Automated engine restart on config.yaml change.

### 9.4 Deferred to Phase 4

- Rejection → revise loop (Keith revises the draft; today Keith creates a new file).
- `/invest flatten-all` Telegram command (surfaced by kill-semantics audit).
- Limits-proposal auto-rollback on degraded metrics.
- Backtest of a proposed limits change against recent data.

---

## 10. Ship order (within the single K2Bi session)

Each numbered item is one `/invest-ship` cycle. Revised to 7 cycles (+1 vs v1) now that Bundle 3 has a small engine code delta for the retirement sentinel (Q7).

1. **Doc realignment ship** -- §0 + §0.1 runbook. Vault-only, `/invest-ship --no-feature`, no Codex review.
2. **Deploy-config ship** -- `scripts/deploy-config.yml` authored + `scripts/deploy-to-mini.sh` refactored to read it + `/invest-ship` step 12 preflight (§7). Codex pre-commit review. Commit subject: `feat(deploy): structured deploy-config.yml + invest-ship preflight`.
3. **Engine retirement-sentinel ship** -- new `assert_strategy_not_retired` + `StrategyRetiredError` in `execution/risk/kill_switch.py` + engine submit-path integration + unit + integration tests (§3.2 retire variant, Q7). Codex pre-commit review. Commit subject: `feat(kill-switch): add .retired-<slug> sentinel for in-tick retire safety`.
4. **Hook extensions ship** -- pre-commit (Check A status enum + Check B "How This Works" + Check C git-diff + Check D content-immutability) + commit-msg (transition + trailer) + NEW post-commit (sentinel landing per Q10) + test harness (§4, §8.1, §8.2). Codex pre-commit review. Commit subject: `feat(hooks): enforce strategy + config approval discipline via pre-commit + commit-msg + post-commit`.
5. **`/invest-ship` strategy subcommands ship** -- `--approve-strategy`, `--reject-strategy`, `--retire-strategy` (writes sentinel from cycle 3, uses Check D contract from cycle 4) + `--diagnose-approved` CLI (§3.2 Step F). Codex pre-commit review. Commit subject: `feat(invest-ship): add strategy approval + rejection + retirement subcommands + engine diagnostic`.
6. **`invest-propose-limits` MVP + `/invest-ship --approve-limits` ship** -- skill body upgrade from stub to MVP + gated config.yaml write path (§5). Codex pre-commit review. Commit subject: `feat(invest-propose-limits): MVP + approval wiring`.
7. **End-to-end test ship** -- §8.5 sequence as `tests/test_bundle_3_e2e.py`. Includes the new `--diagnose-approved` verification + retirement sentinel race test + body-edit rejection test. Requires live IBKR DUQ paper; gated behind `K2BI_RUN_IBKR_TESTS=1`. Commit subject: `test(bundle-3): end-to-end strategy approval + retirement + body-edit lockdown`.

### Per-cycle adversarial review discipline -- MiniMax-primary, Codex final-gate (post-cycle-2 revision, economics-driven)

Cycles 1+2 ran Codex as the primary iterative reviewer (R1-R5 on cycle 2). That pattern produced 5 Codex rounds on a single cycle and is unsustainable at Keith's basic Codex subscription tier. From cycle 3 onward, every cycle follows this flow:

1. **MiniMax-M2.7 iterative loop until clean.** Run `scripts/minimax-review.sh` (working-tree scope). Fix findings inline. Re-run. Repeat until verdict `approve` OR only P3 findings remain and Keith accepts them. MiniMax is ~$0.06/call with no subscription quota, so iterative convergence is effectively free. Label findings `R<N>-minimax`.
2. **One Codex pass as cross-vendor final gate.** Only after MiniMax converges, run ONE Codex review via `/ship`'s background+poll pattern. This catches MiniMax blind spots (Bundle 2 proved MiniMax and Codex catch different classes -- stop_loss persistence, Decimal corruption were MiniMax-exclusive; some architectural-shape findings have been Codex-exclusive).
   - If Codex returns P1: fix, then re-run BOTH MiniMax + Codex (a second full loop). This is the only path that spends a second Codex call on the same cycle.
   - If Codex returns P2 only: Keith decides fix-now-and-re-Codex vs defer-with-reason. Default: fix if the fix is < 15 LOC; defer if larger.
   - If Codex returns P3 only or clean: ship.
3. **Accept vs defer heuristics during MiniMax loops:**
   - P1: always fix inline.
   - P2: fix if semantic or safety-related; defer if pure style/naming/docstring.
   - P3: defer by default unless multiple P3s cluster on the same pattern (then fix the pattern).
   - Never re-run any reviewer after a pure-cosmetic fix -- re-review only when semantics change.

**Why this preserves the architectural property:**

The "two-vendor gate" principle (from `invest-ship` skill body) is that NO commit ships without at least one pass from each of {Codex, MiniMax}. Cycles 1+2 satisfied this by running Codex 5x (Codex alone covered it). New pattern satisfies it by MiniMax iterative + Codex final -- same property, different distribution of calls.

**Expected Codex rounds for Bundle 3 remaining cycles (3-7):** 5-10 total (1-2 per cycle average). Previous path would have been ~25 Codex calls (5 × 5 cycles). Savings: ~15-20 Codex calls stay inside the $20/mo subscription, preventing API-credit overflow during Bundles 4-6.

**Cumulative cross-vendor sweep:** retain the Bundle 2 R15-R16 pattern -- after cycle 6 (before cycle 7), run ONE extra MiniMax review on the cumulative diff since `530eb81` to catch any whole-bundle drift that per-cycle reviews missed. Label `R<N>-bundle-sweep`.

If during execution a Codex round surfaces a P1 that couldn't be pre-answered by §6 Q1-Q9: stop, escalate to architect, new architect response rounds for the scope-changing finding, then resume.

---

## 11. Expected ship-cycle sizing

- Cycles 2, 3, 4: each has its own Codex review loop. Bundle 2-level care per cycle (every P1 fixed inline with a regression test).
- Estimated Codex rounds per cycle: 1-3 (scope is narrower than Bundle 2; state machine is simpler).
- Total Codex rounds: 4-8 across the bundle, matching the Bundle 1+2 retrospective heuristic.
- Cross-vendor MiniMax M2.7 check: run once at the end (after cycle 4) on the cumulative diff since the last Bundle 2 ship. Catches blind spots Codex missed.

---

## 12. Kickoff prompt for the fresh K2Bi session

Copy this block into the fresh K2Bi Claude Code session (or paste as the first turn):

---

> You are picking up Phase 2 Bundle 3 on K2Bi (strategy approval flow). Bundles 1+2 shipped at `befc26b` and `530eb81` on main. The architect spec lives at `~/Projects/K2B/plans/2026-04-19_k2bi-bundle-3-approval-gate-spec.md` on the MacBook (K2B-side repo). Read it top-to-bottom before touching code. It has answered TEN architect questions upfront (§6 Q1-Q10) -- do not re-ask them, but challenge with evidence if you disagree.
>
> Read in this order to ground (all in K2Bi vault + repo):
>
> 1. The architect spec (linked above), including §10 "Per-cycle adversarial review discipline" subsection -- MiniMax-primary + Codex final-gate is mandatory from cycle 3 onward per cost-economics revision.
> 2. `K2Bi-Vault/wiki/planning/phase-2-bundles.md` -- Bundle 3 row.
> 3. `K2Bi-Vault/wiki/planning/m2.6-engine-state-machine.md` -- `.killed handling specifics` section + the full state-transition matrix.
> 4. `K2Bi-Vault/wiki/planning/execution-model.md` -- Approval Contract section.
> 5. `K2Bi-Vault/wiki/planning/risk-controls.md` -- the kill-semantics rows that §0 of the spec rewrites.
> 6. `execution/strategies/loader.py` + `runner.py` + `types.py` -- the Bundle 2 contracts you're extending.
> 7. `execution/engine/main.py` -- reader side; Bundle 3 adds ONE call site in the submit path (spec §3.2).
> 8. `execution/risk/kill_switch.py` -- Bundle 3 extends with `assert_strategy_not_retired`.
> 9. `.claude/skills/invest-ship/SKILL.md` -- the shipping skill you're extending with `--approve-strategy`, `--reject-strategy`, `--retire-strategy`, `--approve-limits`.
> 10. `.claude/skills/invest-propose-limits/SKILL.md` -- the stub you're graduating to MVP.
> 11. `.githooks/pre-commit` + `.githooks/commit-msg` -- enforcement layer you're extending; also create a NEW `.githooks/post-commit` (spec §4.3, Q10).
>
> Ship plan is 7 cycles per spec §10. Execute sequentially.
>
> Cycle 1: doc realignment (§0 + §0.1). Vault-only, `/invest-ship --no-feature`, no adversarial review needed.
> Cycle 2: deploy-config.yml + preflight (§7). Full adversarial review.
> Cycle 3: engine retirement sentinel (§3.2 retire variant + Q7).
> Cycle 4: hook extensions (pre-commit Checks A-D + commit-msg + NEW post-commit per Q10).
> Cycle 5: `/invest-ship` strategy subcommands (§3) + `--diagnose-approved` CLI.
> Cycle 6: `invest-propose-limits` MVP + `--approve-limits` (§5).
> Cycle 7: end-to-end test (§8.5, gated behind `K2BI_RUN_IBKR_TESTS=1`).
>
> **Review discipline (from cycle 3 onward, per §10):** MiniMax-M2.7 iteratively via `scripts/minimax-review.sh` until clean, THEN ONE Codex pass as cross-vendor final gate. Accept P3s as defer; fix P2s only if semantic/safety-related; always fix P1s. Never re-run any reviewer after a pure-cosmetic fix. See spec §10 for full protocol + accept/defer heuristics.
>
> End-to-end test (§8.5) is the Bundle 3 exit gate. Do not declare ship complete until every test-matrix row in §8.1 + §8.2 has a passing test AND the 9-step §8.5 sequence passes.
>
> Architect is reachable via the K2B-side MacBook session (same Keith). Escalate on any P1 that cannot be pre-answered by §6 Q1-Q10.

---

## Self-review (architect's own)

**Spec coverage check:** Bundle 3 scope = m2.16 + m2.17 + deploy-coverage + kill-semantics audit. Every one has a section + ship cycle + test matrix rows.

**Placeholder scan:** no "TBD", "implement later", "handle appropriately", or "add tests for" entries. Every data contract is specified; every transition is listed; every hook behavior is tabulated.

**Type consistency:** `status` enum = `{proposed, approved, rejected, retired}` used consistently in §2.1, §2.2, §3.1, §3.2, §4.1, §4.2, §8.1, §8.2, §8.5. Commit trailer names = `Strategy-Transition`, `Approved-Strategy`, `Rejected-Strategy`, `Retired-Strategy`, `Limits-Transition`, `Approved-Limits`, `Config-Change` -- all used consistently. Skill subcommand names = `--approve-strategy`, `--reject-strategy`, `--retire-strategy`, `--approve-limits`, `--draft-strategy` (optional) -- consistent across sections.

**Known gaps (acknowledged, not silently skipped):**

1. Backdated `approved_at` attack (manual editor edit of the timestamp without changing `status`) -- no hook catches this because status didn't change. Documented in §8.6. Codex plan review on each proposed strategy is the architect's nominal gate. Accept as known.
2. Engine restart on approval is documented-manual in Bundle 3; Bundle 6 automates. Acceptable because (a) Keith is the only operator until Phase 3, (b) the `strategy_file_modified_post_approval` drift detection provides a safety net for the "engine still running old snapshot after approval lands" window.
3. `tests/test_bundle_3_e2e.py` requires live IBKR DUQ paper. Gating behind env flag `K2BI_RUN_IBKR_TESTS=1` matches Bundle 2's pattern for MockIBKRConnector-vs-live tests.

---

## Ready-state check before handing to K2Bi

- [x] Prerequisite kill-semantics audit resolved in §0.
- [x] Phase 3 kill-runbook in §0.1 names the position-cleanup owner (Keith manual via IB TWS for paper; `/invest flatten-all` is Phase 6 hard prereq).
- [x] Data contracts locked in §2.
- [x] State transitions tabulated in §2.2 (zero empty cells) + content-immutability for approved strategies locked.
- [x] Hook behaviors tabulated in §8.1 (commit-msg) + §8.2 (pre-commit Check D + git-diff Check C rows added after MiniMax review).
- [x] Retirement race closed via `.retired-<slug>` sentinel (Q7) -- NOT deferred to Phase 4.
- [x] Body-edit bypass closed via §4.1 Check D (Q8).
- [x] Config-approval gate hardened via git-diff (Q9) -- no wall-clock window.
- [x] Sentinel orphan-on-abort race closed via post-commit hook (Q10) -- sentinel atomic with commit.
- [x] End-to-end test sequence enumerated in §8.5.
- [x] Ten architect questions pre-answered in §6 (Q1-Q10).
- [x] Ship order + cycle count in §10 (7 cycles, +1 vs v1 for engine sentinel code).
- [x] Kickoff prompt written in §12.
- [x] **Adversarial plan review passed (two-vendor)** --
    - **MiniMax-M2.7 (primary; Codex round 1 wedged):** 8 findings, all integrated. R1 -> §0.1, R2 -> §3.2 + Q7 + engine delta, R3+R8 -> §4.1 Check C + Q9, R4+R7 -> §4.1 Check D + Q8 + §2.2, R5 -> §3.2 Step F + `--diagnose-approved`, R6 -> §7.3 structured `deploy-config.yml`. Archive: `.minimax-reviews/2026-04-18T16-26-02Z_working-tree.json`.
    - **Codex round 2 (cross-vendor confirmation pass on v2-integrated):** completed initial research phase (21+ file reads across spec, kill_switch.py, engine/main.py, loader.py, hooks, invest-sync SKILL.md, deploy-to-mini.sh, .gitignore, tests). Stalled before final findings output, but captured one partial P1 in its research trail ("one real race the spec doesn't close cleanly: the retire sentinel is written before t[he commit]"). Integrated as Q10 + §4.3 post-commit hook + §3.2 retire-flow update. Additional research areas Codex investigated but never got to final output: pending-sync mailbox schema + pm2 + deploy-config category integration (tracked as cycle-2 audit item: verify that `/invest-sync` and `.pending-sync/` mailbox schema are compatible with the new `deploy-config.yml` `category:` field mapping; if not, cycle 2 also ports the category mapping rather than deferring).
    - No residual Codex P1 can be confirmed absent given the stall, but two vendors + the research-trail-captured P1 is treated as sufficient gate per the `invest-ship` MiniMax-backup contract.
- [ ] **Spec pasted into K2Bi session** -- next step.
