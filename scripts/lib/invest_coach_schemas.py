"""invest-coach schema validators -- Phase 3.8a.

Validates frontmatter for the new vault artifacts the coach introduces:
- context_<sigid>-lived-signal.md
- vendor_provenance block on wiki/tickers/<SYMBOL>.md (when T5.5 elected)
"""

from __future__ import annotations

import datetime as _dt
from typing import Any


# ---------- lived-signal schema ----------

REQUIRED_LIVED_SIGNAL_TAGS = {"context", "lived-signal", "k2bi"}
ALLOWED_LIVED_SIGNAL_TYPES = {"lived-signal"}
ALLOWED_NARRATIVE_STATUSES = {"raw", "refined"}


def validate_lived_signal_frontmatter(frontmatter_dict: dict[str, Any]) -> None:
    """Raise ValueError if `frontmatter_dict` violates the lived-signal schema.

    Checks:
      - tags includes {context, lived-signal, k2bi}
      - date is present
      - type == 'lived-signal'
      - origin == 'keith'
      - up is present and non-empty
      - sigid is present and non-empty
      - captured_via == 'invest-coach'
      - narrative_status in {raw, refined}
    """
    fm = frontmatter_dict
    if not isinstance(fm, dict):
        raise ValueError(f"frontmatter must be a dict, got {type(fm).__name__}")

    tags = fm.get("tags")
    if not isinstance(tags, list):
        raise ValueError(f"tags must be a list, got {type(tags).__name__}")
    tag_set = set(tags)
    missing_tags = REQUIRED_LIVED_SIGNAL_TAGS - tag_set
    if missing_tags:
        raise ValueError(f"tags missing required entries: {sorted(missing_tags)}")

    if not fm.get("date"):
        raise ValueError("date is required")

    type_val = fm.get("type")
    if type_val not in ALLOWED_LIVED_SIGNAL_TYPES:
        raise ValueError(
            f"type must be one of {sorted(ALLOWED_LIVED_SIGNAL_TYPES)}, got {type_val!r}"
        )

    origin = fm.get("origin")
    if origin != "keith":
        raise ValueError(f"origin must be 'keith', got {origin!r}")

    up = fm.get("up")
    if not up or not str(up).strip():
        raise ValueError("up is required and must be non-empty")

    sigid = fm.get("sigid")
    if not sigid or not str(sigid).strip():
        raise ValueError("sigid is required and must be non-empty")

    captured_via = fm.get("captured_via")
    if captured_via != "invest-coach":
        raise ValueError(f"captured_via must be 'invest-coach', got {captured_via!r}")

    narrative_status = fm.get("narrative_status")
    if narrative_status not in ALLOWED_NARRATIVE_STATUSES:
        raise ValueError(
            f"narrative_status must be one of {sorted(ALLOWED_NARRATIVE_STATUSES)}, "
            f"got {narrative_status!r}"
        )


# ---------- vendor provenance schema ----------


def validate_vendor_provenance_frontmatter(frontmatter_dict: dict[str, Any]) -> None:
    """Raise ValueError if the `vendor_provenance:` block in frontmatter is malformed.

    Checks:
      - vendor_provenance is a dict (when present)
      - vendor is non-empty string
      - timestamp is parseable ISO-8601 datetime
      - prompt is non-empty string
      - source_set_ref is non-empty string

    The block is OPTIONAL -- this validator is meant to be called when the
    caller has already confirmed the block exists (e.g. after T5.5 election).
    """
    fm = frontmatter_dict
    if not isinstance(fm, dict):
        raise ValueError(f"frontmatter must be a dict, got {type(fm).__name__}")

    vp = fm.get("vendor_provenance")
    if vp is None:
        # Block absent is OK for this validator -- caller decides if it should
        # be present based on T5.5 election state.
        return

    if not isinstance(vp, dict):
        raise ValueError(
            f"vendor_provenance must be a mapping, got {type(vp).__name__}"
        )

    vendor = vp.get("vendor")
    if not vendor or not str(vendor).strip():
        raise ValueError("vendor_provenance.vendor is required and must be non-empty")

    timestamp = vp.get("timestamp")
    if not timestamp or not str(timestamp).strip():
        raise ValueError("vendor_provenance.timestamp is required and must be non-empty")
    try:
        _dt.datetime.fromisoformat(str(timestamp).strip())
    except ValueError as exc:
        raise ValueError(
            f"vendor_provenance.timestamp must be ISO-8601 datetime, "
            f"got {timestamp!r}: {exc}"
        ) from exc

    prompt = vp.get("prompt")
    if not prompt or not str(prompt).strip():
        raise ValueError("vendor_provenance.prompt is required and must be non-empty")

    source_set_ref = vp.get("source_set_ref")
    if not source_set_ref:
        raise ValueError(
            "vendor_provenance.source_set_ref is required and must be non-empty"
        )
    # Accept list[str] (runtime type from write_vendor_provenance) or str.
    if isinstance(source_set_ref, list):
        if not source_set_ref or not all(
            isinstance(item, str) and item.strip() for item in source_set_ref
        ):
            raise ValueError(
                "vendor_provenance.source_set_ref list must contain "
                "only non-empty strings"
            )
    elif isinstance(source_set_ref, str):
        if not source_set_ref.strip():
            raise ValueError(
                "vendor_provenance.source_set_ref is required and must be non-empty"
            )
    else:
        raise ValueError(
            "vendor_provenance.source_set_ref must be a list of strings or a string, "
            f"got {type(source_set_ref).__name__}"
        )
