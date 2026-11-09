"""Every Flow's report directory under the protected Project alias is canonicalized."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.lint.flow import LintFlow
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.mcp.base import EXIT_ERROR, EXIT_SUCCESS
from tests.conftest import require_symlinks, symlink_or_skip
from tests.flows import test_target_omission as omission
from tests.flows.test_flow_base import EchoFlow
from tests.runtime.test_sandbox_layout import configure_project_alias

BUILTIN_FLOWS = {"lint": LintFlow, "synth": AsicSynthesizeFlow, "fpga": FpgaImplFlow}


@pytest.fixture
def project(tmp_path, monkeypatch):
    return omission.project.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def executed(monkeypatch):
    return omission.executed.__wrapped__(monkeypatch)


@pytest.fixture
def image_alias(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """A trusted image alias and its data directory, away from the Project root."""
    require_symlinks(tmp_path)
    data = tmp_path / "image-data"
    data.mkdir()
    alias = tmp_path / "image" / "booley-project"
    alias.parent.mkdir()
    symlink_or_skip(alias, data, target_is_directory=True)
    configure_project_alias(monkeypatch, alias, data)
    return alias, data


def _workspace(project: Path, name: str) -> Path:
    workspace = project / f"work-{name}"
    omission.write_targets(workspace, name)
    return workspace


def test_flow_report_root_under_project_alias_is_canonical(
    project, executed, image_alias, monkeypatch
):
    alias, data = image_alias
    for name, flow_type in BUILTIN_FLOWS.items():
        flow = flow_type()
        request = flow.request_type(
            target="chosen", work_dir=_workspace(project, name), report_dir=alias / "reports"
        )
        result = flow.execute(request)
        assert result.exit_code == EXIT_SUCCESS, (name, result.outcome.report_text)
        assert flow.args.report_dir == data / "reports", name
        assert (data / "reports" / name / "1" / "report.json").is_file(), name

    # A Custom Flow takes the same path: the rewrite is by endpoint kind, not by name.
    custom = EchoFlow()
    exit_code = custom.main(
        ["--target", "t", "--work-dir", str(project), "--report-dir", str(alias / "reports")]
    )
    assert exit_code == EXIT_SUCCESS
    assert custom.args.report_dir == data / "reports"
    assert (data / "reports" / "echo_tool" / "1" / "report.json").is_file()


def test_flow_report_root_alias_traversal_is_refused(project, executed, image_alias):
    alias, data = image_alias
    for name, flow_type in BUILTIN_FLOWS.items():
        flow = flow_type()
        request = flow.request_type(
            target="chosen",
            work_dir=_workspace(project, name),
            report_dir=alias / ".." / "reports",
        )
        result = flow.execute(request)
        assert result.exit_code == EXIT_ERROR, name
        assert "traversal" in result.outcome.report_text, name
        assert executed == [], name
    assert not (alias.parent / "reports").exists()
    assert not (data.parent / "reports").exists()
