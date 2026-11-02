"""SIM baselines rebuild generated Target inputs with the Project's post-setup hook.

A Target can consume a file that Git ignores and that the Project's
``.booley/hooks/post-setup`` hook builds from tracked sources (a firmware
image, a copied simulator main). The baseline checkout starts without it, so
the baseline Job runs the entry hook there and proves the file as built from
base sources.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from booley.flows.source_fingerprint import compute_source_fingerprint, hash_named_files
from booley.goals.freshness import DEFAULT_RESOLVERS
from booley.mcp import sim_baseline_execution
from booley.mcp.baseline_preparation import HOOK_PROOF, untracked_digests
from booley.mcp.sim_baseline_execution import contents_stamp
from tests.goals.conftest import enter_goals, git, install_paired_project, run_row
from tests.goals.test_sim_baseline import _complete

pytestmark = pytest.mark.timeout(120)

GENERATED = "fw.hex"
# Builds the "firmware" from its tracked source; Booley runs it with the checkout as cwd.
HOOK = b"""\
import os
from pathlib import Path

root = Path(os.environ["BOOLEY_WORKTREE"])
(root / "fw.hex").write_text((root / "fw.src").read_text().upper())
"""


def _with_generated_dependency(work_dir, *, target):
    """The real source fingerprint, plus the firmware source and image as testbench inputs."""
    fingerprint = compute_source_fingerprint(work_dir, target=target)
    if target is not None:
        fingerprint["tb"] = hash_named_files(work_dir, ["fw.src", GENERATED])
    return fingerprint


@pytest.fixture
def generated(layout, monkeypatch):
    """A Goal whose ``top`` Target consumes an ignored, hook-built ``fw.hex``."""
    resolvers = replace(DEFAULT_RESOLVERS, source=_with_generated_dependency)
    monkeypatch.setattr(sim_baseline_execution, "DEFAULT_RESOLVERS", resolvers)
    (layout.worktree / ".gitignore").write_bytes(b"/fw.hex\n")
    (layout.worktree / "fw.src").write_bytes(b"base firmware\n")
    git(layout.worktree, "add", ".gitignore", "fw.src")
    git(layout.worktree, "commit", "-q", "-m", "firmware source")
    (layout.worktree / GENERATED).write_bytes(b"BASE FIRMWARE\n")
    return SimpleNamespace(**vars(layout), resolvers=resolvers, launches=[])


def _install_hook(layout, body: bytes = HOOK) -> None:
    hook = layout.worktree / ".booley" / "hooks" / "post-setup.py"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_bytes(body)


def _enter(layout) -> SimpleNamespace:
    record = enter_goals(layout, [{"family": "SIM", "target": "top"}])
    # The candidate edits the firmware source and rebuilds the image.
    (layout.worktree / "fw.src").write_bytes(b"candidate firmware\n")
    (layout.worktree / GENERATED).write_bytes(b"CANDIDATE FIRMWARE\n")
    return SimpleNamespace(**vars(layout), record=record)


def _execute(seen):
    def execute(checkout, target, runs, output):
        seen.append((checkout / GENERATED).read_bytes())
        resolvers = sim_baseline_execution.DEFAULT_RESOLVERS
        stamp = contents_stamp(resolvers.fingerprint(checkout, target=target))
        return {"rows": [run_row(run) for run in runs], "stamps": [stamp]}

    return execute


def test_hook_rebuilds_generated_input_from_base_sources(generated):
    _install_hook(generated)
    mode = _enter(generated)
    seen: list[bytes] = []

    rows = _complete(mode, _execute(seen))

    assert seen == [b"BASE FIRMWARE\n"]
    assert rows[0]["grade"] == "pass"
    assert rows[0]["proof"]["dependencies"][GENERATED] == HOOK_PROOF
    assert rows[0]["proof"]["dependencies"]["fw.src"] == mode.record.base_sha


def test_generated_input_without_a_hook_stays_unavailable(generated):
    mode = _enter(generated)

    with pytest.raises(ValueError, match=f"differs from base or is unavailable: {GENERATED}"):
        _complete(mode, _execute([]))


def test_failing_hook_names_its_error_for_a_dependency_it_did_not_build(generated):
    _install_hook(generated, b"raise SystemExit('no toolchain')\n")
    mode = _enter(generated)

    with pytest.raises(
        ValueError, match=r"unavailable: fw\.hex \(post-setup hook failed.*no toolchain"
    ):
        _complete(mode, _execute([]))


def test_failing_hook_leaves_a_target_without_generated_inputs_available(layout):
    _install_hook(layout, b"raise SystemExit('no toolchain')\n")
    record = enter_goals(layout, [{"family": "SIM", "target": "top"}])
    mode = SimpleNamespace(**vars(layout), record=record, launches=[])

    assert _complete(mode)[0]["grade"] == "pass"


def test_hook_output_changed_after_the_hook_is_rejected(generated):
    _install_hook(generated)
    mode = _enter(generated)

    def execute(checkout, target, runs, output):
        (checkout / GENERATED).write_bytes(b"TAMPERED\n")
        return _execute([])(checkout, target, runs, output)

    with pytest.raises(ValueError, match="contents differ from the proven base fingerprint"):
        _complete(mode, execute)


def test_hook_that_rewrites_a_tracked_file_is_rejected(generated):
    _install_hook(
        generated,
        HOOK + b'(root / "fw.src").write_text("rewritten by the hook\\n")\n',
    )
    mode = _enter(generated)

    with pytest.raises(ValueError, match=r"differs from base or is unavailable: fw\.src"):
        _complete(mode, _execute([]))


def test_untracked_digests_cover_a_paired_booley_directory_once(layout, tmp_path):
    paired = install_paired_project(layout, tmp_path)
    (layout.worktree / "built.hex").write_bytes(b"outer\n")
    (paired / "sim").mkdir()
    (paired / "sim" / "main.cc").write_bytes(b"paired\n")
    (layout.worktree / "link.hex").symlink_to("built.hex")

    digests = untracked_digests(layout.worktree)

    # The paired checkout's files are listed once, by its own repository.
    assert {"built.hex", ".booley/sim/main.cc"} <= set(digests)
    assert not any(name.startswith(".booley/.git") for name in digests)
    assert "link.hex" not in digests
    assert digests["built.hex"] == hashlib.sha256(b"outer\n").hexdigest()
