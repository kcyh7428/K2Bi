"""Tests for scripts.lib.pending_sync -- closes the Bundle 3 cycle 2
propagation gap where `/invest-sync`'s SKILL.md still hardcoded K2B's
category set instead of reading `scripts/deploy-config.yml`.

Invariants under test:
- Valid categories are loaded at runtime from deploy_config.yml --
  whatever the current repo declares is what gets accepted. This test
  file runs inside the K2Bi repo whose deploy-config.yml declares
  {execution, pm2, scripts, skills}, so we exercise that set directly.
- Any category NOT in the repo's config is flagged UNREADABLE. This
  catches K2B-native entries (e.g. category=code) that would leak in
  from a forked workflow before the invest-sync skill was updated.
- Malformed JSON, schema violations, stale .tmp_ producers are all
  UNREADABLE, not silently skipped.
- `delete_processed` only touches the caller-named files -- concurrent
  producers under different filenames are preserved (race-free
  lifecycle invariant the mailbox depends on).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from scripts.lib import pending_sync as ps


REPO_ROOT = Path(__file__).resolve().parent.parent


def _valid_entry_payload(
    *,
    categories: list[str] | None = None,
    files: list[str] | None = None,
    commit_sha: str = "abc1234",
    entry_id: str = "test-entry-1",
) -> dict:
    return {
        "pending": True,
        "set_at": "2026-04-19T00:00:00+00:00",
        "set_by_commit": commit_sha,
        "categories": categories if categories is not None else ["execution"],
        "files": files if files is not None else ["execution/dummy.py"],
        "entry_id": entry_id,
    }


def _write_mailbox_entry(mailbox: Path, name: str, payload: dict) -> Path:
    mailbox.mkdir(parents=True, exist_ok=True)
    path = mailbox / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class LoadValidCategoriesTests(unittest.TestCase):
    """Contract: K2Bi's deploy-config.yml is the source of truth for
    the category set. This test pins the live set so an accidental
    deploy-config.yml change that removes `execution` or `pm2` will
    trip here before it silently breaks `/invest-sync`."""

    def test_k2bi_categories_loaded_from_config(self):
        cats = ps.load_valid_categories(REPO_ROOT)
        self.assertIn("execution", cats)
        self.assertIn("skills", cats)
        self.assertIn("scripts", cats)
        self.assertIn("pm2", cats)
        # K2B-specific categories must NOT leak in -- the whole point
        # of this cycle 2 propagation fix.
        self.assertNotIn("code", cats)
        self.assertNotIn("dashboard", cats)

    def test_helper_output_matches_yaml_categories_field(self):
        """MiniMax R1 F2: yaml-symmetry guard. The helper's output
        must equal the set of `category:` values declared in
        `scripts/deploy-config.yml`. Without this check the two can
        drift (helper code refactor, yaml rename) and every pin-test
        above would keep passing against a stale helper.
        """
        import re

        config_path = REPO_ROOT / "scripts" / "deploy-config.yml"
        raw = config_path.read_text(encoding="utf-8")
        yaml_categories: set[str] = set()
        in_targets = False
        for line in raw.splitlines():
            stripped = line.rstrip()
            if stripped.startswith("targets:"):
                in_targets = True
                continue
            if in_targets and stripped and not stripped.startswith(" ") and not stripped.startswith("\t"):
                # left the targets: block
                if not stripped.startswith("-"):
                    in_targets = False
            if in_targets:
                m = re.match(r"\s*category:\s*([A-Za-z_][A-Za-z_0-9-]*)", stripped)
                if m:
                    yaml_categories.add(m.group(1))
        self.assertEqual(
            ps.load_valid_categories(REPO_ROOT),
            frozenset(yaml_categories),
            "helper output drifted from yaml `category:` fields",
        )

    def test_missing_helper_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Pass a "repo" that has no scripts/lib/deploy_config.py.
            with self.assertRaises(ValueError) as cm:
                ps.load_valid_categories(Path(tmp))
            self.assertIn("deploy_config", str(cm.exception))

    def test_empty_helper_output_raises_value_error(self):
        """MiniMax R1 F1+F3: if deploy-config.yml has no categories
        (or is mis-configured so the helper emits nothing), the
        loader must fail-closed rather than return an empty set
        that would make every mailbox entry flag as 'unknown
        category' with a confusing reason.
        """
        # Use a stub deploy_config.py that exits 0 with empty output.
        with tempfile.TemporaryDirectory() as tmp:
            stub_repo = Path(tmp)
            (stub_repo / "scripts" / "lib").mkdir(parents=True)
            stub = stub_repo / "scripts" / "lib" / "deploy_config.py"
            stub.write_text(
                "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)
            with self.assertRaises(ValueError) as cm:
                ps.load_valid_categories(stub_repo)
            self.assertIn("empty", str(cm.exception).lower())


class ScanMailboxHappyPath(unittest.TestCase):
    def test_empty_directory_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Path(tmp) / ".pending-sync"
            result = ps.scan_mailbox(mailbox, REPO_ROOT)
            self.assertEqual(result.state, "EMPTY")

    def test_missing_mailbox_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = ps.scan_mailbox(Path(tmp) / "missing", REPO_ROOT)
            self.assertEqual(result.state, "EMPTY")

    def test_valid_k2bi_category_accepted(self):
        # Happy path: entry with K2Bi-native category `execution` must
        # validate. This is the exact scenario the cycle 4 defer hit
        # and the old hardcoded K2B set rejected.
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Path(tmp) / ".pending-sync"
            payload = _valid_entry_payload(categories=["execution"])
            _write_mailbox_entry(mailbox, "a.json", payload)
            result = ps.scan_mailbox(mailbox, REPO_ROOT)
            self.assertEqual(result.state, "VALID")
            self.assertEqual(len(result.valid), 1)
            name, got_payload = result.valid[0]
            self.assertEqual(name, "a.json")
            self.assertEqual(got_payload["categories"], ["execution"])

    def test_all_k2bi_categories_accepted(self):
        # Every live category from deploy-config.yml must be accepted
        # as a mailbox category too; otherwise future skills that defer
        # under a less common category (pm2, later additions) would
        # silently fail.
        cats = sorted(ps.load_valid_categories(REPO_ROOT))
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Path(tmp) / ".pending-sync"
            payload = _valid_entry_payload(categories=cats)
            _write_mailbox_entry(mailbox, "all.json", payload)
            result = ps.scan_mailbox(mailbox, REPO_ROOT)
            self.assertEqual(result.state, "VALID", result.unreadable)

    def test_multiple_valid_entries_all_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Path(tmp) / ".pending-sync"
            for i in range(3):
                _write_mailbox_entry(
                    mailbox,
                    f"{i:02d}.json",
                    _valid_entry_payload(entry_id=f"e-{i}"),
                )
            result = ps.scan_mailbox(mailbox, REPO_ROOT)
            self.assertEqual(result.state, "VALID")
            self.assertEqual(len(result.valid), 3)


class ScanMailboxRejections(unittest.TestCase):
    def test_unknown_k2b_category_rejected(self):
        # This is the exact cycle 4 regression: K2B's hardcoded set
        # accepted `code`, K2Bi's does not. Proves the reverse
        # direction too -- a K2B mailbox entry accidentally dropped
        # into K2Bi is surfaced, not silently consumed.
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Path(tmp) / ".pending-sync"
            _write_mailbox_entry(
                mailbox,
                "bad.json",
                _valid_entry_payload(categories=["code"]),
            )
            result = ps.scan_mailbox(mailbox, REPO_ROOT)
            self.assertEqual(result.state, "UNREADABLE")
            self.assertEqual(len(result.unreadable), 1)
            name, reason = result.unreadable[0]
            self.assertEqual(name, "bad.json")
            self.assertIn("code", reason)
            self.assertIn("category:unknown", reason)

    def test_nonexistent_category_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Path(tmp) / ".pending-sync"
            _write_mailbox_entry(
                mailbox,
                "bad.json",
                _valid_entry_payload(categories=["totally-made-up"]),
            )
            result = ps.scan_mailbox(mailbox, REPO_ROOT)
            self.assertEqual(result.state, "UNREADABLE")

    def test_empty_categories_list_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Path(tmp) / ".pending-sync"
            _write_mailbox_entry(
                mailbox,
                "bad.json",
                _valid_entry_payload(categories=[]),
            )
            result = ps.scan_mailbox(mailbox, REPO_ROOT)
            self.assertEqual(result.state, "UNREADABLE")

    def test_malformed_json_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Path(tmp) / ".pending-sync"
            mailbox.mkdir()
            (mailbox / "oops.json").write_text("{not valid json", encoding="utf-8")
            result = ps.scan_mailbox(mailbox, REPO_ROOT)
            self.assertEqual(result.state, "UNREADABLE")
            name, reason = result.unreadable[0]
            self.assertEqual(name, "oops.json")
            self.assertIn("json:", reason)

    def test_schema_missing_fields_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Path(tmp) / ".pending-sync"
            mailbox.mkdir()
            (mailbox / "bad.json").write_text(
                json.dumps({"pending": True, "set_at": "x"}),
                encoding="utf-8",
            )
            result = ps.scan_mailbox(mailbox, REPO_ROOT)
            self.assertEqual(result.state, "UNREADABLE")
            _, reason = result.unreadable[0]
            self.assertIn("schema:missing", reason)

    def test_pending_false_entry_silently_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Path(tmp) / ".pending-sync"
            payload = _valid_entry_payload()
            payload["pending"] = False
            _write_mailbox_entry(mailbox, "done.json", payload)
            result = ps.scan_mailbox(mailbox, REPO_ROOT)
            self.assertEqual(result.state, "EMPTY")

    def test_non_json_files_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Path(tmp) / ".pending-sync"
            mailbox.mkdir()
            (mailbox / "README.md").write_text("ignored", encoding="utf-8")
            result = ps.scan_mailbox(mailbox, REPO_ROOT)
            self.assertEqual(result.state, "EMPTY")

    def test_valid_and_invalid_mixed_both_surfaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Path(tmp) / ".pending-sync"
            _write_mailbox_entry(mailbox, "good.json", _valid_entry_payload())
            _write_mailbox_entry(
                mailbox,
                "bad.json",
                _valid_entry_payload(categories=["code"]),
            )
            result = ps.scan_mailbox(mailbox, REPO_ROOT)
            self.assertEqual(result.state, "UNREADABLE")
            self.assertEqual(len(result.unreadable), 1)
            self.assertEqual(len(result.valid), 1)


class StaleTmpFiles(unittest.TestCase):
    def test_fresh_tmp_silently_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Path(tmp) / ".pending-sync"
            mailbox.mkdir()
            tmp_entry = mailbox / ".tmp_a.json"
            tmp_entry.write_text("partial", encoding="utf-8")
            result = ps.scan_mailbox(mailbox, REPO_ROOT)
            self.assertEqual(result.state, "EMPTY")

    def test_stale_tmp_surfaced_as_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Path(tmp) / ".pending-sync"
            mailbox.mkdir()
            tmp_entry = mailbox / ".tmp_stale.json"
            tmp_entry.write_text("partial", encoding="utf-8")
            # Inject a `now` far into the future to trigger the stale path.
            future = time.time() + 3600
            result = ps.scan_mailbox(mailbox, REPO_ROOT, now=future)
            self.assertEqual(result.state, "UNREADABLE")
            name, reason = result.unreadable[0]
            self.assertEqual(name, ".tmp_stale.json")
            self.assertIn("stale-temp", reason)


class DeleteProcessedTests(unittest.TestCase):
    def test_deletes_only_named_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Path(tmp) / ".pending-sync"
            mailbox.mkdir()
            a = _write_mailbox_entry(mailbox, "a.json", _valid_entry_payload())
            b = _write_mailbox_entry(mailbox, "b.json", _valid_entry_payload())
            c = _write_mailbox_entry(mailbox, "c.json", _valid_entry_payload())

            warnings = ps.delete_processed(mailbox, ["a.json", "c.json"])
            self.assertEqual(warnings, [])
            self.assertFalse(a.exists())
            self.assertFalse(c.exists())
            # b.json was NOT in the delete list (race-safety: a
            # concurrent producer wrote it under a different name).
            self.assertTrue(b.exists())

    def test_missing_file_is_benign(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Path(tmp) / ".pending-sync"
            mailbox.mkdir()
            warnings = ps.delete_processed(
                mailbox, ["never-existed.json"]
            )
            self.assertEqual(warnings, [])


class CLITests(unittest.TestCase):
    def _run(self, args: list[str], cwd: Path | None = None):
        return subprocess.run(
            [sys.executable, "-m", "scripts.lib.pending_sync", *args],
            capture_output=True,
            text=True,
            cwd=str(cwd or REPO_ROOT),
            check=False,
        )

    def test_scan_cli_empty_prints_EMPTY(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Path(tmp) / ".pending-sync"
            result = self._run(["scan", "--mailbox", str(mailbox)])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "EMPTY")

    def test_scan_cli_valid_entry_prints_VALID_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Path(tmp) / ".pending-sync"
            _write_mailbox_entry(mailbox, "a.json", _valid_entry_payload())
            result = self._run(["scan", "--mailbox", str(mailbox)])
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = result.stdout.strip().splitlines()
            self.assertTrue(lines[0].startswith("VALID|"))
            parsed = json.loads(lines[0].split("|", 1)[1])
            self.assertEqual(parsed[0][0], "a.json")

    def test_scan_cli_unknown_category_prints_UNREADABLE(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Path(tmp) / ".pending-sync"
            _write_mailbox_entry(
                mailbox,
                "bad.json",
                _valid_entry_payload(categories=["code"]),
            )
            result = self._run(["scan", "--mailbox", str(mailbox)])
            self.assertEqual(result.returncode, 0)
            self.assertTrue(result.stdout.strip().startswith("UNREADABLE|"))

    def test_delete_cli_removes_named_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Path(tmp) / ".pending-sync"
            mailbox.mkdir()
            a = _write_mailbox_entry(mailbox, "a.json", _valid_entry_payload())
            b = _write_mailbox_entry(mailbox, "b.json", _valid_entry_payload())

            result = self._run(
                [
                    "delete",
                    "--mailbox",
                    str(mailbox),
                    "--entries",
                    json.dumps(["a.json"]),
                ]
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(a.exists())
            self.assertTrue(b.exists())


if __name__ == "__main__":
    unittest.main()
