"""The static gate rejects green-test failure modes without importing tests."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from dataclasses import asdict
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / ".github/scripts"))

from check_suppressed_assertions import apply_exemptions, check_repository, scan_source
from ci_changes import classify, required_jobs

_SCRIPT = _ROOT / ".github/scripts/check_suppressed_assertions.py"
_MANIFEST = ".github/scripts/suppressed_assertion_exemptions.json"


def _scan(source: str):
    return scan_source(textwrap.dedent(source), "tests/example.py")


@pytest.mark.parametrize("exception", ["OSError", "RuntimeError", "AssertionError", "Exception"])
def test_rejects_assertions_even_with_narrow_exceptions(exception: str) -> None:
    findings = _scan(f"""
        import contextlib
        def test_contract():
            with contextlib.suppress({exception}):
                assert False
    """)
    assert any(f.rule == "suppressed-assertion" for f in findings)


@pytest.mark.parametrize(
    "assertion",
    [
        "mock.assert_called_once_with(1)",
        "mock.assert_awaited_once()",
        "_assert_contract(value)",
        "assert_contract(value)",
        "check_contract(value)",
        "pytest.fail('bad contract')",
    ],
)
def test_rejects_assertion_calls(assertion: str) -> None:
    findings = _scan(f"""
        from contextlib import suppress
        with suppress(OSError):
            {assertion}
    """)
    assert [f.rule for f in findings] == ["suppressed-assertion-call"]


@pytest.mark.parametrize(
    "imports, expression",
    [
        ("import contextlib", "contextlib.suppress"),
        ("import contextlib as cl", "cl.suppress"),
        ("from contextlib import suppress", "suppress"),
        ("from contextlib import suppress as ignore", "ignore"),
        ("import contextlib\nignore = contextlib.suppress", "ignore"),
        ("import contextlib\ncl = contextlib\nignore = cl.suppress\nagain = ignore", "again"),
    ],
)
def test_import_and_assignment_aliases(imports: str, expression: str) -> None:
    assert _scan(f"{imports}\nwith {expression}(OSError):\n    assert False\n")


def test_late_enclosing_import_and_nested_async_scope() -> None:
    findings = _scan("""
        def test_shutdown():
            async def scenario():
                with contextlib.suppress(OSError):
                    assert False
            import contextlib
    """)
    assert [(f.scope, f.rule) for f in findings] == [
        ("test_shutdown.scenario", "suppressed-assertion")
    ]


def test_parameter_and_local_shadowing_do_not_match_contextlib() -> None:
    assert (
        _scan("""
        from contextlib import suppress
        def test_other(suppress):
            with suppress(OSError):
                assert False
        def test_local():
            suppress = unrelated
            with suppress(OSError):
                assert False
    """)
        == []
    )


def test_nested_blocks_class_bodies_and_multiple_managers() -> None:
    findings = _scan("""
        import contextlib
        with contextlib.suppress(OSError), other():
            if condition:
                with other():
                    assert False
            class Immediate:
                assert False
    """)
    assert [(f.line, f.scope) for f in findings] == [(6, "<module>"), (8, "Immediate")]


def test_later_context_manager_expression_is_suppressed() -> None:
    findings = _scan("""
        from contextlib import suppress
        with suppress(OSError), _assert_contract():
            pass
    """)
    assert [f.rule for f in findings] == ["suppressed-assertion-call"]


def test_deferred_bodies_are_separate_but_defaults_and_decorators_execute_now() -> None:
    findings = _scan("""
        from contextlib import suppress
        with suppress(OSError):
            @_assert_decorator()
            def helper(value=_assert_default()):
                assert False
            deferred = lambda: _assert_later()
    """)
    assert [(f.line, f.rule) for f in findings] == [
        (4, "suppressed-assertion-call"),
        (5, "suppressed-assertion-call"),
    ]


def test_assertion_call_assignment_alias() -> None:
    assert _scan("""
        from contextlib import suppress
        verify = mock.assert_called_once
        with suppress(OSError):
            verify()
    """)


@pytest.mark.parametrize(
    "source, rule",
    [
        ("from contextlib import *", "unsupported-contextlib-star-import"),
        ("manager = contextlib.suppress(OSError)", "unsupported-suppress-use"),
        ("stack.enter_context(contextlib.suppress(OSError))", "unsupported-suppress-use"),
        ("with contextlib.suppress(*errors):\n    pass", "unsupported-suppress-arguments"),
    ],
)
def test_unsupported_forms_fail_closed(source: str, rule: str) -> None:
    assert [f.rule for f in _scan("import contextlib\n" + source)] == [rule]


@pytest.mark.parametrize(
    "exception", ["Exception", "BaseException", "AssertionError", "builtins.Exception"]
)
def test_broad_suppression_cannot_hide_arbitrary_helper_assertions(exception: str) -> None:
    findings = _scan(f"""
        import contextlib
        with contextlib.suppress({exception}):
            arbitrary_helper()
    """)
    assert [f.rule for f in findings] == ["broad-suppression"]


def test_broad_exception_assignment_alias() -> None:
    assert [
        f.rule
        for f in _scan("""
        from contextlib import suppress
        error = Exception
        with suppress(error):
            operation()
    """)
    ] == ["broad-suppression"]


# The supplied issue does not identify the repaired files/commits, and main's
# suppress-related history contains no recoverable pair. These are behavioral
# reproductions, not claimed copies of unidentified historical tests.
@pytest.mark.parametrize(
    "body",
    [
        "raise OSError('operation failed')\nassert result == 'new'",
        "assert result == 'new'\nassert result == 'old'",
    ],
)
def test_green_tests_that_skip_or_hide_contracts_are_rejected(body: str) -> None:
    source = "import contextlib\nresult = 'old'\nwith contextlib.suppress(Exception):\n"
    source += textwrap.indent(body, "    ")
    exec(source, {})  # Prove the defective test really returns successfully.
    findings = _scan(source)
    assert any(f.rule == "suppressed-assertion" for f in findings)


def test_cleanup_and_contract_after_suppression_are_allowed() -> None:
    assert (
        _scan("""
        import contextlib
        with contextlib.suppress(OSError):
            path.unlink()
        assert result == expected
    """)
        == []
    )


def _entry(finding):
    return asdict(finding) | {
        "purpose": "intentional-assertion-input",
        "reason": "Intentional failing assertion is input to assertion handling.",
        "verification": "test_handler checks the captured outcome after the block.",
    }


def test_exemption_targets_one_occurrence() -> None:
    findings = _scan("""
        from contextlib import suppress
        def test_handler():
            with suppress(OSError):
                assert False
                assert False
    """)
    assert apply_exemptions(findings, [_entry(findings[0])]) == [findings[1]]


@pytest.mark.parametrize(
    "mutation", ["duplicate", "stale", "reason", "verification", "wildcard", "position", "purpose"]
)
def test_invalid_exemptions_fail(mutation: str) -> None:
    findings = _scan("from contextlib import suppress\nwith suppress(OSError):\n    assert False")
    entry = _entry(findings[0])
    entries = [entry]
    if mutation == "duplicate":
        entries.append(entry)
    elif mutation == "stale":
        entry["line"] += 1
    elif mutation == "wildcard":
        entry["path"] = "tests/*"
    elif mutation == "position":
        entry["line"] = True
    else:
        entry[mutation] = ""
    with pytest.raises(ValueError):
        apply_exemptions(findings, entries)


def test_unsupported_findings_cannot_be_exempted() -> None:
    findings = _scan("from contextlib import suppress\nmanager = suppress(OSError)")
    with pytest.raises(ValueError, match="only intentional"):
        apply_exemptions(findings, [_entry(findings[0])])


def _repository(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    manifest = tmp_path / _MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.write_text("[]\n", encoding="utf-8")
    return tmp_path


def test_cli_sorted_diagnostics_and_nonzero_exit_without_importing(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    for name in ["z", "a"]:
        (repo / f"tests/{name}.py").write_text(
            "import contextlib\nraise RuntimeError('must not import')\n"
            "with contextlib.suppress(OSError):\n    assert False\n",
            encoding="utf-8",
        )
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--repo", str(repo)],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        "tests/a.py:4:5: suppressed-assertion (<module>)",
        "tests/z.py:4:5: suppressed-assertion (<module>)",
        "suppressed-assertions: checked 2 Python files; 2 violations",
    ]


@pytest.mark.parametrize("problem", ["empty", "syntax", "manifest", "missing-manifest"])
def test_repository_failures_are_not_green(tmp_path: Path, problem: str) -> None:
    repo = _repository(tmp_path)
    if problem != "empty":
        (repo / "tests/test_example.py").write_text(
            "def broken(" if problem == "syntax" else "assert True\n", encoding="utf-8"
        )
    if problem == "manifest":
        (repo / _MANIFEST).write_text("{}", encoding="utf-8")
    if problem == "missing-manifest":
        (repo / _MANIFEST).unlink()
    with pytest.raises((ValueError, SyntaxError, OSError)):
        check_repository(repo)


def test_current_repository_has_no_unexempted_violations() -> None:
    count, diagnostics = check_repository(_ROOT)
    assert count > 0
    assert diagnostics == []


def test_exemption_only_change_runs_lint_and_lint_invokes_guard() -> None:
    assert "lint" in required_jobs(classify([_MANIFEST]))
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8"))
    assert any(
        step.get("run") == "python .github/scripts/check_suppressed_assertions.py"
        for step in workflow["jobs"]["lint"]["steps"]
    )


def test_intentional_assertion_handling_requires_exact_entries_for_both_findings() -> None:
    findings = _scan(
        "from contextlib import suppress\nwith suppress(AssertionError):\n    assert False"
    )
    assert len(findings) == 2
    assert apply_exemptions(findings, [_entry(f) for f in findings]) == []


def test_import_shadowing_and_class_bindings_follow_function_lookup() -> None:
    findings = _scan("""
        import contextlib
        class Example:
            contextlib = unrelated
            def test_real(self):
                with contextlib.suppress(OSError):
                    assert False
        def test_other():
            import unrelated as contextlib
            with contextlib.suppress(OSError):
                assert False
    """)
    assert [(f.scope, f.rule) for f in findings] == [("Example.test_real", "suppressed-assertion")]


def test_repository_accepts_valid_reviewed_exemption(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    source = "from contextlib import suppress\nwith suppress(AssertionError):\n    assert False\n"
    path = "tests/test_example.py"
    (repo / path).write_text(source, encoding="utf-8")
    entries = [_entry(f) for f in scan_source(source, path)]
    (repo / _MANIFEST).write_text(json.dumps(entries), encoding="utf-8")
    assert check_repository(repo) == (1, [])


@pytest.mark.parametrize("assertion", ["assertEqual", "assertTrue", "assertAlmostEqual"])
def test_unittest_assertions_and_receiver_assignment_aliases(assertion: str) -> None:
    findings = _scan(f"""
        import contextlib
        def test_contract(self):
            verify = self.{assertion}
            with contextlib.suppress(OSError):
                self.{assertion}(False)
                verify(False)
    """)
    assert [f.rule for f in findings] == ["suppressed-assertion-call", "suppressed-assertion-call"]


def test_unittest_assertion_skipped_by_narrow_error_is_not_green_to_guard() -> None:
    source = textwrap.dedent("""
        import contextlib
        import unittest
        with contextlib.suppress(OSError):
            raise OSError()
            unittest.TestCase().assertEqual(1, 2)
    """)
    exec(source, {})
    assert [f.rule for f in _scan(source)] == ["suppressed-assertion-call"]


def test_class_attribute_cannot_hide_method_suppression() -> None:
    source = textwrap.dedent("""
        from contextlib import suppress
        class TestContracts:
            suppress = None
            def test_contract(self):
                with suppress(AssertionError):
                    assert False
        TestContracts().test_contract()
    """)
    exec(source, {})
    assert [f.rule for f in _scan(source)] == ["broad-suppression", "suppressed-assertion"]


@pytest.mark.parametrize(
    "argument",
    [
        "(AssertionError, OSError)",
        "(Exception,)",
        "Exception if flag else OSError",
        "errors[0]",
        "get_errors()",
        "errors",
    ],
)
def test_computed_or_unresolved_exception_arguments_fail_closed(argument: str) -> None:
    source = f"import contextlib\nerrors = (Exception, OSError)\nwith contextlib.suppress({argument}):\n    arbitrary_helper()"
    assert "unsupported-suppress-arguments" in [f.rule for f in _scan(source)]


@pytest.mark.parametrize("argument", ["self.failureException", "pytest.fail.Exception"])
def test_failure_exception_attributes_are_broad(argument: str) -> None:
    findings = _scan(f"import contextlib\nwith contextlib.suppress({argument}):\n    helper()")
    assert [f.rule for f in findings] == ["broad-suppression"]


@pytest.mark.parametrize(
    "escape",
    [
        "partial(contextlib.suppress, AssertionError)",
        "managers = [contextlib.suppress]",
        "s, _ = contextlib.suppress, None",
        "(s := contextlib.suppress)",
        "obj.manager = contextlib.suppress",
        "factory(contextlib.suppress)",
    ],
)
def test_suppression_references_cannot_escape_analysis(escape: str) -> None:
    findings = _scan("import contextlib\n" + escape)
    assert [f.rule for f in findings] == ["unsupported-suppress-reference"]


@pytest.mark.parametrize(
    "operation",
    [
        "subprocess.check_call(['command'])",
        "subprocess.check_output(['command'])",
        "process.check_returncode()",
        "run(['command'])",
    ],
)
def test_subprocess_error_checks_are_not_assertion_helpers(operation: str) -> None:
    source = f"import contextlib\nimport subprocess\nrun = subprocess.check_call\nwith contextlib.suppress(subprocess.CalledProcessError):\n    {operation}"
    assert _scan(source) == []


def test_import_without_as_binds_the_top_level_module() -> None:
    assert _scan("""
        import contextlib.some_module
        with contextlib.suppress(OSError):
            assert False
    """)


def test_class_body_retains_outer_binding_before_later_assignment() -> None:
    source = textwrap.dedent("""
        import contextlib
        class TestContracts:
            with contextlib.suppress(AssertionError):
                assert False
            contextlib = None
    """)
    exec(source, {})
    assert [f.rule for f in _scan(source)] == ["broad-suppression", "suppressed-assertion"]


@pytest.mark.parametrize("helper", ["assert_contract", "_assert_contract", "check_contract"])
def test_local_assertion_helper_assignment_alias_preserves_identity(helper: str) -> None:
    source = textwrap.dedent(f"""
        import contextlib
        def {helper}():
            assert False
        verify = {helper}
        with contextlib.suppress(OSError):
            raise OSError()
            verify()
    """)
    exec(source, {})
    assert [f.rule for f in _scan(source)] == ["suppressed-assertion-call"]


@pytest.mark.parametrize("first", ["subprocess.check_call", "mock.assert_called_once"])
def test_mixed_subprocess_and_assertion_bindings_remain_conservative(first: str) -> None:
    second = (
        "mock.assert_called_once" if first == "subprocess.check_call" else "subprocess.check_call"
    )
    source = textwrap.dedent(f"""
        import contextlib, subprocess
        from unittest.mock import Mock
        mock = Mock()
        verify = {first}
        verify = {second}
        with contextlib.suppress(OSError):
            raise OSError()
            verify()
    """)
    exec(source, {})
    assert [f.rule for f in _scan(source)] == ["suppressed-assertion-call"]


@pytest.mark.parametrize(
    "argument",
    [
        "get_errors().error",
        "errors[0].error",
        "self.error",
        "(lambda: errors)().error",
    ],
)
def test_computed_or_unresolved_attribute_exception_arguments_fail_closed(argument: str) -> None:
    findings = _scan(f"import contextlib\nwith contextlib.suppress({argument}):\n    helper()")
    assert [f.rule for f in findings] == ["unsupported-suppress-arguments"]


def test_computed_exception_attribute_cannot_swallow_helper_assertion() -> None:
    source = textwrap.dedent("""
        import contextlib
        class Errors:
            error = AssertionError
        def get_errors():
            return Errors()
        def helper():
            assert False
        with contextlib.suppress(get_errors().error):
            helper()
    """)
    exec(source, {})
    assert [f.rule for f in _scan(source)] == ["unsupported-suppress-arguments"]
