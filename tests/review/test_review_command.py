"""``booley review [<sha>]``: discover findings in one commit and store the Review Run (#3272).

Every test drives the real entry point (``booley -C <project> review ...``) against a temporary
Git repository, the real export, the real Declared Reviews loader and the real Reviewer behind
the shared ``ScriptedAgent``, never a live model.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from booley.harness import booley as cli
from tests.conftest import FeatureFlagHelper
from tests.mcp_tools.reviewer_support import ScriptedAgent, install_agent, issue
from tests.review.test_declared_reviews import SIM_CORE_ID, write_project
from tests.runtime.test_commit_export import (
    add_gitlink,
    commit_all,
    git,
    make_submodule,
    new_repository,
    write,
)

A_V1 = "module a(input logic x);\n  // v1\n  assign y = x;\nendmodule\n"
A_V2 = "module a(input logic x);\n  // v2 committed edit\n  assign y = x;\nendmodule\n"
SPEC = "# Spec\nSPEC-TEXT-V1: the output follows the input.\n"
TABLE = """\
[sim_core.review]
rtl = ["code_style"]
tb = ["quality"]
spec = "docs/spec.md"

[sim_fifo.review]
rtl = ["code_style"]
spec = "docs/spec.md"
"""
BUGS = '[sim_core.review]\nrtl = ["bugs"]\nresolve = "{resolve}"\n'


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A shell outside any Job; the export's scratch lives under the test's own directory."""
    for name in ("BOOLEY_RUN_ID", "_BOOLEY_CLI_INVOCATION_ORIGIN", "BOOLEY_DIR"):
        monkeypatch.delenv(name, raising=False)
    scratch_root = tmp_path / "scratch-root"
    scratch_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch_root))
    return scratch_root


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> ScriptedAgent:
    return install_agent(monkeypatch, snapshot=True)


def review(monkeypatch: pytest.MonkeyPatch, project: Path, *arguments: str) -> int:
    """Run ``booley -C <project> review <arguments>`` in a Sandbox; return its exit status."""
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: True)
    hand_written = cli.runtime_context.RuntimeLocation.HAND_WRITTEN_SANDBOX
    monkeypatch.setattr(cli.runtime_context, "runtime_location", lambda: hand_written)
    monkeypatch.setattr(cli, "_host_install_authority_error", lambda *_: None)
    # The parser reads the ambient Project's Feature Flags; dispatch reads the selected one's.
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "argv", ["booley", "-C", str(project), "review", *arguments])
    try:
        return cli._dispatch_main()
    except SystemExit as exit_request:
        return int(exit_request.code or 0)


def project_on_main(tmp_path: Path, flags: FeatureFlagHelper | None, reviews: str) -> Path:
    """A committed Project (Targets ``sim_core``, ``sim_fifo``) on ``main``, then ``feature``."""
    root = write_project(tmp_path / "proj", reviews)
    write(root / ".gitignore", "rtl/gen/\n")
    write(root / "rtl" / "a.sv", A_V1)
    write(root / "docs" / "spec.md", SPEC)
    if flags is not None:
        flags.enable(root / ".booley", "reviewed-commits")
    git(root, "init", "-q", "-b", "main")
    commit_all(root, "baseline")
    git(root, "checkout", "-q", "-b", "feature")
    return root


def stored_runs(root: Path, commit: str | None = None) -> list[dict[str, Any]]:
    """Every stored Review Run of *commit* (of any commit by default), oldest first."""
    folder = root / ".booley" / "reviews"
    pattern = f"{commit or '*'}/*.json"
    return [json.loads(path.read_bytes()) for path in sorted(folder.glob(pattern))]


def finding(origin: str = "introduced", **overrides: Any) -> dict[str, Any]:
    row = issue(
        **{"file": "rtl/a.sv", "line": 3, "ticket_clause": "Output follows input", **overrides}
    )
    row["origin"] = origin
    return row


def test_review_runs_needed_reviews_in_the_export_and_keeps_every_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    feature_flags: FeatureFlagHelper,
    agent: ScriptedAgent,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC1, AC3, AC4, AC5, AC7: one call for two Targets, none for an unchanged role."""
    root = project_on_main(tmp_path, feature_flags, TABLE)
    base = git(root, "rev-parse", "main")
    write(root / "rtl" / "a.sv", A_V2)
    commit = commit_all(root, "Edit a")
    write(root / "rtl" / "a.sv", "module a; // UNCOMMITTED\nendmodule\n")
    seen: list[tuple[Path, str]] = []
    agent.on_call = lambda params: seen.append(
        (Path(params.cwd), (Path(params.cwd) / "rtl" / "a.sv").read_text(encoding="utf-8"))
    )

    assert review(monkeypatch, root) == 0

    assert len(agent.calls) == 1
    (cwd, text), prompt = seen[0], agent.calls[0].prompt
    assert text == A_V2
    assert root.resolve() not in cwd.resolve().parents and cwd.resolve() != root.resolve()
    assert "Edit a" in prompt
    assert "+  // v2 committed edit" in prompt
    assert "SPEC-TEXT-V1" in prompt
    (run,) = stored_runs(root, commit)
    assert run["kind"] == "discovery"
    assert run["commit"] == commit
    assert run["base"]["commit"] == base
    assert run["base"]["choice"] == "merge-base with local main"
    assert run["changed_files"] == ["rtl/a.sv"]
    assert run["outcome"] == "pass"
    rtl, tb = run["reviews"]
    assert (rtl["role"], rtl["focus"], rtl["outcome"]) == ("rtl", "code_style", "pass")
    assert rtl["targets"] == [
        {
            "target": SIM_CORE_ID,
            "files": ["rtl/a.sv", "rtl/b.sv", "rtl/gen/c.sv"],
            "needed": True,
        },
        {
            "target": "::sim_core:0#sim_fifo",
            "files": ["rtl/a.sv", "rtl/b.sv", "rtl/gen/c.sv"],
            "needed": True,
        },
    ]
    assert (tb["role"], tb["focus"], tb["outcome"]) == ("tb", "quality", "not-needed")
    assert (tb["introduced"], tb["pre_existing"]) == ([], [])
    assert "Review Run" in capsys.readouterr().out

    assert review(monkeypatch, root) == 0

    assert len(stored_runs(root, commit)) == 2
    assert run["run_id"] in {kept["run_id"] for kept in stored_runs(root, commit)}


@pytest.mark.parametrize(
    ("answer", "resolve", "exit_code", "introduced", "pre_existing", "outcome"),
    [
        ([finding()], "major", 1, 1, 0, "open"),
        ([finding("pre_existing")], "major", 0, 0, 1, "pass"),
        ([finding(severity="MINOR")], "major", 0, 1, 0, "pass"),
        ([finding(file="rtl/b.sv", line=1)], "major", 0, 0, 0, "pass"),
        (RuntimeError("model unavailable"), "major", 2, 0, 0, "error"),
    ],
    ids=["introduced", "pre-existing", "below-resolve", "unchanged-file", "reviewer-error"],
)
def test_exit_code_follows_open_introduced_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    feature_flags: FeatureFlagHelper,
    agent: ScriptedAgent,
    answer: Any,
    resolve: str,
    exit_code: int,
    introduced: int,
    pre_existing: int,
    outcome: str,
) -> None:
    """AC6, AC7: exit 1 for an open introduced finding at or above resolve, 2 for an error."""
    root = project_on_main(tmp_path, feature_flags, BUGS.format(resolve=resolve))
    write(root / "rtl" / "a.sv", A_V2)
    commit = commit_all(root, "Edit a")
    agent.answer(answer if isinstance(answer, Exception) else {"issues": answer})

    assert review(monkeypatch, root, "--base", "main") == exit_code

    runs = stored_runs(root, commit)
    assert len(runs) == 1
    (rtl,) = runs[0]["reviews"]
    run = runs[0]
    assert (len(rtl["introduced"]), len(rtl["pre_existing"])) == (introduced, pre_existing)
    assert (rtl["outcome"], run["outcome"]) == (outcome, outcome)
    if introduced:
        assert rtl["introduced"][0]["file"] == "rtl/a.sv"


def test_a_submodule_pin_move_reviews_the_files_it_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    feature_flags: FeatureFlagHelper,
    agent: ScriptedAgent,
) -> None:
    """AC3 converse: a commit that only moves a pin reviews the Target's files inside it."""
    parent, _, _, _ = make_submodule(tmp_path)
    write(
        parent / "sim.core",
        "CAPI=2:\nname: ::sim_core:0\nfilesets:\n  rtl:\n    files: [sub/lib.v]\n"
        "    file_type: verilogSource\ntargets:\n  sim_core:\n    flow: sim\n"
        "    flow_options: {tool: verilator}\n    filesets: [rtl]\n    toplevel: lib\n",
    )
    write(parent / ".booley" / "booley.toml", "[project]\n")
    write(parent / ".booley" / "reviews.toml", BUGS.format(resolve="major"))
    feature_flags.enable(parent / ".booley", "reviewed-commits")
    commit_all(parent, "baseline")
    git(parent, "checkout", "-q", "-b", "feature")
    write(parent / "sub" / "lib.v", "module lib;\nendmodule\n")
    commit_all(parent / "sub", "move sub")
    commit = commit_all(parent, "bump sub")
    agent.answer({"issues": []})

    assert review(monkeypatch, parent) == 0

    prompt = agent.calls[0].prompt
    assert [call.prompt for call in agent.calls] == [prompt] * len(agent.calls), len(agent.calls)
    assert "sub/lib.v" in prompt
    assert "changed inside submodule sub" in prompt
    (run,) = stored_runs(parent, commit)
    assert run["changed_files"] == ["sub/lib.v"]


BUGS_ONLY = '[sim_core.review]\nrtl = ["bugs"]\n'


@pytest.mark.parametrize(
    ("reviews", "scenario", "arguments", "fragment"),
    [
        ('[sim_core.review]\nrtl = ["nonsense"]\n', "feature", (), "[sim_core.review] rtl"),
        (
            '[sim_core.review]\nrtl = ["bugs"]\nspec = "docs/missing.md"\n',
            "feature",
            (),
            "missing.md",
        ),
        (BUGS_ONLY, "main", (), "no Base"),
        (BUGS_ONLY, "feature", ("--", "no-such-revision"), "is not a commit of this Project"),
        (BUGS_ONLY, "feature", ("--", "--exec=x"), "is not a commit of this Project"),
        (BUGS_ONLY, "feature", ("--base", "no-such-base"), "--base 'no-such-base' cannot be used"),
        (BUGS_ONLY, "feature", ("--target", "no-such-target"), "no-such-target"),
        (BUGS_ONLY, "unversioned", (), "rules must be versioned"),
        (BUGS_ONLY, "hook", (), "post-setup hook failed (rc=3)"),
        (BUGS_ONLY, "gitlink", (), "submodule objects missing for libs/core"),
    ],
    ids=[
        "unknown-focus",
        "missing-spec-file",
        "no-base",
        "unknown-revision",
        "option-as-revision",
        "unknown-base",
        "unknown-target",
        "unversioned-rules",
        "failing-hook",
        "unexportable-commit",
    ],
)
def test_refusals_exit_two_and_store_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    feature_flags: FeatureFlagHelper,
    agent: ScriptedAgent,
    capsys: pytest.CaptureFixture[str],
    reviews: str,
    scenario: str,
    arguments: tuple[str, ...],
    fragment: str,
) -> None:
    """AC6: a refusal before any Reviewer call exits 2 and leaves no Review Run."""
    root = project_on_main(tmp_path, feature_flags, reviews)
    if scenario == "main":
        git(root, "checkout", "-q", "main")
    else:
        write(root / "rtl" / "a.sv", A_V2)
    if scenario == "unversioned":
        git(root, "add", "-A", "-f")
        git(root, "rm", "-q", "-r", "--cached", ".booley")
    if scenario == "hook":
        write(root / ".booley" / "hooks" / "post-setup.py", "import sys\nsys.exit(3)\n")
    if scenario == "gitlink":
        add_gitlink(root, "libs/core")
    if scenario != "main":
        commit_all(root, "Edit a", staged_only=scenario in {"unversioned", "gitlink"})

    assert review(monkeypatch, root, *arguments) == 2

    assert fragment in capsys.readouterr().err
    assert not (root / ".booley" / "reviews").exists()
    assert agent.calls == []


def test_an_unwritable_store_exits_two_without_a_misleading_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    feature_flags: FeatureFlagHelper,
    agent: ScriptedAgent,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A Review Run that cannot be written is a failure, not a pass."""
    root = project_on_main(tmp_path, feature_flags, BUGS_ONLY)
    write(root / "rtl" / "a.sv", A_V2)
    commit_all(root, "Edit a")
    write(root / ".booley" / "reviews", "a file where the folder belongs\n")

    assert review(monkeypatch, root) == 2

    captured = capsys.readouterr()
    assert "ERROR: review failed" in captured.err
    assert captured.out == ""


def test_base_is_normalized_like_measure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    feature_flags: FeatureFlagHelper,
    agent: ScriptedAgent,
) -> None:
    """AC2: ``--base`` on another branch becomes its merge-base with the reviewed commit."""
    root = project_on_main(tmp_path, feature_flags, BUGS.format(resolve="major"))
    main = git(root, "rev-parse", "main")
    git(root, "checkout", "-q", "-b", "side", "main")
    write(root / "rtl" / "b.sv", "module b;\nendmodule\n")
    side = commit_all(root, "side work")
    git(root, "checkout", "-q", "feature")
    write(root / "rtl" / "a.sv", A_V2)
    commit = commit_all(root, "Edit a")
    agent.answer({"issues": []})

    assert review(monkeypatch, root, "--base", side) == 0

    (run,) = stored_runs(root, commit)
    assert run["base"] == {
        "commit": main,
        "choice": "--base, replaced by its merge-base with the measured commit",
        "requested": side,
        "no_base": None,
    }
    assert run["changed_files"] == ["rtl/a.sv"]


def test_target_run_is_not_stored_and_json_prints_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    feature_flags: FeatureFlagHelper,
    agent: ScriptedAgent,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC5, AC6: ``--target`` stores nothing; a commit no review covers exits 0."""
    root = project_on_main(tmp_path, feature_flags, TABLE)
    write(root / "rtl" / "a.sv", A_V2)
    commit_all(root, "Edit a")

    assert review(monkeypatch, root, "--target", "sim_core", "--json") == 0

    printed = json.loads(capsys.readouterr().out)
    assert printed["kind"] == "discovery"
    assert [item["role"] for item in printed["reviews"]] == ["rtl", "tb"]
    assert [item["target"] for item in printed["reviews"][0]["targets"]] == [SIM_CORE_ID]
    assert not (root / ".booley" / "reviews").exists()

    git(root, "checkout", "-q", "-b", "docs", "main")
    write(root / "docs" / "spec.md", SPEC + "more\n")
    docs_only = commit_all(root, "Edit the spec")
    agent.calls.clear()

    assert review(monkeypatch, root) == 0

    assert "nothing to review" in capsys.readouterr().out
    assert agent.calls == []
    assert [run["outcome"] for run in stored_runs(root, docs_only)] == ["nothing-to-review"]


def test_review_is_absent_while_its_flag_is_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: ScriptedAgent,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ADR 0095: without ``reviewed-commits`` enabled the command does not exist."""
    root = new_repository(tmp_path / "proj")
    write(root / ".booley" / "booley.toml", "[project]\n")
    commit_all(root, "baseline")

    assert review(monkeypatch, root) == 2

    assert "invalid choice: 'review'" in capsys.readouterr().err
    assert agent.calls == []
