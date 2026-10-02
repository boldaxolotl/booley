"""A build whose compiler output exceeds the campaign record ceiling still verifies.

Campaign readers load ``evidence/build-execution.json`` under
``RECORD_MAX_BYTES``. A traced build of a large design can print more than
that, so the writer keeps only a bounded head and tail of each captured text
field. The full compiler output stays in each test's ``run.log``.

The end-to-end cases run the real ``booley flow sim`` campaign path with the
fake Verilator toolchain shared with the build-reuse tests, covering both the
shared-variant build and the private ``legacy-per-test`` build.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from booley.flows.sim.campaign.codec import RECORD_MAX_BYTES, canonical_json_bytes
from booley.flows.sim.campaign.serial_execution import (
    BUILD_EXECUTION_TEXT_HEAD_CHARS,
    BUILD_EXECUTION_TEXT_TAIL_CHARS,
    bounded_build_execution_document,
)
from booley.flows.sim.campaign.store import _validate_build_execution_document
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from tests.flows.sim.test_campaign_phase3 import _build_execution
from tests.flows.sim.test_verilator_build_reuse_campaign import _configure, flow_project

__all__ = ["flow_project"]  # re-exported pytest fixture

_FIELD_BUDGET = BUILD_EXECUTION_TEXT_HEAD_CHARS + BUILD_EXECUTION_TEXT_TAIL_CHARS

# Bigger than the record ceiling on its own, so one unbounded copy already fails.
_NOISE_BYTES = RECORD_MAX_BYTES + 512 * 1024

# Prints a recognizable head, a large middle, and a recognizable tail on both
# streams before handing over to the real fake Verilator. Identity probes
# (``--getenv``) stay silent so only the compile step is noisy.
_NOISY_VERILATOR = f"""#!/bin/sh
if [ "$1" != "--getenv" ]; then
  python3 - <<'EOF'
import sys
for stream in (sys.stdout, sys.stderr):
    stream.write("NOISE-HEAD\\n")
    stream.write(("x" * 63 + "\\n") * ({_NOISE_BYTES} // 64))
    stream.write("NOISE-MIDDLE\\n")
    stream.write(("y" * 63 + "\\n") * ({_NOISE_BYTES} // 64))
    stream.write("NOISE-TAIL\\n")
    stream.flush()
EOF
fi
exec "$(dirname "$0")/verilator.real" "$@"
"""


def _make_verilator_noisy(project: Path) -> None:
    """Wrap the fake Verilator so every compile prints more than the ceiling."""
    verilator = project.parent / "vroot" / "bin" / "verilator"
    verilator.rename(verilator.with_name("verilator.real"))
    verilator.write_text(_NOISY_VERILATOR, encoding="utf-8")
    verilator.chmod(0o755)


def _run_flow_sim(project: Path, reports: Path) -> Path:
    """Run ``booley flow sim --test first``; return the run's report directory."""
    result = SimulateFlow().execute(
        SimRequest(
            target="sim", work_dir=project, report_dir=reports, test=("first",), timeout_ms=60_000
        )
    )
    assert result.exit_code == 0, result.outcome.report_text
    (run,) = (path for path in reports.glob("sim/*") if path.is_dir())
    return run


_posix_only = pytest.mark.skipif(
    os.name == "nt", reason="Verilator build reuse is proven in the POSIX Sandbox"
)


@_posix_only
@pytest.mark.parametrize(
    "access", [(), ('pre_sim_build_access = "legacy-per-test"',)], ids=["shared", "legacy"]
)
def test_flow_sim_with_build_output_over_record_ceiling_passes(
    flow_project: Path, tmp_path: Path, access: tuple[str, ...]
) -> None:
    """The compile record stays readable; the full output survives in run.log."""
    _configure(flow_project, *access)
    _make_verilator_noisy(flow_project)
    run = _run_flow_sim(flow_project, tmp_path / "reports")

    (record_path,) = run.glob("targets/sim/campaign/**/evidence/build-execution.json")
    raw = record_path.read_bytes()
    assert len(raw) <= RECORD_MAX_BYTES // 2
    document = json.loads(raw)
    assert canonical_json_bytes(document) == raw
    assert document["build"]["ran"] is True
    for text in (
        document["process"]["stdout"],
        document["process"]["stderr"],
        document["build"]["output"],
    ):
        assert "NOISE-HEAD" in text and "NOISE-TAIL" in text
        assert "NOISE-MIDDLE" not in text
        assert "bytes omitted from build-execution evidence" in text

    # The unbounded compiler output is still in the test's run.log.
    logs = [path for path in run.rglob("run.log") if "NOISE-MIDDLE" in path.read_text("utf-8")]
    assert logs, "no run.log kept the full compiler output"
    assert all(path.stat().st_size > 2 * _NOISE_BYTES for path in logs)


def _record_with(text: str) -> dict[str, object]:
    document = _build_execution()
    document["process"]["stdout"] = text
    document["process"]["stderr"] = text
    document["build"]["output"] = text
    return document


def test_text_at_the_budget_is_kept_verbatim() -> None:
    text = "a" * _FIELD_BUDGET
    bounded = bounded_build_execution_document(_record_with(text))
    assert bounded == _record_with(text)


def test_text_over_the_budget_keeps_head_and_tail_with_an_exact_marker() -> None:
    head = "H" * BUILD_EXECUTION_TEXT_HEAD_CHARS
    tail = "T" * BUILD_EXECUTION_TEXT_TAIL_CHARS
    middle = "é" * 10  # two UTF-8 bytes each; the marker counts bytes
    bounded = bounded_build_execution_document(_record_with(head + middle + tail))
    for text in (
        bounded["process"]["stdout"],
        bounded["process"]["stderr"],
        bounded["build"]["output"],
    ):
        assert text.startswith(head) and text.endswith(tail)
        assert "[... 20 bytes omitted from build-execution evidence;" in text
        assert "é" not in text


def test_bounding_leaves_the_input_and_other_fields_untouched() -> None:
    original = _record_with("z" * (_FIELD_BUDGET + 1))
    snapshot = json.loads(json.dumps(original))
    bounded = bounded_build_execution_document(original)
    assert original == snapshot
    assert bounded["build"]["cache_decision"] == original["build"]["cache_decision"]
    assert bounded["process"]["returncode"] == original["process"]["returncode"]


def test_worst_case_escaping_stays_under_the_record_ceiling() -> None:
    """Control characters escape to six bytes each in canonical JSON."""
    bounded = bounded_build_execution_document(_record_with("\x01" * (4 * RECORD_MAX_BYTES)))
    raw = canonical_json_bytes(bounded)
    assert len(raw) < RECORD_MAX_BYTES
    _validate_build_execution_document(json.loads(raw))
