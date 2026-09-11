"""Target Plan validation against semantic authoring deltas."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.core.models import TargetPlanRole
from booley.ticket_board import target_plan
from booley.ticket_board.target_plan import TargetPlanValidationError, analyze_target_plan
from booley.ticket_board.workspace_ops import target_surface_files


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


def _persistent_fields(target: str = "lint_new") -> dict:
    return {
        "target_plan": [{"target": target, "role": "persistent"}],
        "criteria": {"mandatory": {"lint_clean": [target]}},
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


def _analyze(fields: dict, project_root: Path, repositories):
    snapshots = tuple(
        (repository, paths, _git(repository, "rev-parse", "HEAD"))
        for repository, paths in repositories
    )
    return analyze_target_plan(fields, project_root, target_surface_files(snapshots))


def test_replacement_plan_canonicalizes_and_derives_baseline_removal(repository: Path) -> None:
    _add_candidate(repository)

    analysis = _analyze(_replacement_fields(), repository, ((repository, ("toy.core",)),))

    assert analysis.authored_targets == ("acme:lib:toy:1.0#lint_new",)
    assert analysis.removal_targets == ("acme:lib:toy:1.0#lint_old",)
    assert analysis.plan is not None
    assert analysis.plan.entries[0].role is TargetPlanRole.REPLACEMENT


def test_source_boundary_compares_worktree_filtered_baseline(repository: Path) -> None:
    _git(repository, "config", "core.autocrlf", "true")
    path = repository / "toy.core"
    content = _core(
        "  lint_old:\n"
        "    flow: lint\n"
        "    flow_options: {tool: verilator}\n"
        "    filesets: [rtl]\n"
        "  lint_new:\n"
        "    flow: lint\n"
        "    flow_options: {tool: verilator}\n"
        "    filesets: [rtl]\n"
    )
    path.write_bytes(content.replace("\n", "\r\n").encode())

    analysis = _analyze(_replacement_fields(), repository, ((repository, ("toy.core",)),))

    assert analysis.authored_targets == ("acme:lib:toy:1.0#lint_new",)


def test_tests_source_boundary_accepts_crlf_table_append(repository: Path) -> None:
    _git(repository, "config", "core.autocrlf", "true")
    project = repository / ".booley_project"
    project.mkdir()
    tests_path = project / "tests.toml"
    tests_path.write_bytes(b"[existing]\r\nmodule = 'old'\r\n")
    _git(repository, "add", "-f", ".booley_project/tests.toml")
    _git(repository, "commit", "-qm", "add tests baseline")
    core_path = repository / "toy.core"
    core_content = _core(
        "  lint_old:\n"
        "    flow: lint\n"
        "    flow_options: {tool: verilator}\n"
        "    filesets: [rtl]\n"
        "  lint_new:\n"
        "    flow: lint\n"
        "    flow_options: {tool: verilator}\n"
        "    filesets: [rtl]\n"
    )
    core_path.write_bytes(core_content.replace("\n", "\r\n").encode())
    tests_path.write_bytes(b"[existing]\r\nmodule = 'old'\r\n\r\n[lint_new]\r\nmodule = 'new'\r\n")

    analysis = _analyze(
        _replacement_fields(),
        repository,
        ((repository, ("toy.core", ".booley_project/tests.toml")),),
    )

    assert analysis.authored_targets == ("acme:lib:toy:1.0#lint_new",)


def test_changed_core_does_not_misclassify_untouched_siblings(repository: Path) -> None:
    _add_candidate(repository)

    analysis = _analyze(_replacement_fields(), repository, ((repository, ("toy.core",)),))

    assert analysis.authored_targets == ("acme:lib:toy:1.0#lint_new",)


@pytest.mark.parametrize(
    "rewrite",
    [
        lambda text: text.replace(
            "flow_options: {tool: verilator}",
            "flow_options: {tool: verilator} # hidden sibling edit",
            1,
        ),
        lambda text: "# hidden shared-control edit\n" + text,
    ],
)
def test_planned_addition_cannot_hide_existing_core_byte_edits(repository: Path, rewrite) -> None:
    _add_candidate(repository)
    path = repository / "toy.core"
    path.write_text(rewrite(path.read_text(encoding="utf-8")), encoding="utf-8")

    with pytest.raises(TargetPlanValidationError, match="bytes outside planned Targets"):
        _analyze(_replacement_fields(), repository, ((repository, ("toy.core",)),))


def test_plan_omission_rejects_authored_target(repository: Path) -> None:
    _add_candidate(repository)

    with pytest.raises(TargetPlanValidationError, match="unplanned authored Targets"):
        _analyze(
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
        _analyze({}, repository, ((repository, ("toy.core",)),))


def test_added_doctor_selftest_target_requires_plan_entry(repository: Path) -> None:
    path = repository / "toy.core"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "  doctor:\n    flow: lint\n    flow_options: {booley: {doctor_selftest: true}}\n",
        encoding="utf-8",
    )

    with pytest.raises(TargetPlanValidationError, match="unplanned authored Targets"):
        _analyze({}, repository, ((repository, ("toy.core",)),))


@pytest.mark.parametrize("change", ["modify", "delete"])
def test_existing_doctor_selftest_target_cannot_change(tmp_path: Path, change: str) -> None:
    repository = tmp_path
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "user.email", "test@example.invalid")
    original = _core(
        "  doctor:\n"
        "    flow: lint\n"
        "    flow_options: {booley: {doctor_selftest: true}, tool: verilator}\n"
        "    filesets: [rtl]\n"
    )
    (repository / "toy.core").write_text(original, encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "baseline")
    changed = (
        original.replace("tool: verilator", "tool: verible")
        if change == "modify"
        else _core("").replace("targets:\n", "targets: {}\n")
    )
    (repository / "toy.core").write_text(changed, encoding="utf-8")

    with pytest.raises(TargetPlanValidationError, match="cannot modify or delete"):
        _analyze({}, repository, ((repository, ("toy.core",)),))


def test_existing_core_content_outside_targets_cannot_change(repository: Path) -> None:
    text = (repository / "toy.core").read_text(encoding="utf-8")
    (repository / "toy.core").write_text(
        text.replace("files: [toy.sv]", "files: [other.sv]"), encoding="utf-8"
    )

    with pytest.raises(TargetPlanValidationError, match="modify or delete existing filesets"):
        _analyze({}, repository, ((repository, ("toy.core",)),))


def test_planned_target_can_author_dedicated_fileset(repository: Path) -> None:
    path = repository / "toy.core"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "targets:\n",
            "  tb_new:\n    files: [tb/new.py]\n    file_type: user\n    tags: [tb]\ntargets:\n",
        )
        + "  lint_new:\n"
        + "    flow: lint\n"
        + "    filesets_append: [tb_new]\n",
        encoding="utf-8",
    )

    analysis = _analyze(_persistent_fields(), repository, ((repository, ("toy.core",)),))

    assert analysis.authored_targets == ("acme:lib:toy:1.0#lint_new",)
    assert analysis.authored_filesets == ("toy.core#tb_new",)


def test_new_core_can_author_planned_target_fileset(repository: Path) -> None:
    path = repository / "new.core"
    path.write_text(
        "CAPI=2:\n"
        "name: acme:lib:new:1.0\n"
        "filesets:\n"
        "  tb_new: {files: [tb/new.py], file_type: user, tags: [tb]}\n"
        "targets:\n"
        "  lint_new: {flow: lint, filesets: [tb_new]}\n",
        encoding="utf-8",
    )

    analysis = _analyze(_persistent_fields(), repository, ((repository, (path.name,)),))

    assert analysis.authored_filesets == ("new.core#tb_new",)


def test_added_fileset_cannot_resolve_dangling_baseline_reference(repository: Path) -> None:
    path = repository / "toy.core"
    baseline = path.read_text(encoding="utf-8").replace(
        "    filesets: [rtl]\n",
        '    filesets: [rtl, "tool_verilator ? (tb_new)"]\n',
    )
    path.write_text(baseline, encoding="utf-8")
    _git(repository, "add", "toy.core")
    _git(repository, "commit", "-qm", "conditional baseline")
    path.write_text(
        baseline.replace("targets:\n", "  tb_new: {files: [tb/new.py]}\ntargets:\n")
        + "  lint_new: {flow: lint, filesets: [tb_new]}\n",
        encoding="utf-8",
    )

    with pytest.raises(TargetPlanValidationError, match="undefined fileset"):
        _analyze(_persistent_fields(), repository, ((repository, (path.name,)),))


def test_unreferenced_added_fileset_is_rejected(repository: Path) -> None:
    _add_candidate(repository)
    path = repository / "toy.core"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "targets:\n", "  unused: {files: [tb/new.py]}\ntargets:\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(TargetPlanValidationError, match="not referenced"):
        _analyze(_replacement_fields(), repository, ((repository, (path.name,)),))


def test_existing_fileset_cannot_be_modified(repository: Path) -> None:
    _add_candidate(repository)
    path = repository / "toy.core"
    path.write_text(
        path.read_text(encoding="utf-8").replace("files: [toy.sv]", "files: [other.sv]"),
        encoding="utf-8",
    )

    with pytest.raises(TargetPlanValidationError, match="modify or delete existing filesets"):
        _analyze(_replacement_fields(), repository, ((repository, (path.name,)),))


def test_new_core_rejects_unowned_top_level_build_content(repository: Path) -> None:
    path = repository / "new.core"
    path.write_text(
        "CAPI=2:\nname: acme:lib:new:1.0\ngenerators: {}\ntargets:\n  lint_new: {flow: lint}\n",
        encoding="utf-8",
    )

    with pytest.raises(TargetPlanValidationError, match="outside Target definitions"):
        _analyze(_persistent_fields(), repository, ((repository, (path.name,)),))


@pytest.mark.parametrize(
    "content",
    [
        "{}\n",
        "CAPI=2:\nname: acme:lib:empty:1.0\ntargets: {}\n",
    ],
)
def test_new_core_must_define_planned_target(repository: Path, content: str) -> None:
    path = repository / "empty.core"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(TargetPlanValidationError, match="must define at least one Target"):
        _analyze({}, repository, ((repository, (path.name,)),))


def test_replacement_requires_same_flow_family(repository: Path) -> None:
    _add_candidate(repository)
    path = repository / "toy.core"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "  lint_new:\n    flow: lint", "  lint_new:\n    flow: sim"
        ),
        encoding="utf-8",
    )

    with pytest.raises(TargetPlanValidationError, match="same Booley Flow"):
        _analyze(_replacement_fields(), repository, ((repository, ("toy.core",)),))


def test_replacement_accepts_implicit_compatible_flow(repository: Path) -> None:
    _add_candidate(repository)
    path = repository / "toy.core"
    text = path.read_text(encoding="utf-8")
    old = "  lint_new:\n    flow: lint\n    flow_options: {tool: verilator}"
    new = "  lint_new:\n    flow_options: {tool: verilator}"
    path.write_text(text.replace(old, new), encoding="utf-8")

    analysis = _analyze(_replacement_fields(), repository, ((repository, ("toy.core",)),))

    assert analysis.plan is not None


def test_unplanned_owned_test_table_is_rejected(repository: Path) -> None:
    _add_candidate(repository)
    project = repository / ".booley_project"
    project.mkdir()
    tests_path = project / "tests.toml"
    tests_path.write_text('[other]\ntests = ["smoke"]\n', encoding="utf-8")

    with pytest.raises(TargetPlanValidationError, match="not owned by a planned Target"):
        _analyze(
            _replacement_fields(),
            repository,
            ((repository, ("toy.core", ".booley_project/tests.toml")),),
        )


def test_planned_table_cannot_hide_existing_tests_table_byte_edit(repository: Path) -> None:
    project = repository / ".booley_project"
    project.mkdir()
    tests_path = project / "tests.toml"
    tests_path.write_text("[existing]\nmodule = 'old'\n", encoding="utf-8")
    _git(repository, "add", "-f", ".booley_project/tests.toml")
    _git(repository, "commit", "-qm", "add tests baseline")
    _add_candidate(repository)
    tests_path.write_text(
        "[existing] # hidden edit\nmodule = 'old'\n\n[lint_new]\nmodule = 'new'\n",
        encoding="utf-8",
    )

    with pytest.raises(TargetPlanValidationError, match="bytes outside planned tables"):
        _analyze(
            _replacement_fields(),
            repository,
            ((repository, ("toy.core", ".booley_project/tests.toml")),),
        )


def test_planned_table_can_append_with_conventional_separator(repository: Path) -> None:
    project = repository / ".booley_project"
    project.mkdir()
    tests_path = project / "tests.toml"
    tests_path.write_text("[existing]\nmodule = 'old'\n", encoding="utf-8")
    _git(repository, "add", "-f", ".booley_project/tests.toml")
    _git(repository, "commit", "-qm", "add tests baseline")
    _add_candidate(repository)
    tests_path.write_text(
        tests_path.read_text(encoding="utf-8").rstrip() + "\n\n[lint_new]\nmodule = 'new'\n",
        encoding="utf-8",
    )

    analysis = _analyze(
        _replacement_fields(),
        repository,
        ((repository, ("toy.core", ".booley_project/tests.toml")),),
    )

    assert analysis.authored_targets == ("acme:lib:toy:1.0#lint_new",)


def test_planned_target_can_append_with_conventional_separator(repository: Path) -> None:
    _add_candidate(repository)
    path = repository / "toy.core"
    path.write_text(
        path.read_text(encoding="utf-8").replace("  lint_new:", "\n  lint_new:"),
        encoding="utf-8",
    )

    analysis = _analyze(_replacement_fields(), repository, ((repository, ("toy.core",)),))

    assert analysis.authored_targets == ("acme:lib:toy:1.0#lint_new",)


@pytest.mark.parametrize(
    "other_target",
    [
        "{flow: lint}",
        "{flow: lint, flow_options: {booley: {doctor_selftest: true}}}",
    ],
)
def test_bare_planned_table_rejected_when_target_name_is_ambiguous(
    repository: Path, other_target: str
) -> None:
    (repository / "other.core").write_text(
        f"CAPI=2:\nname: acme:lib:other:1.0\ntargets:\n  lint_new: {other_target}\n",
        encoding="utf-8",
    )
    _git(repository, "add", "other.core")
    _git(repository, "commit", "-qm", "add ambiguous target name")
    _add_candidate(repository)
    project = repository / ".booley_project"
    project.mkdir()
    (project / "tests.toml").write_text("[lint_new]\nmodule = 'new'\n", encoding="utf-8")
    fields = _replacement_fields()
    fields["target_plan"][0]["target"] = "acme:lib:toy:1.0#lint_new"
    fields["criteria"]["mandatory"]["lint_clean"] = [
        "acme:lib:toy:1.0#lint_old",
        "acme:lib:toy:1.0#lint_new",
    ]

    with pytest.raises(TargetPlanValidationError, match=r"ambiguous bare tests\.toml"):
        _analyze(
            fields,
            repository,
            ((repository, ("toy.core", ".booley_project/tests.toml")),),
        )


def test_provider_owned_table_append_is_authorized_source(repository: Path) -> None:
    project = repository / ".booley_project"
    project.mkdir()
    tests_path = project / "tests.toml"
    tests_path.write_text("[existing]\nmodule = 'old'\n", encoding="utf-8")
    _git(repository, "add", "-f", ".booley_project/tests.toml")
    _git(repository, "commit", "-qm", "add tests baseline")
    path = repository / "toy.core"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text + "  provider_future:\n    flow: lint\n    filesets: [rtl]\n",
        encoding="utf-8",
    )
    tests_path.write_text(
        tests_path.read_text(encoding="utf-8").rstrip()
        + "\n\n[provider_future]\nmodule = 'provider'\n",
        encoding="utf-8",
    )
    snapshots = (
        (
            repository,
            ("toy.core", ".booley_project/tests.toml"),
            _git(repository, "rev-parse", "HEAD"),
        ),
    )
    target = "acme:lib:toy:1.0#provider_future"

    analysis = analyze_target_plan(
        {},
        repository,
        target_surface_files(snapshots),
        provider_targets=frozenset({target}),
        exported_provider_targets=frozenset({target}),
        provider_test_tables=frozenset({"provider_future"}),
    )

    assert analysis.authored_targets == ()


def test_planned_tests_entry_must_be_a_table(repository: Path) -> None:
    _add_candidate(repository)
    project = repository / ".booley_project"
    project.mkdir()
    tests_path = project / "tests.toml"
    tests_path.write_text("lint_new = 'not a table'\n", encoding="utf-8")

    with pytest.raises(TargetPlanValidationError, match="must be tables: lint_new"):
        _analyze(
            _replacement_fields(),
            repository,
            ((repository, ("toy.core", ".booley_project/tests.toml")),),
        )


@pytest.mark.parametrize("content", ["", "# no owned Target table\n"])
def test_new_tests_file_must_define_owned_planned_table(repository: Path, content: str) -> None:
    project = repository / ".booley_project"
    project.mkdir()
    tests_path = project / "tests.toml"
    tests_path.write_text(content, encoding="utf-8")

    with pytest.raises(TargetPlanValidationError, match="must define at least one"):
        _analyze(
            {},
            repository,
            ((repository, (".booley_project/tests.toml",)),),
        )


def test_surface_parsers_reject_malformed_core_and_test_documents() -> None:
    assert target_plan._core_document(None, path="missing.core") == {}
    with pytest.raises(TargetPlanValidationError, match=r"cannot parse \.core"):
        target_plan._core_document(b"targets: [", path="bad.core")
    with pytest.raises(TargetPlanValidationError, match="mapping-valued targets"):
        target_plan._core_targets(b"name: acme:lib:toy:1.0\ntargets: []\n", path="bad.core")
    with pytest.raises(TargetPlanValidationError, match="non-string Target name"):
        target_plan._core_targets(b"name: acme:lib:toy:1.0\ntargets: {1: {}}\n", path="bad.core")
    with pytest.raises(TargetPlanValidationError, match="cannot parse"):
        target_plan._table_mapping(b"[broken", path="tests.toml")


def test_source_boundaries_handle_absence_and_decode_failures() -> None:
    missing_core = target_plan.TargetSurfaceFile("toy.core", None, None)
    target_plan._validate_core_source_boundary(missing_core, ())
    missing_tests = target_plan.TargetSurfaceFile("tests.toml", None, None)
    target_plan._validate_tests_source_boundary(missing_tests, ())

    bad_core = target_plan.TargetSurfaceFile("toy.core", b"name: old\n", b"\xff")
    with pytest.raises(TargetPlanValidationError):
        target_plan._validate_core_source_boundary(bad_core, ("future",))
    bad_tests = target_plan.TargetSurfaceFile("tests.toml", b"", b"\xff")
    with pytest.raises(TargetPlanValidationError):
        target_plan._validate_tests_source_boundary(bad_tests, ("future",))


def test_test_table_policy_rejects_shared_changed_and_unplanned_tables() -> None:
    empty = ((), (), ())
    shared = target_plan._SurfaceDelta(*empty, ("test_lists",), (), ())
    catalog = object()
    with pytest.raises(TargetPlanValidationError, match="shared"):
        target_plan._validate_test_tables(shared, None, frozenset(), catalog)

    modified = target_plan._SurfaceDelta(*empty, (), ("existing",), ())
    with pytest.raises(TargetPlanValidationError, match="modify or delete"):
        target_plan._validate_test_tables(modified, None, frozenset(), catalog)

    authored = target_plan._SurfaceDelta(*empty, ("future",), (), ())
    with pytest.raises(TargetPlanValidationError, match="target_plan omission"):
        target_plan._validate_test_tables(authored, None, frozenset(), catalog)


def test_surface_coverage_reports_provider_and_plan_mismatches() -> None:
    definition = target_plan._TargetDefinition("acme:lib:toy:1.0#future", "future", {})
    delta = target_plan._SurfaceDelta((definition,), (), (), (), (), ())
    with pytest.raises(TargetPlanValidationError, match="provider Targets changed"):
        target_plan._validate_surface_coverage(
            delta, None, frozenset({"acme:lib:toy:1.0#missing"})
        )

    plan = target_plan.TargetPlan.from_value(
        [{"target": "acme:lib:toy:1.0#planned", "role": "persistent"}]
    )
    with pytest.raises(TargetPlanValidationError, match="unplanned authored Targets") as exc:
        target_plan._validate_surface_coverage(delta, plan, frozenset())
    assert "planned Targets not newly authored" in str(exc.value)


def test_plan_binding_policy_rejects_unbound_and_private_provider_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(target_plan, "_bound_identities", lambda *_args: set())
    with pytest.raises(TargetPlanValidationError, match="must be bound"):
        target_plan._validate_plan_bindings(
            {}, tmp_path, None, {"future"}, frozenset(), frozenset()
        )

    monkeypatch.setattr(target_plan, "_bound_identities", lambda *_args: {"provider"})
    with pytest.raises(TargetPlanValidationError, match="non-exported"):
        target_plan._validate_plan_bindings(
            {}, tmp_path, None, set(), frozenset({"provider"}), frozenset()
        )
