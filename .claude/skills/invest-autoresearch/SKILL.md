---
name: invest-autoresearch
description: Run the Karpathy autoresearch self-improvement loop on K2B-Investment skills. Iteratively improves a target SKILL.md using binary assertions, git-based memory, and the commit-before-test pattern. Use when Keith says /autoresearch, "improve this skill", "run the loop", "optimize skill", "self-improve", or wants to iteratively enhance any K2B-Investment skill's output quality.
---

# Invest Autoresearch

The Karpathy loop adapted for K2B-Investment skills. Iteratively improves a target file using binary assertions and git-based memory.

## Commands

- `/autoresearch [skill-name]` -- Run the improvement loop on a specific skill
- `/autoresearch plan` -- Setup wizard to define target, eval criteria, guard, iteration count
- `/autoresearch status` -- Show results summary for all skills with evals

## Vault & Skill Paths

- Vault: `~/Projects/K2B-Investment-Vault`
- Skills: `~/Projects/K2B-Investment/.claude/skills/`
- Each skill's eval infrastructure: `.claude/skills/invest-[name]/eval/`

## The Loop Protocol

### Phase 0: Precondition Checks

Before starting:
1. Confirm we're in a git repo with a clean working tree (`git status`)
2. Identify the target file: `.claude/skills/invest-[name]/SKILL.md`
3. Identify the eval file: `.claude/skills/invest-[name]/eval/eval.json`
4. If eval.json doesn't exist, CREATE it first (see Eval Creation below)
5. Identify guard command (if any)
6. Run the eval against current SKILL.md to establish baseline
7. Log baseline as iteration 0 in results.tsv

### Phase 1: Review

Before each iteration:
1. Read the target SKILL.md
2. Read last 10-20 entries from `eval/results.tsv`
3. Run `git log --oneline -20` for the target file to see commit history
4. Read `eval/learnings.md` for accumulated patterns
5. Understand: what's working, what's failing, what's been tried

### Phase 2: Ideate

Choose the next change using this priority order:
1. **Fix crashes/errors** from last run
2. **Exploit successes** -- patterns from kept iterations
3. **Address failing assertions** -- target the most common failure
4. **Try untested approaches** -- something not yet in results.tsv
5. **Combine near-misses** -- merge two almost-working ideas
6. **Simplify** -- fewer instructions that achieve the same result (less is more)
7. **Radical experiment** -- only after 5+ consecutive discards

### Phase 3: Modify

Make ONE focused change to the SKILL.md.

**The rule: "If I need 'and' to describe it, it's two experiments."**

Examples of ONE change:
- Add a specific instruction for formatting trade thesis bullets
- Remove a redundant section that causes confusion
- Reword the cross-linking instructions to be more specific
- Add an example output block

Examples of TOO MANY changes:
- Rewrite the entire workflow section AND add new formatting rules
- Change the frontmatter instructions AND the cross-linking rules

### Phase 4: Commit BEFORE Verification

This is critical. Commit the change BEFORE testing it.

```bash
git add .claude/skills/invest-[name]/SKILL.md
git commit -m "experiment(invest-[name]): [brief description of change]"
```

Rules:
- NEVER use `git add -A` or `git add .`
- Only add the specific target file
- Commit message format: `experiment(scope): description`

### Phase 5: Verify

Run each test prompt from eval.json:
1. For each test in eval.json:
   a. Present the test prompt as if Keith said it
   b. Generate output following the SKILL.md instructions
   c. Score each assertion as PASS or FAIL
2. Calculate overall pass rate: (total passes) / (total assertions across all tests)
3. If any single test takes excessively long, note it as a concern

**How to score assertions:**
Each assertion is a binary yes/no question about the output. Read the output carefully and answer each assertion honestly. Do not be lenient -- the point is to find real failures.

### Phase 5.5: Guard Check (Optional)

If a guard command is defined:
1. Run the guard command
2. If guard FAILS despite metric improvement: attempt to rework the change (max 2 attempts)
3. If guard still fails after 2 rework attempts: revert and try a different approach

### Phase 6: Decide

Compare pass rate to previous best:
- **IMPROVED**: Keep the commit. Log as `keep`.
- **SAME OR WORSE**: Revert. Use `git revert HEAD --no-edit`. Log as `discard`.
- **CRASHED**: Log as `crash`. Revert. Attempt to fix in next iteration.

### Phase 7: Log to results.tsv

Append to `.claude/skills/invest-[name]/eval/results.tsv`:

Format (tab-separated):
```
# metric_direction: higher_is_better
iteration	commit	pass_rate	delta	guard	status	description
0	abc1234	72.3	0.0	-	baseline	initial state
1	def5678	80.0	+7.7	pass	keep	added explicit thesis bullet format
2	ghi9012	73.3	-6.7	-	discard	removed insights section
```

Valid statuses: `baseline`, `keep`, `keep (reworked)`, `discard`, `crash`

### Phase 8: Update Learnings

After each iteration, update `eval/learnings.md`:
- After a **keep**: log what worked and why under "## What Works"
- After a **discard**: log what didn't work under "## What Doesn't Work"
- After discovering a pattern: log under "## Patterns Discovered"

### Repeat

Continue until:
- Perfect score (100% pass rate) achieved
- Keith interrupts
- Bounded iteration count reached (if specified)
- Every 5 iterations, print a brief progress summary

**Stuck detection**: After 5+ consecutive discards:
1. Stop and review the entire results.tsv
2. Try combining previous successful approaches
3. Try the opposite of what's been failing
4. Try a radical structural change to the SKILL.md
5. If still stuck after 3 more attempts, stop and report to Keith

## Eval Creation

When a skill lacks an eval.json, create one:

1. Read the skill's SKILL.md thoroughly
2. Identify 3-5 structural/format requirements that define "good" output
3. Write 3 test prompts that exercise different aspects of the skill
4. Write 3-6 binary assertions per test prompt

**Rules for good assertions:**
- Binary yes/no ONLY -- no subjective judgments
- Test structure, format, required sections, naming conventions, forbidden patterns
- Do NOT test tone, creativity, or subjective quality
- Sweet spot: 3-6 assertions per test prompt
- Below 3: agent finds loopholes
- Above 6: agent games the checklist

**eval.json format:**
```json
{
  "tests": [
    {
      "prompt": "The exact prompt to test the skill with",
      "expected_output": "Brief description of what good output looks like",
      "assertions": [
        "Does the output contain X?",
        "Does it avoid Y?",
        "Is Z present in the correct format?"
      ]
    }
  ]
}
```

## /autoresearch plan -- Setup Wizard

Walk Keith through defining the autoresearch target:

1. **Goal**: "What skill do you want to improve?" (list available skills)
2. **Scope**: Confirm the target file path
3. **Eval check**: Does eval.json exist? If not, create one together.
4. **Guard**: "Any command that must always pass?" (optional)
5. **Iterations**: "How many iterations? (unlimited, or a number like 10)"
6. **Launch**: Confirm and start the loop

## /autoresearch status -- Dashboard

Show a summary table for all skills with eval infrastructure:

```
| Skill                  | Assertions | Last Pass Rate | Best | Iterations | Last Run   |
|------------------------|-----------|----------------|------|------------|------------|
| invest-session-wrapup  | 15        | 80.0%          | 86.7%| 12         | 2026-04-18 |
| invest-journal         | 12        | 91.7%          | 91.7%| 5          | 2026-04-18 |
| invest-compile         | 12        | 75.0%          | 83.3%| 8          | 2026-04-17 |
```

Read from each skill's `eval/results.tsv` to populate this table.

## Core Principles (from Karpathy)

1. **Git IS the memory** -- every experiment commits, kept commits form the improvement chain
2. **ONE change per iteration** -- isolation makes learning possible
3. **Commit BEFORE test** -- so revert is clean
4. **Binary assertions** -- no subjective scoring, no LLM-as-judge
5. **Mechanical verification** -- assertions must be answerable by reading the output
6. **Learnings accumulate** -- results.tsv + learnings.md + git history compound over time
7. **Never stop, never ask** -- autonomous until interrupted or perfect

## Usage Logging

After completing the main task, log this skill invocation:
```bash
echo -e "$(date +%Y-%m-%d)\tinvest-autoresearch\t$(echo $RANDOM | md5sum | head -c 8)\tran autoresearch on SKILL" >> ~/Projects/K2B-Investment-Vault/wiki/context/skill-usage-log.tsv
```

## Post-Loop Handoff

After the loop completes (perfect score, interrupted, or iteration limit):
1. Report the summary (iterations run, kept, discarded, final pass rate)
2. Prompt Keith: "Autoresearch created N commits. Run /ship to push + devlog?"
3. Do NOT prompt /sync directly -- /ship handles the sync handoff in its step 12.

Autoresearch always creates commits (Phase 4: commit before test). These commits are local-only until /ship pushes them. Skipping /ship means no devlog entry, no wiki/log.md entry, and the Mac Mini stays stale.

## Notes

- No em dashes, no AI cliches, no sycophancy
- Keep iteration summaries brief -- don't narrate every step
- Print progress every 5 iterations
- The eval file is sacred -- never modify eval.json during a loop (that's gaming the test)
