"""Narrow, formatting-preserving Target removal after Ticket acceptance."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from booley.fusesoc import fusesoc_registry
from booley.targets.catalog import TargetCatalog
from booley.ticket_board.acceptance_targets import AcceptanceTargetBinding
from booley.ticket_board.target_finalization import (
    TargetFinalizationError,
    apply_target_removals,
    plan_target_removals,
)


def _write_core(path: Path, *, vlnv: str, targets: str) -> None:
    path.write_text(
        f"CAPI=2:\nname: {vlnv}\nfilesets:\n  rtl:\n    files: [rtl/toy.sv]\ntargets:\n{targets}",
        encoding="utf-8",
    )


def _binding(*targets: str) -> tuple[AcceptanceTargetBinding, ...]:
    return tuple(
        AcceptanceTargetBinding("synth", "criteria.mandatory.synthesis_ok", target, target)
        for target in targets
    )


def _git(repository: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _init_repository(repository: Path) -> None:
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "user.email", "test@example.invalid")


def test_removal_preserves_core_and_tests_toml_formatting(tmp_path: Path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "toy.sv").write_text("module toy; endmodule\n", encoding="utf-8")
    core = tmp_path / "toy.core"
    _write_core(
        core,
        vlnv="acme:lib:toy:1.0",
        targets=(
            "  # retained comment\n"
            "  baseline:\n"
            "    flow: generic\n"
            "    flow_options: {tool: yosys, ppa_profile: compact}\n"
            "    filesets: [rtl]\n"
            "    toplevel: toy\n"
            "  candidate:\n"
            "    flow: generic\n"
            "    flow_options: {tool: yosys, ppa_profile: fast}\n"
            "    filesets: [rtl]\n"
            "    toplevel: toy\n"
        ),
    )
    project = tmp_path / ".booley_project"
    project.mkdir()
    tests_toml = project / "tests.toml"
    tests_toml.write_text(
        "# registry header\n"
        "[test_lists]\n"
        'smoke = ["works"]\n\n'
        '["acme:lib:toy:1.0#baseline"] # remove this table\n'
        'tests = ["old"]\n\n'
        '["acme:lib:toy:1.0#baseline".env]\n'
        'FLAVOR = "compact"\n\n'
        '[["acme:lib:toy:1.0#baseline".cases]]\n'
        'name = "slow"\n\n'
        "# candidate comment stays byte-for-byte\n"
        "[candidate]\n"
        'tests = ["new"]\n',
        encoding="utf-8",
    )
    canonical = "acme:lib:toy:1.0#baseline"

    plan = plan_target_removals(tmp_path, (canonical,), _binding(canonical))
    changed = apply_target_removals(tmp_path, plan)

    assert changed == (Path(".booley_project/tests.toml"), Path("toy.core"))
    core_text = core.read_text(encoding="utf-8")
    assert core_text.startswith("CAPI=2:\n")
    assert "# retained comment" in core_text
    assert "  baseline:" not in core_text
    assert "  candidate:" in core_text
    parsed_tests = tomllib.loads(tests_toml.read_text(encoding="utf-8"))
    assert parsed_tests == {
        "test_lists": {"smoke": ["works"]},
        "candidate": {"tests": ["new"]},
    }
    assert tests_toml.read_text(encoding="utf-8").startswith("# registry header\n")
    assert "# candidate comment stays byte-for-byte\n[candidate]" in tests_toml.read_text(
        encoding="utf-8"
    )
    with pytest.raises(fusesoc_registry.UnknownTargetError):
        TargetCatalog.build(tmp_path).select(canonical)


def test_last_target_leaves_valid_empty_targets_mapping(tmp_path: Path) -> None:
    core = tmp_path / "toy.core"
    _write_core(core, vlnv="acme:lib:toy:1.0", targets="  obsolete: {flow: lint}\n")
    canonical = "acme:lib:toy:1.0#obsolete"

    plan = plan_target_removals(tmp_path, (canonical,), _binding(canonical))
    apply_target_removals(tmp_path, plan)

    assert fusesoc_registry.read_core(core)["targets"] == {}


def test_finalizer_rejects_target_not_bound_by_ticket_criteria(tmp_path: Path) -> None:
    _write_core(
        tmp_path / "toy.core",
        vlnv="acme:lib:toy:1.0",
        targets="  baseline: {flow: lint}\n  unrelated: {flow: lint}\n",
    )
    baseline = "acme:lib:toy:1.0#baseline"
    with pytest.raises(TargetFinalizationError, match="not bound by this Ticket's criteria"):
        plan_target_removals(tmp_path, ("unrelated",), _binding(baseline))


def test_finalizer_rejects_submodule_owned_target_removal(tmp_path: Path) -> None:
    dependency = tmp_path / "dependency"
    _init_repository(dependency)
    _write_core(
        dependency / "toy.core",
        vlnv="acme:lib:toy:1.0",
        targets="  obsolete: {flow: lint}\n",
    )
    _git(dependency, "add", "toy.core")
    _git(dependency, "commit", "-qm", "dependency target")
    root = tmp_path / "root"
    _init_repository(root)
    _git(
        root,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(dependency),
        "vendor/dependency",
    )
    _git(root, "commit", "-qm", "project")
    canonical = "acme:lib:toy:1.0#obsolete"
    with pytest.raises(
        TargetFinalizationError,
        match="only outer and paired project participants can be finalized",
    ):
        plan_target_removals(root, (canonical,), _binding(canonical))


def test_finalizer_rejects_ambiguous_bare_selector(tmp_path: Path) -> None:
    _write_core(tmp_path / "a.core", vlnv="acme:lib:a:1.0", targets="  synth: {}\n")
    _write_core(tmp_path / "b.core", vlnv="acme:lib:b:1.0", targets="  synth: {}\n")
    canonical = "acme:lib:a:1.0#synth"
    with pytest.raises(TargetFinalizationError, match="declared by 2 cores"):
        plan_target_removals(tmp_path, ("synth",), _binding(canonical))


def test_qualified_target_rejects_ambiguous_bare_tests_registration(tmp_path: Path) -> None:
    _write_core(tmp_path / "a.core", vlnv="acme:lib:a:1.0", targets="  sim: {}\n")
    _write_core(tmp_path / "b.core", vlnv="acme:lib:b:1.0", targets="  sim: {}\n")
    project = tmp_path / ".booley_project"
    project.mkdir()
    (project / "tests.toml").write_text('[sim]\ntests = ["smoke"]\n', encoding="utf-8")
    canonical = "acme:lib:a:1.0#sim"

    with pytest.raises(TargetFinalizationError, match=r"ambiguous bare tests\.toml section"):
        plan_target_removals(tmp_path, (canonical,), _binding(canonical))


def test_removal_preserves_another_targets_qualified_test_table(tmp_path: Path) -> None:
    _write_core(tmp_path / "a.core", vlnv="acme:lib:a:1.0", targets="  same: {}\n")
    _write_core(tmp_path / "b.core", vlnv="acme:lib:b:1.0", targets="  same: {}\n")
    project = tmp_path / ".booley_project"
    project.mkdir()
    tests = project / "tests.toml"
    original = '["acme:lib:b:1.0#same"]\ntests = ["keep"]\n'
    tests.write_text(original, encoding="utf-8")
    removed = "acme:lib:a:1.0#same"

    plan = plan_target_removals(tmp_path, (removed,), _binding(removed))
    apply_target_removals(tmp_path, plan)

    assert tests.read_text(encoding="utf-8") == original
    assert TargetCatalog.build(tmp_path).select("acme:lib:b:1.0#same")


def test_removal_preserves_doctor_private_test_table(tmp_path: Path) -> None:
    _write_core(tmp_path / "a.core", vlnv="acme:lib:a:1.0", targets="  obsolete: {}\n")
    _write_core(
        tmp_path / "doctor.core",
        vlnv="acme:lib:doctor:1.0",
        targets=(
            "  doctor:\n    flow: lint\n    flow_options: {booley: {doctor_selftest: true}}\n"
        ),
    )
    project = tmp_path / ".booley_project"
    project.mkdir()
    tests = project / "tests.toml"
    original = '[doctor]\ntests = ["keep"]\n'
    tests.write_text(original, encoding="utf-8")
    removed = "acme:lib:a:1.0#obsolete"

    plan = plan_target_removals(tmp_path, (removed,), _binding(removed))
    apply_target_removals(tmp_path, plan)

    assert tests.read_text(encoding="utf-8") == original
