"""invest-bear-case -- Bundle 4 cycle 2 (m2.12).

Python merge + atomic-write module for the invest-bear-case skill.
Consumes an already-parsed `BearCaseInput` (the skill body is responsible
for running the single adversarial Claude Code call + parsing the JSON
response; this module owns validation + frontmatter merge + body append
+ atomic write + audit-trail discipline).

Architecture (per spec §3.2):

    Claude (SKILL.md body)
        -> reads thesis from wiki/tickers/<SYMBOL>.md
        -> builds adversarial prompt (template in SKILL.md body)
        -> executes ONE Claude inference call (spec §0.3 constraint 1)
        -> parses JSON response
        -> validates counterpoints count (retry once on mismatch)
        -> builds BearCaseInput
        -> calls run_bear_case(symbol, bear_input, vault_root, ...)
    invest_bear_case.run_bear_case (this module)
        -> validates symbol + conviction range + counterpoints count
           + invalidation-scenario range
        -> requires existing thesis with `thesis_score:` field
        -> freshness check (skip if within FRESH_DAYS + no --refresh)
        -> merges frontmatter (preserves ALL thesis fields byte-for-byte
           at dict level; mutates only the 5 bear_* fields)
        -> appends ## Bear Case (YYYY-MM-DD) body section
        -> atomic write via sf.atomic_write_bytes
    Claude (SKILL.md body, post-return)
        -> invokes scripts/wiki-log-append.sh for the log entry

VETO threshold is LOCKED per spec §5 Q7: `verdict = VETO iff conviction
> 70`. Encoded as VETO_THRESHOLD module constant; strictly greater (70
itself yields PROCEED so callers do not fiddle the boundary).

Schema contract (spec §2.2, locked):

    bear-last-verified: YYYY-MM-DD
    bear_conviction: int 0..100
    bear_top_counterpoints: list of exactly 3 strings
    bear_invalidation_scenarios: list of 2..5 strings
    bear_verdict: "VETO" | "PROCEED"

Body section format (spec §2.2, locked):

    (blank line)
    ## Bear Case (YYYY-MM-DD)

    **Verdict:** <verdict> (conviction: <N>)

    ### Top counterpoints to monitor
    1. <first>
    2. <second>
    3. <third>

    ### Invalidation scenarios
    - <first>
    - <second>

    ### Why this matters for your position   (novice/intermediate + size)
    <2-3 sentences translating bear case to HKD impact>

Multiple runs at 30+ day intervals (or with --refresh) append NEW dated
sections; prior sections are preserved as an audit trail. Frontmatter
always reflects the LATEST verdict only.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from scripts.lib import strategy_frontmatter as sf


# Top-level-key detector for line-level frontmatter editing. Matches a
# line that starts at column 0 with an identifier ([A-Za-z_][\w-]*)
# immediately followed by a colon. Excludes YAML list items (`- foo`),
# comments (`# foo`), and indented continuation lines (`  key: val`).
_TOP_LEVEL_KEY_RE = re.compile(r"^[A-Za-z_][\w-]*:")

# Bear-* top-level key detector -- any top-level key that starts with
# either `bear_` (bear_conviction, bear_verdict, etc.) or the hyphenated
# `bear-` form used for date-style fields (bear-last-verified). Scoped
# to top-level: the `bear_case` thesis field is NOT matched because the
# regex requires the literal `bear_` or `bear-` prefix plus a char class
# that excludes the underscore-word-break needed by `bear_case`. In
# practice we accept the superset and rely on the name-exact removal
# list below instead.
_BEAR_KEY_EXACT: frozenset[str] = frozenset(
    {
        "bear-last-verified",
        "bear_conviction",
        "bear_top_counterpoints",
        "bear_invalidation_scenarios",
        "bear_verdict",
    }
)


# ---------- constants ----------


# Spec §5 Q7 LOCK: VETO iff conviction strictly greater than 70.
# Encoded as a module constant so downstream gate code (scan_bear_case_
# for_ticker in invest_ship_strategy) references the same source of
# truth as the writer.
VETO_THRESHOLD = 70


# Freshness window. A bear-case run within FRESH_DAYS of now counts as
# fresh; re-runs without refresh=True skip. Matches spec §2.2 + the
# engine-side gate's 30-day contract.
FRESH_DAYS = 30


ALLOWED_VERDICTS = frozenset({"VETO", "PROCEED"})


REQUIRED_COUNTERPOINTS_COUNT = 3


# Spec §3.2 step 4: 2..5 invalidation scenarios. Fewer than 2 is weak
# adversarial output; more than 6 is diluted signal. Bounds enforced
# here so callers cannot silently ship a degenerate bear-case.
MIN_INVALIDATION_SCENARIOS = 2
MAX_INVALIDATION_SCENARIOS = 5


ALLOWED_LEARNING_STAGES = frozenset({"novice", "intermediate", "advanced"})


# Same symbol regex as invest-thesis (spec §2.1 ticker format): uppercase
# alphanumeric, optional single `.` separator (HK numeric tickers like
# `0700.HK`, US share classes like `BRK.B`). Anchored.
_SYMBOL_RE = re.compile(r"^[A-Z0-9]+(?:\.[A-Z0-9]+)?$")


# Body section heading regex. Matches `## Bear Case (YYYY-MM-DD)` with
# standard ISO-date suffix. Used only to verify that rewrites produce
# a new section (not to rewrite existing ones) in tests + future lint.
BEAR_SECTION_HEADING_RE = re.compile(
    r"^## Bear Case \(\d{4}-\d{2}-\d{2}\)$",
    re.MULTILINE,
)


# ---------- dataclasses ----------


@dataclass
class BearCaseInput:
    """Structured adversarial output, parsed + validated by the SKILL.md
    orchestrator before being handed to run_bear_case.

    Fields track spec §2.2 schema exactly. `bear_verdict` is not carried
    on the input because it is derived from `bear_conviction` via the
    strict `> VETO_THRESHOLD` rule -- giving the caller a second place
    to set it creates a drift risk (caller asserting "VETO" while
    conviction=60) that this module refuses to resolve.
    """

    bear_conviction: int
    bear_top_counterpoints: list[str]
    bear_invalidation_scenarios: list[str]


@dataclass
class BearCaseResult:
    path: Path
    written: bool
    skipped_reason: Optional[str] = None
    bear_verdict: Optional[str] = None
    bear_conviction: Optional[int] = None


# ---------- validation ----------


def validate_symbol(symbol: str) -> None:
    """Same contract as invest_thesis.validate_symbol -- consistent with
    the ticker-format rule Phase 2 locks. Raises ValueError on format
    violations."""
    if not symbol:
        raise ValueError("symbol must be non-empty")
    if not _SYMBOL_RE.match(symbol):
        raise ValueError(
            f"symbol {symbol!r} does not match required format "
            f"[A-Z0-9]+(\\.[A-Z0-9]+)? (uppercase alphanumeric with "
            f"optional single `.` separator)"
        )
    if not any(ch.isalpha() for ch in symbol):
        raise ValueError(
            f"symbol {symbol!r} must contain at least one letter "
            f"(digits-only strings are not valid tickers)"
        )


def validate_bear_case_input(bear_input: BearCaseInput) -> None:
    """Enforce the schema contract from spec §2.2.

    Validates:
      - bear_conviction is int in [0, 100].
      - bear_top_counterpoints is a list of exactly 3 non-empty strings.
      - bear_invalidation_scenarios is a list of 2..5 non-empty strings.

    Truncation / retry of malformed Claude output is the SKILL.md
    orchestrator's job (spec §3.2 step 4). This module is strict.
    """
    c = bear_input.bear_conviction
    if not isinstance(c, int) or isinstance(c, bool):
        raise ValueError(
            f"bear_conviction must be int, got {type(c).__name__}"
        )
    if c < 0 or c > 100:
        raise ValueError(
            f"bear_conviction must be in [0, 100], got {c!r}"
        )

    cps = bear_input.bear_top_counterpoints
    if not isinstance(cps, list):
        raise ValueError(
            f"bear_top_counterpoints must be a list, "
            f"got {type(cps).__name__}"
        )
    if len(cps) != REQUIRED_COUNTERPOINTS_COUNT:
        raise ValueError(
            f"bear_top_counterpoints must be exactly "
            f"{REQUIRED_COUNTERPOINTS_COUNT} strings, got {len(cps)}"
        )
    for i, cp in enumerate(cps):
        if not isinstance(cp, str) or not cp.strip():
            raise ValueError(
                f"bear_top_counterpoints[{i}] must be a non-empty "
                f"string, got {cp!r}"
            )

    scs = bear_input.bear_invalidation_scenarios
    if not isinstance(scs, list):
        raise ValueError(
            f"bear_invalidation_scenarios must be a list, "
            f"got {type(scs).__name__}"
        )
    if not (
        MIN_INVALIDATION_SCENARIOS <= len(scs) <= MAX_INVALIDATION_SCENARIOS
    ):
        raise ValueError(
            f"bear_invalidation_scenarios must have "
            f"{MIN_INVALIDATION_SCENARIOS}..{MAX_INVALIDATION_SCENARIOS} "
            f"entries, got {len(scs)}"
        )
    for i, sc in enumerate(scs):
        if not isinstance(sc, str) or not sc.strip():
            raise ValueError(
                f"bear_invalidation_scenarios[{i}] must be a non-empty "
                f"string, got {sc!r}"
            )


def derive_verdict(conviction: int) -> str:
    """Apply the locked VETO/PROCEED rule from spec §5 Q7.

    Conviction strictly greater than VETO_THRESHOLD (70) yields VETO;
    everything else yields PROCEED. A single source of truth for this
    decision used by both the writer (run_bear_case) and any
    downstream consumer.
    """
    return "VETO" if conviction > VETO_THRESHOLD else "PROCEED"


# ---------- freshness ----------


def _read_existing_bear_last_verified(path: Path) -> Optional[_dt.date]:
    """Return the existing file's `bear-last-verified` date, or None.

    Malformed values fall through to None (treated as not-fresh) so
    hand-edit corruption never prevents a refresh from writing a clean
    bear-case -- same resilience pattern as invest_thesis.

    Codex round-4 MEDIUM: check datetime.datetime FIRST because
    datetime IS-A date in Python -- without the ordering, a YAML-
    parsed timestamp (`2026-04-19T00:00:00Z`) would be returned as a
    datetime and later `date - datetime` arithmetic in _is_fresh would
    raise TypeError. Normalise to .date() on ingest.
    """
    if not path.exists():
        return None
    try:
        fm = sf.parse(path.read_bytes())
    except ValueError:
        return None
    raw = fm.get("bear-last-verified")
    if raw is None:
        return None
    if isinstance(raw, _dt.datetime):
        return raw.date()
    if isinstance(raw, _dt.date):
        return raw
    if isinstance(raw, str):
        try:
            return _dt.date.fromisoformat(raw.strip())
        except ValueError:
            return None
    return None


def _is_fresh(
    path: Path, now: _dt.date, window_days: int = FRESH_DAYS
) -> bool:
    last = _read_existing_bear_last_verified(path)
    if last is None:
        return False
    delta = (now - last).days
    return 0 <= delta <= window_days


# ---------- body formatting ----------


def _format_teach_mode_footer(
    verdict: str, conviction: int, position_size_hkd: int
) -> str:
    """Teach Mode novice/intermediate dollar/risk translation.

    Only emitted when learning_stage in {novice, intermediate} AND
    position_size_hkd is provided. Narrative is locked so downstream
    lint can recognise the footer deterministically.

    Does NOT compute a position size -- Q3 validator-isolation. The
    value is Keith-provided via the CLI; this helper just translates
    the verdict into a plain-English action note referencing it.
    """
    if verdict == "VETO":
        return (
            f"Bear conviction {conviction}/100 with position size "
            f"HK${position_size_hkd:,}: if VETO, do NOT open the "
            f"position -- address the counterpoints above first. The "
            f"bear case structurally invalidates the thesis; sizing "
            f"smaller or widening the stop does not fix a broken "
            f"thesis."
        )
    return (
        f"Bear conviction {conviction}/100 with position size "
        f"HK${position_size_hkd:,}: if PROCEED, size for your "
        f"validator-capped max loss against the bear scenarios above "
        f"(see `execution/validators/config.yaml` position_size cap). "
        f"Treat the counterpoints as active monitoring items, not "
        f"trade invalidators."
    )


def _format_bear_section(
    now: _dt.date,
    bear_input: BearCaseInput,
    verdict: str,
    learning_stage: str,
    position_size_hkd: Optional[int],
) -> str:
    """Assemble the body section appended to existing thesis content.

    Leading blank line separates from whatever ends the prior body
    content (the cycle prompt makes this explicit). No horizontal
    rule -- the heading alone delimits.
    """
    parts: list[str] = [
        "",  # blank line separator
        f"## Bear Case ({now.isoformat()})",
        "",
        f"**Verdict:** {verdict} (conviction: {bear_input.bear_conviction})",
        "",
        "### Top counterpoints to monitor",
    ]
    for i, cp in enumerate(bear_input.bear_top_counterpoints, start=1):
        parts.append(f"{i}. {cp}")
    parts += ["", "### Invalidation scenarios"]
    for sc in bear_input.bear_invalidation_scenarios:
        parts.append(f"- {sc}")
    parts.append("")
    if (
        learning_stage in ("novice", "intermediate")
        and position_size_hkd is not None
    ):
        parts += [
            "### Why this matters for your position",
            "",
            _format_teach_mode_footer(
                verdict, bear_input.bear_conviction, position_size_hkd
            ),
            "",
        ]
    return "\n".join(parts) + "\n"


# ---------- frontmatter merge (line-level, byte-preserving) ----------


# YAML keys set by the bear-case module. Canonical order matches spec
# §2.2 example + test assertions.
BEAR_FIELDS: tuple[str, ...] = (
    "bear-last-verified",
    "bear_conviction",
    "bear_top_counterpoints",
    "bear_invalidation_scenarios",
    "bear_verdict",
)


def _find_fence_indexes(lines: list[str]) -> tuple[int, int]:
    """Return `(open_idx, close_idx)` for the YAML frontmatter fences.

    Raises ValueError when the file has no fence or the fence is
    unterminated. Mirrors invest_ship_strategy._find_fences byte-for-
    byte so frontmatter consumers across Bundle 3 + Bundle 4 agree on
    edge cases (missing first-line fence, unterminated block).
    """
    if not lines or lines[0].rstrip("\r\n").strip() != "---":
        raise ValueError(
            "file has no YAML frontmatter fence (first line must be `---`)"
        )
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n").strip() == "---":
            return 0, i
    raise ValueError(
        "unterminated YAML frontmatter (missing closing `---`)"
    )


def _is_top_level_key_line(line: str) -> bool:
    """True if `line` begins a top-level frontmatter key.

    A top-level key starts at column 0 (no leading whitespace) with
    `identifier:`. Excludes YAML list items (start with `-`), comments
    (start with `#`), and continuation lines (start with whitespace).
    """
    stripped = line.rstrip("\r\n")
    if not stripped:
        return False
    if stripped[0].isspace():
        return False
    return bool(_TOP_LEVEL_KEY_RE.match(stripped))


def _extract_top_level_key_name(line: str) -> Optional[str]:
    stripped = line.rstrip("\r\n")
    if not _is_top_level_key_line(line):
        return None
    # `key:` or `key: value` -- the key is everything before the first `:`.
    return stripped.split(":", 1)[0]


def _find_bear_block_ranges(
    lines: list[str], open_idx: int, close_idx: int
) -> list[tuple[int, int]]:
    """Locate `(start, end)` index ranges of existing bear_* top-level
    blocks in the frontmatter window `[open_idx+1, close_idx)`.

    Each range covers the key line plus its continuation lines (list
    items, indented sub-keys) through the line before the next top-level
    key (or `close_idx`). Returned in ascending order so callers can
    delete from the end inward without shifting the remaining ranges.
    """
    ranges: list[tuple[int, int]] = []
    i = open_idx + 1
    while i < close_idx:
        key = _extract_top_level_key_name(lines[i])
        if key in _BEAR_KEY_EXACT:
            start = i
            j = i + 1
            while j < close_idx and not _is_top_level_key_line(lines[j]):
                j += 1
            ranges.append((start, j))
            i = j
            continue
        i += 1
    return ranges


def _render_bear_block(
    now: _dt.date, bear_input: BearCaseInput, verdict: str, eol: str
) -> list[str]:
    """Render the 5 bear_* key lines + their values as complete lines
    ready for insertion before the closing frontmatter fence.

    YAML emission goes through yaml.safe_dump(sort_keys=False) per-key
    so list values respect our canonical `default_flow_style=False`
    (block lists). The output is then split into lines and terminated
    with the file's detected line-ending style (`eol`).

    Keys are emitted in spec §2.2 canonical order so on-disk layout
    matches the example verbatim (bear-last-verified, bear_conviction,
    bear_top_counterpoints, bear_invalidation_scenarios, bear_verdict).
    """
    block_dict: dict[str, Any] = {
        "bear-last-verified": now.isoformat(),
        "bear_conviction": bear_input.bear_conviction,
        "bear_top_counterpoints": list(bear_input.bear_top_counterpoints),
        "bear_invalidation_scenarios": list(
            bear_input.bear_invalidation_scenarios
        ),
        "bear_verdict": verdict,
    }
    yaml_text = yaml.safe_dump(
        block_dict,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    # safe_dump terminates with `\n`. Strip, split, re-add with the
    # detected file-wide EOL so the new block's line endings match the
    # surrounding file (preserves byte-preservation invariant).
    stripped = yaml_text.rstrip("\n")
    return [line + eol for line in stripped.split("\n")]


def _detect_eol(lines: list[str]) -> str:
    """Detect file-wide line-ending style by sampling the frontmatter
    fence. K2Bi repo is LF-only; the detector exists for defence in
    depth against stray CRLF files imported from Windows editors.
    """
    for line in lines:
        if line.endswith("\r\n"):
            return "\r\n"
    return "\n"


def _merge_frontmatter_bear_fields_inplace(
    content: bytes,
    now: _dt.date,
    bear_input: BearCaseInput,
    verdict: str,
) -> bytes:
    """Replace any existing bear_* frontmatter keys with the new block
    in place, leaving ALL other frontmatter lines + the body byte-
    identical.

    Invariants (guaranteed by this function + its helpers):

      1. Lines outside the frontmatter (body) are returned byte-
         identical.
      2. Non-bear frontmatter lines are returned byte-identical (no
         YAML round-trip on other keys).
      3. Existing bear_* blocks (if any) are replaced; first-run
         inserts the new block before the closing fence.
      4. Line endings match the file's detected EOL style.

    Why not dict parse + re-serialise: yaml.safe_load/safe_dump is not
    a byte-stable round-trip. Floats may reformat (`0.55` -> `0.5500`),
    strings may requote, lists may flow-vs-block shift. Cycle 5's
    Bundle 3 hook trio avoided that for strategy files by line-level
    editing status + appending fields; Bundle 4 cycle 2 uses the same
    seam here. Keeps the test assertion "byte-identical thesis fields"
    strictly true, not just parse-equal.
    """
    text = content.decode("utf-8")
    lines = text.splitlines(keepends=True)
    eol = _detect_eol(lines)
    open_idx, close_idx = _find_fence_indexes(lines)

    bear_ranges = _find_bear_block_ranges(lines, open_idx, close_idx)

    # Compute the post-removal close_idx before mutating the list.
    # Remove bear ranges from END backwards so earlier indexes stay valid.
    for start, end in reversed(bear_ranges):
        del lines[start:end]
        close_idx -= (end - start)

    # Insert the freshly-rendered bear block before the closing fence.
    new_block = _render_bear_block(now, bear_input, verdict, eol)
    lines[close_idx:close_idx] = new_block

    return "".join(lines).encode("utf-8")


def _append_bear_section_to_body(
    content_after_fm: bytes, new_section: str
) -> bytes:
    """Append `new_section` (a rendered `## Bear Case (DATE)` block) to
    the end of `content_after_fm`. Guarantees a single-LF separator
    before the section.

    `new_section` is expected to start with `\\n` (provided by
    `_format_bear_section`) so a correctly-terminated prior body is
    separated by exactly one blank line, not zero or two.
    """
    # Ensure the existing bytes end with exactly one LF so the leading
    # `\n` of `new_section` yields a single blank-line separator.
    normalised = content_after_fm.rstrip(b"\n") + b"\n"
    return normalised + new_section.encode("utf-8")


# ---------- main entry ----------


def _normalize_learning_stage(stage: str) -> str:
    """Mirror invest_thesis._normalize_learning_stage: unknown values
    fall back to `advanced` (no footer). Skills never fail because the
    dial is unset (CLAUDE.md convention)."""
    if stage in ALLOWED_LEARNING_STAGES:
        return stage
    return "advanced"


def _assert_path_within_vault(path: Path, vault_root: Path) -> None:
    """Refuse writes that escape the vault via symlinks in the path.

    Same pattern as invest_thesis._assert_path_within_vault. Uses
    resolve(strict=False) so a missing leaf does not trigger the check
    before we create the file; only existing-symlink traversal is
    interesting.
    """
    resolved = path.resolve(strict=False)
    root_resolved = vault_root.resolve(strict=False)
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise ValueError(
            f"path {path!s} resolves to {resolved!s}, outside vault "
            f"root {root_resolved!s}; refusing to write"
        ) from None


def run_bear_case(
    symbol: str,
    bear_input: BearCaseInput,
    vault_root: Path,
    *,
    refresh: bool = False,
    learning_stage: str = "advanced",
    position_size_hkd: Optional[int] = None,
    now: Optional[_dt.date] = None,
) -> BearCaseResult:
    """Merge bear-case frontmatter + append body section to the existing
    thesis at `wiki/tickers/<SYMBOL>.md` under `vault_root`.

    Validation order (fail-fast before any I/O):
        1. symbol format
        2. bear_input schema (conviction + counterpoints + scenarios)

    Thesis existence check (after validation):
        - File at `wiki/tickers/<SYMBOL>.md` MUST exist (FileNotFoundError).
        - Must have `thesis_score` field in frontmatter (ValueError).

    Freshness check (after thesis existence):
        - If existing `bear-last-verified` within FRESH_DAYS AND
          refresh is False: skip with informational BearCaseResult.
          No write; prior file bytes unchanged.

    On write:
        - Derive verdict from conviction (VETO iff > VETO_THRESHOLD).
        - Merge 5 bear_* fields into existing frontmatter (preserve all
          thesis fields exactly).
        - Append new `## Bear Case (YYYY-MM-DD)` section to existing
          body (prior sections preserved as audit trail).
        - Atomic write via sf.atomic_write_bytes.

    Args:
        symbol: uppercase ticker (validated, supports HK / share-class).
        bear_input: parsed adversarial response (5 fields schema-checked).
        vault_root: K2Bi vault root (has wiki/tickers/<SYMBOL>.md).
        refresh: force rewrite even if existing bear-case is fresh.
        learning_stage: novice | intermediate | advanced (unknown -> advanced).
        position_size_hkd: Keith-provided; NEVER skill-computed. Skipping
            this argument disables the Teach Mode footer regardless of stage.
        now: date to stamp + freshness check against. Defaults to today.

    Returns:
        BearCaseResult with `written`, `path`, `skipped_reason` on skip,
        `bear_verdict` + `bear_conviction` when a verdict was derived.

    Raises:
        ValueError: any validation failure (symbol, bear_input, vault,
            missing thesis_score).
        FileNotFoundError: thesis file missing.
    """
    # 1. Input validation (no I/O yet).
    validate_symbol(symbol)
    validate_bear_case_input(bear_input)

    if now is None:
        now = _dt.date.today()
    learning_stage = _normalize_learning_stage(learning_stage)

    # 2. Vault sanity: must be an existing directory. Matches invest_thesis.
    if not vault_root.is_dir():
        raise ValueError(
            f"vault_root {vault_root!s} is not an existing directory; "
            f"refusing to write"
        )

    ticker_path = vault_root / "wiki" / "tickers" / f"{symbol}.md"
    _assert_path_within_vault(ticker_path, vault_root)

    # 3. Thesis existence + shape check.
    if not ticker_path.exists():
        raise FileNotFoundError(
            f"no thesis found at {ticker_path}; run "
            f"/invest thesis {symbol} first"
        )
    try:
        existing_content = ticker_path.read_bytes()
    except OSError as exc:
        raise ValueError(
            f"could not read thesis file {ticker_path}: {exc}"
        ) from exc
    try:
        existing_fm = sf.parse(existing_content)
    except ValueError as exc:
        raise ValueError(
            f"thesis file {ticker_path} frontmatter parse error: {exc}"
        ) from exc
    if "thesis_score" not in existing_fm:
        raise ValueError(
            f"thesis file {ticker_path} has no thesis_score field; "
            f"run /invest thesis {symbol} first"
        )

    # 4. Freshness check.
    if not refresh and _is_fresh(ticker_path, now):
        last = _read_existing_bear_last_verified(ticker_path)
        reason: str
        if last == now:
            reason = (
                f"bear-case already run today for {symbol}; use "
                f"--refresh to force re-run"
            )
        else:
            reason = (
                f"bear-case fresh ({last.isoformat() if last else 'unknown'}); "
                f"use --refresh to force re-run"
            )
        return BearCaseResult(
            path=ticker_path,
            written=False,
            skipped_reason=reason,
            bear_verdict=existing_fm.get("bear_verdict"),
            bear_conviction=existing_fm.get("bear_conviction"),
        )

    # 5. Derive verdict + line-level frontmatter merge (byte-preserves
    #    all non-bear frontmatter lines; replaces existing bear_* blocks
    #    in place; appends new bear_* block before closing fence on
    #    first run).
    verdict = derive_verdict(bear_input.bear_conviction)

    # Write-time schema-consistency cross-check. Defence-in-depth per
    # MiniMax cycle-2 R2 finding: if the file already has a bear_verdict
    # whose value disagrees with the derived verdict from the new
    # conviction (e.g. a hand-edit set bear_verdict: PROCEED while the
    # caller supplies conviction=85 which derives VETO), we still
    # overwrite BOTH fields -- but also assert the file is currently
    # internally consistent BEFORE the rewrite so a stale inconsistency
    # surfaces here rather than staying hidden in a partial-update path.
    #
    # R2-bundle-4a-sweep (cumulative Codex, 2026-04-20): the check is
    # gated behind `refresh is False` so the sanctioned recovery path
    # works. Without this, the error message ("Run with refresh=True to
    # rewrite cleanly") pointed at a dead branch -- refresh requests
    # still hit this raise and approval stayed blocked until manual
    # frontmatter surgery. refresh=True is the operator's explicit
    # acknowledgment that the file is in a repair-worthy state; the
    # approval scanner stays strict so any leftover inconsistency is
    # caught at the gate, not here.
    if not refresh:
        existing_verdict = existing_fm.get("bear_verdict")
        existing_conviction = existing_fm.get("bear_conviction")
        if existing_verdict is not None and existing_conviction is not None:
            if (
                isinstance(existing_conviction, int)
                and 0 <= existing_conviction <= 100
            ):
                expected_existing_verdict = derive_verdict(existing_conviction)
                if existing_verdict != expected_existing_verdict:
                    raise ValueError(
                        f"{ticker_path} has inconsistent pre-existing "
                        f"bear-case state: bear_verdict={existing_verdict!r} "
                        f"but bear_conviction={existing_conviction} implies "
                        f"{expected_existing_verdict!r}. Run with refresh=True "
                        f"(or /invest bear-case {symbol} --refresh) to rewrite "
                        f"cleanly after confirming the intended verdict."
                    )

    try:
        content_after_fm = _merge_frontmatter_bear_fields_inplace(
            existing_content, now, bear_input, verdict
        )
    except ValueError as exc:
        raise ValueError(
            f"thesis file {ticker_path} frontmatter structure: {exc}"
        ) from exc

    # 6. Append new `## Bear Case (DATE)` section to end of body.
    new_section = _format_bear_section(
        now, bear_input, verdict, learning_stage, position_size_hkd
    )
    file_bytes = _append_bear_section_to_body(content_after_fm, new_section)

    # 7. Atomic write.
    sf.atomic_write_bytes(ticker_path, file_bytes)

    return BearCaseResult(
        path=ticker_path,
        written=True,
        bear_verdict=verdict,
        bear_conviction=bear_input.bear_conviction,
    )
