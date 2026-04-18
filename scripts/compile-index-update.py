#!/usr/bin/env python3
# scripts/compile-index-update.py
# Atomic 4-index helper for /compile.
#
# Ported from K2B (Tier 1 audit fix #5) on Phase 1 Session 2.
#
# Usage:
#   compile-index-update.py <raw-source-path> <updated-csv> <created-csv>
#
# What it does (single entry point for updating the 4 indexes atomically):
#   1. Resolves the raw source path plus every updated/created wiki page to
#      its deepest containing "index.md" (nested-aware). A single run can
#      touch pages in multiple subfolders; each affected subfolder index is
#      rewritten exactly once.
#   2. Parses the live vault format in place:
#        - subfolder index:  "Last updated: YYYY-MM-DD | Entries: N<tail>"
#        - master wiki/index.md: "| Folder | Purpose | Entries |" 3-column
#          table under "## Subfolders", optional "**Total wiki pages: N**" line.
#      If any target's shape is unrecognized, exits 1 before writing anything.
#   3. Stages every rewrite into a sibling ".tmp" file, then atomic-renames
#      each into place.
#   4. Appends the compile log entry via scripts/wiki-log-append.sh.
#      If the log append fails, exits 2 -- the indices are already written,
#      so loud failure is preferred over silent.
#
# Lock pattern: mkdir-only at /tmp/k2bi-compile-index.lock.d, same as
# scripts/wiki-log-append.sh. The flock fallback path is intentionally
# omitted -- compile runs are short and the mkdir path is sufficient.
#
# Exit codes:
#   0  ok
#   1  validation failure (bad args, unrecognized shape, missing vault/file)
#   2  partial write (renames succeeded but log append failed, or rename
#      failure mid-batch)
#   3  lock timeout (~10s)
#
# Format rule: no em dashes anywhere. Use "--".

import os
import re
import subprocess
import sys
import time
from datetime import date


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAULT_ROOT = os.environ.get(
    "K2BI_VAULT_ROOT",
    os.path.expanduser("~/Projects/K2B-Investment-Vault"),
)
WIKI_LOG_APPEND = os.environ.get(
    "K2BI_WIKI_LOG_APPEND",
    os.path.join(REPO_ROOT, "scripts", "wiki-log-append.sh"),
)
LOCK_DIR = os.environ.get(
    "K2BI_COMPILE_INDEX_LOCK",
    "/tmp/k2bi-compile-index.lock.d",
)
LOCK_MAX_TRIES = 200  # 200 * 0.05s = 10s
LOCK_SLEEP = 0.05

TODAY = date.today().isoformat()

# Subfolder index header line. Captures:
#   g1 = "Last updated: "
#   g2 = the date (replaced by today)
#   g3 = " | Entries: "
#   g4 = the old count (replaced by recomputed count)
#   g5 = the rest of the line (preserved verbatim; may be empty or may carry
#        human commentary such as " top-level + 1 subfolder (...)").
SUBFOLDER_LINE_RE = re.compile(
    r"^(Last updated: )(\d{4}-\d{2}-\d{2})( \| Entries: )(\d+)([^\n]*)$",
    re.MULTILINE,
)

MASTER_TABLE_HEADER = "| Folder | Purpose | Entries |"

# Master table data row:
#   g1 = display text, e.g. "tickers/"
#   g2 = link href, e.g. "tickers/index.md"
#   g3 = purpose text (between second and third pipes)
#   g4 = old count
MASTER_ROW_RE = re.compile(
    r"^\|\s*\[([^\]]+)\]\(([^)]+)\)\s*\|\s*([^|]*?)\s*\|\s*(\d+)\s*\|\s*$",
    re.MULTILINE,
)

# Master total line, e.g. "**Total wiki pages: 42** (some commentary)"
MASTER_TOTAL_RE = re.compile(
    r"^(\*\*Total wiki pages: )(\d+)(\*\*[^\n]*)$",
    re.MULTILINE,
)


def die(code, msg):
    sys.stderr.write("compile-index-update: " + msg + "\n")
    sys.exit(code)


def acquire_lock():
    tries = 0
    while True:
        try:
            os.mkdir(LOCK_DIR)
            return
        except FileExistsError:
            tries += 1
            if tries > LOCK_MAX_TRIES:
                die(3, "could not acquire lock " + LOCK_DIR + " after 10s")
            time.sleep(LOCK_SLEEP)


def release_lock():
    try:
        os.rmdir(LOCK_DIR)
    except FileNotFoundError:
        pass


def to_vault_rel(p):
    # Accept absolute or vault-relative. Return vault-relative posix path.
    if os.path.isabs(p):
        return os.path.relpath(os.path.abspath(p), VAULT_ROOT)
    return p


def resolve_subfolder_index(page_rel):
    # Walk up from the page's parent until we find an index.md inside the vault.
    # Returns the vault-relative path to that index.md, or None if nothing
    # matches before we walk out of the vault.
    abs_page = os.path.join(VAULT_ROOT, page_rel)
    vault_abs = os.path.abspath(VAULT_ROOT)
    current = os.path.dirname(os.path.abspath(abs_page))
    while True:
        if not current.startswith(vault_abs):
            return None
        if current == vault_abs:
            return None
        idx = os.path.join(current, "index.md")
        if os.path.isfile(idx):
            return os.path.relpath(idx, VAULT_ROOT)
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def count_pages(dir_abs):
    # Non-recursive: count .md files in dir_abs, excluding index.md.
    if not os.path.isdir(dir_abs):
        return 0
    n = 0
    for entry in os.listdir(dir_abs):
        if not entry.endswith(".md") or entry == "index.md":
            continue
        if os.path.isfile(os.path.join(dir_abs, entry)):
            n += 1
    return n


def rewrite_subfolder_index(index_rel):
    index_abs = os.path.join(VAULT_ROOT, index_rel)
    dir_abs = os.path.dirname(index_abs)
    new_count = count_pages(dir_abs)
    with open(index_abs, "r", encoding="utf-8") as f:
        content = f.read()
    if not SUBFOLDER_LINE_RE.search(content):
        die(1, "unrecognized subfolder index shape: " + index_rel)

    def sub(match):
        return (
            match.group(1)
            + TODAY
            + match.group(3)
            + str(new_count)
            + match.group(5)
        )

    new_content, n = SUBFOLDER_LINE_RE.subn(sub, content, count=1)
    if n != 1:
        die(1, "failed to rewrite subfolder index: " + index_rel)
    return new_content


def rewrite_master_index():
    master_rel = "wiki/index.md"
    master_abs = os.path.join(VAULT_ROOT, master_rel)
    if not os.path.isfile(master_abs):
        die(1, "master index missing: " + master_rel)
    with open(master_abs, "r", encoding="utf-8") as f:
        content = f.read()
    if MASTER_TABLE_HEADER not in content:
        die(1, "unrecognized master index shape: missing 3-column header line")
    rows = MASTER_ROW_RE.findall(content)
    if not rows:
        die(1, "unrecognized master index shape: no data rows")

    row_new_counts = {}
    total = 0
    for display, href, _purpose, _old in rows:
        if not href.endswith("/index.md"):
            die(1, "unrecognized master row href (must end in /index.md): " + href)
        sub_rel = href[: -len("/index.md")]
        dir_abs = os.path.join(VAULT_ROOT, "wiki", sub_rel)
        cnt = count_pages(dir_abs)
        row_new_counts[(display, href)] = cnt
        total += cnt

    def row_sub(match):
        display = match.group(1)
        href = match.group(2)
        purpose = match.group(3)
        cnt = row_new_counts[(display, href)]
        return "| [" + display + "](" + href + ") | " + purpose + " | " + str(cnt) + " |"

    new_content = MASTER_ROW_RE.sub(row_sub, content)

    if MASTER_TOTAL_RE.search(new_content):
        def total_sub(match):
            return match.group(1) + str(total) + match.group(3)

        new_content = MASTER_TOTAL_RE.sub(total_sub, new_content, count=1)

    return new_content


def stage_write(path_rel, new_content, tmp_paths):
    abs_path = os.path.join(VAULT_ROOT, path_rel)
    tmp_path = abs_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    tmp_paths.append((tmp_path, abs_path))


def cleanup_tmps(tmp_paths):
    for tmp, _final in tmp_paths:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass


def main():
    if len(sys.argv) != 4:
        die(
            1,
            "usage: compile-index-update.py <raw-source> <updated-csv> <created-csv>",
        )
    raw_arg = sys.argv[1].strip()
    updated_csv = sys.argv[2]
    created_csv = sys.argv[3]
    if not raw_arg:
        die(1, "raw source path is required")

    updated = [p.strip() for p in updated_csv.split(",") if p.strip()]
    created = [p.strip() for p in created_csv.split(",") if p.strip()]
    if not updated and not created:
        die(1, "updated and created lists are both empty")

    if not os.path.isdir(VAULT_ROOT):
        die(1, "vault root not found: " + VAULT_ROOT)

    raw_rel = to_vault_rel(raw_arg)
    updated_rel = [to_vault_rel(p) for p in updated]
    created_rel = [to_vault_rel(p) for p in created]

    # Resolve each page to its owning subfolder index. dict preserves
    # insertion order so a deterministic iteration order falls out naturally.
    targets = {}
    raw_index = resolve_subfolder_index(raw_rel)
    if raw_index is None:
        die(1, "cannot resolve raw subfolder index for: " + raw_rel)
    targets[raw_index] = None

    for p in updated_rel + created_rel:
        idx = resolve_subfolder_index(p)
        if idx is None:
            die(1, "cannot resolve subfolder index for: " + p)
        targets[idx] = None

    # Build new content for every touched subfolder index. This reads and
    # validates every target before we take the lock or touch disk.
    rewrites = []  # list of (vault-relative path, new_content)
    for idx_rel in targets.keys():
        new_content = rewrite_subfolder_index(idx_rel)
        rewrites.append((idx_rel, new_content))

    master_new = rewrite_master_index()
    rewrites.append(("wiki/index.md", master_new))

    # Stage 1: acquire lock, write every rewrite to a sibling tempfile.
    acquire_lock()
    tmp_paths = []
    try:
        for rel, new_content in rewrites:
            stage_write(rel, new_content, tmp_paths)

        # Stage 2: atomic rename each tempfile into place. On POSIX this is
        # per-file atomic; if a rename fails mid-batch we report exit 2
        # (partial write) so the caller knows to audit.
        try:
            for tmp, final in tmp_paths:
                os.rename(tmp, final)
        except OSError as e:
            sys.stderr.write(
                "compile-index-update: rename failed mid-batch: " + str(e) + "\n"
            )
            sys.exit(2)

        # Stage 3: append to wiki/log.md via the single-writer helper. If it
        # fails, the indices are already on disk; exit 2 surfaces that loudly
        # so Keith knows to audit rather than silently losing the audit-trail
        # entry.
        updated_field = ",".join(updated_rel) if updated_rel else "(none)"
        created_field = ",".join(created_rel) if created_rel else "(none)"
        summary = "updated: " + updated_field + " | created: " + created_field
        result = subprocess.run(
            [WIKI_LOG_APPEND, "/compile", raw_rel, summary],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            sys.stderr.write(
                "compile-index-update: wiki-log-append.sh failed (rc="
                + str(result.returncode)
                + "); indices already written\n"
            )
            if result.stderr:
                sys.stderr.write(result.stderr.decode("utf-8", errors="replace"))
            sys.exit(2)
    finally:
        cleanup_tmps(tmp_paths)
        release_lock()


if __name__ == "__main__":
    main()
