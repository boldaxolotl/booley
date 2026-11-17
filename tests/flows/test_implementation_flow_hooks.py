"""The implementation Flow base's run-sequence hooks: defaults and required overrides."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.flows.fpga.cli import FpgaArguments
from booley.flows.fpga.request import FpgaRequest
from booley.flows.implementation_flow import ImplementationFlow
from tests.flows.test_implementation_flow import _ProbeMetrics


class _Bare(ImplementationFlow[FpgaRequest, _ProbeMetrics]):
    request_type = FpgaRequest
    argument_adapter = FpgaArguments
    name = "fpga"


def test_run_sequence_hooks_must_be_overridden() -> None:
    flow = _Bare()
    with pytest.raises(NotImplementedError):
        flow._plan_implementation(["t"])
    with pytest.raises(NotImplementedError):
        flow._run_target("t", lambda _metrics: None)
    with pytest.raises(NotImplementedError):
        flow._format_config_line("t", _ProbeMetrics(), None, human=True)
    with pytest.raises(NotImplementedError):
        flow._result_text(["t"], {}, {}, {}, None)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        flow._result_detail(["t"], {}, {}, None, {}, None)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        flow._display_lines(["t"], {})


def test_optional_run_sequence_hooks_default_to_no_policy_and_no_setup(tmp_path: Path) -> None:
    flow = _Bare()
    assert flow._run_policy_error() is None
    assert flow._begin_run(["t"]) is None
    assert flow._enter_baseline_worktree([], tmp_path, tmp_path) is None
