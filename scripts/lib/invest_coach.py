"""invest-coach -- Phase 3.8a Python helpers.

Python compute module for the invest-coach skill. The skill body owns the
multi-turn conversation; this module owns:

- T5.5 research prompt composer + vendor response ingestor
- T5.5 vendor_provenance frontmatter writer
- T6 sub-section atomic-write helper
- T7 verifier with T5.5 vendor-warning surface
- T11 forward-guidance assembler (delegates validation to strategy_frontmatter)
- T12 final-summary renderer
- Stage-advancement reflection helper with flock concurrency guard

All vault writes are atomic (tmp + os.replace).
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from scripts.lib import invest_thesis as it
from scripts.lib import strategy_frontmatter as sf


# ---------- T5.5 bulk-research-handoff ----------


DEFAULT_AHERN_QUESTIONS: tuple[str, ...] = (
    "Phase 1: What is the company's revenue split by segment, and where does pricing power come from?",
    "Phase 2: What is the competitive moat (network effects, switching costs, brand, regulation, cost advantage)?",
    "Phase 3: What are the key financial-quality metrics (margin trend, ROIC, balance-sheet health, cash conversion)?",
    "Phase 4: What are the top 3 risks and a rough valuation boundary (cheap/expensive vs history and peers)?",
)

DEFAULT_SUBSCORE_QUESTIONS: tuple[str, ...] = (
    "Thesis sub-scores (0-20 each): catalyst_clarity, asymmetry, timeline_precision, edge_identification, conviction_level.",
    "Fundamental sub-scores (0-20 each): valuation, growth, profitability, financial_health, moat_strength.",
    "Bull / Base / Bear scenario probabilities and price targets (probabilities must sum to 1.00).",
    "EV-weighted asymmetry: expected value of the position under Bull/Base/Bear/Neutral scenarios.",
)


def compose_research_prompt(
    source_set: list[str],
    ahern_questions: tuple[str, ...] | None = None,
    subscore_questions: tuple[str, ...] | None = None,
) -> str:
    """Draft a structured research prompt for an external deep-research vendor.

    The prompt references the T5 source set explicitly and asks the vendor to
    cover Ahern 4-phase questions + 5-dim thesis sub-score band questions +
    fundamental sub-score band questions + bull/bear/base evidence questions +
    EV-weighted asymmetry scenario questions.

    Args:
        source_set: List of source URLs or identifiers gathered at T5.
        ahern_questions: Optional override for the Ahern phase questions.
        subscore_questions: Optional override for the sub-score question set.

    Returns:
        A single multi-line prompt string the operator can paste into a
        deep-research vendor (Kimi DR, Perplexity, NBLM, etc.).
    """
    ahern = ahern_questions or DEFAULT_AHERN_QUESTIONS
    subscores = subscore_questions or DEFAULT_SUBSCORE_QUESTIONS

    source_block = "\n".join(f"- {url}" for url in source_set) if source_set else "(no sources provided)"

    lines = [
        "# K2Bi Research Prompt",
        "",
        "You are assisting a retail investor who operates a disciplined research pipeline.",
        "Every claim you make MUST be traceable to one of the sources below.",
        "Do NOT fabricate regulatory events, earnings dates, or management quotes.",
        "",
        "## Source set",
        source_block,
        "",
        "## Questions",
        "",
    ]
    for q in ahern:
        lines.append(f"- Ahern: {q}")
    lines.append("")
    for q in subscores:
        lines.append(f"- Sub-scores: {q}")
    lines.append("")
    lines.extend([
        "## Output format",
        "",
        "Return structured markdown with one section per question above.",
        "Cite the specific source URL for every factual claim.",
        "Flag any claim that is your own inference rather than a direct source statement.",
    ])
    return "\n".join(lines) + "\n"


def ingest_vendor_response(
    response_text: str,
    vendor_name: str,
    timestamp: str,
    prompt: str,
) -> dict[str, Any]:
    """Ingest a vendor response into draft thesis input for T6 section-by-section review.

    Every claim from the vendor output is tagged `un_verified` until T7 manual
    click-through. The returned dict contains draft sections that T6 will present
    to the operator one at a time for confirmation or correction.

    Args:
        response_text: Raw text pasted back by the operator from the vendor.
        vendor_name: Name of the vendor (e.g. 'Kimi DR', 'Perplexity').
        timestamp: ISO-8601 timestamp of when the vendor response was received.
        prompt: The prompt text that was sent to the vendor.

    Returns:
        A dict with keys: vendor_name, timestamp, prompt, sections (list of
        dicts with keys: heading, body, status='un_verified').
    """
    if not response_text or not str(response_text).strip():
        raise ValueError("response_text must be non-empty")
    if not vendor_name or not str(vendor_name).strip():
        raise ValueError("vendor_name must be non-empty")

    # Parse the response into coarse sections by ## heading
    sections: list[dict[str, Any]] = []
    current_heading = "(no heading)"
    current_lines: list[str] = []

    for line in response_text.splitlines():
        if line.startswith("## "):
            if current_lines:
                sections.append({
                    "heading": current_heading,
                    "body": "\n".join(current_lines).strip(),
                    "status": "un_verified",
                })
                current_lines = []
            current_heading = line[3:].strip()
        else:
            current_lines.append(line)

    if current_lines or sections:
        sections.append({
            "heading": current_heading,
            "body": "\n".join(current_lines).strip(),
            "status": "un_verified",
        })

    return {
        "vendor_name": vendor_name,
        "timestamp": timestamp,
        "prompt": prompt,
        "sections": sections,
    }


def write_vendor_provenance(
    thesis_path: Path,
    vendor: str,
    timestamp: str,
    prompt: str,
    source_set_ref: list[str],
) -> None:
    """Atomically write the `vendor_provenance:` block into a thesis draft's frontmatter.

    Reads the existing file at `thesis_path`, injects the vendor_provenance dict
    into the parsed frontmatter, serialises back to YAML, and writes via tmp +
    os.replace. If the file does not exist, creates a minimal draft with only
    the vendor_provenance block plus mandatory vault frontmatter keys.

    Args:
        thesis_path: Path to the draft thesis file (wiki/tickers/<SYMBOL>.md).
        vendor: Vendor name (e.g. 'Kimi DR').
        timestamp: ISO-8601 timestamp string.
        prompt: The exact prompt text sent to the vendor.
        source_set_ref: List of source URLs referenced in the prompt.
    """
    if thesis_path.is_symlink():
        raise ValueError(f"refusing to write through symlink at {thesis_path!s}")

    provenance = {
        "vendor": vendor,
        "timestamp": timestamp,
        "prompt": prompt,
        "source_set_ref": source_set_ref,
    }

    if thesis_path.exists():
        content = thesis_path.read_bytes()
        try:
            fm = sf.parse(content)
        except ValueError:
            fm = {}
        body = sf._split_body(content)
    else:
        fm = {}
        body = ""

    fm["vendor_provenance"] = provenance

    # Ensure mandatory keys exist if this is a new file
    for key, val in [
        ("tags", ["ticker", "draft", "thesis"]),
        ("date", _dt.date.today().isoformat()),
        ("type", "ticker"),
        ("origin", "k2bi-extract"),
        ("up", "[[tickers/index]]"),
    ]:
        if key not in fm:
            fm[key] = val

    file_bytes = it._serialize_file(fm, body)
    sf.atomic_write_bytes(thesis_path, file_bytes)


def enforce_vendor_must_differ(
    spot_check_vendor: str,
    vendor_provenance: dict[str, Any] | None,
) -> bool:
    """Return True if the spot-check vendor differs from the T5.5 vendor.

    Compound-bias mitigation: a vendor that produced the curated info set
    cannot also be the spot-check validator for claims from that set.

    Args:
        spot_check_vendor: Name of the vendor the operator wants to use for
            a spot-check call.
        vendor_provenance: The `vendor_provenance` dict from thesis frontmatter,
            or None if T5.5 was skipped.

    Returns:
        True if the spot-check is permitted (different vendor or no T5.5 record).
        False if the vendors match.
    """
    if not vendor_provenance:
        return True
    t55_vendor = str(vendor_provenance.get("vendor", "")).strip().lower()
    spot = str(spot_check_vendor).strip().lower()
    if not t55_vendor:
        return True
    return spot != t55_vendor


# ---------- T6 sub-section atomic-write helper ----------


SUB_SECTION_KEYS: tuple[str, ...] = (
    "phase_1_business_model",
    "phase_2_competitive_moat",
    "phase_3_financial_quality",
    "phase_4_risks_valuation",
    "sub_score_catalyst_clarity",
    "sub_score_asymmetry",
    "sub_score_timeline_precision",
    "sub_score_edge_identification",
    "sub_score_conviction_level",
    "fundamental_valuation",
    "fundamental_growth",
    "fundamental_profitability",
    "fundamental_financial_health",
    "fundamental_moat_strength",
    "asymmetry_scenarios",
)


def atomic_write_thesis_subsection(
    thesis_path: Path,
    section_key: str,
    section_content: str,
    vault_root: Path,
) -> None:
    """Atomically write one confirmed T6 sub-section into the draft thesis file.

    The draft file builds incrementally: each Ahern phase confirmed -> immediate
    atomic write; each thesis sub-score confirmed -> immediate atomic write; same
    for fundamental sub-scores and asymmetry scenarios. If the file does not yet
    exist, it is created with minimal frontmatter.

    Args:
        thesis_path: Path to draft thesis (wiki/tickers/<SYMBOL>.md).
        section_key: One of the SUB_SECTION_KEYS tuple.
        section_content: The confirmed text for this sub-section.
        vault_root: K2Bi vault root, used for path containment check.
    """
    if section_key not in SUB_SECTION_KEYS:
        raise ValueError(
            f"section_key {section_key!r} not in allowed set {SUB_SECTION_KEYS}"
        )

    from scripts.lib.invest_thesis import _assert_path_within_vault

    _assert_path_within_vault(thesis_path, vault_root)

    if thesis_path.exists():
        content = thesis_path.read_bytes()
        try:
            fm = sf.parse(content)
        except ValueError:
            fm = {}
        body = sf._split_body(content)
    else:
        fm = {
            "tags": ["ticker", "draft", "thesis"],
            "date": _dt.date.today().isoformat(),
            "type": "ticker",
            "origin": "k2bi-extract",
            "up": "[[tickers/index]]",
        }
        body = ""

    # Store confirmed sub-sections under a draft_sections key in frontmatter
    draft_sections: dict[str, Any] = fm.get("draft_sections", {})
    if not isinstance(draft_sections, dict):
        draft_sections = {}
    draft_sections[section_key] = section_content
    fm["draft_sections"] = draft_sections

    file_bytes = it._serialize_file(fm, body)
    sf.atomic_write_bytes(thesis_path, file_bytes)


# ---------- T7 verifier ----------


MIN_OVERRIDE_REASON_LEN = 20


def build_verification_result(
    claims: list[dict[str, Any]],
    vendor_provenance: dict[str, Any] | None = None,
    operator_override_reason: str | None = None,
) -> dict[str, Any]:
    """Build the verification result dict from operator-marked claims.

    Enforces the full verification matrix:
      - Every operator_check must be in {verified, refused, override, advisory}.
      - refused and override require operator_note >= MIN_OVERRIDE_REASON_LEN.
      - pass: all load-bearing claims are verified.
      - operator-override: at least one load-bearing claim is refused,
        all other load-bearing claims are verified, AND the caller supplies
        operator_override_reason >= MIN_OVERRIDE_REASON_LEN chars.
      - refuse: any load-bearing claim is refused/advisory/override/unknown,
        or the override contract is incomplete.

    The default when load-bearing claims are refused is refuse. The caller
    (coach skill body) must explicitly pass operator_override_reason to
    upgrade to operator-override.

    Args:
        claims: List of claim dicts with keys:
            claim_id, claim_text, claim_load_bearing, source_url,
            operator_check, operator_note.
        vendor_provenance: Optional T5.5 vendor provenance dict; when present,
            the result includes a `vendor_warning_surface` flag.
        operator_override_reason: If provided and >= MIN_OVERRIDE_REASON_LEN,
            and the claim pattern supports override, status becomes
            operator-override instead of refuse.

    Returns:
        A dict suitable for passing to invest_thesis.validate_verification
        after converting to the Verification dataclass.
    """
    if not claims:
        raise ValueError("claims list must not be empty")

    ALLOWED_CHECKS = {"verified", "refused", "override", "advisory"}

    # Validate per-claim checks and note lengths
    for c in claims:
        check = c.get("operator_check")
        if not isinstance(check, str) or check not in ALLOWED_CHECKS:
            raise ValueError(
                f"claim {c.get('claim_id', '?')!r} has unknown operator_check "
                f"{check!r}; must be one of {sorted(ALLOWED_CHECKS)}"
            )
        if check in {"refused", "override"}:
            note = c.get("operator_note") or ""
            if len(note) < MIN_OVERRIDE_REASON_LEN:
                raise ValueError(
                    f"claim {c.get('claim_id', '?')!r} operator_check={check!r} "
                    f"requires operator_note >= {MIN_OVERRIDE_REASON_LEN} chars, "
                    f"got {len(note)}"
                )

    verified_count = sum(
        1 for c in claims if c.get("operator_check") == "verified"
    )
    refused_count = sum(
        1 for c in claims if c.get("operator_check") == "refused"
    )
    override_count = sum(
        1 for c in claims if c.get("operator_check") == "override"
    )

    load_bearing = [c for c in claims if c.get("claim_load_bearing")]
    lb_refused = [c for c in load_bearing if c.get("operator_check") == "refused"]
    lb_advisory = [c for c in load_bearing if c.get("operator_check") == "advisory"]
    lb_override = [c for c in load_bearing if c.get("operator_check") == "override"]
    lb_non_refused = [c for c in load_bearing if c.get("operator_check") != "refused"]
    lb_non_refused_verified = all(
        c.get("operator_check") == "verified" for c in lb_non_refused
    )

    if lb_advisory:
        status = "refuse"
    elif lb_refused and lb_non_refused_verified:
        # Some load-bearing refused, but ALL others are verified.
        # This is the operator-override shape ONLY if the caller
        # explicitly provides an override reason.
        if (
            operator_override_reason
            and isinstance(operator_override_reason, str)
            and len(operator_override_reason.strip()) >= MIN_OVERRIDE_REASON_LEN
        ):
            status = "operator-override"
        else:
            status = "refuse"
    elif lb_refused:
        # Some load-bearing refused and at least one other load-bearing
        # is not verified (could be advisory, override, or refused).
        status = "refuse"
    elif lb_override:
        # Load-bearing claim marked override without being refused first
        # is a malformed state.
        status = "refuse"
    elif load_bearing and not all(c.get("operator_check") == "verified" for c in load_bearing):
        # Should not happen after the above branches, but defensive.
        status = "refuse"
    else:
        status = "pass"

    result: dict[str, Any] = {
        "completed_at": _dt.datetime.now().isoformat(),
        "claims": claims,
        "status": status,
        "override_reason": (
            operator_override_reason
            if status == "operator-override"
            else None
        ),
        "refuse_reason": None,
        "verified_count": verified_count,
        "refused_count": refused_count,
        "override_claim_count": override_count,
        "vendor_warning_surface": bool(vendor_provenance),
    }

    if status == "refuse":
        bad_ids = [
            c["claim_id"] for c in load_bearing
            if c.get("operator_check") in {"refused", "advisory", "override"}
        ] or [c["claim_id"] for c in claims if c.get("operator_check") not in ALLOWED_CHECKS]
        result["refuse_reason"] = (
            f"Load-bearing claims {bad_ids} are not fully verified. "
            f"Correct the info set or provide override reason >= {MIN_OVERRIDE_REASON_LEN} chars."
        )

    return result


def surface_vendor_warning(vendor_provenance: dict[str, Any] | None) -> str:
    """Return the T7 entry warning text when T5.5 was elected.

    Args:
        vendor_provenance: The vendor_provenance dict from thesis frontmatter.

    Returns:
        Warning text string, or empty string if T5.5 was skipped.
    """
    if not vendor_provenance:
        return ""
    vendor = vendor_provenance.get("vendor", "unknown vendor")
    return (
        f"This thesis was drafted from '{vendor}' deep research. "
        f"The verification gate that follows exists because vendor output "
        f"without primary-source verification is the CALX failure mode "
        f"(L-2026-04-30-001). Do not skip this turn."
    )


# ---------- T11 forward-guidance assembler ----------


def assemble_forward_guidance_check(
    thresholded_metrics: list[dict[str, Any]],
    status: str,
    override_reason: str | None = None,
    waive_reason: str | None = None,
) -> sf.ForwardGuidanceCheck:
    """Assemble a ForwardGuidanceCheck block and run it through the MVP-3 validator.

    Args:
        thresholded_metrics: List of metric dicts with keys:
            metric, locked_threshold_text, guide_source_text,
            guide_range_text, sits_inside_guide, operator_note (optional).
        status: 'pass', 'override', or 'waive'.
        override_reason: Required when status == 'override'.
        waive_reason: Required when status == 'waive'.

    Returns:
        A validated ForwardGuidanceCheck dataclass.

    Raises:
        ValueError: If the assembled block fails validate_forward_guidance_check.
    """
    metrics = [
        sf.ThresholdedMetric(
            metric=m["metric"],
            locked_threshold_text=m["locked_threshold_text"],
            guide_source_text=m["guide_source_text"],
            guide_range_text=m["guide_range_text"],
            sits_inside_guide=bool(m["sits_inside_guide"]),
            operator_note=m.get("operator_note"),
        )
        for m in thresholded_metrics
    ]

    fgc = sf.ForwardGuidanceCheck(
        completed_at=_dt.datetime.now().isoformat(),
        status=status,
        override_reason=override_reason,
        waive_reason=waive_reason,
        thresholded_metrics=metrics,
    )

    sf.validate_forward_guidance_check(fgc)
    return fgc


# ---------- T12 final-summary renderer ----------


def render_final_summary(
    sigid: str,
    symbol: str,
    theme_slug: str,
    verification_status: str,
    forward_guidance_status: str,
    overrides: list[dict[str, Any]],
    vendor_provenance: dict[str, Any] | None = None,
) -> str:
    """Render the T12 final summary text including D5 and D10 visibility.

    Args:
        sigid: The lived signal ID.
        symbol: Ticker symbol.
        theme_slug: Macro theme slug.
        verification_status: MVP-2 aggregate status ('pass', 'operator-override', 'refuse').
        forward_guidance_status: MVP-3 aggregate status ('pass', 'override', 'waive').
        overrides: List of override dicts with keys:
            gate (e.g. 'MVP-2' or 'MVP-3'), claim_id or threshold_name,
            original_verdict, override_reason, categorical_reason.
        vendor_provenance: Optional T5.5 vendor provenance dict.

    Returns:
        Multi-line summary string for T12 presentation.
    """
    lines = [
        f"# Coach Final Summary: {sigid}",
        "",
        f"- **Lived signal:** [[context_{sigid}-lived-signal]]",
        f"- **Theme:** [[theme_{theme_slug}]]",
        f"- **Ticker:** [[{symbol}]]",
        f"- **MVP-2 verification:** {verification_status}",
        f"- **MVP-3 forward guidance:** {forward_guidance_status}",
    ]

    if vendor_provenance:
        vendor = vendor_provenance.get("vendor", "unknown")
        lines.append("")
        lines.append(
            f"- **Vendor source (T5.5):** {vendor} -- "
            f"deep research draft verified manually at T7"
        )

    lines.append("")
    lines.append("## Pipeline overrides taken")
    if overrides:
        for ov in overrides:
            gate = ov.get("gate", "unknown")
            name = ov.get("claim_id") or ov.get("threshold_name", "unknown")
            reason = ov.get("override_reason", "")
            cat = ov.get("categorical_reason", "")
            lines.append(
                f"- **{gate}** | {name} | "
                f"original={ov.get('original_verdict', '?')} | "
                f"reason={reason!r} | category={cat!r}"
            )
    else:
        lines.append("No overrides taken; pipeline ran clean.")

    lines.append("")
    lines.append(
        "Next step: run `/invest-ship --approve-strategy <slug>` to lock the strategy."
    )
    lines.append("")
    lines.append(
        "Coach falls silent now. The approval gate will evaluate pass or refuse."
    )

    return "\n".join(lines) + "\n"


# ---------- Stage-advancement reflection (D8) ----------


_ACTIVE_RULES_PATH = Path("K2Bi-Vault/System/memory/active_rules.md")
_LEARNING_STAGE_RE = re.compile(r"^learning-stage:\s*(\S+)", re.IGNORECASE | re.MULTILINE)
_ALLOWED_STAGES = ("novice", "intermediate", "advanced")


def _resolve_active_rules_path(vault_root: Path) -> Path:
    """Return the absolute path to active_rules.md."""
    # Prefer the explicit vault_root if provided; fallback to canonical path.
    return vault_root / "System" / "memory" / "active_rules.md"


def read_learning_stage(vault_root: Path) -> str:
    """Read the current learning-stage dial from active_rules.md.

    Returns 'novice' if the file does not exist or the dial is missing.
    """
    path = _resolve_active_rules_path(vault_root)
    if not path.exists():
        return "novice"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "novice"
    match = _LEARNING_STAGE_RE.search(text)
    if match:
        stage = match.group(1).strip().lower()
        if stage in _ALLOWED_STAGES:
            return stage
    return "novice"


def suggest_stage_advancement(
    vault_root: Path,
    explained_concepts: list[str],
) -> dict[str, Any]:
    """Suggest a learning-stage dial flip based on operator self-explanation.

    Args:
        vault_root: K2Bi vault root.
        explained_concepts: Distinct concepts the operator explained back without
            coach explanation during the session.

    Returns:
        Dict with keys:
            current_stage, suggested_stage, threshold_met (bool), concept_count.
    """
    current = read_learning_stage(vault_root)
    count = len(set(explained_concepts))
    threshold_met = count >= 3

    stage_index = {s: i for i, s in enumerate(_ALLOWED_STAGES)}
    current_idx = stage_index.get(current, 0)
    suggested_idx = min(current_idx + 1, len(_ALLOWED_STAGES) - 1)
    suggested = _ALLOWED_STAGES[suggested_idx]

    return {
        "current_stage": current,
        "suggested_stage": suggested,
        "threshold_met": threshold_met and suggested != current,
        "concept_count": count,
    }


def capture_coach_rejection(
    vault_root: Path,
    sigid: str,
    turn_id: str,
    rejected_framing: str,
    operator_correction: str,
) -> Path:
    """Write a coach rejection event to the invest-feedback raw queue.

    Path: K2Bi-Vault/raw/coach-feedback/<sigid>_<turn>_rejected.md
    Frontmatter includes sigid, turn_id, rejected_framing, operator_correction.
    Atomic write via tmp + os.replace.

    Args:
        vault_root: K2Bi vault root.
        sigid: Lived signal ID.
        turn_id: Turn where rejection occurred (e.g. 'T2', 'T6').
        rejected_framing: The coach-generated text that was rejected.
        operator_correction: The operator's corrected framing.

    Returns:
        Path to the written file.
    """
    if not sigid or not str(sigid).strip():
        raise ValueError("sigid must be non-empty")
    if not turn_id or not str(turn_id).strip():
        raise ValueError("turn_id must be non-empty")
    # Strict filename allowlist + null-byte guard (no leading hyphen)
    _ALLOWED_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]*$")
    for val, name in ((sigid, "sigid"), (turn_id, "turn_id")):
        if "\x00" in str(val):
            raise ValueError(f"{name} contains null bytes: {val!r}")
        if not _ALLOWED_ID_RE.match(str(val)):
            raise ValueError(
                f"{name} contains disallowed characters: {val!r}; "
                f"must match {_ALLOWED_ID_RE.pattern!r}"
            )
    out_dir = vault_root / "raw" / "coach-feedback"
    out_path = out_dir / f"{sigid}_{turn_id}_rejected.md"
    # Resolve and verify containment BEFORE any filesystem mutation
    it._assert_path_within_vault(out_path, vault_root)
    out_dir.mkdir(parents=True, exist_ok=True)

    frontmatter = {
        "tags": ["coach-feedback", "rejection", sigid, turn_id],
        "date": _dt.date.today().isoformat(),
        "type": "coach-feedback",
        "origin": "keith",
        "up": "[[index]]",
        "sigid": sigid,
        "turn_id": turn_id,
    }
    body = (
        f"## Rejected framing ({turn_id})\n\n"
        f"> {rejected_framing}\n\n"
        f"## Operator correction\n\n"
        f"> {operator_correction}\n"
    )
    file_bytes = it._serialize_file(frontmatter, body)
    sf.atomic_write_bytes(out_path, file_bytes)
    return out_path


def write_learning_stage_dial(
    vault_root: Path,
    new_stage: str,
    expected_current: str,
) -> bool:
    """Atomically update the learning-stage dial in active_rules.md with a
    compare-and-swap (CAS) guard.

    Acquires an exclusive flock, reads the current value under the lock,
    confirms it matches `expected_current`, writes the new value, and
    releases the lock. Returns False if the current value differs from
    `expected_current` (another session changed it concurrently).

    Args:
        vault_root: K2Bi vault root.
        new_stage: One of {'novice', 'intermediate', 'advanced'}.
        expected_current: The stage value this session believes is current.

    Returns:
        True if the write succeeded, False if CAS detected a concurrent change.
    """
    if new_stage not in _ALLOWED_STAGES:
        raise ValueError(
            f"new_stage must be one of {_ALLOWED_STAGES}, got {new_stage!r}"
        )
    if expected_current not in _ALLOWED_STAGES:
        raise ValueError(
            f"expected_current must be one of {_ALLOWED_STAGES}, "
            f"got {expected_current!r}"
        )

    path = _resolve_active_rules_path(vault_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    lock_path = path.parent / ".active_rules.md.lock"
    with open(lock_path, "a+") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            # Truncate lock file while holding the lock to prevent unbounded growth.
            # Crash here leaves an empty lock file, which is harmless for flock.
            lock_f.truncate(0)
            # Authoritative read under lock (inlined to ensure the read happens
            # while the flock is held, avoiding any helper that might re-resolve
            # the path independently).
            if path.exists():
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    text = ""
            else:
                text = ""
            match = _LEARNING_STAGE_RE.search(text)
            if match:
                under_lock = match.group(1).strip().lower()
                if under_lock not in _ALLOWED_STAGES:
                    under_lock = "novice"
            else:
                under_lock = "novice"
            if under_lock != expected_current:
                return False

            if not text:
                text = (
                    "# Active Rules\n\n"
                    "Cap 12 LRU. Least-reinforced-in-last-30-days demotes on overflow.\n"
                )

            # Replace or insert the learning-stage line
            if _LEARNING_STAGE_RE.search(text):
                new_text = _LEARNING_STAGE_RE.sub(
                    f"learning-stage: {new_stage}", text
                )
            else:
                new_text = text.rstrip("\n") + f"\n\nlearning-stage: {new_stage}\n"

            sf.atomic_write_bytes(path, new_text.encode("utf-8"))
            return True
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
