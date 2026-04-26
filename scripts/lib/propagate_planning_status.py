"""Propagate Phase-3 / Bundle-5 status snapshots into K2Bi-Vault planning docs.

The propagation engine scans every `*.md` under
`K2Bi-Vault/wiki/planning/` for `<!-- AUTO: <tag> --> ... <!-- END AUTO -->`
fences, dispatches each fence's content to the matching handler in
`scripts.lib.propagate_handlers.HANDLERS`, and atomically rewrites the
file when any fence body changes. Unknown tags log a warning and skip;
handler exceptions log an error and exit non-zero so the post-build
hook fails loud.

Source-of-truth read path: handlers parse `K2Bi-Vault/wiki/planning/milestones.md`.
Mirror docs (roadmap.md, index.md, upcoming-sessions.md, phase-2-bundles.md,
plus regenerated parts of milestones.md itself) hold derived snippets
inside AUTO fences.

CLI:
    python3 -m scripts.lib.propagate_planning_status

Exit codes:
    0  -- success (zero or more fences updated cleanly)
    1  -- a handler raised an exception
    2  -- no planning docs found at the resolved vault path

The engine derives the vault path as a sibling of the K2Bi git repo
(parent-of-`git rev-parse --show-toplevel` / `K2Bi-Vault`). Override
with the `K2BI_VAULT_ROOT` env var when running outside that layout
(useful for tests).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from scripts.lib.propagate_handlers import HANDLERS
from scripts.lib.strategy_frontmatter import atomic_write_bytes

logger = logging.getLogger("propagate_planning_status")

# AUTO fence regex: matches the opening and closing markers and captures
# the tag plus the body. `re.DOTALL` lets `.*?` cross newlines; the
# non-greedy `.*?` ensures multiple fences in one file are treated as
# distinct matches rather than one giant span. `re.IGNORECASE` accepts
# mixed-case tag names so that auto-formatters or inadvertent capitalization
# (e.g. `<!-- AUTO: Phase3-Status -->`) do not silently drop fences out of
# the dispatch table -- the tag is canonicalized to lowercase before
# HANDLERS lookup so the registered names stay lowercase.
FENCE_RE = re.compile(
    r"<!--\s*AUTO:\s*([a-zA-Z0-9_-]+)\s*-->(.*?)<!--\s*END AUTO\s*-->",
    re.DOTALL,
)


def _resolve_vault_root() -> Path:
    """Resolve the K2Bi-Vault root.

    Order of precedence:
        1. K2BI_VAULT_ROOT env override
        2. <repo>/../K2Bi-Vault where <repo> = git rev-parse --show-toplevel
        3. ~/Projects/K2Bi-Vault as a final fallback (logged loudly)

    The hardcoded fallback exists so a non-standard checkout does not
    silently no-op; it logs a WARNING when reached so an operator who
    ends up there sees that path resolution drifted off the expected
    layout. Set K2BI_VAULT_ROOT explicitly to opt out of the fallback.
    """
    env = os.environ.get("K2BI_VAULT_ROOT")
    if env:
        return Path(env).expanduser()

    try:
        repo_root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if repo_root:
            sibling = Path(repo_root).parent / "K2Bi-Vault"
            if sibling.exists():
                return sibling
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    fallback = Path.home() / "Projects" / "K2Bi-Vault"
    logger.warning(
        "vault root resolved via hardcoded fallback %s; set K2BI_VAULT_ROOT "
        "explicitly to silence this warning",
        fallback,
    )
    return fallback


def _planning_docs(vault_root: Path) -> list[Path]:
    """Return every *.md beneath vault_root/wiki/planning/, recursive."""
    base = vault_root / "wiki" / "planning"
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*.md") if p.is_file())


def _milestones_path(vault_root: Path) -> Path:
    return vault_root / "wiki" / "planning" / "milestones.md"


def _replace_fences(text: str, milestones_md: Path) -> tuple[str, int, int]:
    """Apply HANDLERS to every AUTO fence in `text`.

    Returns (new_text, n_replaced, n_unknown). `n_replaced` counts fences
    whose body was rewritten (or re-confirmed identical, which still
    counts because the handler ran). `n_unknown` counts unknown-tag
    fences that were skipped untouched.
    """
    n_replaced = 0
    n_unknown = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal n_replaced, n_unknown
        tag = match.group(1).lower()
        old_body = match.group(2)
        handler = HANDLERS.get(tag)
        if handler is None:
            n_unknown += 1
            logger.warning(
                "skip unknown AUTO tag %r (no handler in scripts.lib.propagate_handlers.HANDLERS)",
                tag,
            )
            return match.group(0)
        new_body_raw = handler(milestones_md)
        # Preserve the source fence's wrapping whitespace so block-style
        # fences (newline before + after body) stay block, and inline
        # fences (no surrounding whitespace, e.g. inside a markdown
        # table cell) stay inline. Handlers return one-line strings;
        # the surrounding fence layout is the doc author's choice.
        leading_ws = re.match(r"\A\s*", old_body).group(0) if old_body else ""
        trailing_ws = re.search(r"\s*\Z", old_body).group(0) if old_body else ""
        if leading_ws or trailing_ws:
            rendered = f"{leading_ws}{new_body_raw}{trailing_ws}"
        else:
            # No prior surrounding whitespace -> inline form, no newlines.
            rendered = new_body_raw
        n_replaced += 1
        if rendered != old_body:
            logger.info(
                "fence updated tag=%s old_len=%d new_len=%d",
                tag,
                len(old_body),
                len(rendered),
            )
        return f"<!-- AUTO: {tag} -->{rendered}<!-- END AUTO -->"

    new_text = FENCE_RE.sub(_sub, text)
    return new_text, n_replaced, n_unknown


def _compute_file(path: Path, milestones_md: Path) -> tuple[str, str, int, int]:
    """Pass-1 helper: read file, compute regenerated content.

    Returns (original, new_text, n_replaced, n_unknown). Pure read + handler
    dispatch; never writes to disk. Surfaces any handler exception so the
    caller can abort the whole batch before any file is touched.
    """
    original = path.read_text(encoding="utf-8")
    new_text, n_replaced, n_unknown = _replace_fences(original, milestones_md)
    return original, new_text, n_replaced, n_unknown


def propagate(vault_root: Path | None = None) -> dict:
    """Run the propagation. Returns a structured summary suitable for tests.

    Two-pass design:
        1. Read every planning doc + run handlers + compute new content.
        2. Atomically write only the files whose content changed.

    Pass 1 surfaces handler exceptions BEFORE any file is mutated, so a
    handler bug cannot leave the vault in a half-rewritten split-brain
    state. Pass 2's atomic_write_bytes handles per-file durability via
    tempfile + os.replace; a write failure mid-pass-2 still produces
    only fully-written or fully-unchanged individual files (never a
    partial line), and re-running the post-build hook on the next
    /ship picks up where the failed write left off.
    """
    if vault_root is None:
        vault_root = _resolve_vault_root()

    milestones_md = _milestones_path(vault_root)
    if not milestones_md.is_file():
        logger.error("milestones.md not found at %s", milestones_md)
        return {
            "status": "no-source",
            "vault_root": str(vault_root),
            "files_scanned": 0,
            "files_rewritten": 0,
            "fences_replaced": 0,
            "unknown_tags": 0,
        }

    docs = _planning_docs(vault_root)
    if not docs:
        logger.error("no planning docs found at %s/wiki/planning/", vault_root)
        return {
            "status": "no-docs",
            "vault_root": str(vault_root),
            "files_scanned": 0,
            "files_rewritten": 0,
            "fences_replaced": 0,
            "unknown_tags": 0,
        }

    # Pass 1: read + compute new content for every file. Any handler
    # exception aborts the whole batch before any disk write happens.
    pending_writes: list[tuple[Path, str]] = []
    fences_replaced = 0
    unknown_tags = 0
    files_scanned = len(docs)
    for path in docs:
        original, new_text, n_replaced, n_unknown = _compute_file(
            path, milestones_md
        )
        fences_replaced += n_replaced
        unknown_tags += n_unknown
        if new_text != original:
            pending_writes.append((path, new_text))

    # Pass 2: atomically write the files that changed.
    files_rewritten = 0
    for path, new_text in pending_writes:
        atomic_write_bytes(path, new_text.encode("utf-8"))
        files_rewritten += 1
        logger.info("rewrote %s", path)

    return {
        "status": "ok",
        "vault_root": str(vault_root),
        "files_scanned": files_scanned,
        "files_rewritten": files_rewritten,
        "fences_replaced": fences_replaced,
        "unknown_tags": unknown_tags,
    }


def main(argv: Iterable[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
        result = propagate()
    except Exception:
        logger.exception("handler raised; aborting propagation")
        return 1

    if result["status"] == "no-source" or result["status"] == "no-docs":
        return 2

    print(
        f"propagate-planning-status: scanned={result['files_scanned']} "
        f"rewritten={result['files_rewritten']} "
        f"fences={result['fences_replaced']} "
        f"unknown={result['unknown_tags']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
