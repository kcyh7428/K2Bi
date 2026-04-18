# invest-compile Autoresearch Learnings

> Inherited from k2b-compile at Phase 1 Session 2 port. Historical entries below reference k2b-compile context; new entries land under invest-compile autoresearch runs.


## What Works

- **Raw index FIRST ordering:** The recurring failure (E-2026-04-12-001, L-2026-04-12-002) was always the raw subfolder index being skipped. Moving it to position 5a with an explicit "this is the step that gets forgotten" callout makes it impossible to skip without consciously ignoring a warning. The self-check gate ("walk through 5a-5e before reporting done") adds a second enforcement layer.

- **Defense-in-depth dedup:** The L-2026-04-12-001 failure (creating a duplicate reference page instead of enriching an existing work page) had a clear root cause: Opus treated MiniMax's JSON output as authoritative. Adding dedup checks in TWO places (Step 2 plan validation + Step 4 pre-create) means even if one check is rushed, the other catches it.

- **"Suggestion not directive" framing:** Explicitly calling out that MiniMax output is a suggestion changes the Opus agent's posture from "execute the plan" to "validate then execute." This is a general pattern for any commander/worker architecture where the worker is cheaper but less context-aware.

## What Doesn't Work

(none yet -- all 3 iterations kept)

## Patterns Discovered

- **Binary eval ceiling:** When a skill's instructions already mention all the right steps but the failure is in execution emphasis, binary assertions score 100% at baseline. The improvement is qualitative (ordering, warnings, defense-in-depth) not quantitative. Future evals for execution-emphasis problems should test ordering/priority explicitly, not just presence.

- **Two-class failures in compile:** (1) Index completeness failures (forgetting one of the 4 indexes) are attention/ordering problems. (2) Dedup failures (creating vs enriching) are judgment/validation problems. Different fix patterns: ordering fixes attention, validation gates fix judgment.
