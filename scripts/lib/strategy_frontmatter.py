"""Shared strategy-file frontmatter helper -- Bundle 3 cycle 4.

Single parser + validator used by every enforcement point that touches a
`wiki/strategies/strategy_*.md` file:

- `.githooks/pre-commit` Checks A (status enum), B (How This Works
  non-empty), D (approved content immutability)
- `.githooks/commit-msg` strategy-transition trailer enforcement
- `.githooks/post-commit` retire-sentinel landing (via in-process
  imports, not the CLI)
- `/invest-ship --approve-strategy` Step A (cycle 5)

Having one helper closes the parity risk Cycle 3 flagged: if each hook
re-implements YAML parsing in bash, they drift on edge cases (quoted
values, multi-line lists, `: ` inside strings, CRLF endings). This is
the single seam.

Python API (called in-process):

    parse(content: bytes) -> dict[str, Any]
        YAML frontmatter as a mapping, or {} for files with no
        frontmatter fence. Raises ValueError on invalid YAML, invalid
        utf-8, or unterminated frontmatter.

    extract_status(frontmatter: dict) -> str | None
        Normalised `status` value (stripped, None if missing/empty).

    extract_how_this_works_body(content: bytes) -> str
        Body text under the `## How This Works` heading (any suffix
        allowed -- e.g. `## How This Works (Plain English)`), stripped
        of surrounding whitespace. Empty string if section missing.

    check_immutable(head_path, staged_path) -> (exit_code, msg)
        Check D: when HEAD state is `approved`, only the pure retire
        transition is permitted in the staged diff.

    check_transition(head_path, staged_path) -> (exit_code, msg, old, new)
        Validate (old_status, new_status) is in the allowed matrix
        (or body-only edit at same status).

CLI (called by bash hooks via `python3 -m scripts.lib.strategy_frontmatter`):

    status               read stdin; print status value
    validate-status      read stdin; exit 1 if not in ALLOWED_STATUSES
    how-this-works       read stdin; print section body; exit 1 if missing
    check-approved-immutable --head <p> --staged <p>
    validate-transition  --head <p> --staged <p>
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
import tempfile
import unicodedata
from pathlib import Path
from dataclasses import dataclass
from typing import Any

import yaml


FRONTMATTER_DELIM = "---"

# Spec §2.2 authoritative set. Kept in lockstep with
# `execution.strategies.types.ALLOWED_STATUSES` (verified by
# test_strategy_frontmatter.AllowedStatusesTests.test_enum_matches_loader_types).
ALLOWED_STATUSES = frozenset({"proposed", "approved", "rejected", "retired"})

# Sentinel used when a strategy file has no prior HEAD state (new file
# at this commit). Lets `(old, new)` pair lookups stay total.
NEW_FILE = "(new file)"

# Allowed (old_status, new_status) pairs. Anything else is a commit-msg
# hook rejection. Spec §2.2 transition matrix, authoritative.
ALLOWED_TRANSITIONS = frozenset(
    {
        (NEW_FILE, "proposed"),
        ("proposed", "approved"),
        ("proposed", "rejected"),
        ("approved", "retired"),
    }
)

# Fields that /invest-ship --retire-strategy is permitted to add on the
# approved -> retired transition. The status value flip is handled
# separately; these are the *new keys* that may appear in the staged
# frontmatter but not in HEAD.
RETIRE_ADDED_FIELDS = frozenset({"retired_at", "retired_reason"})


STRATEGY_FILENAME_PREFIX = "strategy_"


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Write `content` to `path` atomically via tempfile + os.replace.

    The tempfile lives in the same directory as `path` so `os.replace`
    stays on one filesystem. `f.flush()` + `os.fsync()` before replace
    guarantees the bytes hit disk before the rename swaps the inode;
    readers therefore never see a partial file even if the process is
    killed between fsync and replace (the final file keeps its prior
    content, and the temp is orphaned for janitor cleanup).

    Any exception raised during write / replace unlinks the tempfile
    before re-raising, so callers that retry on failure do not leak
    dot-prefixed temps into the target directory. `FileNotFoundError`
    on the unlink is swallowed (some failure modes leave no tempfile).

    Refuses to write through a symlink at `path`. POSIX rename(2)
    semantics mean `os.replace` on a symlinked `path` replaces the
    symlink itself (not the target), so the attack surface is minimal
    on Linux/macOS -- but defence-in-depth for future portability to
    non-standard filesystems (NFS / container overlays with differing
    symlink semantics) + easy to reason about for reviewers. The
    refusal is a ValueError so callers can surface a clear message.

    Parent directories are created on demand; callers do not need to
    mkdir upfront. Shared by Bundle 3 cycle 5 (`invest_ship_strategy`
    uses a private mirror of this helper -- same pattern, not yet
    refactored to import this one) and Bundle 4 cycle 1 onward
    (`invest_thesis`, `invest_backtest`, future Analyst-tier writers).
    """
    if path.is_symlink():
        raise ValueError(
            f"refusing to write through symlink at {path!s}; "
            f"resolve or remove the symlink first"
        )
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp.",
        dir=str(parent),
    )
    tmp_path = Path(tmp_name)
    # `fd` is initially owned by this frame. Once `os.fdopen(fd, "wb")`
    # succeeds and the `with` block enters, the file object owns the
    # fd and its __exit__ will close it. We track the handoff with
    # `fd_owned` so the except path closes fd iff fdopen raised before
    # ownership transferred (Bundle 4 R5 HIGH #2). Swallowing EBADF on
    # a double-close would also work, but explicit state is cleaner
    # and lets a reviewer see the invariant at a glance.
    fd_owned = True
    try:
        with os.fdopen(fd, "wb") as f:
            fd_owned = False
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if fd_owned:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def has_section(body: str, heading: str) -> bool:
    """Return True if `body` contains a top-level `## <heading>` section.

    Matching is case-insensitive on the heading text. To avoid
    prefix-collisions (`## Backtest Overrides Pending` matching
    `heading="Backtest Override"`), suffix characters are restricted:
    an exact match, a match followed by whitespace, or a match
    followed by an opening paren all count (so
    `## Backtest Override (2026-04-19)` still matches). A match
    followed by any other non-word character would also be safe but
    the two-allowed-suffixes rule covers every shape we've authored
    and keeps the function narrow. Closes Codex R7 R2 #3.

    Used by the Bundle 4 cycle 5 `/invest-ship --approve-strategy`
    backtest-gate override check + by `invest_thesis` for idempotent
    heading probes.
    """
    target = heading.strip().lower()
    target_sp = target + " "
    target_paren = target + "("
    for line in body.splitlines():
        # Require the heading at column 0 -- an indented `    ## foo`
        # is a code-block line, not a real section. Prior behavior
        # stripped leading whitespace first and would be satisfied by
        # a pasted snippet in fenced code. Closes Codex R7 R4 #2.
        if not line.startswith("## "):
            continue
        rest = line[3:].strip().lower()
        if rest == target:
            return True
        if rest.startswith(target_sp) or rest.startswith(target_paren):
            return True
    return False


def derive_retire_slug(source_path: str) -> str:
    """Compute the retirement-sentinel slug for a strategy file path.

    Duplicates `execution.engine.main.derive_retire_slug` verbatim so
    the hooks can compute the slug without importing the heavy engine
    module (which pulls in ib_async). Parity with the engine's copy
    is enforced by test_strategy_frontmatter.DeriveRetireSlugParity.

    The slug is the filename stem with the `strategy_` prefix stripped
    when present. Post-commit + commit-msg hooks both route through
    this function (directly in Python, or via the `retire-slug`
    CLI subcommand for bash callers) so the sentinel write path +
    trailer validation + engine read path all agree on the slug.
    """
    stem = Path(source_path).stem
    if stem.startswith(STRATEGY_FILENAME_PREFIX):
        return stem[len(STRATEGY_FILENAME_PREFIX):]
    return stem


def _try_canonical_datetime(text: str) -> str | None:
    """Return ISO isoformat for `text` if it parses as an ISO-8601
    datetime/date, else None. `Z` suffix is normalised to `+00:00`
    before parsing so UTC-marked timestamps round-trip cleanly."""
    candidate = text.strip()
    if not candidate:
        return None
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        return _dt.datetime.fromisoformat(candidate).isoformat()
    except ValueError:
        pass
    try:
        return _dt.date.fromisoformat(candidate).isoformat()
    except ValueError:
        return None


def _nfc(value: Any) -> Any:
    """Canonicalise a YAML scalar / container for content-immutability
    comparisons.

    Goals:

    1. Unicode NFC normalisation so canonically-equivalent strings
       (`é` as U+00E9 vs U+0065 U+0301) compare equal.
    2. Type coercion so scalars YAML round-trips to the same string
       (`0.01` float vs `"0.01"` quoted string) compare equal. R2
       MiniMax finding.
    3. Datetime normalisation so a datetime value and its ISO-8601
       string form compare equal (`approved_at: 2026-04-19T10:00:00Z`
       unquoted vs quoted). R3 MiniMax finding; covers the common
       reformat-to-add-quotes case that would otherwise trip Check D.

    Nested dicts / lists / tuples recurse. `None` becomes `""` so a
    missing key (rendered as `foo:`) and an explicit null compare
    equal. datetime/date objects are normalised via `isoformat()`.
    Strings that look like ISO-8601 datetimes/dates are parsed and
    also normalised to isoformat. Everything else is str()-ified
    then NFC-normalised.
    """
    if isinstance(value, dict):
        return {k: _nfc(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_nfc(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_nfc(v) for v in value)
    if value is None:
        return ""
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, str):
        dt_form = _try_canonical_datetime(value)
        if dt_form is not None:
            return dt_form
        return unicodedata.normalize("NFC", value)
    return unicodedata.normalize("NFC", str(value))


# ---------- forward-guidance check (Phase 3.8.6 MVP-3) ----------


@dataclass(frozen=True)
class ThresholdedMetric:
    """One thresholded metric in a strategy's bucket-rule logic."""

    metric: str
    locked_threshold_text: str
    guide_source_text: str
    guide_range_text: str
    sits_inside_guide: bool
    operator_note: str | None = None


@dataclass(frozen=True)
class ForwardGuidanceCheck:
    """Aggregate forward-guidance check block for a strategy spec."""

    completed_at: str
    status: str
    override_reason: str | None = None
    waive_reason: str | None = None
    thresholded_metrics: list[ThresholdedMetric] = None  # type: ignore[assignment]


# Minimum length for override / waive reasons (same as MVP-2).
MIN_FGC_REASON_LEN = 20

# Allowed aggregate statuses.
ALLOWED_FGC_STATUSES = frozenset({"pass", "override", "waive"})


def _coerce_bool(raw: Any) -> bool:
    """Accept only real booleans (not strings like 'true')."""
    if isinstance(raw, bool):
        return raw
    raise ValueError(f"expected bool, got {type(raw).__name__}")


def _require_str(raw: Any, path: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{path} must be a non-empty string, got {raw!r}")
    return raw.strip()


def extract_forward_guidance_check(
    frontmatter: dict[str, Any],
) -> ForwardGuidanceCheck | None:
    """Parse the ``forward_guidance_check:`` block from parsed YAML frontmatter.

    Returns ``None`` when the block is absent (the validator handles
    missing-block as a refusal; the parser is permissive). Raises
    ``ValueError`` if the block is structurally malformed (wrong types,
    missing required sub-fields).
    """
    raw = frontmatter.get("forward_guidance_check")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(
            f"forward_guidance_check must be a mapping, got {type(raw).__name__}"
        )

    status = _require_str(raw.get("status"), "forward_guidance_check.status")
    completed_at = _require_str(
        raw.get("completed_at"), "forward_guidance_check.completed_at"
    )

    # Validate completed_at is parseable ISO-8601.
    try:
        _dt.datetime.fromisoformat(completed_at)
    except ValueError as exc:
        raise ValueError(
            f"forward_guidance_check.completed_at must be ISO-8601 datetime, "
            f"got {completed_at!r}: {exc}"
        ) from exc

    override_reason = raw.get("override_reason")
    if override_reason is not None and not isinstance(override_reason, str):
        raise ValueError(
            f"forward_guidance_check.override_reason must be a string or null, "
            f"got {type(override_reason).__name__}"
        )

    waive_reason = raw.get("waive_reason")
    if waive_reason is not None and not isinstance(waive_reason, str):
        raise ValueError(
            f"forward_guidance_check.waive_reason must be a string or null, "
            f"got {type(waive_reason).__name__}"
        )

    raw_metrics = raw.get("thresholded_metrics")
    if raw_metrics is None:
        raise ValueError(
            "forward_guidance_check.thresholded_metrics is required"
        )
    if not isinstance(raw_metrics, list):
        raise ValueError(
            f"forward_guidance_check.thresholded_metrics must be a list, "
            f"got {type(raw_metrics).__name__}"
        )

    metrics: list[ThresholdedMetric] = []
    for idx, entry in enumerate(raw_metrics):
        if not isinstance(entry, dict):
            raise ValueError(
                f"forward_guidance_check.thresholded_metrics[{idx}] must be a "
                f"mapping, got {type(entry).__name__}"
            )
        metric = _require_str(
            entry.get("metric"),
            f"forward_guidance_check.thresholded_metrics[{idx}].metric",
        )
        locked_threshold_text = _require_str(
            entry.get("locked_threshold_text"),
            f"forward_guidance_check.thresholded_metrics[{idx}].locked_threshold_text",
        )
        guide_source_text = _require_str(
            entry.get("guide_source_text"),
            f"forward_guidance_check.thresholded_metrics[{idx}].guide_source_text",
        )
        guide_range_text = _require_str(
            entry.get("guide_range_text"),
            f"forward_guidance_check.thresholded_metrics[{idx}].guide_range_text",
        )
        try:
            sits_inside_guide = _coerce_bool(entry.get("sits_inside_guide"))
        except ValueError as exc:
            raise ValueError(
                f"forward_guidance_check.thresholded_metrics[{idx}].sits_inside_guide: "
                f"{exc}"
            ) from exc
        operator_note = entry.get("operator_note")
        if operator_note is not None and not isinstance(operator_note, str):
            raise ValueError(
                f"forward_guidance_check.thresholded_metrics[{idx}].operator_note "
                f"must be a string or null, got {type(operator_note).__name__}"
            )
        metrics.append(
            ThresholdedMetric(
                metric=metric,
                locked_threshold_text=locked_threshold_text,
                guide_source_text=guide_source_text,
                guide_range_text=guide_range_text,
                sits_inside_guide=sits_inside_guide,
                operator_note=operator_note,
            )
        )

    return ForwardGuidanceCheck(
        completed_at=completed_at,
        status=status,
        override_reason=override_reason,
        waive_reason=waive_reason,
        thresholded_metrics=metrics,
    )


def validate_forward_guidance_check(fgc: ForwardGuidanceCheck | None) -> None:
    """Enforce the MVP-3 validator matrix. Raises ``ValueError`` with a
    structured message naming which rule failed.

    Matrix:
      - status="pass": all metrics must have sits_inside_guide=false
      - status="override": any inside-guide accepted; override_reason >= 20 required
      - status="waive": empty list OK; waive_reason >= 20 required
      - missing block, malformed fields, or missing reasons → raise
    """
    if fgc is None:
        raise ValueError(
            "forward_guidance_check block is missing from strategy frontmatter; "
            "required for all new approvals per L-2026-04-27-005"
        )

    if fgc.status not in ALLOWED_FGC_STATUSES:
        raise ValueError(
            f"forward_guidance_check.status={fgc.status!r} not in allowed enum "
            f"{sorted(ALLOWED_FGC_STATUSES)}"
        )

    if fgc.status == "pass":
        for tm in fgc.thresholded_metrics:
            if tm.sits_inside_guide:
                raise ValueError(
                    f"forward_guidance_check.status == 'pass' but "
                    f"thresholded_metric '{tm.metric}' has sits_inside_guide=true"
                )
        return

    if fgc.status == "override":
        reason = fgc.override_reason or ""
        if len(reason) < MIN_FGC_REASON_LEN:
            raise ValueError(
                f"forward_guidance_check.status == 'override' requires "
                f"override_reason >= {MIN_FGC_REASON_LEN} chars, got {len(reason)}"
            )
        return

    if fgc.status == "waive":
        reason = fgc.waive_reason or ""
        if len(reason) < MIN_FGC_REASON_LEN:
            raise ValueError(
                f"forward_guidance_check.status == 'waive' requires "
                f"waive_reason >= {MIN_FGC_REASON_LEN} chars, got {len(reason)}"
            )
        non_waiveable = [
            tm for tm in fgc.thresholded_metrics
            if tm.guide_range_text.strip().lower() != "no quantitative guide given"
        ]
        if non_waiveable:
            raise ValueError(
                f"forward_guidance_check.status == 'waive' requires either an empty "
                f"thresholded_metrics list OR every entry's guide_range_text to be "
                f"'no quantitative guide given'; found {len(non_waiveable)} entries "
                f"with quantitative guide: {[tm.metric for tm in non_waiveable]}"
            )
        return


# ---------- parsing ----------


def parse(content: bytes) -> dict[str, Any]:
    """Parse YAML frontmatter from the given file contents.

    Returns an empty dict when the file has no frontmatter fence
    (nothing to enforce -- e.g. an unrelated markdown note staged by
    accident). Raises ValueError on:

    - non-utf-8 bytes (file isn't a text document)
    - unterminated `---` fence (malformed)
    - YAML syntax error inside the fence
    - top-level non-mapping YAML (frontmatter contract is a dict)
    """
    if not content:
        return {}
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"file is not valid utf-8: {exc}") from exc

    lines = text.splitlines(keepends=False)
    if not lines or lines[0].strip() != FRONTMATTER_DELIM:
        return {}
    try:
        end = lines.index(FRONTMATTER_DELIM, 1)
    except ValueError as exc:
        raise ValueError(
            "unterminated YAML frontmatter (missing closing `---`)"
        ) from exc

    frontmatter_text = "\n".join(lines[1:end])
    try:
        data = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML syntax error in frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"YAML frontmatter must be a mapping, got {type(data).__name__}"
        )
    return data


def _split_body(content: bytes) -> str:
    """Return the markdown body -- everything after the closing `---`.

    Empty string when there's no frontmatter fence (treat the whole
    document as body-less; callers that care about body content already
    handle empty). Non-utf-8 also returns empty so this helper never
    raises -- the parse() path owns utf-8 validation.
    """
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    lines = text.splitlines(keepends=False)
    if not lines or lines[0].strip() != FRONTMATTER_DELIM:
        return ""
    try:
        end = lines.index(FRONTMATTER_DELIM, 1)
    except ValueError:
        return ""
    return "\n".join(lines[end + 1 :])


def extract_status(frontmatter: dict[str, Any]) -> str | None:
    """Return the `status:` value, stripped, or None if missing/empty."""
    raw = frontmatter.get("status")
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def extract_how_this_works_body(content: bytes) -> str:
    """Return the body of the `## How This Works` section, stripped.

    Heading match is case-insensitive and allows any suffix after the
    canonical title (so `## How This Works (Plain English)` from Bundle
    1's TODO language still matches). Section ends at the next `## `
    heading or end of file. Whitespace-only bodies collapse to `""`.
    """
    body = _split_body(content)
    if not body:
        return ""
    lines = body.splitlines()
    start_idx = -1
    for i, line in enumerate(lines):
        stripped_low = line.strip().lower()
        if stripped_low.startswith("## how this works"):
            start_idx = i
            break
    if start_idx < 0:
        return ""
    out: list[str] = []
    for line in lines[start_idx + 1 :]:
        if line.strip().startswith("## "):
            break
        out.append(line)
    return "\n".join(out).strip()


# ---------- hook-facing checks ----------


def _read(path: Path) -> bytes:
    return path.read_bytes() if path.exists() else b""


def _bucket_status(status: str | None, file_has_content: bool) -> str:
    """Map status to the bucket used by the transition matrix.

    `None` + no-content = NEW_FILE. `None` + content-exists means
    malformed frontmatter that the parser didn't reject -- treat as
    NEW_FILE so the matrix rejection surfaces as a missing-transition
    error rather than a cryptic type error.
    """
    if status is None:
        return NEW_FILE if not file_has_content else NEW_FILE
    return status


def check_immutable(head_path: Path, staged_path: Path) -> tuple[int, str]:
    """Pre-commit Check D -- approved files are content-immutable.

    Returns `(exit_code, stderr_message)`. `exit_code=0` means the
    check passes (either HEAD wasn't approved, or the staged diff is a
    pure retire transition). `exit_code=1` with a descriptive message
    means the staged diff violates the approval lockdown.

    Approved state invariants enforced:

    1. HEAD frontmatter keys present in staged (none removed)
    2. Only `retired_at` + `retired_reason` may be added in staged
    3. All HEAD keys other than `status` byte-equal between HEAD/staged
    4. Body after frontmatter byte-identical

    The status flip itself (approved -> retired) is the permitted
    change; forbidden status changes from `approved` (e.g. back to
    `proposed`) also fail here even though commit-msg owns the
    transition matrix -- belt-and-braces.
    """
    head = _read(head_path)
    staged = _read(staged_path)

    try:
        head_fm = parse(head)
    except ValueError as exc:
        return 1, f"head frontmatter parse error: {exc}"

    head_status = extract_status(head_fm)
    if not head or head_status != "approved":
        # Either a new file or a non-approved HEAD; Check D does not
        # apply -- other checks govern this space.
        return 0, ""

    # HEAD is approved. A byte-identical staged blob is trivially
    # immutable (mtime bumped but no real diff staged).
    if head == staged:
        return 0, ""

    try:
        staged_fm = parse(staged)
    except ValueError as exc:
        return 1, f"staged frontmatter parse error: {exc}"

    staged_status = extract_status(staged_fm)
    head_keys = set(head_fm.keys())
    staged_keys = set(staged_fm.keys())
    removed = head_keys - staged_keys
    added = staged_keys - head_keys
    changed = sorted(
        k for k in head_keys & staged_keys if head_fm[k] != staged_fm[k]
    )
    head_body = _split_body(head)
    staged_body = _split_body(staged)

    if staged_status == "retired":
        # Pure retire transition is the one permitted staged diff.
        if removed:
            return 1, (
                f"approved->retired must not remove frontmatter keys; "
                f"removed: {sorted(removed)}"
            )
        extraneous = added - RETIRE_ADDED_FIELDS
        if extraneous:
            return 1, (
                f"approved->retired may only add "
                f"{sorted(RETIRE_ADDED_FIELDS)} fields; got {sorted(extraneous)}"
            )
        # Unicode NFC normalisation so a canonically-equivalent
        # re-encoding of a non-ASCII value is not reported as a change.
        non_status_changed = [
            k for k in changed if k != "status" and _nfc(head_fm[k]) != _nfc(staged_fm.get(k))
        ]
        if non_status_changed:
            key = non_status_changed[0]
            return 1, (
                f"approved->retired must not change frontmatter field "
                f"{key!r} (head={head_fm[key]!r}, "
                f"staged={staged_fm.get(key)!r})"
            )
        if _nfc(head_body) != _nfc(staged_body):
            return 1, (
                "approved->retired must not change the body (markdown "
                "after the closing `---` fence). Body changes require "
                "retire + new proposed draft."
            )
        return 0, ""

    # staged_status is not retired but HEAD is approved and the file
    # differs. Name the specific delta so Keith sees exactly what the
    # hook caught. Value comparisons use NFC normalisation to avoid
    # false positives on canonically-equivalent Unicode re-encodings.
    nfc_changed = [
        k for k in changed if _nfc(head_fm[k]) != _nfc(staged_fm.get(k))
    ]
    body_changed = _nfc(head_body) != _nfc(staged_body)
    # If the NFC-normalised structure matches on both sides, the byte
    # diff is a Unicode re-encoding only -- not a real content change.
    # Treat as immutable-compliant.
    if not removed and not added and not nfc_changed and not body_changed:
        return 0, ""
    parts: list[str] = []
    if removed:
        parts.append(f"removed keys {sorted(removed)}")
    if added:
        parts.append(f"added keys {sorted(added)}")
    if nfc_changed:
        parts.append(f"changed keys {nfc_changed}")
    if body_changed:
        parts.append("body changed")
    delta = "; ".join(parts) if parts else "bytes differ"
    return 1, (
        f"approved strategy has post-approval modifications "
        f"(staged status={staged_status!r}; {delta}). Approved files "
        f"are content-immutable except for retirement. Use "
        f"`/invest-ship --retire-strategy` first, then create a new "
        f"proposed draft for revisions."
    )


def check_transition(
    head_path: Path, staged_path: Path
) -> tuple[int, str, str, str]:
    """Validate the status transition implied by HEAD -> staged.

    Returns `(exit_code, stderr_msg, old_status, new_status)`.
    `exit_code` is:
        0  -- body-only edit (same status) or allowed transition
        1  -- forbidden transition
        2  -- parse error on either side (hook should fail loud)

    `old_status` / `new_status` are always returned (possibly NEW_FILE)
    so the caller can echo them to the user in error reporting.
    """
    head = _read(head_path)
    staged = _read(staged_path)
    try:
        head_fm = parse(head)
    except ValueError as exc:
        return (
            2,
            f"head frontmatter parse error: {exc}",
            NEW_FILE,
            NEW_FILE,
        )
    try:
        staged_fm = parse(staged)
    except ValueError as exc:
        return (
            2,
            f"staged frontmatter parse error: {exc}",
            NEW_FILE,
            NEW_FILE,
        )

    head_status = extract_status(head_fm) if head else None
    staged_status = extract_status(staged_fm)
    old = _bucket_status(head_status, bool(head))
    new = _bucket_status(staged_status, bool(staged))

    if old == new:
        # Body-only edit; no trailer required. Pre-commit Check D
        # decides whether the body edit is itself permitted.
        return 0, "", old, new
    if (old, new) in ALLOWED_TRANSITIONS:
        return 0, "", old, new
    return (
        1,
        (
            f"forbidden strategy status transition {old!r} -> {new!r}; "
            f"allowed: {sorted(ALLOWED_TRANSITIONS)}"
        ),
        old,
        new,
    )


# ---------- CLI ----------


def _cli_status(content: bytes) -> int:
    try:
        fm = parse(content)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    s = extract_status(fm)
    if s is not None:
        print(s)
    return 0


def _cli_validate_status(content: bytes) -> int:
    try:
        fm = parse(content)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    s = extract_status(fm)
    if s is None:
        print("error: no `status:` field in frontmatter", file=sys.stderr)
        return 1
    if s not in ALLOWED_STATUSES:
        print(
            f"error: status={s!r} not in allowed enum "
            f"{sorted(ALLOWED_STATUSES)}",
            file=sys.stderr,
        )
        return 1
    return 0


def _cli_how_this_works(content: bytes) -> int:
    body = extract_how_this_works_body(content)
    if body:
        print(body)
        return 0
    print(
        "error: missing or empty `## How This Works` section",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="strategy_frontmatter",
        description="Strategy-file frontmatter parser + hook-check CLI.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="print `status:` value from stdin")
    sub.add_parser(
        "validate-status",
        help="exit 0 if stdin's `status:` is in the allowed enum",
    )
    sub.add_parser(
        "how-this-works",
        help="print body of `## How This Works` section from stdin; "
        "exit 1 if missing",
    )
    immut = sub.add_parser(
        "check-approved-immutable",
        help="Check D: verify HEAD(approved)->staged is a pure retire "
        "transition or identical",
    )
    immut.add_argument("--head", required=True, help="HEAD-state file")
    immut.add_argument("--staged", required=True, help="staged-state file")
    trans = sub.add_parser(
        "validate-transition",
        help="validate status transition HEAD->staged matches allowed "
        "matrix; prints `old<TAB>new` on stdout",
    )
    trans.add_argument("--head", required=True, help="HEAD-state file")
    trans.add_argument("--staged", required=True, help="staged-state file")
    slug = sub.add_parser(
        "retire-slug",
        help="print the retirement-sentinel slug for a strategy file "
        "path (strips the `strategy_` prefix from the stem)",
    )
    slug.add_argument("path", help="strategy file path")
    args = parser.parse_args(argv)

    if args.cmd in {"status", "validate-status", "how-this-works"}:
        content = sys.stdin.buffer.read()
        if args.cmd == "status":
            return _cli_status(content)
        if args.cmd == "validate-status":
            return _cli_validate_status(content)
        return _cli_how_this_works(content)

    if args.cmd == "check-approved-immutable":
        code, msg = check_immutable(Path(args.head), Path(args.staged))
        if msg:
            print(msg, file=sys.stderr)
        return code

    if args.cmd == "validate-transition":
        code, msg, old, new = check_transition(Path(args.head), Path(args.staged))
        print(f"{old}\t{new}")
        if msg:
            print(msg, file=sys.stderr)
        return code

    if args.cmd == "retire-slug":
        print(derive_retire_slug(args.path))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
