"""The static gate rejects green-test failure modes without importing tests."""

from __future__ import annotations

import itertools
import json
import sys
import textwrap
from dataclasses import asdict
from pathlib import Path

import pytest
import yaml
from tests.subprocess_budget import run_bounded_child

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / ".github/scripts"))

from check_suppressed_assertions import apply_exemptions, check_repository, scan_source
from ci_changes import CATEGORIES, classify, required_jobs

_SCRIPT = _ROOT / ".github/scripts/check_suppressed_assertions.py"
_MANIFEST = ".github/scripts/suppressed_assertion_exemptions.json"


def _scan(source: str):
    return scan_source(textwrap.dedent(source), "tests/example.py")


_SUPPRESS = "import contextlib\nwith contextlib.suppress({}):\n    {}\n"
_CALL = "from contextlib import suppress\nwith suppress(OSError):\n    {}\n"
_ARGS = ["unsupported-suppress-arguments"]
_CALLED = ["suppressed-assertion-call"]
_BOTH = ["broad-suppression", "suppressed-assertion"]
_HIDDEN = "import contextlib\nclass Errors:\n    error = AssertionError\ndef get_errors():\n    return Errors()\n"
_RAISED = "import contextlib\nwith contextlib.suppress(OSError):\n    raise OSError()\n    {}\n"

# Each source keeps its text; the expected value is the exact rule list the scanner returns.
_RULE_CASES: dict[str, tuple[str, list[str]]] = {
    **{
        f"narrow-{name}": (
            f"import contextlib\ndef test_contract():\n    with contextlib.suppress({name}):\n        assert False\n",
            rules,
        )
        for name, rules in [
            ("OSError", ["suppressed-assertion"]),
            ("RuntimeError", ["suppressed-assertion"]),
            ("AssertionError", _BOTH),
            ("Exception", _BOTH),
        ]
    },
    **{
        f"call-{call}": (_CALL.format(call), _CALLED)
        for call in [
            "mock.assert_called_once_with(1)",
            "mock.assert_awaited_once()",
            "_assert_contract(value)",
            "assert_contract(value)",
            "check_contract(value)",
            "pytest.fail('bad contract')",
        ]
    },
    **{
        f"alias-{index}-{expression}": (
            f"{imports}\nwith {expression}(OSError):\n    assert False\n",
            ["suppressed-assertion"],
        )
        for index, (imports, expression) in enumerate(
            [
                ("import contextlib", "contextlib.suppress"),
                ("import contextlib as cl", "cl.suppress"),
                ("from contextlib import suppress", "suppress"),
                ("from contextlib import suppress as ignore", "ignore"),
                ("import contextlib\nignore = contextlib.suppress", "ignore"),
                (
                    "import contextlib\ncl = contextlib\nignore = cl.suppress\nagain = ignore",
                    "again",
                ),
                ("import contextlib.some_module", "contextlib.suppress"),
            ]
        )
    },
    "shadowed-names": (
        "from contextlib import suppress\ndef test_other(suppress):\n    with suppress(OSError):\n        assert False\ndef test_local():\n    suppress = unrelated\n    with suppress(OSError):\n        assert False\n",
        [],
    ),
    "later-manager": (
        "from contextlib import suppress\nwith suppress(OSError), _assert_contract():\n    pass\n",
        _CALLED,
    ),
    "call-alias": (
        "from contextlib import suppress\nverify = mock.assert_called_once\nwith suppress(OSError):\n    verify()\n",
        _CALLED,
    ),
    "star-import": (
        "import contextlib\nfrom contextlib import *",
        ["unsupported-contextlib-star-import"],
    ),
    "stored-manager": (
        "import contextlib\nmanager = contextlib.suppress(OSError)",
        ["unsupported-suppress-use"],
    ),
    "entered-manager": (
        "import contextlib\nstack.enter_context(contextlib.suppress(OSError))",
        ["unsupported-suppress-use"],
    ),
    "starred-arguments": (
        "import contextlib\nwith contextlib.suppress(*errors):\n    pass",
        _ARGS,
    ),
    **{
        f"broad-{name}": (_SUPPRESS.format(name, "arbitrary_helper()"), ["broad-suppression"])
        for name in ["Exception", "BaseException", "AssertionError", "builtins.Exception"]
    },
    "broad-assignment": (
        "from contextlib import suppress\nerror = Exception\nwith suppress(error):\n    operation()\n",
        ["broad-suppression"],
    ),
    # The issue does not identify the repaired tests, so these are behavioral reproductions.
    "skipped-contract": (
        "import contextlib\nresult = 'old'\nwith contextlib.suppress(Exception):\n    raise OSError('operation failed')\n    assert result == 'new'\n",
        _BOTH,
    ),
    "hidden-contract": (
        "import contextlib\nresult = 'old'\nwith contextlib.suppress(Exception):\n    assert result == 'new'\n    assert result == 'old'\n",
        [*_BOTH, "suppressed-assertion"],
    ),
    "cleanup-then-contract": (
        "import contextlib\nwith contextlib.suppress(OSError):\n    path.unlink()\nassert result == expected\n",
        [],
    ),
    **{
        f"unittest-{name}": (
            f"import contextlib\ndef test_contract(self):\n    verify = self.{name}\n    with contextlib.suppress(OSError):\n        self.{name}(False)\n        verify(False)\n",
            _CALLED * 2,
        )
        for name in ["assertEqual", "assertTrue", "assertAlmostEqual"]
    },
    "unittest-after-narrow-error": (
        "import contextlib\nimport unittest\nwith contextlib.suppress(OSError):\n    raise OSError()\n    unittest.TestCase().assertEqual(1, 2)\n",
        _CALLED,
    ),
    "class-attribute": (
        "from contextlib import suppress\nclass TestContracts:\n    suppress = None\n    def test_contract(self):\n        with suppress(AssertionError):\n            assert False\n",
        _BOTH,
    ),
    "class-body-before-assignment": (
        "import contextlib\nclass TestContracts:\n    with contextlib.suppress(AssertionError):\n        assert False\n    contextlib = None\n",
        _BOTH,
    ),
    **{
        f"computed-{argument}": (
            f"import contextlib\nerrors = (Exception, OSError)\nwith contextlib.suppress({argument}):\n    arbitrary_helper()",
            _ARGS,
        )
        for argument in [
            "(AssertionError, OSError)",
            "(Exception,)",
            "Exception if flag else OSError",
            "errors[0]",
            "get_errors()",
            "errors",
        ]
    },
    **{
        f"failure-exception-{argument}": (
            _SUPPRESS.format(argument, "helper()"),
            ["broad-suppression"],
        )
        for argument in ["self.failureException", "pytest.fail.Exception"]
    },
    **{
        f"escape-{escape}": (f"import contextlib\n{escape}", ["unsupported-suppress-reference"])
        for escape in [
            "partial(contextlib.suppress, AssertionError)",
            "managers = [contextlib.suppress]",
            "s, _ = contextlib.suppress, None",
            "(s := contextlib.suppress)",
            "obj.manager = contextlib.suppress",
            "factory(contextlib.suppress)",
        ]
    },
    **{
        f"subprocess-{operation}": (
            f"import contextlib\nimport subprocess\nrun = subprocess.check_call\nwith contextlib.suppress(subprocess.CalledProcessError):\n    {operation}",
            [],
        )
        for operation in [
            "subprocess.check_call(['command'])",
            "subprocess.check_output(['command'])",
            "process.check_returncode()",
            "run(['command'])",
        ]
    },
    **{
        f"helper-alias-{helper}": (
            f"import contextlib\ndef {helper}():\n    assert False\nverify = {helper}\nwith contextlib.suppress(OSError):\n    raise OSError()\n    verify()\n",
            _CALLED,
        )
        for helper in ["assert_contract", "_assert_contract", "check_contract"]
    },
    **{
        f"mixed-{first}": (
            f"import contextlib, subprocess\nfrom unittest.mock import Mock\nmock = Mock()\nverify = {first}\nverify = {second}\nwith contextlib.suppress(OSError):\n    raise OSError()\n    verify()\n",
            _CALLED,
        )
        for first, second in [
            ("subprocess.check_call", "mock.assert_called_once"),
            ("mock.assert_called_once", "subprocess.check_call"),
        ]
    },
    **{
        f"attribute-{argument}": (_SUPPRESS.format(argument, "helper()"), _ARGS)
        for argument in [
            "get_errors().error",
            "errors[0].error",
            "self.error",
            "(lambda: errors)().error",
        ]
    },
    "attribute-of-call": (
        _HIDDEN
        + "def helper():\n    assert False\nwith contextlib.suppress(get_errors().error):\n    helper()\n",
        _ARGS,
    ),
}


@pytest.mark.parametrize("source, rules", _RULE_CASES.values(), ids=list(_RULE_CASES))
def test_scan_rules(source: str, rules: list[str]) -> None:
    assert [f.rule for f in _scan(source)] == rules


@pytest.mark.parametrize(
    "source, expected",
    [
        pytest.param(
            """
            def test_shutdown():
                async def scenario():
                    with contextlib.suppress(OSError):
                        assert False
                import contextlib
            """,
            [(5, "test_shutdown.scenario", "suppressed-assertion")],
            id="late-enclosing-import",
        ),
        pytest.param(
            """
            import contextlib
            with contextlib.suppress(OSError), other():
                if condition:
                    with other():
                        assert False
                class Immediate:
                    assert False
            """,
            [(6, "<module>", "suppressed-assertion"), (8, "Immediate", "suppressed-assertion")],
            id="nested-blocks",
        ),
        pytest.param(
            """
            from contextlib import suppress
            with suppress(OSError):
                @_assert_decorator()
                def helper(value=_assert_default()):
                    assert False
                deferred = lambda: _assert_later()
            """,
            [
                (4, "<module>", "suppressed-assertion-call"),
                (5, "<module>", "suppressed-assertion-call"),
            ],
            id="deferred-bodies",
        ),
        pytest.param(
            """
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
            """,
            [(7, "Example.test_real", "suppressed-assertion")],
            id="class-bindings",
        ),
    ],
)
def test_scan_positions(source: str, expected: list[tuple[int, str, str]]) -> None:
    findings = _scan(source)
    assert [(f.line, f.scope, f.rule) for f in findings] == expected


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
    result = run_bounded_child([sys.executable, str(_SCRIPT), "--repo", str(repo)], check=False)
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


# The required ``lint`` job is the sole enforcement of the whole-repository scan
# (pytest no longer re-runs it), so every change that can affect the scan's
# inputs, or that runs pytest at all, must select ``lint``.
@pytest.mark.parametrize(
    "paths",
    [
        pytest.param([".github/scripts/check_suppressed_assertions.py"], id="checker-script"),
        pytest.param([_MANIFEST], id="exemption-manifest"),
        pytest.param([".github/workflows/test.yml"], id="workflow"),
        pytest.param(["pyproject.toml"], id="packaging-config"),
        pytest.param(["src/booley/__init__.py"], id="booley-source"),
        pytest.param(["tests/ci/test_suppressed_assertions.py"], id="test-file"),
        pytest.param(["website/mkdocs.yml"], id="project-site"),
        pytest.param(["unmapped/config.xyz"], id="unknown-path"),
        pytest.param(["docs/architecture.md", "tests/test_example.py"], id="docs-plus-test"),
    ],
)
def test_scan_input_changes_require_lint(paths: list[str]) -> None:
    assert "lint" in required_jobs(classify(paths))


def _test_workflow() -> dict:
    """Parse the CI workflow that hosts the ``lint`` and ``ci-required`` jobs."""
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8"))


def test_ci_required_waits_for_lint() -> None:
    """A failed or skipped ``lint`` must block the protected ``ci-required`` check."""
    assert "lint" in _test_workflow()["jobs"]["ci-required"]["needs"]


def test_lint_checker_step_can_fail_the_job() -> None:
    """The checker step must not be skippable or advisory, or ``lint`` stays green."""
    lint = _test_workflow()["jobs"]["lint"]
    assert "continue-on-error" not in lint
    checker_steps = [
        step
        for step in lint["steps"]
        if step.get("run") == "python .github/scripts/check_suppressed_assertions.py"
    ]
    assert len(checker_steps) == 1
    assert "continue-on-error" not in checker_steps[0]
    assert "if" not in checker_steps[0]


def test_every_category_set_that_runs_pytest_also_runs_lint() -> None:
    """No classification may run the pytest legs without the ``lint`` scan."""
    for size in range(len(CATEGORIES) + 1):
        for categories in itertools.combinations(CATEGORIES, size):
            jobs = required_jobs(set(categories))
            if jobs & {"test", "test-verify", "coverage-shards", "coverage"}:
                assert "lint" in jobs, categories


def test_intentional_assertion_handling_requires_exact_entries_for_both_findings() -> None:
    findings = _scan(
        "from contextlib import suppress\nwith suppress(AssertionError):\n    assert False"
    )
    assert len(findings) == 2
    assert apply_exemptions(findings, [_entry(f) for f in findings]) == []


def test_repository_accepts_valid_reviewed_exemption(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    source = "from contextlib import suppress\nwith suppress(AssertionError):\n    assert False\n"
    path = "tests/test_example.py"
    (repo / path).write_text(source, encoding="utf-8")
    entries = [_entry(f) for f in scan_source(source, path)]
    (repo / _MANIFEST).write_text(json.dumps(entries), encoding="utf-8")
    assert check_repository(repo) == (1, [])
