# K2Bi DEVLOG

Session-by-session ship log. Append-only. New entries on top.

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
