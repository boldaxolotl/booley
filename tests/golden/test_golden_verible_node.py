"""Golden snapshot for the Edalize flow-API Verible lint tool node (ADR 0033).

The node (``src/booley/data/edalize/verible.py``) is Booley-authored but
destined for upstream Edalize — this test doubles as the upstream PR's test,
so it exercises the node exactly as the Generic/Lint flow would: ``setup()``
against a canned EDAM, then the EdaCommands Makefile snapshot. Drift classes
this protects against: a silently dropped ``--parse_fatal`` (parse errors
would score as clean, the QA-7 trap), ``--lint_fatal=false`` falling out
(the flag defaults to TRUE in the binary, so findings would become a hard
tool error), and rules/waiver plumbing falling out of the command line.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tests.golden.conftest import assert_matches_golden

pytest.importorskip("edalize")


def _load_verible_node():
    """Import the vendored node from booley's data dir (its install target is
    the sandbox image's ``site-packages/edalize/tools/`` — see Dockerfile)."""
    import booley

    src = Path(booley.__file__).parent / "data" / "edalize" / "verible.py"
    spec = importlib.util.spec_from_file_location("booley_data_edalize_verible", src)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _edam(files: list[dict], eda_tool_options: dict | None = None) -> dict:
    return {
        "name": "lint_style_demo",
        "toplevel": "top",
        "files": files,
        # A vlogdefine parameter must be *tolerated* (the lint flow declares
        # the argtype) but never forwarded — verible-verilog-lint lints
        # unpreprocessed sources and has no define flag.
        "parameters": {
            "CFG": {"datatype": "bool", "paramtype": "vlogdefine", "default": True},
        },
        "tool_options": {"verible": eda_tool_options or {}},
    }


_FULL_FILESET = [
    {"name": "rtl/top.sv", "file_type": "systemVerilogSource"},
    {"name": "rtl/legacy.v", "file_type": "verilogSource"},
    # Include files are reached from their includers; never linted standalone.
    {"name": "rtl/defs.svh", "file_type": "systemVerilogSource", "is_include_file": True},
    {"name": "lint/rules.cfg", "file_type": "veribleLintRules"},
    {"name": "lint/waivers.txt", "file_type": "veribleLintWaiver"},
    {"name": "lint/waivers_extra.txt", "file_type": "veribleLintWaiver"},
    # Non-Verilog files pass through untouched (downstream nodes' business).
    {"name": "constraints/timing.sdc", "file_type": "SDC"},
]


def _setup(files: list[dict], eda_tool_options: dict | None = None, work_root: str = "."):
    module = _load_verible_node()
    node = module.Verible()
    node.work_root = work_root
    node.setup(_edam(files, eda_tool_options))
    return node


def test_verible_node_makefile_golden(tmp_path: Path) -> None:
    """Full fixture: rules config + waivers + defines + pass-through options."""
    node = _setup(
        _FULL_FILESET,
        eda_tool_options={
            "ruleset": "default",
            "rules": ["-module-filename", "line-length"],
            "verible_lint_args": ["--show_diagnostic_context"],
        },
    )
    makefile = tmp_path / "Makefile"
    node.commands.write(str(makefile))
    assert_matches_golden(
        "lint/verible_node_makefile.mk",
        makefile.read_text(encoding="utf-8"),
    )


def test_verible_node_minimal_makefile_golden(tmp_path: Path) -> None:
    """No rules/waivers/options: bare ``--parse_fatal`` and the sources — the
    node injects no ruleset policy of its own (ADR 0033 decision 3)."""
    node = _setup([{"name": "rtl/top.sv", "file_type": "systemVerilogSource"}])
    makefile = tmp_path / "Makefile"
    node.commands.write(str(makefile))
    assert_matches_golden(
        "lint/verible_node_minimal_makefile.mk",
        makefile.read_text(encoding="utf-8"),
    )


def test_verible_node_never_emits_lint_fatal_or_defines() -> None:
    """``--lint_fatal`` defaults to TRUE in the binary — it must be disabled
    explicitly or findings become a hard tool error (QA-7 misread); a
    forwarded define would imply preprocessing Verible won't do."""
    node = _setup(_FULL_FILESET)
    [command] = node.commands.commands
    argv = [str(tok) for tok in command.commands[0]]
    assert "--parse_fatal" in argv
    assert "--lint_fatal=false" in argv
    assert "--lint_fatal" not in argv
    assert not any("CFG" in tok for tok in argv)
    assert not any(tok.startswith(("-D", "+define+")) for tok in argv)


def test_verible_node_rejects_multiple_rules_configs() -> None:
    files = [
        {"name": "rtl/top.sv", "file_type": "systemVerilogSource"},
        {"name": "a.cfg", "file_type": "veribleLintRules"},
        {"name": "b.cfg", "file_type": "veribleLintRules"},
    ]
    with pytest.raises(RuntimeError, match="single rules file"):
        _setup(files)


def test_verible_node_rejects_empty_fileset() -> None:
    """An empty lint would `make` to a trivial PASS — refuse at setup instead."""
    with pytest.raises(RuntimeError, match="at least one"):
        _setup([{"name": "lint/rules.cfg", "file_type": "veribleLintRules"}])
