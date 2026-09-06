"""Target Plan validation against semantic authoring deltas."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.core.models import TargetPlanRole
from booley.ticket_board.target_plan import TargetPlanValidationError, analyze_target_plan


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return result.stdout.strip()


def _core(targets: str) -> str:
    return (
        "CAPI=2:\n"
        "name: acme:lib:toy:1.0\n"
        "filesets:\n"
        "  rtl:\n"
        "    files: [toy.sv]\n"
        "    file_type: systemVerilogSource\n"
        "targets:\n"
        f"{targets}"
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "toy.sv").write_text("module toy; endmodule\n", encoding="utf-8")
    (tmp_path / "toy.core").write_text(
        _core(
            "  lint_old:\n"
            "    flow: lint\n"
            "    flow_options: {tool: verilator}\n"
            "    filesets: [rtl]\n"
        ),
        encoding="utf-8",
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "baseline")
    return tmp_path


def _replacement_fields() -> dict:
    return {
        "target_plan": [{"target": "lint_new", "role": "replacement", "replaces": "lint_old"}],
        "criteria": {"mandatory": {"lint_clean": ["lint_old", "lint_new"]}},
        "on_success": {"merge": True},
    }


def _add_candidate(repository: Path) -> None:
    (repository / "toy.core").write_text(
        _core(
            "  lint_old:\n"
            "    flow: lint\n"
            "    flow_options: {tool: verilator}\n"
            "    filesets: [rtl]\n"
            "  lint_new:\n"
            "    flow: lint\n"
            "    flow_options: {tool: verilator}\n"
            "    filesets: [rtl]\n"
        ),
        encoding="utf-8",
    )


def test_replacement_plan_canonicalizes_and_derives_baseline_removal(repository: Path) -> None:
    _add_candidate(repository)

    analysis = analyze_target_plan(
        _replacement_fields(), repository, ((repository, ("toy.core",)),)
    )

    assert analysis.authored_targets == ("acme:lib:toy:1.0#lint_new",)
    assert analysis.removal_targets == ("acme:lib:toy:1.0#lint_old",)
    assert analysis.plan is not None
    assert analysis.plan.entries[0].role is TargetPlanRole.REPLACEMENT


def test_changed_core_does_not_misclassify_untouched_siblings(repository: Path) -> None:
    _add_candidate(repository)

    analysis = analyze_target_plan(
        _replacement_fields(), repository, ((repository, ("toy.core",)),)
    )

    assert analysis.authored_targets == ("acme:lib:toy:1.0#lint_new",)


def test_plan_omission_rejects_authored_target(repository: Path) -> None:
    _add_candidate(repository)

    with pytest.raises(TargetPlanValidationError, match="unplanned authored Targets"):
        analyze_target_plan(
            {"criteria": {"mandatory": {"lint_clean": ["lint_new"]}}},
            repository,
            ((repository, ("toy.core",)),),
        )


def test_existing_target_mutation_is_rejected(repository: Path) -> None:
    (repository / "toy.core").write_text(
        _core(
            "  lint_old:\n    flow: lint\n    flow_options: {tool: verible}\n    filesets: [rtl]\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(TargetPlanValidationError, match="cannot modify or delete"):
        analyze_target_plan({}, repository, ((repository, ("toy.core",)),))


def test_existing_core_content_outside_targets_cannot_change(repository: Path) -> None:
    text = (repository / "toy.core").read_text(encoding="utf-8")
    (repository / "toy.core").write_text(
        text.replace("files: [toy.sv]", "files: [other.sv]"), encoding="utf-8"
    )

    with pytest.raises(TargetPlanValidationError, match="outside targets"):
        analyze_target_plan({}, repository, ((repository, ("toy.core",)),))


def test_unplanned_owned_test_table_is_rejected(repository: Path) -> None:
    _add_candidate(repository)
    project = repository / ".booley_project"
    project.mkdir()
    tests_path = project / "tests.toml"
    tests_path.write_text('[other]\ntests = ["smoke"]\n', encoding="utf-8")

    with pytest.raises(TargetPlanValidationError, match="not owned by a planned Target"):
        analyze_target_plan(
            _replacement_fields(),
            repository,
            ((repository, ("toy.core", ".booley_project/tests.toml")),),
        )
