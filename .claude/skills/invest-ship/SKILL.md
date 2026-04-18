---
name: invest-ship
description: End-of-session shipping workflow -- runs Codex pre-commit review, commits, pushes, updates the feature note, updates wiki/concepts/index.md lane membership, appends DEVLOG.md and wiki/log.md, suggests next Backlog promotion, and reminds Keith to /sync. Use when Keith says /ship, "ship it", "wrap up", "end of session", "done shipping", or at the natural end of a build session where code was modified.
---

# K2B Ship

Keystone skill for shipping discipline. Replaces the manual Session Discipline checklist with an enforceable workflow that keeps `wiki/concepts/index.md` (the canonical roadmap) honest.

## When to Trigger

**Explicit:** Keith says `/ship`, "ship it", "ship this", "wrap up", "end of session", "done shipping", "close out", "commit and push this".

**Proactive prompt:** At the natural end of any session where K2B modified code in `.claude/skills/`, `CLAUDE.md`, `K2B_ARCHITECTURE.md`, `k2b-remote/`, `scripts/`, `k2b-dashboard/`, or a feature note moved into `in-progress` or `shipped` state -- say: "We have uncommitted changes in [list]. Want me to /ship?"

**Do NOT auto-ship.** Always confirm the commit message and the Codex findings before committing.

## When NOT to Use

- Vault-only changes (daily notes, review processing, content drafts) -- these sync via Syncthing, no commit needed
- Emergency hotfixes where Keith explicitly says "just commit, skip review"
- When the user is mid-implementation and just wants an interim checkpoint -- they should say `/commit` or commit manually

## Commands

- `/ship` -- full workflow with Codex review + feature note updates + roadmap updates
- `/ship --skip-codex <reason>` -- skip Codex review with a recorded reason (must provide reason)
- `/ship --no-feature` -- ship code without touching feature notes or the roadmap (e.g. typo fix, config tweak)
- `/ship status` -- show what would ship without actually shipping

## Workflow

### 0. Active rules auto-promotion scan

Before anything else, scan for learnings that have crossed the promotion threshold (reinforced 3x) and surface them to Keith for inline y/n/skip confirmation. This step runs on every `/ship` call, including `--no-feature` and `--defer` variants. It is read-only until Keith answers `y`.

Run (skip gracefully if the script is absent -- sibling repos like K2Bi do not carry the active-rules pipeline yet, so absence is normal, not an error):

```bash
if [ -x scripts/promote-learnings.py ]; then
  scripts/promote-learnings.py
else
  echo "auto-promote: skipped (no scripts/promote-learnings.py in $(pwd))"
fi
```

If the script is absent, skip the rest of step 0 entirely (no candidate surfacing, no wiki-log-append at end of section) and proceed to step 0a. When the script IS present, the scanner prints a JSON array of candidate learnings. Each candidate has: `learn_id`, `count`, `distilled_rule`, `area`, `source_excerpt`, `would_exceed_cap`, `current_active_count`, `cap`. If the array is empty, print `auto-promote: 0 candidates` and continue to step 0a.

For each candidate, surface Keith inline:

```
L-<id> has been reinforced <count>x and is not in active_rules.
Distilled: "<distilled_rule>"
Promote now? [y/n/skip]
```

If `distilled_rule` is `null` (no frontmatter line, no bolded first sentence in the body), print the full `source_excerpt` first and ask Keith to supply the rule text inline before promoting. Save his answer as the rule text for the append step.

Act on Keith's answer:

- **y**: Append a new numbered rule to `active_rules.md` using the distilled rule text. Section placement is by topical fit (Identity, Vault, Deployment, Karpathy); if unsure, drop it in the section the source learning's `Area:` field maps to. Include `(<L-id>, last-reinforced: <today>)` in the parenthetical per the Fix #5 format.
  - **Before** appending, if `would_exceed_cap` is `true` OR the post-append rule count would exceed `cap`, resolve the LRU victim:
    ```bash
    scripts/select-lru-victim.py
    ```
    The helper reads `active_rules.md`, parses `last-reinforced:` and reinforcement count, and prints the oldest rule as JSON (`{"rule_number": N, "title": "...", "learn_id": "...", "last_reinforced": "..."}`). Surface the demotion to Keith as `[warn] demoting rule <N> (<title>) to make room for <new rule>` and wait for his confirmation. On `y`, call:
    ```bash
    scripts/demote-rule.sh <N>
    ```
    which moves the rule block intact into `self_improve_learnings.md`'s `## Demoted Rules` section, renumbers the remaining rules contiguously, and logs via the Fix #1 helper. Only after the demotion returns success do you append the new rule.
- **n**: Append `auto-promote-rejected: true` to the learning's entry body in `self_improve_learnings.md` (as a bullet: `- **auto-promote-rejected:** true`) so the scanner skips it on future `/ship` runs. Do not modify the count.
- **skip**: Do nothing. The candidate will re-appear on the next `/ship`.

After all candidates are processed, log the net change via the Fix #1 helper:

```bash
scripts/wiki-log-append.sh /ship "step-0" "promoted=<N> rejected=<M> skipped=<K> demoted=<D>"
```

Then continue to step 0a.

### 0a. Ownership drift check (advisory)

Run (skip gracefully if absent -- sibling repos like K2Bi may not carry this script):

```bash
if [ -x scripts/audit-ownership.sh ]; then
  scripts/audit-ownership.sh || true
else
  echo "ownership-drift: skipped (no scripts/audit-ownership.sh in $(pwd))"
fi
```

When the script IS present, it exits non-zero when it finds known rule phrases outside their canonical home (see `scripts/ownership-watchlist.yml`). This step is **advisory**. Drift does not block `/ship`. Surface the offenders to Keith inline:

```
[warn] ownership drift: rule=<id> phrase=<phrase>
  offender: <path>
```

Keith decides fix-inline or defer. When he defers, append the drift summary to the ship commit body under a "Deferred:" trailer so the next session sees it.

### 1. Scope detection

Run in parallel:

```bash
git status
git diff --stat
git log -5 --oneline
```

Categorize touched files into:

| Category | Matching paths | Needs /sync? |
|----------|---------------|--------------|
| skills    | `.claude/skills/`, `.claude/settings.json`, `CLAUDE.md`, `README.md`, `DEVLOG.md` | yes |
| execution | `execution/`, `requirements.txt` | yes (Python deps + engine restart on Mini) |
| scripts   | `scripts/` including `scripts/hooks/` and `scripts/lib/` | yes |
| pm2       | `pm2/` (Phase 6+ populates this) | yes (pm2 restart on Mini) |
| vault     | `K2Bi-Vault/` | no (Syncthing) |
| plans     | `plans/`, `.claude/plans/` | no |
| proposals | `proposals/` | no |
| tests     | `tests/` | no (tests run locally; Mini runs the engine) |

**The four sync categories (`skills`, `execution`, `scripts`, `pm2`) are the single source of truth anchored in `scripts/deploy-config.yml`.** Run `python3 scripts/lib/deploy_config.py list-categories` to see the live list. Any category label that `/ship --defer` writes into a mailbox entry must be one of those four -- otherwise `/sync` would consume the entry without a deploy target, silently dropping the change. To classify a specific file, pipe it through `python3 scripts/lib/deploy_config.py classify <path>`. In particular, `scripts/hooks/**` rolls up into `scripts` (not a separate `hooks` category): the deploy script's `scripts` mode already rsyncs `scripts/` recursively, which covers hooks.

If there are NO changes at all, report "No changes to ship" and stop.

### 2. Identify the feature being shipped

Read `K2Bi-Vault/wiki/concepts/index.md`, find the **In Progress** lane.

- If exactly one feature is In Progress -> that is the candidate feature
- If zero features are In Progress -> ask Keith whether this ships under an existing Backlog feature (and if so, which), or is infrastructure work with no feature attached (`--no-feature`)
- If multiple features are In Progress (shouldn't happen per lane rules) -> ask Keith to disambiguate

For multi-ship features (e.g. `feature_mission-control-v3`), read the feature note's Shipping Status table. Identify the current ship row (`in-flight` / `in progress`). Ask Keith to confirm which ship this commit completes.

### 3. Codex pre-commit review gate

**Mandatory unless `--skip-codex <reason>` is passed.** This is **Checkpoint 2** of the two K2B adversarial review checkpoints. (Checkpoint 1 is **plan review** -- see below -- and runs earlier, before implementation. `/ship` only owns Checkpoint 2.)

Run Codex review on the uncommitted diff using the **background + poll** pattern, not a synchronous foreground slash invocation:

```bash
CODEX_PLUGIN="$HOME/.claude/plugins/marketplaces/openai-codex/plugins/codex"
if [ ! -f "$CODEX_PLUGIN/scripts/codex-companion.mjs" ]; then
  echo "Codex plugin not found at $CODEX_PLUGIN. Run /codex:setup or re-run with /ship --skip-codex <reason>." >&2
  exit 2
fi

# Launch via Bash tool with run_in_background: true. This gives the assistant
# a task_id + output file to tail, and prevents the review from blocking the
# session turn when Codex's backend does a cold-start reconnect storm
# (observed: 5 silent reconnect attempts ~= 10+ minutes before any output).
CLAUDE_PLUGIN_ROOT="$CODEX_PLUGIN" \
  node "$CODEX_PLUGIN/scripts/codex-companion.mjs" \
  review --wait --scope working-tree 2>&1
```

**Polling protocol while the background task runs:**

1. Read the task's output file every ~90 seconds (first poll at 90s is enough to catch fast completions, subsequent polls every 90-120s until `[codex] Turn completed.` or a terminal `# Codex Review` section appears).
2. Surface progress to Keith at each poll: file count read, reconnect status, current Codex action (`grep`, `sed`, `nl`). "Codex actively reading N files, not hung" beats silent spinner.
3. If the output shows 5 reconnect attempts with no recovery for >= 3 minutes after the last reconnect line, assume Codex CLI is wedged: stop the background task (`TaskStop`), report to Keith, and offer `--skip-codex codex-cli-wedged` as the recorded reason.
4. When the review finishes, parse the output verbatim and present findings to Keith. Do NOT paraphrase, rank, or pre-filter. Codex's own prioritization (P0/P1/P2/P3) stays intact.

Why this pattern instead of a foreground `/codex:review` slash call: the slash wrapper runs the companion script synchronously via Bash and echoes stdout when it finally returns. On a Codex cold-start the companion script silently retries its WebSocket connection up to 5 times before surfacing anything, during which the session appears hung with no visible progress. The background + poll pattern eliminates that failure mode -- the output file is always tailable, so "still working" is observable, not guessed.

- Present findings neutrally to Keith. Do not argue with Codex. Let Keith decide.
- Keith decides: fix now, defer, or accept. If he fixes, re-run the same background + poll pattern on the new diff.
- Log the gate result: `reviewed / skipped:<reason>`, number of findings, fix verdict.

If `codex-companion.mjs` is missing entirely (plugin not installed): fail loudly with "Run `/codex:setup` or re-run with `/ship --skip-codex <reason>`."

**Fallback: MiniMax M2.7 when Codex is unavailable**

If Codex cannot run (quota reset, CLI wedged, plugin missing, network hard-fail, auth error), the approved backup is `scripts/minimax-review.sh`. Same adversarial-review prompt (vendored locally at `scripts/lib/adversarial-review.md`), different vendor (MiniMax M2.7 on `api.minimaxi.com`), single-shot, ~90s per call, ~$0.06 per call, no subscription quota. Cross-vendor adversarial property preserved.

```bash
./scripts/minimax-review.sh                                  # working-tree scope, markdown output (default)
./scripts/minimax-review.sh --json > /tmp/review.json        # machine-parseable JSON
./scripts/minimax-review.sh --focus "<specific concern>"     # add focus area to the prompt
./scripts/minimax-review.sh --model MiniMax-M2.5             # fall further if M2.7 also misbehaves
```

**Procedure when using the fallback:**

1. Run `./scripts/minimax-review.sh` (or `--json` if you want to grep findings programmatically).
2. Present findings neutrally to Keith. Severity translation: `critical` ≈ P0, `high` ≈ P1, `medium` ≈ P2, `low` ≈ P3 -- apply the same architect stop-rule (e.g. "ship when P1=0 + P2 isolated") against MiniMax severities using this mapping.
3. Keith decides fix now / defer / accept. If fixed, re-run the tool on the new diff. Re-runs label the round `R<N>-minimax` (not just `R<N>`) so the audit trail is honest about the reviewer switch.
4. The `/ship` gate is **still bypassed via `--skip-codex <reason>`** -- this skill's behavior does not change. Use one of these reason strings so the audit trail reflects whether MiniMax ran:
   - `codex-unavailable-minimax-verified` -- MiniMax ran clean (verdict `approve`, or residual findings accepted/deferred by Keith)
   - `codex-quota-exhausted-minimax-verified` -- more specific sub-case
   - `codex-unavailable` -- MiniMax was NOT run (escape hatch only; requires Keith's explicit override; strictly worse audit trail)
5. Reference the archive file path (e.g. `.minimax-reviews/2026-04-18T13-40-19Z_working-tree.json`) in the commit footer or feature note so the gate path is reproducible later. Archives include the full prompt, raw response, parsed JSON, and token usage.

**Does NOT cover:**

- **Plan review (Checkpoint 1).** `scripts/minimax-review.sh` is pre-commit (working-tree) only. If Checkpoint 1 also needs running AND Codex is unavailable, defer the ship until Codex is back rather than shipping unreviewed at the plan level.
- **Automatic `/ship` integration.** `/ship` does not detect Codex failure and auto-fallback. The assistant runs `minimax-review.sh` manually when Codex errors, inspects the output, then proceeds with `/ship --skip-codex <reason>`. Keeping the switch explicit preserves audit clarity.

**When to prefer waiting for Codex over running MiniMax:**

- Iterative review loops (R1, R2, R3, ...) that were already started on Codex. Switching vendors mid-loop invalidates the convergence signal (different model, different blind spots, different finding baseline). Finish the loop on Codex if you can wait; relabel as `R<N>-minimax` if you switch.
- Plan-review-sized work where Checkpoint 1 is the main gate and you don't have a Codex session on hand.

### Codex Adversarial Review -- the two checkpoints

K2B uses OpenAI Codex (via the `/codex:` plugin) as a second-model reviewer to catch blind spots Claude cannot see in its own work. Two mandatory checkpoints bracket any non-trivial build:

**Checkpoint 1: Plan Review.** Before implementing any new feature, skill, or significant refactor, after the plan is written but before code is touched:

- Run `/codex:adversarial-review challenge the plan` with the plan file path
- Look for: over-engineering, simpler alternatives, missing edge cases, unnecessary complexity
- Adjust the plan based on findings BEFORE writing code

This checkpoint lives outside `/ship` -- it is the author's responsibility at plan-time. `/ship` only sees the result (the already-reviewed plan, or its absence) via the diff it is about to commit.

**Checkpoint 2: Pre-Commit Review.** Before committing changes from a build session, `/ship` runs Codex review on the uncommitted diff via the background + poll pattern documented in step 3 above -- not via a synchronous `/codex:review` slash call, because that can silently hang the session on a Codex cold-start reconnect storm. Look for: bugs, logic errors, drift from the plan, edge cases. Fix issues before committing.

**When Codex review can be skipped:**

- Vault-only changes (daily notes, review processing, content drafts)
- Config tweaks, typo fixes, one-line changes
- Emergency hotfixes where the bug-fix speed matters more than review (review after the fact)

**Never skip both checkpoints.** If Checkpoint 1 was skipped because the feature was small enough that no plan was written, Checkpoint 2 becomes mandatory. Conversely, if Checkpoint 2 is skipped via `/ship --skip-codex <reason>`, Checkpoint 1 must have run earlier in the session -- otherwise the build has had no adversarial review at all, and `/ship` should refuse to proceed without Keith's explicit override.

**Rules for presenting Codex findings to Keith:**

- Report findings neutrally. Do not argue with Codex.
- Do not pre-filter findings by "importance" before Keith sees them.
- Let Keith decide which to fix, defer, or accept.

### 4. Generate commit message

Build a commit message from the categorized diff. Format:

```
<type>: <short summary>

<optional body with bullet points of major changes>

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
Co-Shipped-By: invest-ship
```

Types: `feat`, `fix`, `refactor`, `docs`, `chore`, `infra`. **Never use em dashes** (K2B rule).

Show Keith the draft. Confirm before committing.

### 5. Stage + commit + push

Stage every file this session touched, regardless of category. The category table in step 1 is for `/sync` routing decisions, not for gating staging -- a touched file in `docs/`, `allhands/`, or any other uncategorized path still gets staged if it belongs to this session. Files in the working tree that predate the session and were not touched in this session must NOT be staged.

```bash
# Stage only the files we know about -- no git add -A (active rule: sensitive file avoidance)
git add <each file this session touched, from step 1 git status>
git commit -m "$(cat <<'EOF'
<message from step 4>
EOF
)"

# Push only if an `origin` remote is configured. Sibling repos like K2Bi may
# not have one yet (Phase 1), which is expected, not an error. Match by exact
# name -- a remote called `upstream` would otherwise pass `git remote | grep -q .`
# and then fail at push time when we still target `origin`.
if git remote | grep -qx 'origin'; then
  git push origin main
else
  echo "push: skipped (no 'origin' remote configured in $(pwd))"
fi
```

Never pass `--no-verify`. Never pass `--amend` unless Keith explicitly asked. If pre-commit hooks fail, fix the underlying issue and create a NEW commit.

Capture the commit SHA.

### 6. Update the feature note

If `--no-feature` was passed, skip this step.

Read the feature note at `K2Bi-Vault/wiki/concepts/feature_<slug>.md`.

**Single-ship feature (no Shipping Status table):**
- Update frontmatter: `status: shipped`, add `shipped-date: YYYY-MM-DD`
- Append an `## Updates` section entry with: date, commit SHA, one-line what shipped, Codex findings summary, any follow-ups
- Move the file to `K2Bi-Vault/wiki/concepts/Shipped/feature_<slug>.md`

**Multi-ship feature (has Shipping Status table, e.g. mission-control-v3):**
- Do NOT set the top-level `status: shipped` -- only the current ship is done
- Update the Shipping Status table row for the current ship: mark `shipped: YYYY-MM-DD`, set `state: in-measurement` (or `state: gate-passed` if no measurement window), set gate date if applicable
- Append an `## Updates` entry with ship details, commit SHA, Codex findings
- If this was the final ship in the plan AND it has passed its gate, THEN set feature-level `status: shipped` and move to `Shipped/`. Otherwise leave in place.

### 7. Update `wiki/concepts/index.md`

Load the index, locate the feature's row, move it between lanes:

- **Single-ship feature shipped:** Remove from In Progress, add to Shipped with `shipped-date`. If Shipped now has more than 10 rows, move the oldest one's wiki-link target file into `Shipped/` (update its `up:` still points to `[[index]]`, but the wiki-link in the index now references `Shipped/feature_<slug>`).
- **Multi-ship feature, ship complete but feature not done:** Update In Progress row to show the new ship state (`Ship N (in measurement, gate YYYY-MM-DD)`). Do not move.
- **Multi-ship feature, final ship complete and gate passed:** Move to Shipped lane as above.

Also update `Last updated: YYYY-MM-DD` at top of index.

### 8. Append DEVLOG.md and create follow-up commit

`DEVLOG.md` is tracked in git at project root, so appending to it creates dirty state that must be committed. Because the entry needs to reference the code commit's SHA (captured in step 5), this is always a two-commit flow: code first, devlog second.

Read the last DEVLOG entry for style. Append a new entry:

```markdown
## YYYY-MM-DD -- <one-line title>

**Commit:** `<short-sha>` <commit message title>

**What shipped:** <one paragraph>

**Codex review:** <findings summary or "skipped: <reason>">

**Feature status change:** <feature slug> <status-from> -> <status-to>

**Follow-ups:** <bullets, or "none">

**Key decisions (if divergent from claude.ai project specs):** <bullets, or "none">
```

Then commit and push as a standalone devlog commit (matches the repo's existing pattern, e.g. `dc2ba69 docs: devlog for active rules staleness detection`):

```bash
git add DEVLOG.md
git commit -m "$(cat <<'EOF'
docs: devlog for <short-sha>

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
Co-Shipped-By: invest-ship
EOF
)"

# Same remote-guard as step 5 -- match by exact name `origin`, not "any remote".
if git remote | grep -qx 'origin'; then
  git push origin main
else
  echo "push: skipped (no 'origin' remote configured in $(pwd))"
fi
```

Never `--amend` the step-5 commit to include DEVLOG.md -- amends rewrite history and can drop signed state. Always create a new commit.

If shipping multiple logical changes in one session (two or more code commits back-to-back), batch all their DEVLOG entries into a single follow-up `docs: devlog` commit after the last code commit, referencing each code SHA in its own entry.

### 9. Append wiki/log.md

Call the single-writer helper (never append to wiki/log.md directly):

```bash
scripts/wiki-log-append.sh /ship "<feature-slug>" "shipped <feature-slug>: <one-line-summary>"
```

Replace `<feature-slug>` with the feature note basename (e.g. `feature_invest-ship`) and `<one-line-summary>` with the same text used in the commit message subject. Helper handles locking, timestamp, and format.

### 10. Multi-ship gate handling

If the feature has a Shipping Status table and this ship has a gate scheduled (per minimax-offload phase gate pattern):

- Remind Keith: "Ship X of Y done. Gate review scheduled for YYYY-MM-DD. Nothing else should start on Ship X+1 until the gate passes."
- Offer to create a scheduled task via `/schedule` if the gate review is not already scheduled: the task should run `/observe` and the phase gate checklist from the feature note, then Telegram Keith the go/no-go summary.

### 11. Promote next Backlog item to Next Up (only for single-ship ships or final-ship ships)

If the just-shipped feature was removed from In Progress (leaving In Progress empty):

- Read Next Up lane. Count items.
- If Next Up has fewer than 3 items, look at the top of Backlog (sorted by priority then effort).
- Suggest to Keith: "Backlog top candidate: `feature_X`. Promote to Next Up? [Y/n]"
- On Y: move the row from Backlog to Next Up in `wiki/concepts/index.md`, ask Keith for a "Why now" reason for the Next Up table.
- **Never auto-promote.** Always require explicit confirmation.

### 12. Deployment handoff -- explicit sync-now or defer

**Pre-check: does this repo even have a deploy target?** Before prompting sync-now / defer, verify the current repo has a deploy script:

```bash
if [ ! -x scripts/deploy-to-mini.sh ]; then
  echo "deploy-handoff: skipped (no scripts/deploy-to-mini.sh in $(pwd) -- repo has no Mac Mini deploy target yet)"
  # Skip the rest of step 12 entirely. Do not write to .pending-sync/.
fi
```

For K2B, the script exists -- flow continues as normal. For K2Bi (Phase 1 through Phase 3), no Mini provisioning exists yet, so the sync question is meaningless and the mailbox entry would be a dead letter. Once K2Bi gets its own `deploy-to-mini.sh` in Phase 4, this check starts passing and the rest of step 12 engages automatically.

**Deploy-coverage preflight (required when deploy script exists):** before any sync-now / defer prompt, run the deploy-config.yml drift check. A top-level path that is not covered by `targets:` AND not in `excludes:` is a silent-deploy bug waiting to happen (the path gets added locally, nobody updates the deploy script, the Mini goes out of sync undetected). The preflight blocks /ship entirely until Keith resolves the drift -- he cannot defer what cannot be routed.

```bash
if [ -x scripts/lib/deploy_config.py ]; then
  if ! python3 scripts/lib/deploy_config.py preflight; then
    echo ""
    echo "Fix the drift above before /ship continues. Either:"
    echo "  - add the path to scripts/deploy-config.yml under 'targets:' (with its category)"
    echo "  - add the path to scripts/deploy-config.yml under 'excludes:' (local-only)"
    exit 1
  fi
fi
```

The preflight runs even on `--no-feature` + `--defer` paths, because mailbox entries carry category labels derived from deploy-config.yml; drift breaks the mailbox schema too.

If the deploy script exists AND the preflight passes, continue:

If any files in categories `skills`, `execution`, `scripts`, or `pm2` (the live `scripts/deploy-config.yml` category set; run `python3 scripts/lib/deploy_config.py list-categories` to confirm) were in the commits, the Mac Mini is now out of date with the pushed code. (`scripts/hooks/**` rolls up into `scripts` -- do not write a separate `hooks` category into mailbox entries, `/sync` has no deploy target for it and would silently drop the change.) A soft reminder is not enough because it can be missed and leaves no recovery signal. Ask Keith an explicit question:

> Project files changed (list the categories + files). Run `/sync` now, or defer to a later session?
> - **now** -- invoke `/sync` in-line, confirm it completed, done
> - **defer** -- drop a new entry in the `.pending-sync/` mailbox so the next session (or the next `/sync`) catches up

**If Keith picks `now`:**
1. Invoke the `invest-sync` skill via the Skill tool (or run `"$(git rev-parse --show-toplevel)"/scripts/deploy-to-mini.sh auto` if skill invocation is unavailable in the current harness -- the path resolves to the current repo's deploy script, not hardcoded to K2B, so a sibling repo with its own `scripts/deploy-to-mini.sh` deploys its own tree).
2. Report what was synced.
3. **Do NOT touch the `.pending-sync/` mailbox.** `/sync` is the sole owner of the mailbox lifecycle. It consumes and deletes its own entries on success. Any cleanup `/ship` did after-the-fact would race with a concurrent `/ship --defer` in another session and could silently destroy a newer deferred entry. Leave the mailbox alone.

**If Keith picks `defer`:**

1. Write a **new unique entry** in the **current repo's** `.pending-sync/` mailbox directory -- that is, the `.pending-sync/` folder at the root of whichever git repo `/ship` is running from. For K2B sessions this resolves to `~/Projects/K2B/.pending-sync/`; for sibling repos like K2Bi it resolves to the sibling's own `.pending-sync/` (each repo has its own mailbox and its own `/sync` consumer). Each defer creates its own file -- we never rewrite an existing file -- so concurrent defers from other sessions cannot race. Write via temp-file + `os.replace()` so a crash mid-write cannot leave partial JSON that downstream readers would flag as UNREADABLE:

   ```bash
   python3 <<PYEOF
   import json, os, datetime, tempfile, uuid, subprocess
   # Derive mailbox dir from git repo root, NOT hardcoded to K2B
   repo_root = subprocess.check_output(
     ["git", "rev-parse", "--show-toplevel"], text=True
   ).strip()
   dir_ = os.path.join(repo_root, ".pending-sync")
   os.makedirs(dir_, exist_ok=True)

   now = datetime.datetime.now(datetime.timezone.utc)
   entry_id = f"{now.strftime('%Y%m%dT%H%M%S')}_<short-sha from step 5>_{uuid.uuid4().hex[:8]}"
   final_path = os.path.join(dir_, f"{entry_id}.json")

   payload = {
     "pending": True,
     "set_at": now.isoformat(),
     "set_by_commit": "<short-sha from step 5>",
     "categories": ["<list from above>"],
     "files": ["<list from step 1>"],
     "entry_id": entry_id,
   }

   # Atomic write: temp file in the SAME directory, then os.replace into final name.
   # Temp names start with '.tmp_' so mailbox readers know to ignore in-progress writes.
   fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=dir_)
   try:
       with os.fdopen(fd, "w") as f:
           json.dump(payload, f, indent=2)
           f.flush()
           os.fsync(f.fileno())
       os.replace(tmp, final_path)
   except Exception:
       try: os.unlink(tmp)
       except FileNotFoundError: pass
       raise
   PYEOF
   ```

   Required schema fields: `pending` (bool, must be `true` for an active entry), `set_at` (ISO-8601 UTC timestamp), `set_by_commit` (short SHA from step 5), `categories` (list of strings matching the category table), `files` (list of file paths relative to `~/Projects/K2B/`), and `entry_id` (matches the filename stem for traceability). `invest-sync`'s Step 0 validates these fields and fails loud if any are missing.

2. Tell Keith: "Deferred. Entry `<entry_id>` added to `.pending-sync/` mailbox. Next session's startup hook will surface pending mailbox entries, and any later `/sync` invocation will consume them before checking conversation context."

3. The mailbox directory is gitignored (`/.pending-sync/` in `.gitignore`), never propagates to the Mini, and survives session boundaries on the MacBook only. **Consuming and deleting mailbox entries is `/sync`'s exclusive responsibility**, and it only deletes the specific entries it actually processed -- a `/ship --defer` running concurrently writes to a different filename, so nothing can be clobbered.

**Race-safety invariant:** The mailbox is a multi-producer / single-consumer queue where each producer writes a unique filename. Producers (`/ship --defer`) never read or delete. The consumer (`/sync`) deletes only filenames it has observed and processed. No state is ever rewritten in place. This makes the lifecycle race-free on POSIX without locks.

**If no syncable files changed:** Skip the question entirely. Do not write a marker. Report "Nothing to sync -- all changes were vault/plan/devlog only."

Do NOT auto-sync without asking. Per Active Rule L-2026-03-29-002, never run manual rsync -- always go through the deploy script via `/sync` or `invest-sync`.

### 13. Usage logging

```bash
echo -e "$(date +%Y-%m-%d)\tinvest-ship\t$(echo $RANDOM | md5sum | head -c 8)\tshipped FEATURE_SLUG SHORT_SHA" >> ~/Projects/K2Bi-Vault/wiki/context/skill-usage-log.tsv
```

### 13.5. Session summary capture

Extract implicit behavioral signals from this session and write a compact summary to the vault. The observer picks these up asynchronously and feeds them into the preference pipeline. This step runs on ALL /ship variants (including `--no-feature` and `--skip-codex`).

**Signal extraction:** Scan the conversation for up to 10 signals across 5 types:
- **[interest]** -- topics Keith drilled into vs skipped
- **[anti-pref]** -- things Keith redirected or pushed back on
- **[decision]** -- choices made and the reasoning behind them
- **[priority]** -- what Keith focused on when time was limited
- **[connection]** -- links Keith made that K2B didn't anticipate

**Best-effort:** If the conversation is too short or heavily compacted, emit what's available. If no signals are found, log "session-capture: no signals detected, skipping" and move on. Do not write an empty file.

**Grounding rule:** Every signal must cite a specific moment from the conversation. Do not invent timings, counts, or motives not directly evidenced. "Keith spent time on X" requires X to be visible in the conversation. If unsure whether something happened, omit it.

**Write the summary** (atomic, via temp + rename):

```bash
SESSIONS_DIR="$HOME/Projects/K2Bi-Vault/raw/sessions"
mkdir -p "$SESSIONS_DIR"
FILENAME="$(date +%Y-%m-%d_%H%M%S)_session-summary.md"
TMPFILE="$SESSIONS_DIR/.tmp_${FILENAME}"
# Write frontmatter + body to TMPFILE, then:
mv "$TMPFILE" "$SESSIONS_DIR/$FILENAME"
```

**Frontmatter:**
```yaml
---
tags: [raw, session-summary]
date: YYYY-MM-DD
type: session-summary
origin: k2b-extract
commit: <short-sha from step 5>
feature: <feature-slug or "infrastructure">
up: "[[index]]"
---
```

**Body:** One bullet per signal, max 10 lines. Example:
```
- [interest] Keith spent 40 min on source-hash dedup design, skipped decay model
- [anti-pref] Keith rejected MVP-only approach, wanted full 4-phase implementation
- [decision] Write-through model chosen over rebuild-only after Codex flagged gap
- [priority] All 6 Codex findings fixed before commit, no deferral
- [connection] Canonical memory completes the observer->profile->k2b-remote chain
```

**First-run setup** (only if `raw/sessions/index.md` does not exist):
1. Create `raw/sessions/index.md` with standard raw subfolder index format
2. Add a sessions row to `raw/index.md` if not already listed

## Error Handling

- **Pre-commit hook fails** -> fix the underlying issue (per Active Rule 8, never `--no-verify`), re-stage, create a NEW commit (never `--amend`).
- **Push fails (not a force-push scenario)** -> investigate. Fetch, check if the branch diverged, ask Keith how to reconcile.
- **Codex plugin missing** -> loud failure with next-step instruction; do not silently skip.
- **Feature note not found** -> ask Keith which feature this belongs to, or offer to ship as `--no-feature`.
- **`wiki/concepts/index.md` parse failure** -> fail loudly, point Keith at the file, do not guess the lane structure.
- **DEVLOG.md / wiki/log.md append failure** -> commit has already landed, so degrade gracefully: print the entry Keith should add manually, continue with the rest of the workflow.

## What /ship Does NOT Do

- Auto-sync to Mac Mini (Keith must run `/sync` explicitly)
- Edit vault files other than the feature note, `wiki/concepts/index.md`, `wiki/log.md`, `DEVLOG.md`, the skill-usage-log, and `raw/sessions/`
- Overwrite `store/` (production SQLite on Mac Mini)
- Touch `.env` files
- Force-push, amend existing commits, rebase, or use any destructive git operation
- Run deployment scripts

## Notes

- `/ship` is intentional, not a hook. Shipping is a human-in-the-loop action.
- The Codex pre-commit review gate is mandatory per CLAUDE.md. Skipping requires a recorded reason.
- `wiki/concepts/index.md` is the source of truth. `/ship` is how state transitions get written safely -- never edit lane membership by hand mid-session.
- For multi-ship features, the Shipping Status table and phase gate pattern (modeled on `project_minimax-offload`) stay authoritative. `/ship` updates rows within it; it does not replace the table.
- `/ship --no-feature` is the escape hatch for infrastructure commits that don't map to a feature (e.g. fixing CI, rotating a credential). Use sparingly.
