"""Handlers for the planning-status propagation engine.

Each handler is a pure function taking the path of milestones.md and
returning a deterministic string. The string is what gets inserted
between matching `<!-- AUTO: <tag> -->` ... `<!-- END AUTO -->` markers
in the K2Bi-Vault planning mirror docs.

Handlers MUST be deterministic (stable input -> stable output, byte for
byte) and side-effect free. They never call datetime.now() -- date
stamps come from milestones.md table content, not wall-clock time.

The current canonical output strings match today's manual content in
the mirror docs (see scripts/lib/propagate_planning_status.py for the
fenced-block design). Adding a new tag requires (a) a new render_*
function below, (b) a HANDLERS entry, and (c) AUTO fences in the mirror
docs whose initial content equals the handler's output.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

# Final-sequence label for the m2.22 Codex full-stack review slot. The
# review row sits in the Phase 2 Bundle 5 table (not Phase 3), but the
# architect's final sequence narration places it as Phase 3.7.5 between
# 3.7 and 3.8. The actual gate phrasing is computed dynamically by
# `_m22_gate_phrase` so the rendered status always reflects the current
# state of m2.13 (the prerequisite) and m2.22 (the slot itself).
M2_22_SLOT_LABEL = "3.7.5 m2.22"


def _read_text(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def _phase3_section(text: str) -> str:
    """Extract the Phase 3 section content (between '## Phase 3 --' and the next top-level header)."""
    match = re.search(
        r"^## Phase 3 -- .*?(?=^## (?!#))",
        text,
        re.DOTALL | re.MULTILINE,
    )
    if not match:
        return ""
    return match.group()


def _bundle5_section(text: str) -> str:
    """Extract the Bundle 5 section content from milestones.md."""
    match = re.search(
        r"^### Bundle 5 -- Go Live Prep.*?(?=^### |^## (?!#))",
        text,
        re.DOTALL | re.MULTILINE,
    )
    if not match:
        return ""
    return match.group()


def _split_table_row(line: str) -> list[str]:
    """Parse a markdown table row into its cell values, stripped."""
    if not line.startswith("|"):
        return []
    parts = line.split("|")
    # Drop leading and trailing empties from the outer pipes.
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return [p.strip() for p in parts]


def _is_separator_row(line: str) -> bool:
    stripped = line.replace(" ", "").replace("|", "")
    return bool(stripped) and all(c in "-:" for c in stripped)


def _phase3_rows(text: str) -> list[dict]:
    """Walk the Phase 3 table and produce structured rows.

    Each row dict: {label, status, m2_id, raw_label, raw_milestone, raw_verification}.
    status is one of "shipped" (col2 starts with checkmark), "next" (col2
    contains hourglass + NEXT), or "pending".
    """
    section = _phase3_section(text)
    if not section:
        return []

    rows: list[dict] = []
    in_table = False
    saw_header = False
    for line in section.splitlines():
        if not line.startswith("|"):
            in_table = False
            continue
        if not saw_header:
            cells = _split_table_row(line)
            if any("Milestone" in c for c in cells) and any("Verification" in c for c in cells):
                saw_header = True
                in_table = True
                continue
            continue
        if _is_separator_row(line):
            continue
        if not in_table:
            continue
        cells = _split_table_row(line)
        if len(cells) < 3:
            continue
        col_label, col_status, col_verification = cells[0], cells[1], cells[2]

        # Extract the bare label: numeric (3.1, 3.6.5) or Q-prefixed (Q42).
        label_match = re.match(r"^\*?\*?\s*(Q\d+|\d+(?:\.\d+)*)", col_label)
        if not label_match:
            continue
        label = label_match.group(1)

        is_shipped = col_status.lstrip().startswith("✅")
        is_next = ("\U0001f7e1" in col_status) and ("NEXT" in col_status.upper())
        if is_shipped:
            status = "shipped"
        elif is_next:
            status = "next"
        else:
            status = "pending"

        # m2.X reference for NEXT rows: scan verification column.
        m2_match = re.search(r"\bm2\.\d+\b", col_verification)
        m2_id = m2_match.group(0) if m2_match else ""

        rows.append({
            "label": label,
            "status": status,
            "m2_id": m2_id,
            "raw_label": col_label,
            "raw_milestone": col_status,
            "raw_verification": col_verification,
        })
    return rows


def _bundle5_rows(text: str) -> dict[str, dict]:
    """Walk the Bundle 5 table and produce a {milestone_id: row_dict} map.

    Each row dict: {label, status, shas, raw_milestone}.
    status is "shipped" (col2 starts with checkmark) or "pending".
    shas is the list of backtick-fenced commit SHAs in display order;
    for m2.9 we keep only SHAs that appear after the second `SHIPPED`
    marker in the column so the rendered status reflects the latest
    (z.4)+(bb) follow-ups, not the original Bundle 5a base ship.
    """
    section = _bundle5_section(text)
    if not section:
        return {}

    rows: dict[str, dict] = {}
    in_table = False
    saw_header = False
    for line in section.splitlines():
        if not line.startswith("|"):
            in_table = False
            continue
        if not saw_header:
            cells = _split_table_row(line)
            if any("Milestone" in c for c in cells) and any("Status" in c for c in cells):
                saw_header = True
                in_table = True
                continue
            continue
        if _is_separator_row(line):
            continue
        if not in_table:
            continue
        cells = _split_table_row(line)
        if len(cells) < 2:
            continue
        col_label, col_status = cells[0], cells[1]
        label_match = re.match(r"^(m2\.\d+)", col_label)
        if not label_match:
            continue
        label = label_match.group(1)

        is_shipped = col_status.lstrip().startswith("✅") or "SHIPPED" in col_status.upper()

        # For m2.9, take only the SHAs in the trailing `SHIPPED` segment
        # so the rendered status reflects the (z.4)+(bb) follow-ups.
        # For all other milestones, take every backticked sha.
        sha_source = col_status
        if label == "m2.9":
            shipped_segments = re.split(r"\bSHIPPED\b", col_status)
            if len(shipped_segments) >= 3:
                # Multiple SHIPPED markers -> use everything after the
                # last one (the most-recent ship).
                sha_source = shipped_segments[-1]

        shas: list[str] = []
        for token in re.findall(r"`([^`]+)`", sha_source):
            if re.fullmatch(r"[0-9a-f]{7,}", token):
                shas.append(token)
        rows[label] = {
            "label": label,
            "status": "shipped" if is_shipped else "pending",
            "shas": shas,
            "raw_milestone": col_status,
        }
    return rows


def _collapse_shipped_main(labels: list[str]) -> str:
    """Render shipped main milestones with consecutive-numeric range collapse.

    `["3.1", "3.2", "3.3", "3.4", "3.5", "3.6", "3.6.5", "3.9"]` ->
    `"3.1-3.6, 3.6.5, 3.9"`. Non-numeric labels (e.g., Q42) are appended
    verbatim at the end. The range collapse only fuses labels of the
    form "3.X" (one decimal, integer X) with consecutive X values.
    """
    if not labels:
        return ""

    simple_pattern = re.compile(r"^3\.(\d+)$")

    runs: list[list[str]] = []
    current_run: list[str] = []
    last_simple: int | None = None

    for label in labels:
        m = simple_pattern.match(label)
        if m:
            n = int(m.group(1))
            if last_simple is not None and n == last_simple + 1:
                current_run.append(label)
            else:
                if current_run:
                    runs.append(current_run)
                current_run = [label]
            last_simple = n
        else:
            if current_run:
                runs.append(current_run)
                current_run = []
            last_simple = None
            runs.append([label])

    if current_run:
        runs.append(current_run)

    parts: list[str] = []
    for run in runs:
        if len(run) >= 2 and simple_pattern.match(run[0]) and simple_pattern.match(run[-1]):
            parts.append(f"{run[0]}-{run[-1]}")
        else:
            parts.extend(run)
    return ", ".join(parts)


def _label_sort_key(label: str) -> tuple:
    """Sort key that orders 3.1 < 3.6 < 3.6.5 < 3.7 < 3.10 numerically."""
    if label.startswith("Q"):
        # Q-labels sort after numeric labels.
        return (1, label)
    parts = tuple(int(p) for p in label.split("."))
    return (0, parts)


def render_phase3_status(milestones_md_path: Path) -> str:
    """Phase 3 milestone status snapshot (one line)."""
    text = _read_text(milestones_md_path)
    rows = _phase3_rows(text)

    if not rows:
        return "<no Phase 3 table rows parsed from milestones.md>"

    main_rows = [r for r in rows if not r["label"].startswith("Q")]
    special_rows = [r for r in rows if r["label"].startswith("Q")]

    shipped_main = sorted(
        (r["label"] for r in main_rows if r["status"] == "shipped"),
        key=_label_sort_key,
    )
    shipped_special = sorted(
        (r["label"] for r in special_rows if r["status"] == "shipped"),
        key=_label_sort_key,
    )
    next_rows = [r for r in main_rows if r["status"] == "next"]
    pending_main = sorted(
        (r["label"] for r in main_rows if r["status"] == "pending"),
        key=_label_sort_key,
    )

    shipped_segment = _collapse_shipped_main(shipped_main)
    if shipped_special:
        shipped_segment = (
            f"{shipped_segment}, {', '.join(shipped_special)}"
            if shipped_segment
            else ", ".join(shipped_special)
        )

    # Denominator derived from the parsed Phase 3 table (non-Q rows).
    # Adding a new main milestone (e.g. 3.12) automatically grows the
    # count without code edits; Q-prefixed special inserts (e.g. Q42)
    # never count toward main and are listed in the shipped
    # parenthetical only.
    main_total = len(main_rows)

    parts: list[str] = []
    parts.append(
        f"✅ {len(shipped_main)} of {main_total} main milestones shipped"
        f" ({shipped_segment})"
    )

    if next_rows:
        nl = next_rows[0]
        m2 = nl["m2_id"]
        if m2:
            parts.append(f"{nl['label']} {m2} \U0001f7e1 NEXT")
        else:
            parts.append(f"{nl['label']} \U0001f7e1 NEXT")

    m22_phrase = _m22_gate_phrase(text)
    if m22_phrase is not None:
        parts.append(f"{M2_22_SLOT_LABEL} ⏳ {m22_phrase}")

    if pending_main:
        parts.append(f"{' + '.join(pending_main)} pending")

    return "; ".join(parts)


def _m22_gate_phrase(text: str) -> str | None:
    """Derive the m2.22 gate phrasing from milestones.md state.

    Returns:
        - None if m2.22 itself has shipped: callers should drop the
          m2.22 mention from their output entirely.
        - "READY (m2.13 ✅; awaits architect greenlight)" if m2.13 has
          shipped but m2.22 has not: the gate has cleared but the
          single-pass full-stack review is still architect-coordinated,
          so the slot is not auto-runnable.
        - "gates on m2.13" if m2.13 has not yet shipped: the original
          dependency holds; m2.22 cannot start until m2.13 closes.

    Replaces the previous M2_22_GATE_DESCRIPTION constant so derived
    prose reflects current source-of-truth state rather than a frozen
    snapshot. m2.13's status is read from the Phase 3 table (the row
    whose verification column references `m2.13`); m2.22's status is
    read from the Bundle 5 table directly.
    """
    bundle5 = _bundle5_rows(text)
    if "m2.22" in bundle5 and bundle5["m2.22"]["status"] == "shipped":
        return None

    rows = _phase3_rows(text)
    m2_13_row = next((r for r in rows if r.get("m2_id") == "m2.13"), None)
    if m2_13_row and m2_13_row["status"] == "shipped":
        return "READY (m2.13 ✅; awaits architect greenlight)"

    return "gates on m2.13"


def _bundle5_sort_key(label: str) -> tuple:
    """Sort m2.X labels numerically (m2.9 < m2.19 < m2.20 < m2.22)."""
    m = re.match(r"^m2\.(\d+)$", label)
    if m:
        return (0, int(m.group(1)))
    return (1, label)


def render_bundle5_status(milestones_md_path: Path) -> str:
    """Bundle 5 milestone status snapshot (one paragraph; SHAs from milestones.md)."""
    text = _read_text(milestones_md_path)
    rows = _bundle5_rows(text)

    if not rows:
        return "<no Bundle 5 table rows parsed from milestones.md>"

    # Iterate every parsed Bundle 5 row in numeric order. Adding a new
    # m2.X milestone to the table automatically lands in the rendered
    # output without code edits; the order is derived from the label
    # itself rather than a maintained constant.
    ordered_ids = sorted(rows.keys(), key=_bundle5_sort_key)
    shipped_ids = [mid for mid in ordered_ids if rows[mid]["status"] == "shipped"]
    pending_ids = [mid for mid in ordered_ids if rows[mid]["status"] != "shipped"]
    total = len(ordered_ids)

    # Find the shipped date by scanning shipped rows for YYYY-MM-DD
    # patterns. Use the last (most-recent looking) date that appears.
    shipped_date = ""
    for mid in shipped_ids:
        date_match = re.search(
            r"\b(\d{4}-\d{2}-\d{2})\b", rows[mid]["raw_milestone"]
        )
        if date_match:
            d = date_match.group(1)
            if d > shipped_date:
                shipped_date = d

    header = f"Bundle 5 ✅ {len(shipped_ids)} of {total} SHIPPED"
    if shipped_date:
        header = f"{header} {shipped_date}"
    header = f"{header}:"

    # Render shipped milestone descriptions with their SHAs.
    shipped_parts: list[str] = []
    for mid in shipped_ids:
        shas = rows[mid]["shas"]
        sha_chunk = "+".join(f"`{s}`" for s in shas) if shas else "(no sha captured)"
        if mid == "m2.9":
            # Bundle 5a m2.9 was the standalone ship; (z.4)+(bb) tagged
            # the kill_switch_active classifier + fresh-install bootstrap
            # follow-ups. Render with the (z.4)+(bb) suffix so the line
            # matches today's manual content.
            shipped_parts.append(f"m2.9 (z.4)+(bb) {sha_chunk}")
        else:
            shipped_parts.append(f"{mid} {sha_chunk}")

    pending_parts: list[str] = []
    m22_phrase = _m22_gate_phrase(text)
    for mid in pending_ids:
        if mid == "m2.22":
            # m2.22 in pending_ids means it has not shipped yet; the
            # phrase is "gates on m2.13" pre-m2.13-ship and shifts to
            # "READY (m2.13 ✅; awaits architect greenlight)" after.
            suffix = m22_phrase if m22_phrase is not None else "LAST"
            pending_parts.append(f"{mid} ⏳ LAST, {suffix}")
        else:
            pending_parts.append(f"{mid} ⏳ pending")

    body_segments: list[str] = []
    if shipped_parts:
        body_segments.append("; ".join(shipped_parts) + ".")
    if pending_parts:
        body_segments.append(" ".join(pending_parts) + ".")

    return f"{header} " + " ".join(body_segments).rstrip()


def render_next_concrete_action(milestones_md_path: Path) -> str:
    """Phase 3 NEXT row -> one-sentence next concrete action.

    Falls back to Bundle 5 m2.22 when there is no Phase 3 🟡 NEXT row,
    since the canonical sequence places m2.22 at slot 3.7.5 (Codex
    full-stack review) between 3.7 and 3.8. Falls back further to the
    first pending Phase 3 row when m2.22 is also shipped.
    """
    text = _read_text(milestones_md_path)
    rows = _phase3_rows(text)
    next_rows = [r for r in rows if r["status"] == "next"]
    if next_rows:
        nl = next_rows[0]
        m2 = nl["m2_id"]
        # Pull the skill name from either the milestone artifact column
        # or the verification column (different milestones.md rows put
        # the artifact name in different columns; scanning both keeps
        # the handler robust to that drift).
        searchspace = f"{nl['raw_milestone']} {nl['raw_verification']}"
        skill_match = re.search(r"(invest-[a-z0-9-]+)\b", searchspace)
        skill_name = skill_match.group(1) if skill_match else ""

        head = f"Phase {nl['label']}"
        if m2:
            head = f"{head} {m2}"
        if skill_name:
            head = f"{head} {skill_name} MVP"

        return (
            f"{head} (spec drafted as kimi-handoff content; "
            f"awaiting paste-into-K2Bi-Opus to fire)"
        )

    # No 🟡 NEXT row in Phase 3. Fall back to Bundle 5 m2.22 if pending.
    bundle5 = _bundle5_rows(text)
    if "m2.22" in bundle5 and bundle5["m2.22"]["status"] != "shipped":
        gate = _m22_gate_phrase(text) or "ready"
        return (
            f"Phase {M2_22_SLOT_LABEL} Codex full-stack review "
            f"({gate}; covers Q42 + Ship 1 + Ship 2 + "
            f"Bundle 5 + m2.13 in one pass)"
        )

    # Both NEXT and m2.22 absent -> fall back to first pending Phase 3 row.
    pending_rows = sorted(
        (r for r in rows if r["status"] == "pending" and not r["label"].startswith("Q")),
        key=lambda r: _label_sort_key(r["label"]),
    )
    if pending_rows:
        nl = pending_rows[0]
        return f"Phase {nl['label']} (next pending Phase 3 milestone)"

    return "<no next action found in milestones.md Phase 3 table; check status>"


HANDLERS: dict[str, Callable[[Path], str]] = {
    "phase3-status": render_phase3_status,
    "bundle5-status": render_bundle5_status,
    "next-concrete-action": render_next_concrete_action,
}
