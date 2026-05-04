---
name: invest-feedback
description: Capture learnings, errors, and feature requests to make K2Bi smarter over time. This skill should be used when Keith says /learn, /error, /request, /feedback, "remember that", "don't do that again", "next time do X", "that broke", "something went wrong", "I wish you could", "can you do X" (and the answer is no), or any variation of teaching K2Bi, reporting a failure, or identifying a capability gap. For reviewing all captured feedback, use invest-improve (once built, currently via invest-feedback + invest-observer manual review).
tier: utility
---

# K2B Feedback Capture

Capture corrections, errors, and feature requests in a single unified skill. Three subcommands, three files, one skill.

## Memory Paths

- Learnings: `~/.claude/projects/*/memory/self_improve_learnings.md`
- Errors: `~/.claude/projects/*/memory/self_improve_errors.md`
- Requests: `~/.claude/projects/*/memory/self_improve_requests.md`

## Quick Reference

| Situation | Command | File |
|-----------|---------|------|
| Keith corrects K2B or teaches a preference | `/learn [description]` | self_improve_learnings.md |
| Something broke or failed | `/error [description]` | self_improve_errors.md |
| Keith wants something K2B can't do | `/request [description]` | self_improve_requests.md |
| Auto-detect from context | `/feedback [description]` | Routes automatically |

## Auto-Routing (/feedback)

When Keith says `/feedback` without specifying type, read the conversation context and route:

- Corrections, preferences, best practices --> learn
- Failures, bugs, unexpected behavior --> error
- Missing capabilities, "I wish you could" --> request
- If ambiguous, ask Keith: "Is this a learning, an error, or a feature request?"

## Command: /learn [description]

Capture a correction, preference, or best practice.

1. If description provided, use it. If not, infer the learning from the current conversation context.
2. Read `self_improve_learnings.md`.
3. Check if a similar learning already exists (match on topic/area). If yes, increment `Reinforced` count and update the entry with any new context. If no, append a new entry.
4. Write the updated file.
5. **Policy ledger update**: If the learning is actionable as a guard (it says "never do X", "always do Y", "before doing Z, check..."), also append a JSONL entry to `~/Projects/K2Bi-Vault/wiki/context/policy-ledger.jsonl`:
   ```json
   {"type":"guard","scope":"<skill-name-or-*>","action":"<action-type>","rule":"<the learning text>","source":"<learning-ID>","risk":"<low|medium|high|critical>"}
   ```
   - Scope: the skill this applies to, or `*` for all skills
   - Action: what specific action this guards (e.g., `create_wiki_page`, `deploy_remote`, `send_email`)
   - Risk: how bad it would be to violate this (critical = data loss, high = wrong behavior, medium = suboptimal, low = style)
   - Check for duplicates in the ledger before appending (match on scope + action + similar rule text)
6. Confirm with one line: what was captured (and whether a ledger entry was added).

Entry format:
```markdown
### L-YYYY-MM-DD-NNN
- **Area:** [preferences | workflow | knowledge | tools | writing-style | vault]
- **Distilled rule:** [one-sentence active-voice rule, max ~150 chars, the exact text that would land in active_rules.md on promotion]
- **Learning:** [what K2B should do differently, longer form]
- **Context:** [what triggered this learning]
- **Reinforced:** 1
- **Confidence:** low
- **Date:** YYYY-MM-DD
```

Also write a frontmatter-style `distilled-rule:` field at the top of the entry body so `scripts/promote-learnings.py` can parse it without relying on bullet-text heuristics:

```markdown
### L-YYYY-MM-DD-NNN
distilled-rule: "one-sentence active-voice rule"
- **Area:** ...
- **Distilled rule:** same text as the frontmatter line above
- **Learning:** ...
- **Reinforced:** 1
...
```

Both forms (the `distilled-rule:` line and the `- **Distilled rule:**` bullet) should be written on every new entry. The scanner prefers the frontmatter line. The bullet is there so a human skimming the file sees the rule text without decoding frontmatter.

**Reinforcement handling:** If a new `/learn` call cites an existing L-ID or matches an existing `distilled-rule:` (case-insensitive substring), increment the existing entry's `- **Reinforced:**` count (keeping the `Reinforced` field name as-is) and append a note to its body with the new date and context. Do not create a duplicate entry. The `distilled-rule:` line is never rewritten on reinforcement; only the first capture sets it.

ID format: `L-YYYY-MM-DD-NNN` where NNN auto-increments based on existing entries for that date.

### Confidence Scoring

Confidence is derived from the Reinforced count:

| Reinforced | Confidence | Behavior |
|------------|------------|----------|
| 1 | low | Suggest but don't enforce. Mention when relevant. |
| 2-5 | medium | Surfaced in session-start watch list. Apply when relevant. Can be overridden without comment. |
| 6+ | high | Treat as core behavior. Auto-apply. Candidate for promotion to active rules. |

When updating `Reinforced`, always recalculate and update `Confidence`:
- Set `low` for 1-2
- Set `medium` for 3-5
- Set `high` for 6+

When confidence reaches `high`, the session-start hook automatically surfaces the learning so all skills apply it.

## Command: /error [description]

Log a failure with root cause and fix.

1. If description provided, use it. If not, infer from the current conversation what went wrong.
2. Read `self_improve_errors.md`.
3. Append a new entry.
4. If the error reveals a generalizable learning, also add it to `self_improve_learnings.md` following the /learn entry format.
5. Confirm with one line.

Entry format:
```markdown
### E-YYYY-MM-DD-NNN
- **What happened:** [description of failure]
- **Root cause:** [why it failed]
- **Fix:** [what resolved it or what to do next time]
- **Date:** YYYY-MM-DD
```

ID format: `E-YYYY-MM-DD-NNN` where NNN auto-increments based on existing entries for that date.

## Command: /request [description]

Log a capability K2B doesn't have yet.

1. If description provided, use it. If not, infer from the current conversation what was requested.
2. Read `self_improve_requests.md`.
3. Check for duplicates. If a similar request exists, note it was requested again and update the entry.
4. Append new entry or update existing.
5. Confirm with one line.

Entry format:
```markdown
### R-YYYY-MM-DD-NNN
- **Request:** [what Keith wanted]
- **Why needed:** [context for the request]
- **Status:** open
- **Date:** YYYY-MM-DD
```

ID format: `R-YYYY-MM-DD-NNN` where NNN auto-increments based on existing entries for that date.

## Behavioral Note

When Keith corrects K2B during normal conversation ("no, do it like this", "that's wrong", "next time..."), proactively offer: "Want me to /learn that?" -- but only offer, never auto-capture without confirmation.

## Usage Logging

After completing the main task, log this skill invocation:
```bash
echo -e "$(date +%Y-%m-%d)\tinvest-feedback\t$(echo $RANDOM | md5sum | head -c 8)\tcaptured TYPE: DESCRIPTION" >> ~/Projects/K2Bi-Vault/wiki/context/skill-usage-log.tsv
```

## invest-coach auto-capture (D7)

When the invest-coach skill detects an operator rejection event at turns T2, T6, T8, or T10, it calls `capture_coach_rejection` from `scripts.lib.invest_coach`. This function writes a structured raw record to `K2Bi-Vault/raw/coach-feedback/<sigid>_<turn>_rejected.md` without modifying the existing `/learn`, `/error`, `/request` flow.

The record format:
- Frontmatter: tags, date, type=coach-feedback, origin=keith, up, sigid, turn_id
- Body: rejected framing block + operator correction block

These records are raw feedback streams that the observer loop or `/invest-improve` (when built) can harvest for pattern analysis. They do NOT replace `/learn` or `/error`; they supplement them with turn-level granularity.

## Notes

- No em dashes, no AI cliches, no sycophancy
- Keep confirmations to one line
- Don't over-explain the system to Keith, just capture and confirm
- The three subcommands maintain backward compatibility -- /learn, /error, /request still work as before
