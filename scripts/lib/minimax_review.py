"""Standalone MiniMax M2.7 adversarial code reviewer.

Phase A MVP: working-tree scope, single-shot, JSON output validated against
Codex's review-output schema. Touches nothing in /ship or the codex plugin.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from minimax_common import (
    MinimaxError,
    chat_completion,
    extract_assistant_text,
    extract_token_usage,
)

LIB_DIR = Path(__file__).resolve().parent
REPO_ROOT = Path(
    subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], text=True
    ).strip()
)
PROMPT_PATH = LIB_DIR / "adversarial-review.md"
SCHEMA_PATH = LIB_DIR / "review-output.schema.json"
DEFAULT_ARCHIVE_DIR = REPO_ROOT / ".minimax-reviews"

MAX_FILE_BYTES = 256 * 1024  # skip large files; M2.7 has 200K context but stay sane
BINARY_SNIFF_BYTES = 4096


def run_git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=cwd or REPO_ROOT, text=True, errors="replace"
    )


def is_binary(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:BINARY_SNIFF_BYTES]
    except OSError:
        return True
    if b"\x00" in chunk:
        return True
    return False


def gather_working_tree_context(
    repo_root: Path | None = None,
) -> tuple[str, list[str]]:
    """Return (context_text, changed_file_list) for working-tree scope.

    Includes:
      - git status --short (overview)
      - diffstat
      - diff vs HEAD for tracked changes
      - full content of each changed/untracked file (truncated if huge)
    """
    root = repo_root or REPO_ROOT
    status = run_git("status", "--short", cwd=root)
    changed_files: list[str] = []
    for line in status.splitlines():
        if not line.strip():
            continue
        # status format: "XY path" or "XY orig -> new"
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        changed_files.append(path.strip().strip('"'))

    if not changed_files:
        return "", []

    diffstat = run_git("diff", "HEAD", "--stat", cwd=root)
    diff = run_git("diff", "HEAD", cwd=root)

    sections: list[str] = []
    sections.append("## git status --short\n```\n" + status.rstrip() + "\n```")
    if diffstat.strip():
        sections.append("## diffstat (HEAD)\n```\n" + diffstat.rstrip() + "\n```")
    if diff.strip():
        sections.append("## diff vs HEAD\n```diff\n" + diff.rstrip() + "\n```")

    sections.append("## Full file contents (changed and untracked)")
    for rel in sorted(set(changed_files)):
        path = root / rel
        if not path.exists():
            sections.append(f"### {rel}\n_(deleted)_")
            continue
        if path.is_dir():
            sections.append(f"### {rel}\n_(directory)_")
            continue
        if is_binary(path):
            sections.append(f"### {rel}\n_(binary, skipped)_")
            continue
        try:
            data = path.read_bytes()
        except OSError as e:
            sections.append(f"### {rel}\n_(unreadable: {e})_")
            continue
        truncated_note = ""
        if len(data) > MAX_FILE_BYTES:
            data = data[:MAX_FILE_BYTES]
            truncated_note = f"\n_(truncated to {MAX_FILE_BYTES} bytes)_"
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        # Add line numbers so the model can reference line_start / line_end accurately
        numbered = "\n".join(
            f"{i + 1:5d}  {line}" for i, line in enumerate(text.splitlines())
        )
        sections.append(
            f"### {rel}{truncated_note}\n```\n{numbered}\n```"
        )

    return "\n\n".join(sections), sorted(set(changed_files))


def gather_diff_scoped_context(
    files: list[str],
    repo_root: Path | None = None,
) -> tuple[str, list[str]]:
    """Return (context_text, file_list) restricted to the given files.

    Includes per-file `git diff HEAD <file>` and per-file `git status -- <file>`,
    plus full content of each file. Other dirty files in the working tree
    are NOT included -- this is the "review only what I asked for" gatherer.
    """
    root = repo_root or REPO_ROOT
    if not files:
        return "", []
    files_sorted = sorted(set(files))
    sections: list[str] = []
    sections.append("## diff-scoped review (explicit file list)")
    for rel in files_sorted:
        path = root / rel if not Path(rel).is_absolute() else Path(rel)
        try:
            status = run_git("status", "--short", "--", rel, cwd=root).rstrip()
        except subprocess.CalledProcessError:
            status = ""
        try:
            diff = run_git("diff", "HEAD", "--", rel, cwd=root).rstrip()
        except subprocess.CalledProcessError:
            diff = ""
        sections.append(f"### {rel}")
        if status:
            sections.append("```\n" + status + "\n```")
        else:
            sections.append("_(no working-tree change vs HEAD)_")
        if diff:
            sections.append("```diff\n" + diff + "\n```")
        if not path.exists():
            sections.append("_(file missing from working tree)_")
            continue
        if path.is_dir():
            sections.append("_(directory, skipped)_")
            continue
        if is_binary(path):
            sections.append("_(binary, skipped)_")
            continue
        try:
            data = path.read_bytes()
        except OSError as e:
            sections.append(f"_(unreadable: {e})_")
            continue
        truncated_note = ""
        if len(data) > MAX_FILE_BYTES:
            data = data[:MAX_FILE_BYTES]
            truncated_note = f"\n_(truncated to {MAX_FILE_BYTES} bytes)_"
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        numbered = "\n".join(
            f"{i + 1:5d}  {line}" for i, line in enumerate(text.splitlines())
        )
        sections.append(f"```\n{numbered}\n```{truncated_note}")
    return "\n\n".join(sections), files_sorted


def gather_file_list_context(
    paths: list[str],
    repo_root: Path | None = None,
) -> tuple[str, list[str]]:
    """Return (context_text, file_list) for an explicit list of file paths.

    No git context. Missing files and directories are skipped with a
    stderr warning -- never crash. Useful for ad-hoc "review these files"
    runs not tied to a diff or a plan.
    """
    root = repo_root or REPO_ROOT
    if not paths:
        return "", []
    sections: list[str] = []
    sections.append("## file-list review (no git context)")
    included: list[str] = []
    for rel in paths:
        path = (root / rel) if not Path(rel).is_absolute() else Path(rel)
        if not path.exists():
            print(
                f"[minimax-review] warning: skipping missing file: {rel}",
                file=sys.stderr,
            )
            continue
        if path.is_dir():
            print(
                f"[minimax-review] warning: skipping directory: {rel}",
                file=sys.stderr,
            )
            continue
        if is_binary(path):
            sections.append(f"### {rel}\n_(binary, skipped)_")
            included.append(rel)
            continue
        try:
            data = path.read_bytes()
        except OSError as e:
            sections.append(f"### {rel}\n_(unreadable: {e})_")
            continue
        truncated_note = ""
        if len(data) > MAX_FILE_BYTES:
            data = data[:MAX_FILE_BYTES]
            truncated_note = f"\n_(truncated to {MAX_FILE_BYTES} bytes)_"
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        numbered = "\n".join(
            f"{i + 1:5d}  {line}" for i, line in enumerate(text.splitlines())
        )
        sections.append(f"### {rel}{truncated_note}\n```\n{numbered}\n```")
        included.append(rel)
    return "\n\n".join(sections), included


WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")

# Path references: matched in three forms (anchored on common punctuation):
#   1. Absolute path: starts with '/', any depth, no extension required
#   2. Relative path with known extension: scripts/foo.py, docs/notes.md
#   3. Top-level filename with known extension: README.md, foo.sh
# Tokens containing '/' but NO known extension (e.g. prose like "gather/run_git",
# "abs/rel") are intentionally NOT matched -- they generated false-positive
# '_(file missing)_' noise on plans containing slash-separated identifiers.
# `K2B-Vault/...` shorthand is NOT specially handled -- callers wanting vault
# files use absolute paths (K2B-Vault is a sibling of the repo, not a subdir).
_PATH_EXT = "py|sh|md|json|ya?ml|toml|js|ts|tsx|jsx|html|css|sql|txt|env"
PATH_REF_RE = re.compile(
    r"(?:^|[\s`(\[<,;])"
    r"("
    r"/(?:[\w.\-]+/)*[\w.\-]+"                                 # absolute path
    r"|"
    r"(?:[\w.\-]+/)+[\w.\-]+\.(?:" + _PATH_EXT + ")"            # rel path + ext
    r"|"
    r"[\w][\w.\-]*\.(?:" + _PATH_EXT + ")"                      # bare filename + ext
    r")"
    r"(?=[\s`)\]>.,;:!?]|$)",
    re.MULTILINE,
)


def _resolve_wikilink(token: str, root: Path) -> Path | None:
    """Resolve a bare [[wikilink]] target by searching wiki/ then raw/.

    Returns the first matching .md file, or repo-root-relative <token>.md as
    a final fallback. None means we couldn't identify any file.
    """
    for subdir in ("wiki", "raw"):
        base = root / subdir
        if not base.is_dir():
            continue
        for ext in (".md", ""):
            for match in base.rglob(f"{token}{ext}"):
                if match.is_file():
                    return match
    candidate = root / f"{token}.md"
    if candidate.is_file():
        return candidate
    return None


def _resolve_path_ref(token: str, root: Path) -> Path:
    """Resolve a path token (abs or rel) to a Path.

    Returns the candidate Path (whether or not it exists). Caller checks
    `.is_file()` -- missing files are marked in the output, never silently
    dropped (per the Phase A 'mark, don't drop' rule).
    """
    if Path(token).is_absolute():
        return Path(token)
    return root / token


def gather_plan_context(
    plan_path: str,
    repo_root: Path | None = None,
) -> tuple[str, list[str]]:
    """Return (context_text, file_list) for a plan file and its references.

    Parses [[wikilinks]] (resolved via wiki/ then raw/ search), inline path
    references (any token containing '/' or ending in a known file extension),
    and absolute paths.

    Failure modes (intentionally distinct):
      - Unparseable wikilink (no file matches the search) -> warn to stderr,
        skip. We can't mark what we couldn't identify.
      - Path ref that resolves to a missing file -> include `### <token>`
        section with `_(file missing)_` marker. Caller knows exactly which
        file was meant; reviewer needs to see the gap.
    """
    root = repo_root or REPO_ROOT
    plan_full = (
        Path(plan_path) if Path(plan_path).is_absolute() else (root / plan_path)
    )
    if not plan_full.is_file():
        raise FileNotFoundError(f"plan not found: {plan_full}")

    plan_text = plan_full.read_text(errors="replace")

    found_refs: list[tuple[str, Path]] = []  # (display_name, real_path)
    missing_refs: list[str] = []  # display_name only
    seen: set[str] = set()

    def _track(display: str, real: Path | None) -> None:
        if display in seen or display == plan_path:
            return
        seen.add(display)
        if real is not None and real.is_file():
            found_refs.append((display, real))
        else:
            missing_refs.append(display)

    for match in WIKILINK_RE.finditer(plan_text):
        token = match.group(1).strip()
        resolved = _resolve_wikilink(token, root)
        if resolved is None:
            print(
                f"[minimax-review] warning: unresolvable wikilink: [[{token}]]",
                file=sys.stderr,
            )
            continue
        try:
            display = str(resolved.relative_to(root))
        except ValueError:
            display = str(resolved)
        _track(display, resolved)

    for match in PATH_REF_RE.finditer(plan_text):
        token = match.group(1).strip()
        resolved = _resolve_path_ref(token, root)
        try:
            display = str(resolved.relative_to(root))
        except ValueError:
            display = token  # absolute path or out-of-tree
        _track(display, resolved if resolved.is_file() else None)

    sections: list[str] = []
    sections.append("## plan-scoped review")
    sections.append(f"### {plan_path} (plan)")
    numbered_plan = "\n".join(
        f"{i + 1:5d}  {line}" for i, line in enumerate(plan_text.splitlines())
    )
    sections.append(f"```\n{numbered_plan}\n```")

    if found_refs or missing_refs:
        sections.append("### Referenced files")
        for display, real in found_refs:
            if is_binary(real):
                sections.append(f"#### {display}\n_(binary, skipped)_")
                continue
            try:
                data = real.read_bytes()
            except OSError as e:
                sections.append(f"#### {display}\n_(unreadable: {e})_")
                continue
            truncated_note = ""
            if len(data) > MAX_FILE_BYTES:
                data = data[:MAX_FILE_BYTES]
                truncated_note = f"\n_(truncated to {MAX_FILE_BYTES} bytes)_"
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("utf-8", errors="replace")
            numbered = "\n".join(
                f"{i + 1:5d}  {line}" for i, line in enumerate(text.splitlines())
            )
            sections.append(f"#### {display}{truncated_note}\n```\n{numbered}\n```")
        for display in missing_refs:
            sections.append(f"#### {display}\n_(file missing)_")

    file_list = [plan_path] + [d for d, _ in found_refs] + missing_refs
    return "\n\n".join(sections), file_list


def build_prompt(target_label: str, focus: str, content: str, schema_text: str) -> str:
    template = PROMPT_PATH.read_text()
    return (
        template.replace("{{TARGET_LABEL}}", target_label)
        .replace("{{USER_FOCUS}}", focus or "No extra focus provided.")
        .replace("{{OUTPUT_SCHEMA}}", schema_text)
        .replace("{{REVIEW_INPUT}}", content)
    )


def extract_json_object(text: str) -> dict | None:
    """Try strict json.loads first, then regex-extract the first {...} block."""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Kimi wraps JSON in ```json ... ``` fences. Use json.JSONDecoder.raw_decode
    # instead of a regex brace-match: raw_decode scans linearly with a real
    # JSON parser, so it cannot catastrophic-backtrack on pathological input
    # (the K2B 2026-04-25 swap flagged the prior greedy regex as a hang risk).
    fence = re.search(r"```(?:json)?\s*", text)
    if fence:
        rest = text[fence.end():]
        brace = rest.find("{")
        if brace != -1:
            try:
                obj, _ = json.JSONDecoder().raw_decode(rest[brace:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
    # Greedy first-{ to last-} fallback
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def render_markdown(parsed: dict, model: str, usage: dict) -> str:
    verdict = parsed.get("verdict", "?")
    summary = parsed.get("summary", "(no summary)")
    findings = parsed.get("findings") or []
    next_steps = parsed.get("next_steps") or []

    findings_sorted = sorted(
        findings, key=lambda f: SEVERITY_ORDER.get(f.get("severity", "low"), 99)
    )

    badge = "APPROVE" if verdict == "approve" else "NEEDS-ATTENTION"
    lines: list[str] = []
    lines.append(f"# {model} review -- {badge}")
    lines.append("")
    lines.append(f"**Summary:** {summary}")
    lines.append("")
    lines.append(
        f"**Tokens:** prompt={usage.get('prompt_tokens')}  "
        f"completion={usage.get('completion_tokens')}  "
        f"total={usage.get('total_tokens')}"
    )
    lines.append("")
    if not findings_sorted:
        lines.append("_No findings._")
    else:
        lines.append(f"## Findings ({len(findings_sorted)})")
        lines.append("")
        for i, f in enumerate(findings_sorted, 1):
            sev = (f.get("severity") or "?").upper()
            conf = f.get("confidence")
            conf_pct = f"{int(conf * 100)}%" if isinstance(conf, (int, float)) else "?"
            lines.append(
                f"### {i}. [{sev}] {f.get('title', '(untitled)')}  ({conf_pct} conf)"
            )
            lines.append(
                f"`{f.get('file', '?')}` lines "
                f"{f.get('line_start', '?')}-{f.get('line_end', '?')}"
            )
            lines.append("")
            lines.append(f.get("body", ""))
            rec = f.get("recommendation")
            if rec:
                lines.append("")
                lines.append(f"**Recommendation:** {rec}")
            lines.append("")
    if next_steps:
        lines.append("## Next steps")
        for step in next_steps:
            lines.append(f"- {step}")
        lines.append("")
    return "\n".join(lines)


def archive(
    archive_dir: Path,
    *,
    scope: str,
    model: str,
    parsed: dict | None,
    raw_text: str,
    prompt: str,
    response: dict,
    usage: dict,
) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out = archive_dir / f"{ts}_{scope}.json"
    record = {
        "timestamp_utc": ts,
        "scope": scope,
        "model": model,
        "usage": usage,
        "parsed": parsed,
        "raw_text": raw_text,
        "prompt_chars": len(prompt),
        "response_id": response.get("id"),
    }
    out.write_text(json.dumps(record, indent=2, ensure_ascii=False))
    return out


def append_usage_log(archive_dir: Path, model: str, scope: str, usage: dict) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    log = archive_dir / "usage.log"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = (
        f"{ts}\t{model}\t{scope}\t"
        f"prompt={usage.get('prompt_tokens')}\t"
        f"completion={usage.get('completion_tokens')}\t"
        f"total={usage.get('total_tokens')}\n"
    )
    with log.open("a") as f:
        f.write(line)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Standalone MiniMax M2.7 adversarial code reviewer."
    )
    parser.add_argument(
        "--scope",
        default="working-tree",
        choices=["working-tree", "diff", "plan", "files"],
        help=(
            "Context gatherer: 'working-tree' (default, Phase A behavior), "
            "'diff' (only --files paths + their diffs), "
            "'plan' (--plan path + files it references), "
            "'files' (just --files paths, no git context)"
        ),
    )
    parser.add_argument(
        "--plan",
        default=None,
        help="Plan file path (required when --scope plan)",
    )
    parser.add_argument(
        "--files",
        default=None,
        help="Comma-separated list of paths (required when --scope diff or files)",
    )
    # Default model tracks the active provider: kimi-for-coding when
    # K2B_LLM_PROVIDER=kimi (current default since the K2B 2026-04-25 swap),
    # MiniMax-M2.7 on MiniMax rollback. Callers can still pass --model.
    _default_model = os.environ.get(
        "K2B_LLM_MODEL",
        "kimi-for-coding"
        if os.environ.get("K2B_LLM_PROVIDER", "kimi") == "kimi"
        else "MiniMax-M2.7",
    )
    parser.add_argument(
        "--model",
        default=_default_model,
        help=f"Model id (default {_default_model})",
    )
    parser.add_argument(
        "--focus",
        default="",
        help="Optional focus text passed into the adversarial template",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=16384,
        help="Max completion tokens (default 16384; 4096 truncates rich reviews)",
    )
    parser.add_argument(
        "--archive-dir",
        default=str(DEFAULT_ARCHIVE_DIR),
        help="Where to archive raw + parsed output",
    )
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="Skip writing the archive file",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit parsed JSON to stdout instead of rendered markdown",
    )
    args = parser.parse_args()

    schema_text = SCHEMA_PATH.read_text()

    print(f"[minimax-review] gathering {args.scope} context...", file=sys.stderr)
    if args.scope == "working-tree":
        context, changed = gather_working_tree_context()
        if not changed:
            print(
                "[minimax-review] no working-tree changes; nothing to review.",
                file=sys.stderr,
            )
            return 0
    elif args.scope == "diff":
        if not args.files:
            print("[minimax-review] --scope diff requires --files", file=sys.stderr)
            return 1
        file_list = [p.strip() for p in args.files.split(",") if p.strip()]
        if not file_list:
            print(
                "[minimax-review] --scope diff: --files parsed to empty list",
                file=sys.stderr,
            )
            return 1
        context, changed = gather_diff_scoped_context(file_list)
    elif args.scope == "plan":
        if not args.plan:
            print("[minimax-review] --scope plan requires --plan", file=sys.stderr)
            return 1
        try:
            context, changed = gather_plan_context(args.plan)
        except FileNotFoundError as e:
            print(f"[minimax-review] {e}", file=sys.stderr)
            return 1
    elif args.scope == "files":
        if not args.files:
            print("[minimax-review] --scope files requires --files", file=sys.stderr)
            return 1
        file_list = [p.strip() for p in args.files.split(",") if p.strip()]
        if not file_list:
            print(
                "[minimax-review] --scope files: --files parsed to empty list",
                file=sys.stderr,
            )
            return 1
        context, changed = gather_file_list_context(file_list)
    else:
        print(f"[minimax-review] unknown scope: {args.scope}", file=sys.stderr)
        return 1
    print(
        f"[minimax-review] {len(changed)} changed files, "
        f"{len(context)} chars of context",
        file=sys.stderr,
    )

    if args.scope == "working-tree":
        # Phase A wording preserved verbatim -- byte-for-byte back-compat for
        # the prompt MiniMax sees. Do not alter.
        target_label = (
            f"working tree of {REPO_ROOT.name} ({len(changed)} files changed)"
        )
    elif args.scope == "diff":
        target_label = (
            f"diff-scoped review of {REPO_ROOT.name} ({len(changed)} files)"
        )
    elif args.scope == "plan":
        target_label = f"plan {args.plan} ({len(changed)} files referenced)"
    else:  # files
        target_label = (
            f"explicit file list ({len(changed)} files, repo {REPO_ROOT.name})"
        )
    prompt = build_prompt(target_label, args.focus, context, schema_text)

    print(
        f"[minimax-review] calling {args.model} ({len(prompt)} prompt chars)...",
        file=sys.stderr,
    )
    try:
        response = chat_completion(
            model=args.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=args.max_tokens,
            temperature=0.2,
        )
    except MinimaxError as e:
        print(f"[minimax-review] FAIL: {e}", file=sys.stderr)
        return 2

    raw_text = extract_assistant_text(response)
    usage = extract_token_usage(response)
    parsed = extract_json_object(raw_text)

    archive_dir = Path(args.archive_dir)
    if not args.no_archive:
        out = archive(
            archive_dir,
            scope=args.scope,
            model=args.model,
            parsed=parsed,
            raw_text=raw_text,
            prompt=prompt,
            response=response,
            usage=usage,
        )
        append_usage_log(archive_dir, args.model, args.scope, usage)
        print(f"[minimax-review] archived: {out.relative_to(REPO_ROOT)}", file=sys.stderr)

    if parsed is None:
        print(
            "[minimax-review] could not parse JSON from response. "
            "See archive for raw output.",
            file=sys.stderr,
        )
        if args.json:
            print(json.dumps({"error": "unparseable", "raw": raw_text}, indent=2))
        else:
            print("# MiniMax review -- UNPARSEABLE\n")
            print("Raw response (truncated to 4KB):\n")
            print("```\n" + raw_text[:4096] + "\n```")
        return 3

    if args.json:
        print(json.dumps(parsed, indent=2, ensure_ascii=False))
    else:
        print(render_markdown(parsed, args.model, usage))
    return 0


if __name__ == "__main__":
    sys.exit(main())
