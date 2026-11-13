"""The Mutation Flow's ``--scope`` handling: rejections, repeats and fitting hints.

An unusable scope is an input error: it ends ``ERROR`` (exit 2) before any Mutant is
generated or built, in ``--dry-run`` and in a real run alike.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from booley.flows.mutation.flow import MutationFlow
from booley.runtime import runtime_context
from booley.runtime.endpoint_execution import EXIT_ERROR, EXIT_SUCCESS, EndpointOutcome
from tests.flows.mutation.test_flow import (
    _DEFAULT_SCOPE_CORE,
    _SCOPE,
    _SETTINGS,
    _VERILATED,
    _campaign,
    _detects_every_mutant,
    _env_with_state,
    _make_endpoint,
    _make_state,
    _mutant_keys,
    _prepare_scope_files,
    _run_count_preview,
    _two_core_endpoint,
    _write_tests_toml,
)

# Every file of ``_DEFAULT_SCOPE_CORE`` plus one ``.sv`` file the Target does not name.
_CORE_FILES = (
    "rtl/a.sv",
    "rtl/b.v",
    "rtl/defs.svh",
    "rtl/notes.cpp",
    "rtl/table.mem",
    "rtl/other.sv",
    "vendor/v.sv",
    "gen/g.sv",
    "tb/tb_top.sv",
)


def _author_project(root: Path, core: str = _DEFAULT_SCOPE_CORE) -> None:
    """Write *core* and a stub for each file it (or ``_CORE_FILES``) names."""
    (root / "design.core").write_text(core, encoding="utf-8")
    for rel in _CORE_FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// stub\n", encoding="utf-8")
    _write_tests_toml(root, '[sim]\ntests = ["smoke"]\n')


def _main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *args: str,
) -> tuple[int, str, dict[str, Any]]:
    """Run ``booley flow mutation`` on the project at ``tmp_path/project``.

    Returns the exit code, everything the run printed and the flat ``mutation.json``
    report (``{}`` when none was written).  Generation is stubbed, so a scope that gets past
    validation ends the run there instead of building anything.
    """
    root = tmp_path / "project"
    reports = tmp_path / "reports"
    state_file = _make_state(tmp_path)
    for key, value in _env_with_state(state_file, tmp_path / "logs").items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    reached = EndpointOutcome(exit_code=EXIT_SUCCESS, report_text="reached generation")
    (reports / "mutation.json").unlink(missing_ok=True)  # never read a previous run's report
    with patch.object(MutationFlow, "_generate", return_value=reached):
        code = MutationFlow().main(
            ["--work-dir", str(root), "--report-dir", str(reports), "--target", "sim", *args]
        )
    captured = capsys.readouterr()
    report = reports / "mutation.json"
    parsed: dict[str, Any] = json.loads(report.read_text("utf-8")) if report.is_file() else {}
    return code, captured.out + captured.err, parsed


def _assert_real_report(code: int, report: dict[str, Any], label: str) -> None:
    """A real run that refused its input exits 2 and records exactly that."""
    assert code == EXIT_ERROR, label
    assert report.get("exit_code") == EXIT_ERROR, label
    assert report.get("passed") is False, label


def test_unusable_scope_entries_end_as_error_in_dry_and_real_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC1, AC4, AC6: each rejected ``--scope`` is ``ERROR`` (exit 2), dry and real."""
    root = tmp_path / "project"
    root.mkdir()
    _author_project(root)
    (tmp_path / "outside.sv").write_text("// stub\n", encoding="utf-8")
    cases = [
        ("rtl/missing.sv", "does not exist"),
        ("../outside.sv", "outside the Project"),
        ("rtl", "is a directory"),
        ("rtl/table.mem", "not an HDL RTL mutation source"),
        ("rtl/other.sv", "does not match any source file"),
        ("tb/tb_top.sv", "is a testbench file"),
        ("tb/tb_top.sv", "never mutated"),
        ("", "--scope is empty"),
        (",", "--scope is empty"),
        ("rtl/a.sv,rtl/missing.sv", "does not exist"),
    ]

    for scope, reason in cases:
        label = f"--scope {scope!r}: {reason}"
        dry_code, dry_out, _ = _main(
            tmp_path, monkeypatch, capsys, "--scope", scope, "--count", "3", "--dry-run"
        )
        code, out, report = _main(tmp_path, monkeypatch, capsys, "--scope", scope, "--count", "3")
        assert dry_code == EXIT_ERROR, label
        assert reason in dry_out, label
        _assert_real_report(code, report, label)
        assert reason in out, label
        for printed in (dry_out, out):
            last_line = printed.rstrip().splitlines()[-1]
            assert last_line.startswith("RESULT: ERROR (scope: --scope "), label

    # Vendored and generated files may be named explicitly: both runs get past validation.
    for extra in ((), ("--dry-run",)):
        code, _, _ = _main(
            tmp_path,
            monkeypatch,
            capsys,
            "--scope",
            "vendor/v.sv,gen/g.sv",
            "--count",
            "3",
            *extra,
        )
        assert code == EXIT_SUCCESS, extra

    # A file the Target still lists but that is gone from disk is refused by both runs alike.
    (root / "rtl/a.sv").unlink()
    for extra in ((), ("--dry-run",)):
        code, out, report = _main(
            tmp_path, monkeypatch, capsys, "--scope", "rtl/a.sv", "--count", "3", *extra
        )
        assert code == EXIT_ERROR, extra
        assert "does not exist" in out, extra
        if not extra:
            _assert_real_report(code, report, "deleted Target file")


def test_target_without_mutable_rtl_ends_as_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC1, AC2: a Target of only testbench, vendored and generated RTL has nothing to mutate."""
    root = tmp_path / "project"
    root.mkdir()
    core = _DEFAULT_SCOPE_CORE.replace(
        "filesets: [rtl, imported, built, tb]", "filesets: [imported, built, tb]"
    )
    assert core != _DEFAULT_SCOPE_CORE
    _author_project(root, core)

    dry_code, dry_out, _ = _main(tmp_path, monkeypatch, capsys, "--count", "3", "--dry-run")
    code, out, report = _main(tmp_path, monkeypatch, capsys, "--count", "3")

    assert dry_code == EXIT_ERROR
    assert "has no mutable RTL" in dry_out
    _assert_real_report(code, report, "no mutable RTL")
    assert "has no mutable RTL" in out


def test_scope_that_differs_from_the_sealed_criterion_scope_ends_as_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: under strict criteria an explicit scope may not replace the criterion's."""
    endpoint = _two_core_endpoint(
        tmp_path,
        monkeypatch,
        {"mutation_score_first": {**_SETTINGS, "scope": ["x.v"]}},
        scope="y.v",
        strict=True,
    )

    result = endpoint._apply_campaign_defaults()

    assert result is not None
    assert result.exit_code == EXIT_ERROR
    assert "does not match the sealed Target criterion scope" in result.report_text


def test_repeated_scope_path_is_scoped_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: ``--scope a.sv,a.sv`` behaves like ``--scope a.sv``, dry and real."""
    outcomes: dict[str, tuple[int, list[tuple[Any, ...]]]] = {}
    for name, scope in (("twice", f"{_SCOPE},{_SCOPE}"), ("once", _SCOPE)):
        _, result, _ = _campaign(
            tmp_path / name, monkeypatch, scope=scope, run=_detects_every_mutant, count=3
        )
        assert result.exit_code == EXIT_SUCCESS, f"{name}: {result.report_text}"
        outcomes[name] = (result.exit_code, _mutant_keys(result))
    assert outcomes["twice"] == outcomes["once"]

    # The dry run (which fakes the campaign lookup) goes last.
    _prepare_scope_files(tmp_path, [_SCOPE])
    for scope in (f"{_SCOPE},{_SCOPE}", _SCOPE):
        endpoint = _make_endpoint(tmp_path, monkeypatch, scope=scope, extra_args=["--dry-run"])
        result, _ = _run_count_preview(endpoint, monkeypatch)
        assert result.exit_code == EXIT_SUCCESS, scope
        assert result.detail["scope"] == [_SCOPE], scope


def test_repeated_scope_flags_accumulate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC5: ``--scope a.sv --scope b.sv`` scopes both files."""
    scope = ["rtl/mod_a.sv", "rtl/mod_b.sv"]
    _prepare_scope_files(tmp_path, scope)
    endpoint = _make_endpoint(
        tmp_path,
        monkeypatch,
        scope=None,
        extra_args=["--scope", scope[0], "--scope", scope[1], "--dry-run"],
    )

    result, _ = _run_count_preview(endpoint, monkeypatch)

    assert result.exit_code == EXIT_SUCCESS
    assert result.detail["scope"] == scope


def test_mutation_errors_carry_hints_that_fit_their_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC7: each failure names its own fix; "pass --scope" only follows a default scope."""
    pass_scope = "pass --scope to choose the files to mutate"
    parse_error = "module m(;\n  assign = ;\nendmodule\n"
    no_site = "module m;\nendmodule\n"
    dead = "module m;\n  assign y = a + b; // dead\nendmodule\n"
    cases: list[tuple[str, dict[str, Any], str, list[str], list[str]]] = [
        # (name, campaign arguments, scope file text, must contain, must not contain)
        ("parse-error", {}, parse_error, [_SCOPE, "defines", "include directories"], [pass_scope]),
        ("no-site explicit", {}, no_site, ["name RTL files that hold logic"], [pass_scope]),
        ("no-site default", {"scope": None}, no_site, [pass_scope], []),
        (
            "all-equivalent explicit",
            {"simulation": _VERILATED, "count": 2},
            dead,
            ["none of the Mutants of", _SCOPE, "design_top_tb"],
            [pass_scope],
        ),
        (
            "all-equivalent default",
            {"simulation": _VERILATED, "count": 2, "scope": None},
            dead,
            ["none of the Mutants of", pass_scope],
            [],
        ),
    ]

    for name, arguments, text, expected, forbidden in cases:
        case_dir = tmp_path / name.replace(" ", "-")
        (case_dir / "rtl").mkdir(parents=True)
        (case_dir / _SCOPE).write_text(text, encoding="utf-8")
        _, result, _ = _campaign(case_dir, monkeypatch, **{"count": 3, **arguments})
        assert result.exit_code == EXIT_ERROR, name
        for fragment in expected:
            assert fragment in result.report_text, f"{name}: {fragment}"
        for fragment in forbidden:
            assert fragment not in result.report_text, f"{name}: {fragment}"
