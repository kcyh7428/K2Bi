#!/usr/bin/env python3
"""Read scripts/deploy-config.yml and answer queries for deploy-to-mini.sh and
/invest-ship's step 12 preflight.

This is the single source of truth for:
  - which paths deploy to the Mac Mini and under which /sync mailbox category
  - which paths are intentionally kept local (design docs, reviewer archives,
    per-machine state)
  - whether the working tree has a top-level dir that drifted out of coverage

Subcommands are text-oriented so bash can consume them via command substitution:

  deploy_config.py list-categories
      Print one category per line (skills, execution, scripts, pm2, ...).
      Stable alphabetical order.

  deploy_config.py list-targets [CATEGORY]
      Print one target path per line. With a category filter, limits to that
      category. Order matches the config file for deterministic rsync ordering.

  deploy_config.py classify FILE...
      For each file on stdin or argv, print 'category<TAB>path' if any target
      covers it, else print 'uncovered<TAB>path'. Prefix match: target
      'execution/' covers 'execution/engine/main.py' etc.

  deploy_config.py preflight
      Scan the repo's top-level entries (files + dirs). Any entry not covered
      by `targets:` AND not in `excludes:` is a drift signal; print each to
      stderr and exit 1. Exit 0 on clean.

The config file path defaults to $(git rev-parse --show-toplevel)/scripts/deploy-config.yml;
override via $K2BI_DEPLOY_CONFIG.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _split_kv(s: str) -> tuple[str, str]:
    key, value = s.split(":", 1)
    return key.strip(), value.strip().strip('"').strip("'")


def _fallback_parse(text: str) -> dict:
    """Stdlib-only parser for deploy-config.yml's specific schema.

    Handles just what deploy-config.yml uses -- two top-level list keys
    (`targets:` of path+category dicts, `excludes:` of bare strings), plus
    `#`-prefixed comments and blank lines. No anchors, aliases, nested dicts,
    or multi-line strings.

    Why a fallback exists (Codex R2 P2): the deploy flow is now invoked from
    /ship + /sync, which are operational commands the host's default python
    must be able to run. PyYAML is declared in requirements.txt and is the
    preferred path, but a fresh clone that has not yet run `pip install -r
    requirements.txt` must still be able to deploy rather than hard-fail
    with ModuleNotFoundError.

    Raises ValueError on malformed input so the caller can report cleanly.
    """
    targets: list[dict] = []
    excludes: list[str] = []
    section: str | None = None
    current_target: dict | None = None

    for raw in text.splitlines():
        # Strip trailing comment only when # is unquoted AND preceded by
        # whitespace -- any `#` inside a path (e.g. `foo#bar`) is treated
        # as literal because our schema uses no such paths.
        line = raw
        cmt = -1
        in_single = in_double = False
        for i, ch in enumerate(line):
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif ch == "#" and not in_single and not in_double:
                if i == 0 or line[i - 1].isspace():
                    cmt = i
                    break
        if cmt >= 0:
            line = line[:cmt]
        line = line.rstrip()
        if not line.strip():
            continue

        if not line[0].isspace():
            if not line.endswith(":"):
                raise ValueError(
                    f"deploy-config: unexpected top-level line: {line!r}"
                )
            key = line[:-1].strip()
            if key == "targets":
                section = "targets"
            elif key == "excludes":
                section = "excludes"
            else:
                raise ValueError(
                    f"deploy-config: unknown top-level key: {key!r}"
                )
            current_target = None
            continue

        stripped = line.strip()
        if section == "targets":
            if stripped.startswith("- "):
                current_target = {}
                targets.append(current_target)
                kv = stripped[2:].strip()
                if ":" in kv:
                    k, v = _split_kv(kv)
                    current_target[k] = v
            elif current_target is not None and ":" in stripped:
                k, v = _split_kv(stripped)
                current_target[k] = v
            else:
                raise ValueError(
                    f"deploy-config: unexpected line in targets: {line!r}"
                )
        elif section == "excludes":
            if stripped.startswith("- "):
                excludes.append(stripped[2:].strip())
            else:
                raise ValueError(
                    f"deploy-config: unexpected line in excludes: {line!r}"
                )
        else:
            raise ValueError(
                f"deploy-config: indented content before section header: {line!r}"
            )

    return {"targets": targets, "excludes": excludes}


def _parse_yaml(text: str) -> dict:
    """Prefer PyYAML (declared in requirements.txt); fall back to a stdlib
    parser for deploy-config.yml's specific schema so a fresh clone's host
    python can still run /ship + /sync before `pip install -r requirements.txt`.

    Codex R3 P2: a non-mapping top level (e.g. a stray top-level list from an
    editor mistake) must fail loud here rather than being coerced to {} --
    otherwise /ship reports every repo path as uncovered and /sync sees no
    categories, both of which are much harder to diagnose than a parse error.
    """
    try:
        import yaml as _yaml
    except ImportError:
        return _fallback_parse(text)
    try:
        data = _yaml.safe_load(text)
    except _yaml.YAMLError as exc:
        raise ValueError(f"deploy-config: YAML parse error: {exc}") from exc
    # Codex R4 P2: do NOT coerce None / empty-list / empty-scalar to {} via
    # `or {}` -- that silently swallows malformed configs. An empty file is
    # just as broken for /ship + /sync as a bad top-level list; both must
    # fail loud so Keith gets a clear actionable error instead of "no
    # categories" or "every path uncovered".
    if data is None:
        raise ValueError(
            "deploy-config: config file is empty (expected top-level "
            "`targets:` and `excludes:` keys)"
        )
    if not isinstance(data, dict):
        raise ValueError(
            "deploy-config: top-level YAML must be a mapping with `targets:` "
            f"and `excludes:` keys, got {type(data).__name__}"
        )
    return data


def _repo_root() -> Path:
    override = os.environ.get("K2BI_DEPLOY_CONFIG")
    if override:
        return Path(override).resolve().parent.parent
    try:
        return Path(
            subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"], text=True
            ).strip()
        )
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(f"deploy-config: not in a git repo: {exc}\n")
        sys.exit(2)


def _config_path() -> Path:
    override = os.environ.get("K2BI_DEPLOY_CONFIG")
    if override:
        return Path(override)
    return _repo_root() / "scripts" / "deploy-config.yml"


def _load() -> dict:
    path = _config_path()
    if not path.exists():
        sys.stderr.write(f"deploy-config: config file missing at {path}\n")
        sys.exit(2)
    try:
        data = _parse_yaml(path.read_text())
    except ValueError as exc:
        sys.stderr.write(f"deploy-config: parse error in {path}: {exc}\n")
        sys.exit(2)
    if not isinstance(data, dict):
        sys.stderr.write(f"deploy-config: top-level YAML must be a mapping in {path}\n")
        sys.exit(2)
    targets = data.get("targets") or []
    excludes = data.get("excludes") or []
    if not isinstance(targets, list) or not isinstance(excludes, list):
        sys.stderr.write(
            f"deploy-config: `targets` and `excludes` must be lists in {path}\n"
        )
        sys.exit(2)
    parsed_targets: list[dict] = []
    for i, entry in enumerate(targets):
        if not isinstance(entry, dict):
            sys.stderr.write(
                f"deploy-config: targets[{i}] must be a mapping in {path}\n"
            )
            sys.exit(2)
        path_s = entry.get("path")
        category = entry.get("category")
        if not path_s or not isinstance(path_s, str):
            sys.stderr.write(
                f"deploy-config: targets[{i}].path missing or not a string\n"
            )
            sys.exit(2)
        if not category or not isinstance(category, str):
            sys.stderr.write(
                f"deploy-config: targets[{i}].category missing or not a string\n"
            )
            sys.exit(2)
        parsed_targets.append({"path": path_s, "category": category})
    parsed_excludes: list[str] = []
    for i, entry in enumerate(excludes):
        if not isinstance(entry, str):
            sys.stderr.write(
                f"deploy-config: excludes[{i}] must be a string in {path}\n"
            )
            sys.exit(2)
        parsed_excludes.append(entry)
    return {"targets": parsed_targets, "excludes": parsed_excludes}


def _enumerate_repo_paths(repo: Path) -> list[str]:
    """Return the set of repo-relative paths that git considers part of the
    project: everything tracked plus every untracked file that is NOT matched
    by .gitignore. This is the authoritative "what ships with the repo" set.

    Rationale (Codex round 1 P2 #1): iterating the filesystem directly causes
    preflight false positives from developer-local scratch files (e.g. a
    temporary `notes/` folder, `.DS_Store` OS metadata). Those would block
    /ship unjustly. Git already knows which entries are intentional via
    .gitignore, so we defer to it and fall back to filesystem iteration only
    when the directory is not a git repo (test contexts without git init,
    which git-init themselves when they need real behavior).
    """
    try:
        tracked = subprocess.check_output(
            ["git", "-C", str(repo), "ls-files"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).splitlines()
        untracked = subprocess.check_output(
            [
                "git",
                "-C",
                str(repo),
                "ls-files",
                "--others",
                "--exclude-standard",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        ).splitlines()
        return sorted(set(filter(None, tracked + untracked)))
    except (subprocess.CalledProcessError, FileNotFoundError):
        paths: list[str] = []
        for dirpath, _, filenames in os.walk(repo):
            rel_dir = Path(dirpath).relative_to(repo)
            if ".git" in rel_dir.parts:
                continue
            for name in filenames:
                rel = (rel_dir / name).as_posix()
                if rel.startswith("./"):
                    rel = rel[2:]
                paths.append(rel)
        return sorted(paths)


def _covered(path: str, targets: list[tuple[str, str]], excludes: list[str]) -> bool:
    """True if `path` is covered by a target (deploys) or an exclude
    (intentionally local). Full-path prefix match, not top-segment match."""
    for tpath, _ in targets:
        if path == tpath or path.startswith(tpath + "/"):
            return True
    for e in excludes:
        if path == e or path.startswith(e + "/"):
            return True
    return False


def _drift_root(
    path: str, targets: list[tuple[str, str]], excludes: list[str]
) -> str:
    """Collapse an uncovered path to the shortest ancestor whose entire
    subtree is uncovered (no nested target or exclude).

    Example: with targets=[.claude/skills/] and .claude/plugins in excludes,
    an uncovered .claude/commands/foo.md collapses to `.claude/commands`
    (reporting that root instead of every file lets Keith add one line to
    the config and resolve the whole drift).

    Falls back to the full path if nothing collapses (e.g. a single top-level
    uncovered file like `random.md`).
    """
    parts = path.split("/")
    for depth in range(1, len(parts) + 1):
        prefix = "/".join(parts[:depth])
        # Does any target or exclude sit STRICTLY INSIDE `prefix`? If so,
        # `prefix` is only partially uncovered; keep walking deeper.
        inside_target = any(
            tp.startswith(prefix + "/") for tp, _ in targets
        )
        inside_exclude = any(e.startswith(prefix + "/") for e in excludes)
        if not inside_target and not inside_exclude:
            return prefix
    return path


def cmd_list_categories(config: dict) -> int:
    cats = sorted({t["category"] for t in config["targets"]})
    for c in cats:
        print(c)
    return 0


def cmd_list_targets(config: dict, category: str | None) -> int:
    for t in config["targets"]:
        if category and t["category"] != category:
            continue
        print(t["path"])
    return 0


def cmd_classify(config: dict, files: list[str]) -> int:
    targets = [(t["path"].rstrip("/"), t["category"]) for t in config["targets"]]
    # Longest-prefix match wins so 'execution/' catches 'execution/foo.py'
    # before a shorter shared prefix like 'ex' could interfere. (Trivially
    # safe here since paths don't overlap, but keeps semantics crisp.)
    targets.sort(key=lambda pc: len(pc[0]), reverse=True)
    for f in files:
        # Strip a leading './' prefix only; do NOT strip leading dots
        # (that would turn '.claude/foo' into 'claude/foo' and miss the
        # .claude/skills/ target).
        fnorm = f[2:] if f.startswith("./") else f
        category = None
        for tpath, tcat in targets:
            if fnorm == tpath or fnorm.startswith(tpath + "/"):
                category = tcat
                break
        if category:
            print(f"{category}\t{fnorm}")
        else:
            print(f"uncovered\t{fnorm}")
    return 0


def cmd_preflight(config: dict) -> int:
    """Fail loud on any repo path that is neither deployed (covered by a
    `targets:` entry) nor intentionally local (covered by an `excludes:`
    entry). Operates on git-visible paths only so developer-local scratch
    files (gitignored runtime state, editor artifacts) do not produce false
    positives.

    Reporting collapses uncovered files to their drift-root directory so a
    new `.claude/commands/foo.md` shows up as `.claude/commands` rather than
    each individual file -- Keith can fix the whole drift with one config
    line.

    Fixes Codex round 1 P2 #1 (false positives from non-repo entries) and
    P2 #2 (partial-tree coverage miss when a parent dir has some targets
    but a new uncovered sibling lands under it).
    """
    repo = _repo_root()
    paths = _enumerate_repo_paths(repo)

    targets = [(t["path"].rstrip("/"), t["category"]) for t in config["targets"]]
    targets.sort(key=lambda pc: len(pc[0]), reverse=True)
    excludes = sorted(
        [e.rstrip("/") for e in config["excludes"]], key=len, reverse=True
    )

    uncovered = [p for p in paths if not _covered(p, targets, excludes)]
    if not uncovered:
        return 0

    drift_roots = sorted({_drift_root(p, targets, excludes) for p in uncovered})
    sys.stderr.write(
        "deploy-config preflight: repo paths not covered by targets or excludes:\n"
    )
    for u in drift_roots:
        sys.stderr.write(f"  {u}\n")
    sys.stderr.write(
        "Add each entry to scripts/deploy-config.yml under `targets:` (with a\n"
        "category, to deploy it) or under `excludes:` (to intentionally keep\n"
        "it local and out of the deploy rsync).\n"
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-categories", help="list unique category names")

    p_lt = sub.add_parser("list-targets", help="list target paths, optionally filtered")
    p_lt.add_argument("category", nargs="?", default=None)

    p_cl = sub.add_parser("classify", help="map files to categories")
    p_cl.add_argument("files", nargs="*")

    sub.add_parser("preflight", help="fail on uncovered top-level entries")

    args = parser.parse_args()
    config = _load()

    if args.cmd == "list-categories":
        return cmd_list_categories(config)
    if args.cmd == "list-targets":
        return cmd_list_targets(config, args.category)
    if args.cmd == "classify":
        files = args.files or [line.strip() for line in sys.stdin if line.strip()]
        return cmd_classify(config, files)
    if args.cmd == "preflight":
        return cmd_preflight(config)
    parser.error(f"unknown subcommand {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
