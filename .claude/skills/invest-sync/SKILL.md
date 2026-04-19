---
name: invest-sync
description: Sync K2Bi project files to the Mac Mini server -- detects what changed, syncs every category declared in scripts/deploy-config.yml (skills, execution, scripts, pm2 today; extensible), rebuilds on the Mini when the category needs it. Use when Keith says /sync, "sync to mini", "deploy to mini", "push to mini", or after K2Bi modifies project files tracked by the deploy config.
---

# K2Bi Sync to Mac Mini

Push K2Bi project file changes from MacBook to the always-on Mac Mini server.

## When to Trigger

**Explicitly:** Keith says `/sync`, "sync to mini", "deploy", "push to mini".

**Proactively prompt Keith** at the end of any session where K2Bi modified files inside a tree declared in `scripts/deploy-config.yml`. Don't hardcode the list here -- ask the config helper at prompt time:

```bash
python3 scripts/lib/deploy_config.py list-targets
```

As of cycle 2 + cycle 4's infra cleanup, that returns (alphabetical within category):

- `skills` lane: `.claude/skills/`, `.claude/settings.json`, `CLAUDE.md`, `README.md`, `DEVLOG.md`
- `execution` lane: `execution/`, `requirements.txt`
- `scripts` lane: `scripts/`
- `pm2` lane: `pm2/` (populated Phase 6+)

The live authoritative output always wins over this note.

Say: "Changes were made to [list]. These are on your MacBook only. Run /sync to push to Mac Mini?"

Do NOT auto-sync without Keith's confirmation. Always ask first.

## Commands

- `/sync` -- auto-detect what changed and sync it
- `/sync <category>` -- sync a single category, where `<category>` is any value printed by `python3 scripts/lib/deploy_config.py list-categories`. The helper + `deploy-to-mini.sh` are the single source of truth; do not hardcode category names here.
- `/sync all` -- force full sync of every category
- `/sync status` -- `deploy-to-mini.sh --dry-run`; shows what would sync without changing anything

## Paths

- Deploy script: `~/Projects/K2Bi/scripts/deploy-to-mini.sh`
- Config source of truth: `~/Projects/K2Bi/scripts/deploy-config.yml`
- Mac Mini SSH alias: `macmini`
- Remote K2Bi project: `~/Projects/K2Bi/` on Mac Mini

## Workflow

### 0. Consume the pending-sync mailbox

Before detecting changes in the current session, check the **current repo's** `.pending-sync/` mailbox for entries deferred by `/invest-ship --defer` in this or a previous session. Each entry is one JSON file written atomically; multiple entries accumulate if Keith defers several ships before running `/sync`. `/sync` is the sole consumer: it reads entries, folds them into the sync scope, runs the deploy, and deletes each entry it successfully processed -- **by filename**, never by a rewrite-compare pattern. That makes the protocol race-free without locks.

**Parse failures must be loud, not silent.** A malformed entry is *worse* than no entry -- it means the durable recovery signal is broken, and silently skipping it would re-create the lost-recovery problem the mailbox was added to prevent. If an entry file cannot be parsed (bad JSON, missing schema fields, category not in `deploy-config.yml`), stop and report to Keith so they can inspect, fix, or delete it manually.

**Category validation is runtime, not hardcoded.** Bundle 3 cycle 2 anchored the category allowlist in `scripts/deploy-config.yml`; cycle 4's infra fix propagates that anchor to this skill. `scripts/lib/pending_sync.py` shells out to `deploy_config.py list-categories` on every scan, so whatever categories the current repo declares (K2Bi ships `{skills, execution, scripts, pm2}`; a fork can ship any set) are what this skill accepts -- no edit needed at fork time beyond the config itself.

**Scan step.** Call the helper; state is encoded in stdout so a caller that wraps this in `set -e` does not lose the signal:

```bash
mailbox_state=$(python3 -m scripts.lib.pending_sync scan 2>/dev/null)
```

The helper prints one of:

- `EMPTY` -- no actionable entries.
- `VALID|<json-array-of-[filename,payload]>` -- every entry passed schema + category validation.
- `UNREADABLE|<json-array-of-[filename,reason]>[\nVALID|<json-array>]` -- at least one entry is malformed or references an unknown category. A mixed output (both lines) surfaces the good entries but still requires Keith to acknowledge the broken state before proceeding.

Always exits 0; mailbox state is in stdout.

**Decision tree:**

1. `EMPTY`: no deferred entries. Proceed with normal conversation-context detection in step 1.
2. `VALID|...`: parse the JSON list. Fold all payloads into a single sync scope (union of `files`, union of `categories`). Report to Keith: "Consuming N mailbox entries: list the `entry_id`s." Save the list of filenames -- those are the exact files you will delete after the sync succeeds.
3. `UNREADABLE|...`: STOP. Do not proceed to detection or sync. Report loudly: "Mailbox entries at `<repo-root>/.pending-sync/` are unreadable: {list}. Durable deferred-sync signal is broken. Inspect, fix, or delete the bad files and re-run `/sync`." Never auto-delete corrupted entries -- they may be useful evidence (e.g. a category mismatch that points to a fork-time skill gap).

**After a successful sync:**

Delete only the specific filenames that were in the `VALID|` list at the start of this run. Do NOT scan the directory again -- any new entries that appeared during the sync were written by a concurrent `/invest-ship --defer` under a DIFFERENT filename (producers use a unique `entry_id`) and must be preserved for the next `/sync` run to pick up.

```bash
# $PROCESSED is a JSON array captured from the VALID| line at the start.
python3 -m scripts.lib.pending_sync delete --entries "$PROCESSED"
```

If the sync **fails**, do NOT run the delete command. Entries stay in place so the next `/sync` attempt can retry from the same mailbox state.

**Why the mailbox design is race-free:** producers (`/invest-ship --defer`) write each entry as a unique filename via `os.replace()` (atomic rename). They never read or delete. The consumer (`/sync`) deletes only filenames it read at the start of its run -- so any concurrent producer's new filename is simply not in the delete list and survives untouched. No compare-and-swap, no locks, no TOCTOU. Correct on POSIX under concurrent `/invest-ship` and `/invest-sync` invocations.

This is the durable recovery path: a fresh Claude Code session can discover that the Mini is stale and act on it without needing access to a previous session's conversation.

### 1. Detect Changes

**Primary method: use conversation context.** K2Bi knows which files it modified in the current session. List exactly those files -- don't scan the whole repo.

**If context is unclear** (e.g. Keith runs `/sync` in a fresh session), use `deploy-to-mini.sh --dry-run` to compare MacBook vs Mac Mini via the same rsync invocations the real sync uses. That avoids duplicating hardcoded paths in this skill.

```bash
~/Projects/K2Bi/scripts/deploy-to-mini.sh --dry-run
```

This compares actual file contents between machines -- not git state. Only files that genuinely differ will show up. If a specific category is suspected:

```bash
~/Projects/K2Bi/scripts/deploy-to-mini.sh --dry-run <category>
```

**Do NOT use `git diff --name-only HEAD`** -- that shows all uncommitted changes since last commit, including files already synced in previous sessions.

### 2. Categorize and Summarize

Classify files via the config helper instead of a hardcoded table:

```bash
printf '%s\n' <files> | python3 scripts/lib/deploy_config.py classify
```

Each line comes back as `<category><TAB><path>` or `uncovered<TAB><path>`. Show Keith a summary of only the files that actually differ, grouped by category:

```
Out of sync with Mac Mini:
  - .claude/skills/invest-ship/SKILL.md
  - .claude/skills/invest-sync/SKILL.md
  Category: skills

Sync to Mac Mini?
```

The category → "needs build / restart" mapping lives in the deploy script itself; the skill does not hardcode it. As of today, no category in K2Bi requires a build step -- `invest-remote` and a dashboard will arrive in later phases and the deploy script will grow build/restart hooks then.

### 3. Execute Sync

Preview:

```bash
~/Projects/K2Bi/scripts/deploy-to-mini.sh --dry-run
```

Actual sync:

```bash
~/Projects/K2Bi/scripts/deploy-to-mini.sh auto
```

Or force a single category (any value from `list-categories`):

```bash
~/Projects/K2Bi/scripts/deploy-to-mini.sh <category>
~/Projects/K2Bi/scripts/deploy-to-mini.sh all
```

### 4. Verify

After sync completes, confirm the Mini received what we intended.

**Skills category (always verifiable):**

```bash
ssh macmini "head -3 ~/Projects/K2Bi/CLAUDE.md"
ssh macmini "ls ~/Projects/K2Bi/.claude/skills/ | wc -l"
```

The deploy script itself runs a skill-count sanity check at the end of a skills sync -- use its output as the primary signal.

**pm2-backed categories (when they exist -- Phase 4+):** check `pm2 status` for the specific daemon the category owns. At the time of this write there are no pm2 daemons in K2Bi yet; when a category gains one, add the verification call here.

**For any sync target:** report what was synced and any warnings.

### 5. Report

Tell Keith:
- What categories were synced
- How many files transferred
- Verification results (skill count match, pm2 status where applicable)
- Any errors or warnings

## Error Handling

- **Mac Mini unreachable:** "Can't reach Mac Mini via SSH. Is it on the network?"
- **Build / restart failure (when a future phase adds one):** show the relevant log output; do not attempt the next step until Keith decides.
- **No changes detected:** "No syncable changes found. Use `/sync all` to force a full sync."
- **Category unknown to deploy-config.yml:** the helper's error is the canonical message. Keith edits `scripts/deploy-config.yml` to add the category (and its target paths) before re-running `/sync`. The fork-time swap note lives in `scripts/deploy-config.yml` itself.

## What Does NOT Sync

- **Vault** (`K2Bi-Vault/`) -- handled by Syncthing, not this skill
- **node_modules/** and **dist/** -- excluded from rsync, rebuilt on Mini (no current K2Bi code has these; applies once invest-remote / a dashboard lands)
- **store/** -- production SQLite database lives on Mac Mini, NEVER overwrite from MacBook
- **.env** -- environment config stays local to each machine
- **.git/** -- each machine has its own git state
- Anything listed under `excludes:` in `scripts/deploy-config.yml` (hooks, reviewer archives, mailbox)

## Usage Logging

After completing the main task, log this skill invocation:

```bash
echo -e "$(date +%Y-%m-%d)\tinvest-sync\t$(echo $RANDOM | md5 | head -c 8)\tsynced CATEGORIES to mac mini" >> ~/Projects/K2Bi-Vault/wiki/context/skill-usage-log.tsv
```

## Notes

- Always confirm with Keith before syncing. Never auto-sync.
- The deploy script handles SSH connectivity checks.
- If Keith is iterating fast on a single category, suggest batching changes before syncing.
- Category list + target paths live in `scripts/deploy-config.yml`; this skill never hardcodes them. Adding a new deploy category = edit the config, no skill edit required.
