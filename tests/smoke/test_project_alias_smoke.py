"""Real image-owned Project alias, with its symlink deliberately unmasked."""

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
from tests.smoke.test_verilator_coverage_collector_smoke import _write_generated_target

from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.runtime.sandbox_layout import canonical_project_alias_path

pytestmark = pytest.mark.skipif(
    os.environ.get("BOOLEY_ALIAS_IMAGE_SMOKE") != "1",
    reason="requires an unmasked Booley image alias and dedicated /work mounts",
)


def _prepare_traced_project(root: Path, data: Path) -> None:
    _write_generated_target(root, post_reset=False)
    (data / "booley.toml").write_text("[flows.sim]\n")
    (data / "tests.toml").write_text('[sim]\ntests=["good", "bad"]\nselect="+test={name}"\n')
    top = root / "tb/counter_tb.sv"
    top.write_text(
        top.read_text()
        .replace(
            "  initial begin ",
            '  string tracefile;\n  initial begin if ($value$plusargs("tracefile=%s", tracefile)) begin $dumpfile(tracefile); $dumpvars; end ',
        )
        .replace(
            '$display("[SIM_RESULT] PASSED");',
            'if ($test$plusargs("test=bad")) $display("[SIM_RESULT] FAILED");\n'
            '    else $display("[SIM_RESULT] PASSED");',
        )
    )


def _assert_trace_reference(root: Path, alias: Path) -> None:
    result = SimulateFlow().execute(
        SimRequest(
            target="sim",
            work_dir=root,
            report_dir=alias / "trace-reports",
            test=("good",),
            trace=True,
            timeout_ms=90_000,
        )
    )
    assert result.exit_code == 0, result.outcome.report_text
    campaign = result.outcome.detail["campaigns"]["sim"]
    manifest = (
        root / ".booley_project/trace-reports/sim/1" / campaign["artifacts"]["manifest"]["path"]
    )
    receipt = next(manifest.parent.glob("work-items/*/result.json"))
    document = json.loads(receipt.read_text())
    trace = next(item for item in document["evidence"] if item["kind"] == "trace")
    attempt = (
        receipt.parent / "attempts" / f"{document['attempt_ordinal']:04}-{document['attempt_id']}"
    )
    path = attempt / trace["path"]
    raw = path.read_bytes()
    assert len(raw) == trace["bytes"] > 0
    assert "sha256:" + hashlib.sha256(raw).hexdigest() == trace["sha256"]
    assert path.is_relative_to(root / ".booley_project/trace-reports")


def test_image_project_alias_good_bad_coverage_and_user_links() -> None:
    root = Path("/work")
    alias = Path("/booley-project")
    data = root / ".booley_project"
    assert shutil.which("verilator") and shutil.which("verilator_coverage")
    assert alias.is_symlink() and alias.readlink() == data
    assert canonical_project_alias_path(alias / "reports") == data / "reports"
    _prepare_traced_project(root, data)
    for name, expected in (("good", 0), ("bad", 1)):
        result = SimulateFlow().execute(
            SimRequest(
                target="sim",
                work_dir=root,
                report_dir=alias / "reports",
                test=(name,),
                coverage=True,
                trace=True,
                timeout_ms=90_000,
            )
        )
        assert result.exit_code == expected, result.outcome.report_text
        campaign = result.outcome.detail["campaigns"]["sim"]
        assert campaign["artifacts"]["manifest"]["path"]
        assert campaign["artifacts"]["simulation"]["path"]
        assert campaign["artifacts"]["coverage"]["path"]
    reports = data / "reports"
    assert len(list(reports.glob("sim/*/targets/sim/campaign/manifest.json"))) == 2
    assert len(list(reports.glob("sim/.invocation-*.lock"))) == 2
    assert list(reports.rglob("coverage-points.jsonl.gz"))
    assert list(reports.rglob("*.fst")) or list(reports.rglob("*.vcd"))
    assert all(
        json.loads(p.read_text())["complete"]
        for p in reports.glob("sim/*/targets/sim/simulation.json")
    )
    _assert_trace_reference(root, alias)
    (data / "linked").symlink_to(data, target_is_directory=True)
    refused = SimulateFlow().execute(
        SimRequest(
            target="sim",
            work_dir=root,
            report_dir=alias / "linked/reports",
            test=("good",),
            timeout_ms=30_000,
        )
    )
    assert refused.exit_code == 2
    assert "Invocation lock paths must not contain symlinks" in refused.outcome.report_text
