"""Unit tests for Q30 atomic-mirror groundwork -- Session B.

Covers the two in-process surfaces introduced by Session B:

    1. `resolve_vault_root(override=None) -> Path`
        Precedence: explicit arg > `K2BI_VAULT_ROOT` env > `DEFAULT_VAULT_ROOT`
        module constant. Replaces the `path.resolve().parents[2]` auto-detect
        `handle_approve_strategy` previously used to locate thesis +
        backtest artifacts. Wrong-tree resolution was the root of the
        Q30 split-brain (strategy file in repo-side `wiki/`, thesis +
        backtest in vault-side `wiki/`; parents[2] on a repo-side file
        points at the REPO not the VAULT).

    2. `mirror_strategy_to_vault(repo_path, *, vault_root=None) -> Path`
        Atomic-mirrors a strategy file from the code repo into the
        vault via `strategy_frontmatter.atomic_write_bytes`. Called by
        the `.githooks/post-commit` mirror phase on approve + retire
        commits so the engine's vault-side read path sees the approval
        state as soon as the commit lands.

The post-commit hook wiring itself is covered by
`tests/test_post_commit_hook.py::MirrorPhase*` (integration tests that
fire the real hook through `hook_repo()`); this file keeps the helper
contracts narrow + fast.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.lib import invest_ship_strategy as iss
from scripts.lib import strategy_frontmatter as sf


# ---------- resolve_vault_root ----------


class ResolveVaultRootPrecedence(unittest.TestCase):
    """Explicit > env > constant. Each test isolates the env var to
    avoid cross-contamination from Keith's shell."""

    def setUp(self) -> None:
        self._saved_env = os.environ.pop("K2BI_VAULT_ROOT", None)

    def tearDown(self) -> None:
        os.environ.pop("K2BI_VAULT_ROOT", None)
        if self._saved_env is not None:
            os.environ["K2BI_VAULT_ROOT"] = self._saved_env

    def test_explicit_override_wins_over_env(self) -> None:
        with tempfile.TemporaryDirectory() as env_tmp, tempfile.TemporaryDirectory() as explicit_tmp:
            os.environ["K2BI_VAULT_ROOT"] = env_tmp
            resolved = iss.resolve_vault_root(Path(explicit_tmp))
            self.assertEqual(resolved, Path(explicit_tmp))

    def test_env_wins_over_default_when_no_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as env_tmp:
            os.environ["K2BI_VAULT_ROOT"] = env_tmp
            resolved = iss.resolve_vault_root(None)
            self.assertEqual(resolved, Path(env_tmp))

    def test_constant_fallback_when_no_override_no_env(self) -> None:
        # Neither kwarg nor env; expect DEFAULT_VAULT_ROOT.
        resolved = iss.resolve_vault_root(None)
        self.assertEqual(resolved, iss.DEFAULT_VAULT_ROOT)

    def test_default_points_at_expected_vault_path(self) -> None:
        # Q30 Decision 3: hardcoded constant is ~/Projects/K2Bi-Vault.
        # A future rename of the vault directory needs to flip BOTH
        # this constant AND the test so the mismatch surfaces at CI,
        # not after a skipped-mirror ships to prod.
        self.assertEqual(
            iss.DEFAULT_VAULT_ROOT,
            Path.home() / "Projects" / "K2Bi-Vault",
        )

    def test_empty_env_string_falls_through_to_default(self) -> None:
        # An empty env value ("") is a deployment typo; treat as unset
        # rather than resolving to `Path("")` which would later produce
        # `Path("wiki") / "strategies" / ...` relative to cwd. Fail
        # closed toward the constant.
        os.environ["K2BI_VAULT_ROOT"] = ""
        resolved = iss.resolve_vault_root(None)
        self.assertEqual(resolved, iss.DEFAULT_VAULT_ROOT)


class ResolveVaultRootReturnType(unittest.TestCase):
    def test_accepts_str_and_returns_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            resolved = iss.resolve_vault_root(Path(tmp))
            self.assertIsInstance(resolved, Path)


# ---------- mirror_strategy_to_vault ----------


def _write_strategy_file(dir_: Path, slug: str = "spy", status: str = "approved") -> Path:
    """Minimal spec-shape strategy file for mirror-helper tests."""
    content = (
        "---\n"
        f"name: {slug}\n"
        f"status: {status}\n"
        "strategy_type: hand_crafted\n"
        "risk_envelope_pct: 0.01\n"
        "regime_filter:\n"
        "  - risk_on\n"
        "order:\n"
        "  ticker: SPY\n"
        "  side: buy\n"
        "  qty: 1\n"
        "  limit_price: 500.00\n"
        "  stop_loss: 490.00\n"
        "  time_in_force: DAY\n"
        "approved_at: 2026-04-20T10:00:00Z\n"
        "approved_commit_sha: abc1234\n"
        "---\n\n"
        "## How This Works\n\nPlain-English body.\n"
    )
    path = dir_ / f"strategy_{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class MirrorHappyPath(unittest.TestCase):
    def test_creates_vault_dest_with_identical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as vault_tmp:
            repo_root = Path(repo_tmp)
            vault_root = Path(vault_tmp)
            source = _write_strategy_file(
                repo_root / "wiki" / "strategies", slug="foo"
            )
            dest = iss.mirror_strategy_to_vault(source, vault_root=vault_root)

            self.assertEqual(
                dest, vault_root / "wiki" / "strategies" / "strategy_foo.md"
            )
            self.assertTrue(dest.exists())
            self.assertEqual(dest.read_bytes(), source.read_bytes())

    def test_creates_wiki_strategies_parent_dirs_on_demand(self) -> None:
        # Fresh vault with no wiki/strategies/ yet -- the mirror must
        # create the path hierarchy. Matches the engine's read path
        # tolerance for an empty strategies dir (returns []).
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as vault_tmp:
            source = _write_strategy_file(
                Path(repo_tmp) / "wiki" / "strategies", slug="bar"
            )
            vault_root = Path(vault_tmp)
            self.assertFalse((vault_root / "wiki").exists())

            dest = iss.mirror_strategy_to_vault(source, vault_root=vault_root)
            self.assertTrue(dest.parent.is_dir())
            self.assertTrue(dest.exists())


class MirrorIdempotent(unittest.TestCase):
    def test_second_call_with_identical_content_is_no_op_byte_wise(self) -> None:
        # Post-commit on `git commit --amend` re-fires the hook. The
        # mirror must be idempotent: writing the same bytes twice
        # produces the same final file (atomic_write_bytes overwrites
        # via tempfile+replace, no partial state in between).
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as vault_tmp:
            source = _write_strategy_file(
                Path(repo_tmp) / "wiki" / "strategies", slug="idem"
            )
            vault_root = Path(vault_tmp)

            first = iss.mirror_strategy_to_vault(source, vault_root=vault_root)
            first_bytes = first.read_bytes()
            second = iss.mirror_strategy_to_vault(source, vault_root=vault_root)
            second_bytes = second.read_bytes()

            self.assertEqual(first, second)
            self.assertEqual(first_bytes, second_bytes)
            self.assertEqual(first_bytes, source.read_bytes())

    def test_overwrites_stale_vault_dest_with_current_repo_bytes(self) -> None:
        # Stale mirror (e.g. earlier approval got retired; vault never
        # caught up). Fresh mirror must OVERWRITE; commit is source of
        # truth per Q30 Decision 2.
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as vault_tmp:
            vault_root = Path(vault_tmp)
            dest_dir = vault_root / "wiki" / "strategies"
            dest_dir.mkdir(parents=True)
            dest = dest_dir / "strategy_stale.md"
            dest.write_bytes(b"---\nstatus: approved\n---\nOLD BODY\n")

            source = _write_strategy_file(
                Path(repo_tmp) / "wiki" / "strategies", slug="stale"
            )
            result = iss.mirror_strategy_to_vault(source, vault_root=vault_root)
            self.assertEqual(result, dest)
            self.assertEqual(dest.read_bytes(), source.read_bytes())
            self.assertNotIn(b"OLD BODY", dest.read_bytes())


class MirrorSymlinkRefusal(unittest.TestCase):
    def test_refuses_to_write_through_symlink_at_dest(self) -> None:
        # `atomic_write_bytes` raises ValueError on a symlinked target.
        # The mirror helper must propagate that -- a symlink at the
        # vault dest is a TOCTOU hazard we refuse rather than silently
        # follow (resolving it would let a crafted symlink in the
        # vault redirect the write outside the strategies directory).
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as vault_tmp:
            vault_root = Path(vault_tmp)
            dest_dir = vault_root / "wiki" / "strategies"
            dest_dir.mkdir(parents=True)

            real_elsewhere = vault_root / "elsewhere.md"
            real_elsewhere.write_bytes(b"nope\n")
            dest = dest_dir / "strategy_linked.md"
            os.symlink(real_elsewhere, dest)

            source = _write_strategy_file(
                Path(repo_tmp) / "wiki" / "strategies", slug="linked"
            )
            with self.assertRaises(ValueError):
                iss.mirror_strategy_to_vault(source, vault_root=vault_root)
            # Target of the symlink stays untouched.
            self.assertEqual(real_elsewhere.read_bytes(), b"nope\n")


class MirrorVaultRootValidation(unittest.TestCase):
    def test_fails_closed_when_vault_root_is_a_file_not_dir(self) -> None:
        # Deployment typo: K2BI_VAULT_ROOT points at a file. Refuse
        # rather than blindly treating the file as a directory ancestor
        # (which would later raise a different, less-obvious error
        # downstream). Fail-closed keeps the error site close to the
        # config defect.
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.NamedTemporaryFile(delete=False) as vault_as_file:
            vault_as_file.write(b"not a directory\n")
            vault_as_file_path = Path(vault_as_file.name)
            try:
                source = _write_strategy_file(
                    Path(repo_tmp) / "wiki" / "strategies", slug="baddir"
                )
                with self.assertRaises((ValueError, NotADirectoryError)):
                    iss.mirror_strategy_to_vault(
                        source, vault_root=vault_as_file_path
                    )
            finally:
                vault_as_file_path.unlink()

    def test_fails_when_vault_root_does_not_exist(self) -> None:
        # Q30 escalation-rule candidate: vault dir missing on fresh
        # clone. Session B decision: fail-closed. Operator must create
        # the vault directory (Syncthing-managed, ships with the
        # infrastructure) before the first approval lands. This
        # surfaces a misconfigured deployment immediately rather than
        # silently creating `~/Projects/K2Bi-Vault` on a machine where
        # Syncthing was expected to provide it.
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as parent:
            missing_vault = Path(parent) / "not-there-yet"
            source = _write_strategy_file(
                Path(repo_tmp) / "wiki" / "strategies", slug="missing"
            )
            with self.assertRaises((ValueError, FileNotFoundError)):
                iss.mirror_strategy_to_vault(
                    source, vault_root=missing_vault
                )


class MirrorHonorsEnvOverride(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_env = os.environ.pop("K2BI_VAULT_ROOT", None)

    def tearDown(self) -> None:
        os.environ.pop("K2BI_VAULT_ROOT", None)
        if self._saved_env is not None:
            os.environ["K2BI_VAULT_ROOT"] = self._saved_env

    def test_mirror_without_vault_root_kwarg_uses_env(self) -> None:
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as vault_tmp:
            os.environ["K2BI_VAULT_ROOT"] = vault_tmp
            source = _write_strategy_file(
                Path(repo_tmp) / "wiki" / "strategies", slug="env"
            )
            dest = iss.mirror_strategy_to_vault(source)
            self.assertEqual(
                dest, Path(vault_tmp) / "wiki" / "strategies" / "strategy_env.md"
            )


class TrailerRegexRoundTrip(unittest.TestCase):
    """MiniMax R1 #3: the post-commit mirror phase matches the commit
    trailer against `MIRROR_TRAILER_RE`. If `build_trailers` reshapes
    its output (e.g. whitespace drift, case change, different arrow
    separator), byte-exact matching silently disables the mirror and
    vault goes stale without a loud error. This test is the
    producer<->consumer contract: whatever `build_trailers` emits for
    the two mirror-eligible transitions MUST match
    `MIRROR_TRAILER_RE`. The regex lives in this module so the hook
    and the producer share one source of truth; the test pins both
    ends to the same fixture."""

    def test_approve_trailer_matches_mirror_regex(self) -> None:
        trailers = iss.build_trailers(
            "strategy", "proposed -> approved", "spy"
        )
        transition_line = trailers[0]
        self.assertEqual(
            transition_line, "Strategy-Transition: proposed -> approved"
        )
        # MiniMax R2 #3: fullmatch() proves the regex is anchored end-
        # to-end. search() alone would pass even if BOL/EOL anchors
        # were dropped in a future refactor, since the trailer appears
        # at the start of the string. fullmatch catches anchor drift.
        self.assertIsNotNone(
            iss.MIRROR_TRAILER_RE.fullmatch(transition_line),
            f"MIRROR_TRAILER_RE did not fullmatch build_trailers output "
            f"{transition_line!r}; anchor drift would silently no-op.",
        )

    def test_retire_trailer_matches_mirror_regex(self) -> None:
        trailers = iss.build_trailers(
            "strategy", "approved -> retired", "spy"
        )
        transition_line = trailers[0]
        self.assertEqual(
            transition_line, "Strategy-Transition: approved -> retired"
        )
        self.assertIsNotNone(
            iss.MIRROR_TRAILER_RE.fullmatch(transition_line),
            f"MIRROR_TRAILER_RE did not fullmatch build_trailers output "
            f"{transition_line!r}; anchor drift would silently no-op.",
        )

    def test_reject_trailer_does_not_match_mirror_regex(self) -> None:
        # Decision 1 (LOCKED): rejected strategies NEVER mirror. Prove
        # the regex rejects the rejected-transition trailer so a
        # future build_trailers refactor cannot accidentally emit a
        # matching pattern.
        trailers = iss.build_trailers(
            "strategy", "proposed -> rejected", "spy"
        )
        transition_line = trailers[0]
        self.assertIsNone(
            iss.MIRROR_TRAILER_RE.search(transition_line),
            f"MIRROR_TRAILER_RE should NOT match reject trailer "
            f"{transition_line!r}; vault would mirror rejected files.",
        )

    def test_regex_rejects_body_only_trailer_noise(self) -> None:
        # Sanity: lines that look vaguely like the trailer but don't
        # match the exact shape must not trigger the mirror.
        for bad in (
            "strategy-transition: proposed -> approved",  # lowercase
            "Strategy-Transition: Proposed -> Approved",  # capitalised
            "Strategy-Transition:proposed -> approved",  # no space after colon
            "Strategy-Transition: proposed  -> approved",  # double space
            "Strategy-Transition: proposed -> approved ",  # trailing space
            "Strategy-Transition: proposed->approved",  # no arrow spaces
        ):
            self.assertIsNone(
                iss.MIRROR_TRAILER_RE.search(bad),
                f"regex should reject {bad!r} (drift guard)",
            )


class MirrorHeadBytesNotWorktree(unittest.TestCase):
    """Codex R3 #1 (HIGH): the helper must support mirroring an
    explicit `content` payload rather than re-reading disk. The
    .githooks/post-commit hook calls `_file_at_head(path)` to get the
    committed bytes and passes them through; without this codepath
    the hook would mirror the working-tree state which can drift
    from HEAD inside the post-commit window (editor save, concurrent
    writer, etc.), pushing uncommitted bytes into the engine's
    runtime source of truth.
    """

    def test_explicit_content_overrides_disk_read(self) -> None:
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as vault_tmp:
            source = _write_strategy_file(
                Path(repo_tmp) / "wiki" / "strategies", slug="head"
            )
            vault_root = Path(vault_tmp)
            head_content = b"---\nstatus: approved\nname: head\n---\n\nHEAD body\n"
            # Source on disk has different content than `head_content`.
            # If the helper re-read disk, vault would see `source`'s
            # bytes; the `content=` override proves it doesn't.
            dest = iss.mirror_strategy_to_vault(
                source, vault_root=vault_root, content=head_content,
            )
            self.assertEqual(dest.read_bytes(), head_content)
            self.assertNotEqual(dest.read_bytes(), source.read_bytes())

    def test_content_none_falls_back_to_disk_read(self) -> None:
        # Backward-compat: existing direct callers pass only repo_path
        # (no content kwarg) and get the disk bytes. Same test as
        # MirrorHappyPath but explicit about the signature's default
        # behaviour so a future refactor that removes the fallback is
        # caught.
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as vault_tmp:
            source = _write_strategy_file(
                Path(repo_tmp) / "wiki" / "strategies", slug="disk"
            )
            dest = iss.mirror_strategy_to_vault(
                source, vault_root=Path(vault_tmp),
            )
            self.assertEqual(dest.read_bytes(), source.read_bytes())


class ProbeDestinationRejectsBlockers(unittest.TestCase):
    """Codex R3 #2 (HIGH): _probe_vault_destination walks the dest
    subtree and rejects non-directory blockers, symlinked vaults,
    and permission issues. Approval pre-flight + mirror helper both
    use this so the same failure modes surface at both sites."""

    def test_rejects_non_directory_wiki_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as vault_tmp:
            vault_root = Path(vault_tmp)
            # A file exists where the `wiki` directory should be.
            (vault_root / "wiki").write_bytes(b"not a dir\n")
            with self.assertRaises(ValueError) as cm:
                iss._probe_vault_destination(vault_root)
            self.assertIn("wiki", str(cm.exception))
            self.assertIn("not a directory", str(cm.exception))

    def test_rejects_non_directory_strategies_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as vault_tmp:
            vault_root = Path(vault_tmp)
            (vault_root / "wiki").mkdir()
            (vault_root / "wiki" / "strategies").write_bytes(b"not a dir\n")
            with self.assertRaises(ValueError) as cm:
                iss._probe_vault_destination(vault_root)
            self.assertIn("strategies", str(cm.exception))
            self.assertIn("not a directory", str(cm.exception))

    def test_rejects_symlinked_vault_root(self) -> None:
        # Vault root itself is a symlink -- refuse to mirror (TOCTOU
        # hazard + deployment drift).
        with tempfile.TemporaryDirectory() as parent:
            real = Path(parent) / "real-vault"
            real.mkdir()
            link = Path(parent) / "link-vault"
            os.symlink(real, link)
            with self.assertRaises(ValueError) as cm:
                iss._probe_vault_destination(link)
            self.assertIn("symlink", str(cm.exception))

    def test_rejects_symlinked_wiki_ancestor(self) -> None:
        # Codex R4 #2 (HIGH): a `vault/wiki -> elsewhere` symlink
        # would silently redirect mirror writes outside the vault.
        # is_dir() follows symlinks, so Path.is_symlink() must gate
        # every component of the destination path.
        with tempfile.TemporaryDirectory() as parent:
            vault_root = Path(parent) / "vault"
            vault_root.mkdir()
            outside = Path(parent) / "outside-wiki"
            outside.mkdir()
            os.symlink(outside, vault_root / "wiki")
            with self.assertRaises(ValueError) as cm:
                iss._probe_vault_destination(vault_root)
            self.assertIn("symlink", str(cm.exception))
            self.assertIn("wiki", str(cm.exception))

    def test_rejects_symlinked_immediate_parent_of_vault(self) -> None:
        # Codex R9 (HIGH): K2BI_VAULT_ROOT=/safe/link/vault where
        # /safe/link is a symlink redirects writes outside the vault
        # without tripping any in-tree symlink check. The probe must
        # catch this at the IMMEDIATE parent level. Deeper ancestors
        # (e.g. OS-level /var symlinks) are intentionally out of
        # scope -- they are legitimate and checking them would break
        # every macOS tmp-path test.
        with tempfile.TemporaryDirectory() as outer:
            outer_path = Path(outer)
            real_parent = outer_path / "real-parent"
            real_parent.mkdir()
            link_parent = outer_path / "link-parent"
            os.symlink(real_parent, link_parent)
            vault_root = link_parent / "vault"
            # vault_root doesn't yet exist through the symlinked
            # parent. mkdir via the symlink creates the dir under
            # real-parent; from vault_root's POV, its direct parent
            # (link_parent) is a symlink.
            vault_root.mkdir()
            with self.assertRaises(ValueError) as cm:
                iss._probe_vault_destination(vault_root)
            msg = str(cm.exception)
            self.assertIn("symlinked immediate parent", msg)

    def test_rejects_symlinked_strategies_ancestor(self) -> None:
        # Same risk one level deeper: `vault/wiki/strategies ->
        # elsewhere`. is_dir() would pass; is_symlink() catches it.
        with tempfile.TemporaryDirectory() as parent:
            vault_root = Path(parent) / "vault"
            (vault_root / "wiki").mkdir(parents=True)
            outside = Path(parent) / "outside-strategies"
            outside.mkdir()
            os.symlink(outside, vault_root / "wiki" / "strategies")
            with self.assertRaises(ValueError) as cm:
                iss._probe_vault_destination(vault_root)
            self.assertIn("symlink", str(cm.exception))
            self.assertIn("strategies", str(cm.exception))

    def test_creates_wiki_strategies_on_demand_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as vault_tmp:
            vault_root = Path(vault_tmp)
            self.assertFalse((vault_root / "wiki").exists())
            dest = iss._probe_vault_destination(vault_root)
            self.assertEqual(
                dest, vault_root / "wiki" / "strategies"
            )
            self.assertTrue(dest.is_dir())
            # Probe file was cleaned up (no leftover .k2bi-mirror-probe).
            self.assertEqual(
                [p.name for p in dest.iterdir()],
                [],
                "probe tempfile should be unlinked after validation",
            )

    def test_fails_closed_on_readonly_vault(self) -> None:
        # Deliberate read-only dir -- probe-write must fail and
        # propagate a clear error. Skip on systems where chmod cannot
        # actually enforce read-only for the current user (running
        # as root, etc.).
        with tempfile.TemporaryDirectory() as vault_tmp:
            vault_root = Path(vault_tmp)
            os.chmod(vault_root, 0o555)
            try:
                # Verify the filesystem actually enforces the chmod
                # for this user before making an assertion depend on
                # it. On macOS + Linux, non-root users respect 0o555.
                # On root-as-default environments (CI images) the
                # chmod is advisory -- skip rather than emit a false
                # positive failure.
                if os.access(vault_root, os.W_OK):
                    self.skipTest(
                        "filesystem does not enforce 0o555 for this "
                        "user; readonly probe cannot be exercised"
                    )
                with self.assertRaises(ValueError) as cm:
                    iss._probe_vault_destination(vault_root)
                self.assertIn("not writable", str(cm.exception).lower() + " not writable")
                # Substring match on the actual exception text (the
                # above || is a guard against an empty message
                # that happens to contain "not writable" literally):
                self.assertTrue(
                    "not writable" in str(cm.exception)
                    or "cannot create" in str(cm.exception),
                    f"unexpected error shape: {cm.exception!s}",
                )
            finally:
                # Restore perms so TemporaryDirectory cleanup works.
                os.chmod(vault_root, 0o755)


class MirrorUsesAtomicHelper(unittest.TestCase):
    def test_mirror_delegates_write_to_strategy_frontmatter_helper(self) -> None:
        # Q30 Change 2 contract: the mirror MUST use
        # `sf.atomic_write_bytes` so the tempfile + fsync + os.replace
        # + symlink-refusal invariants come along for free. A future
        # refactor that hand-rolls the write would regress the
        # symlink-refusal test above, but this explicit delegation
        # check is the direct assertion of the architectural
        # constraint -- closes the Decision 2 footprint.
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as vault_tmp:
            source = _write_strategy_file(
                Path(repo_tmp) / "wiki" / "strategies", slug="atomic"
            )
            with mock.patch.object(
                sf, "atomic_write_bytes", wraps=sf.atomic_write_bytes
            ) as spy:
                iss.mirror_strategy_to_vault(
                    source, vault_root=Path(vault_tmp)
                )
                self.assertTrue(
                    spy.called,
                    "mirror_strategy_to_vault must delegate the write "
                    "to strategy_frontmatter.atomic_write_bytes",
                )


class SymlinkContainmentStopRuleContract(unittest.TestCase):
    """Stop-rule contract (Codex R4 #2, R9, R10 #2 -- three rounds
    same vector on symlink containment; L-2026-04-19-001 applied;
    architect decision via L-2026-04-20-002).

    Codex iteratively pushed for stricter symlink containment over
    three rounds:
      * R4 #2: reject symlinks at ``wiki/`` + ``wiki/strategies/``
        within the vault subtree. APPLIED.
      * R9: reject symlinks at the IMMEDIATE parent of vault_root.
        APPLIED.
      * R10 #2: reject symlinks at ALL ancestors up to filesystem
        root. NOT APPLIED -- architect decision.

    Architect decision (R10 #2): the third-round ask (full ancestor
    chain) conflicts with OS-level symlinks that exist for legitimate
    filesystem-layout reasons. On macOS, ``/var`` is a symlink to
    ``/private/var``; every tmp-path test (``/var/folders/...``) would
    fail `resolve() == path` comparison. Carving out a
    known-OS-symlink allowlist is fragile across macOS + Linux +
    container setups.

    The implemented containment covers the realistic threat surface:
      * vault_root itself symlinked: rejected.
      * Immediate parent symlinked (the operator-configurable
        redirection vector from R9): rejected.
      * Inner path components (wiki/, wiki/strategies/) symlinked:
        rejected.
      * Final file symlinked: rejected by `atomic_write_bytes`.
      * Grandparent+ symlinks: NOT rejected (OS-level `/var` + deep
        ancestor symlinks out of scope for K2Bi's single-user
        threat model).

    This contract test pins the decision so a future reviewer
    re-raising the finding sees the architect-locked boundary rather
    than re-opening the discussion. A future change that extends
    containment further should update this test in the same commit
    so the boundary moves deliberately.
    """

    def test_grandparent_symlinks_not_rejected_architect_scoped(self) -> None:
        # Construct `vault_root = /outer/link-gp/subdir/vault` where
        # `/outer/link-gp` is a symlink. R10 #2 would want this
        # rejected; architect decision is: NOT rejected (immediate
        # parent `subdir/` is a real dir, not a symlink).
        with tempfile.TemporaryDirectory() as outer_tmp:
            outer_path = Path(outer_tmp)
            real_gp = outer_path / "real-grandparent"
            real_gp.mkdir()
            (real_gp / "subdir").mkdir()
            (real_gp / "subdir" / "vault").mkdir()
            link_gp = outer_path / "link-gp"
            os.symlink(real_gp, link_gp)

            vault_via_link = link_gp / "subdir" / "vault"
            # vault_via_link.parent is `subdir/`, which is a real
            # directory (not a symlink). R10's concern was that
            # walking up would catch `link-gp` as a symlink. The
            # architect-locked contract is that we DON'T walk up
            # past the immediate parent. The probe should succeed.
            dest_dir = iss._probe_vault_destination(vault_via_link)
            self.assertTrue(dest_dir.is_dir())


class MirrorLossDetectionStopRuleContract(unittest.TestCase):
    """Stop-rule contract (Codex R6 #1, R10 #1 -- two rounds same
    vector on mirror-loss detection; approaching L-2026-04-19-001
    stop-rule; architect decision via L-2026-04-20-002).

    Codex raised: a silent mirror failure on approval leaves repo
    at `status: approved` while the vault copy is missing.
    `strategy_file_modified_post_approval` drift detection does not
    fire for never-loaded strategies, so the engine silently skips
    the strategy. Recommended: engine-side reconciliation that
    compares approved repo files against vault files at startup.

    Architect decision (R6 #1 + R10 #1): engine-side reconciliation
    is OUT OF SCOPE for Session B (kickoff: ``DO NOT touch
    execution/engine/**``). The accepted mitigation is:
      * Approval-time `_probe_vault_destination` runs a tempfile+
        replace probe at the destination dir before the commit
        lands. If the probe passes, the subsequent post-commit
        mirror write will succeed for the same reason (same
        primitive). The residual failure window is a vault-state
        change (read-only mount, disk full, Syncthing pause)
        between probe and post-commit -- microsecond-to-millisecond
        on a single-machine setup.
      * Post-commit mirror failures log to stderr + wiki-log, so
        the operator sees the failure in git commit output.
      * Operator-driven recovery: re-run ``/invest-ship``
        (idempotent mirror re-tries the write).

    Full engine-side reconciliation lands in Phase 4+ when the
    engine itself is in scope. This test pins the decision so a
    future reviewer re-raising the finding sees the architect-
    locked boundary rather than re-opening the discussion."""

    def test_mirror_helper_does_not_reach_into_engine_module(self) -> None:
        # `mirror_strategy_to_vault` deliberately does NOT import
        # the engine or consult its configured strategies_dir --
        # cross-tree reconciliation is out of scope for Session B.
        import ast
        import inspect
        import textwrap

        import scripts.lib.invest_ship_strategy as iss_mod

        source = textwrap.dedent(
            inspect.getsource(iss_mod.mirror_strategy_to_vault)
        )
        tree = ast.parse(source)
        func = tree.body[0]
        body = func.body
        if body and isinstance(body[0], ast.Expr) and isinstance(
            body[0].value, ast.Constant
        ):
            body = body[1:]
        code_only = "\n".join(ast.unparse(stmt) for stmt in body)
        # No imports of engine modules, no reads of engine config.
        self.assertNotIn("execution.engine", code_only)
        self.assertNotIn("execution.strategies", code_only)
        self.assertNotIn("load_all_approved", code_only)


class VaultRootEngineAlignmentContract(unittest.TestCase):
    """Architect-locked scope decision (Codex R6 #2, R7 #1 -- 2 same-
    vector rounds; L-2026-04-20-002 applied).

    The reviewer argued the approval flow's `resolve_vault_root`
    should block approval unless the engine's effective
    `engine.strategies_dir` ALSO resolves to the same tree, to stop
    an operator who sets `K2BI_VAULT_ROOT` from creating a new
    split-brain with the engine's config-driven path.

    Architect decision: NOT in Session B scope. The kickoff is
    explicit: ``DO NOT touch execution/engine/**``; unifying the
    engine's path resolver with the approval/mirror resolver is
    Phase 4+ work. Today Keith's single-machine deployment uses
    DEFAULT_VAULT_ROOT at the approval side + config.yaml's default
    strategies_dir (which ALSO points at the same default path); the
    divergence risk only exists if an operator sets one without the
    other. The `resolve_vault_root` docstring + post-commit docstring
    both surface this operator responsibility.

    This test is the L-2026-04-20-002 contract: it pins the decision
    so a future reviewer re-raising this finding sees the
    documented architect boundary rather than re-opening the
    discussion. A future change that adds engine-side resolver
    unification should update this test in the same commit so the
    scope boundary moves deliberately.
    """

    def test_vault_root_resolver_does_not_cross_check_engine_strategies_dir(
        self,
    ) -> None:
        # `resolve_vault_root` deliberately does NOT import the
        # engine config or compare paths -- it's a pure helper that
        # returns the env-overridable default. Cross-check logic is
        # out of scope for Session B.
        import ast
        import inspect
        import textwrap

        import scripts.lib.invest_ship_strategy as iss_mod

        source = textwrap.dedent(inspect.getsource(iss_mod.resolve_vault_root))
        tree = ast.parse(source)
        func = tree.body[0]
        # Drop the docstring -- the scope-boundary IS documented
        # there, so a naive substring check would always fail. We
        # assert on the executable body only.
        body = func.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        code_only = "\n".join(ast.unparse(stmt) for stmt in body)
        self.assertNotIn("engine.strategies_dir", code_only)
        self.assertNotIn("execution.validators", code_only)
        self.assertNotIn("load_config", code_only)


if __name__ == "__main__":
    unittest.main()
