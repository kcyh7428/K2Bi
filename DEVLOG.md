# K2B-Investment DEVLOG

Session-by-session ship log. Append-only. New entries on top.

---

## 2026-04-18 -- Phase 1 Session 2: Skill Ports + Helpers + Hooks

**Scope:** Port 7 skills from K2B with prompt-domain swaps; port the wiki/log.md single-writer helper + atomic 4-index helper; add pre-commit + commit-msg hooks; wire up `core.hooksPath`. Full /ship end-to-end smoke test deferred to Session 3.

**Skills ported (under `.claude/skills/`):**

- `invest-compile` (was k2b-compile) -- with `eval/eval.json` (3 tests) + inherited `eval/learnings.md`. MiniMax compile worker `~/Projects/K2B-Investment/scripts/minimax-compile.sh` marked TODO Phase 2.
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

- `wiki-log-append.sh` PASSED (wrote a real test entry to `K2B-Investment-Vault/wiki/log.md`, then proceeded past it -- the entry remains in the log as the appended audit trail of the smoke).
- `compile-index-update.py` arg-validation PASSED (exit 1 on missing args, expected behavior).
- All shell scripts pass `bash -n` syntax check; Python helper passes `ast.parse`.
- Pre-commit hook PASSED a live block test: created a file containing `echo "..." >> wiki/log.md`, attempted commit, hook printed the offending lines and exited 1 as designed. (Earlier failed test where the hook seemed not to fire turned out to be `git stash --include-untracked` swallowing the `.githooks/` dir from the working tree -- recovered via `git reset --hard d30e203 && git stash pop`, re-tested, hook now correct.)

**Subagent dispatch pattern (Keith asked about this explicitly):** all 7 skill ports ran as parallel `general-purpose` subagents in background mode, fed a precise port spec at `/tmp/k2b-investment-port-spec.md` (created in main session, then referenced by every subagent). Each subagent only used Read/Write/Edit/Grep -- no external CLI calls -- which avoided the codex:rescue silent-stall pattern from 2026-04-17. All 7 returned cleanly within ~3 minutes. Helper scripts + hooks were written in main session in parallel while subagents ran. Three shallow swaps caught in the review pass and fixed in main session: position wikilinks (`[[position_<symbol>...]]` → `[[<SYMBOL>_YYYY-MM-DD]]`), compile-index lock path mismatch between SKILL.md and the actual script, and learnings.md headers carrying source skill names.

**Phase 1 exit criteria status:** 7 of 8 met. Only #8 (full `/ship` end-to-end smoke test) remains and is the lead item for Session 3.

**Resume handle:** Keith says "continue k2b investment" -> CLAUDE.md routes to `K2B-Vault/wiki/projects/k2b-investment/index.md` Resume Card -> next action is "Phase 1 Session 3: full /ship smoke test + first /autoresearch loop + start Phase 2 MiniMax helper ports".

**Next action:** Phase 1 Session 3 (when Keith picks it up).

---

## 2026-04-17 -- Phase 1 Session 1: Repo + Vault Scaffold

**Scope (per Keith decision 2026-04-17):** Scaffold only. Dirs + CLAUDE.md + indexes. Skill ports + eval + `/ship` smoke test deferred to Phase 1 Session 2. Mac Mini sync OFF until Phase 4.

**Created:**

- `~/Projects/K2B-Investment/` git repo skeleton (.git initialized, empty `.claude/skills/`, `scripts/`, `.pending-sync/`)
- `~/Projects/K2B-Investment/CLAUDE.md` written from scratch -- ownership-matrix-compliant, identity + taxonomy + soft rules only, no procedural duplication
- `~/Projects/K2B-Investment/.gitignore` -- excludes secrets, `.env*`, `.killed` lock, `__pycache__`, `.pending-sync/` contents
- `~/Projects/K2B-Investment/DEVLOG.md` (this file)
- `~/Projects/K2B-Investment-Vault/` plain Syncthing-managed directory (NOT a git repo) with full skeleton:
  - `raw/` with subfolders: news, filings, analysis, earnings, macro, youtube, research
  - `wiki/` with subfolders: tickers, sectors, macro-themes, strategies, positions, watchlist, playbooks, regimes, reference, insights, context
  - `review/` with subfolders: trade-ideas, strategy-approvals, alerts, contradictions
  - `Daily/`, `Archive/`, `Assets/{images,audio,video}/`, `System/`, `Templates/`
- `wiki/index.md` master catalog (LLM reads first on every query)
- `wiki/log.md` append-only spine (single-writer rule documented; helper script ports in Session 2)
- Per-folder `index.md` in every `wiki/`, `raw/`, and `review/` subfolder
- `Home.md` vault landing page
- Memory symlink: `~/.claude/projects/-Users-keithmbpm2-Projects-K2B-Investment/memory/` -> `K2B-Investment-Vault/System/memory/`

**Deliberately NOT done this session (deferred):**

- Skill ports (7 invest-* skills): invest-compile, invest-lint, invest-weave, invest-observer, invest-autoresearch, invest-journal, invest-session-wrapup
- Skill eval harness runs
- `/ship` smoke test from new repo
- Syncthing config to Mac Mini
- Pre-commit hook (Tier 1 K2B fix #8 -- needs the helper scripts that ship in Session 2)
- Single-writer log helper script

**Phase 1 exit criteria status:** 4 of 8 met (1, 2, 3, 7). Remaining (4, 5, 6, 8) require skill ports + `/ship` test + memory symlink validation.

**Resume handle:** Keith says "continue k2b investment" in any new session -> CLAUDE.md routes to `K2B-Vault/wiki/projects/k2b-investment/index.md` Resume Card -> next action is now "Phase 1 Session 2: port 7 skills + run eval harness + `/ship` smoke test".

**Next action:** Phase 1 Session 2 (when Keith picks it up). Port skills, run evals, ship.
