"""Human presentation changes alongside frozen machine/report representations."""

from __future__ import annotations

import ast
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from booley.flows import endpoint_reporting, terminal_progress
from booley.flows.fpga.flow import FpgaImplFlow, FpgaMetrics
from booley.flows.lint.flow import (
    LintConfigResult,
    LintFlow,
    _lint_result_parts,
    _target_summary_line,
)
from booley.flows.sim import flow as sim
from booley.flows.sim.standalone import StandaloneMixin
from booley.flows.synth.flow import AsicSynthesizeFlow, SynthMetrics
from booley.mcp.report_encoding import ReportEncoder
from booley.runtime.endpoint_execution import EndpointOutcome

OLD_NAMES = {
    "_age",
    "_duration",
    "_elapsed",
    "fmt_elapsed",
    "_format_duration",
    "_format_minutes",
    "_trace_age_note",
}


def test_old_formatters_are_removed():
    root = Path(sim.__file__).resolve().parents[2]
    found = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ImportFrom)):
                names = (
                    [node.name]
                    if isinstance(node, ast.FunctionDef)
                    else [a.name for a in node.names]
                )
                found.extend(
                    f"{path.relative_to(root)}:{name}" for name in names if name in OLD_NAMES
                )
    assert not found, f"private time formatters remain: {found}"


def test_human_sinks_use_adaptive_durations():
    result = sim.TargetResult(
        target="top",
        passed=True,
        elapsed_s=3661,
        tests=[sim.TestResult(name="smoke", passed=True)],
    )
    assert sim._build_display_lines([result], 3661) == ["✓ PASS  1h 01m"], (
        "human duration migration missing"
    )
    lint = LintConfigResult("top", duration_s=61)
    assert _target_summary_line(lint).endswith("1m 01s")
    failed = sim.TargetResult(
        target="top",
        passed=False,
        elapsed_s=3661,
        tests=[sim.TestResult(name="smoke", passed=False, elapsed_s=61, build_s=3600)],
    )
    assert "1m 01s  (incl. 1h 00m build)" in "\n".join(sim._build_display_lines([failed], 3661))

    synth = object.__new__(AsicSynthesizeFlow)
    assert "1h 01m" in synth._format_config_line(
        "top", SynthMetrics(elapsed_s=3661), None, human=True
    )
    assert "1m 01s" in FpgaImplFlow._format_config_line(
        "top", FpgaMetrics(elapsed_s=61), None, human=True
    )
    standalone = object.__new__(StandaloneMixin)
    standalone.args = SimpleNamespace(mode=SimpleNamespace(value="elab-only"))
    standalone.set_criterion = lambda *_a, **_k: None
    assert standalone._standalone_verdict(
        "verilator", [("top", "top.sv")], [], [], [], None, 61
    ).display.endswith("1m 01s")


def _protected_outputs(root):
    """Fixed inputs also used to capture literal base output before source edits."""
    synth = object.__new__(AsicSynthesizeFlow)
    fpga = object.__new__(FpgaImplFlow)
    simflow = object.__new__(sim.SimulateFlow)
    simflow._build_context_lines = lambda _target: ["build: fixture"]
    outputs = []
    for passed in (True, False):
        for elapsed in (0.012, 61, 3661):
            test = sim.TestResult(
                name="smoke", passed=passed, elapsed_s=elapsed, build_s=3600, cycles=123
            )
            result = sim.TargetResult(target="top", passed=passed, tests=[test], elapsed_s=elapsed)
            outputs.append(sim._test_status_line(test))
            outputs.append(simflow._headline_lines([result]))
            outputs.append(
                synth._format_config_line(
                    "top", SynthMetrics(elapsed_s=elapsed, returncode=0 if passed else 1), None
                )
            )
            outputs.append(
                fpga._format_config_line(
                    "top", FpgaMetrics(elapsed_s=elapsed, returncode=0 if passed else 1), None
                )
            )
            outputs.append(_lint_result_parts(["top"], [], elapsed))
            outputs.append(LintFlow._build_summary([]))
            outputs.append(sim._test_report_entry(test))
    standalone = object.__new__(StandaloneMixin)
    standalone.args = SimpleNamespace(mode=SimpleNamespace(value="elab-only"))
    standalone.set_criterion = lambda *_a, **_k: None
    outcome = standalone._standalone_verdict(
        "verilator", [("top", "top.sv")], [], [], [], None, 3661
    )
    outputs.append(outcome.lines)
    report = {"report_text": "RESULT: PASS\n  elapsed 3661.0s", "elapsed_s": 3661, "passed": True}
    outputs.append(ReportEncoder.text_result(0, report["report_text"] + "\n", "", report))
    outputs.append(ReportEncoder.structured_report(report))
    outputs.extend(_protected_transport())
    outputs.append(_protected_targets(root))
    return json.dumps(outputs, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _protected_transport():
    outputs = []
    stream = io.StringIO()
    progress = terminal_progress.TerminalProgress(
        "sim", "top", Path("/fixture"), stream=stream, clock=lambda: 1000
    )
    progress.started = 0
    with patch.object(terminal_progress, "utc_now_rfc3339", return_value="2026-10-08T10:00:00Z"):
        progress._record("heartbeat (elapsed time; process health unknown)")
    outputs.append(stream.getvalue())
    assert (
        stream.getvalue()
        == "[booley-progress] 2026-10-08T10:00:00Z sim Target=top stage=preparing elapsed=1000s heartbeat (elapsed time; process health unknown)\n"
    )
    for passed in (True, False):
        report_text = f"RESULT: {'PASS' if passed else 'FAIL'}\n  elapsed 3661.0s"
        stdout = io.StringIO()
        witness = endpoint_reporting._StdoutWitness(stdout)
        endpoint = SimpleNamespace(_console_publication_requested=True, _stdout_witness=witness)
        outcome = EndpointOutcome(exit_code=0 if passed else 1, report_text=report_text)
        with redirect_stdout(witness):
            print(report_text)
            endpoint_reporting._publish_console_report(endpoint, outcome)
        assert stdout.getvalue() == report_text + "\n"
        encoded = ReportEncoder.text_result(
            outcome.exit_code, stdout.getvalue(), "", {"report_text": report_text}
        )
        assert encoded.count(report_text) == 1
        outputs.append(encoded)
    return outputs


def _protected_targets(root):
    from tests.harness.test_targets_cmd import _CORE, _run

    root.mkdir(exist_ok=True)
    (root / "alpha.core").write_text(_CORE)
    (root / ".booley").mkdir(exist_ok=True)
    (root / ".booley" / "booley.toml").write_text("[flows.sim]\n")
    targets = io.StringIO()
    with redirect_stdout(targets):
        assert _run(root, "--json") == 0
    return targets.getvalue()


# Captured from the pinned pre-change tree, with additive lint finding fields.
# Duration strings remain unchanged.
PROTECTED_OUTPUTS = '["  smoke                PASS        123 cycles     12ms  (incl. 3600s build)",["","--- summary (Sandbox) ---","[sim] top: PASS (1/1 tests, 12ms)","  Test    Result   Cycles   Time   Build","  ─────   ──────   ──────   ────   ─────","  smoke   PASS     123      12ms   3600s"],"[synth] top          -- KGe   -- ps    0.0s","[fpga] top             -- LUTs   -- FFs   0.0s FAIL",[["0 warnings"],{"elapsed_s":0.01,"findings":[],"findings_total":0,"findings_truncated":false,"targets":["top"],"total_warnings":0}],[0,"RESULT: PASS"],{"build_s":3600,"cycle_observation":"missing","cycles":123,"elapsed_s":0.012,"error_tail":"","failure_kind":"","name":"smoke","passed":true,"phase_timings_s":{},"resources":{},"sva_errors":0,"termination":"completed","test_validated":true,"timed_out":false,"verdict":"pass"},"  smoke                PASS        123 cycles    61.0s  (incl. 3600s build)",["","--- summary (Sandbox) ---","[sim] top: PASS (1/1 tests, 61.0s)","  Test    Result   Cycles   Time    Build","  ─────   ──────   ──────   ─────   ─────","  smoke   PASS     123      61.0s   3600s"],"[synth] top          -- KGe   -- ps    61.0s","[fpga] top             -- LUTs   -- FFs   61.0s FAIL",[["0 warnings"],{"elapsed_s":61,"findings":[],"findings_total":0,"findings_truncated":false,"targets":["top"],"total_warnings":0}],[0,"RESULT: PASS"],{"build_s":3600,"cycle_observation":"missing","cycles":123,"elapsed_s":61,"error_tail":"","failure_kind":"","name":"smoke","passed":true,"phase_timings_s":{},"resources":{},"sva_errors":0,"termination":"completed","test_validated":true,"timed_out":false,"verdict":"pass"},"  smoke                PASS        123 cycles  3661.0s  (incl. 3600s build)",["","--- summary (Sandbox) ---","[sim] top: PASS (1/1 tests, 3661.0s)","  Test    Result   Cycles   Time      Build","  ─────   ──────   ──────   ───────   ─────","  smoke   PASS     123      3661.0s   3600s"],"[synth] top          -- KGe   -- ps    3661.0s","[fpga] top             -- LUTs   -- FFs   3661.0s FAIL",[["0 warnings"],{"elapsed_s":3661,"findings":[],"findings_total":0,"findings_truncated":false,"targets":["top"],"total_warnings":0}],[0,"RESULT: PASS"],{"build_s":3600,"cycle_observation":"missing","cycles":123,"elapsed_s":3661,"error_tail":"","failure_kind":"","name":"smoke","passed":true,"phase_timings_s":{},"resources":{},"sva_errors":0,"termination":"completed","test_validated":true,"timed_out":false,"verdict":"pass"},"  smoke                FAIL        123 cycles     12ms  (incl. 3600s build)",["","--- summary (Sandbox) ---","[sim] top: FAIL (0/1 tests, 12ms)","  Test    Result   Cycles   Time   Build","  ─────   ──────   ──────   ────   ─────","  smoke   FAIL     123      12ms   3600s","  build: fixture"],"[synth] top          -- KGe   -- ps    0.0s   FAIL (rc=1, no metrics)","[fpga] top             -- LUTs   -- FFs   0.0s FAIL",[["0 warnings"],{"elapsed_s":0.01,"findings":[],"findings_total":0,"findings_truncated":false,"targets":["top"],"total_warnings":0}],[0,"RESULT: PASS"],{"build_s":3600,"cycle_observation":"missing","cycles":123,"elapsed_s":0.012,"error_tail":"","failure_kind":"","name":"smoke","passed":false,"phase_timings_s":{},"resources":{},"sva_errors":0,"termination":"completed","test_validated":true,"timed_out":false,"verdict":"fail"},"  smoke                FAIL        123 cycles    61.0s  (incl. 3600s build)",["","--- summary (Sandbox) ---","[sim] top: FAIL (0/1 tests, 61.0s)","  Test    Result   Cycles   Time    Build","  ─────   ──────   ──────   ─────   ─────","  smoke   FAIL     123      61.0s   3600s","  build: fixture"],"[synth] top          -- KGe   -- ps    61.0s   FAIL (rc=1, no metrics)","[fpga] top             -- LUTs   -- FFs   61.0s FAIL",[["0 warnings"],{"elapsed_s":61,"findings":[],"findings_total":0,"findings_truncated":false,"targets":["top"],"total_warnings":0}],[0,"RESULT: PASS"],{"build_s":3600,"cycle_observation":"missing","cycles":123,"elapsed_s":61,"error_tail":"","failure_kind":"","name":"smoke","passed":false,"phase_timings_s":{},"resources":{},"sva_errors":0,"termination":"completed","test_validated":true,"timed_out":false,"verdict":"fail"},"  smoke                FAIL        123 cycles  3661.0s  (incl. 3600s build)",["","--- summary (Sandbox) ---","[sim] top: FAIL (0/1 tests, 3661.0s)","  Test    Result   Cycles   Time      Build","  ─────   ──────   ──────   ───────   ─────","  smoke   FAIL     123      3661.0s   3600s","  build: fixture"],"[synth] top          -- KGe   -- ps    3661.0s   FAIL (rc=1, no metrics)","[fpga] top             -- LUTs   -- FFs   3661.0s FAIL",[["0 warnings"],{"elapsed_s":3661,"findings":[],"findings_total":0,"findings_truncated":false,"targets":["top"],"total_warnings":0}],[0,"RESULT: PASS"],{"build_s":3600,"cycle_observation":"missing","cycles":123,"elapsed_s":3661,"error_tail":"","failure_kind":"","name":"smoke","passed":false,"phase_timings_s":{},"resources":{},"sva_errors":0,"termination":"completed","test_validated":true,"timed_out":false,"verdict":"fail"},["[sim:elab-only] standalone (1 modules, 0 shared, verilator) PASS   3661.0s"],"EXIT_CODE: 0\\n\\n--- stdout ---\\nRESULT: PASS\\n  elapsed 3661.0s\\n",{"passed":true,"reports":[{"elapsed_s":3661,"passed":true,"report_text":"RESULT: PASS\\n  elapsed 3661.0s"}]},"[booley-progress] 2026-10-08T10:00:00Z sim Target=top stage=preparing elapsed=1000s heartbeat (elapsed time; process health unknown)\\n","EXIT_CODE: 0\\n\\n--- stdout ---\\nRESULT: PASS\\n  elapsed 3661.0s\\n","EXIT_CODE: 1\\n\\n--- stdout ---\\nRESULT: FAIL\\n  elapsed 3661.0s\\n","{\\n  \\"cores\\": [\\n    {\\n      \\"vlnv\\": \\"acme:ip:alpha:1.0\\",\\n      \\"core_file\\": \\"alpha.core\\",\\n      \\"targets\\": [\\n        {\\n          \\"name\\": \\"sim\\",\\n          \\"selector\\": \\"sim\\",\\n          \\"flow\\": \\"sim\\",\\n          \\"eda_tool\\": \\"verilator\\",\\n          \\"cocotb_module\\": null,\\n          \\"toplevel\\": \\"tb_alpha\\",\\n          \\"doctor_flows\\": [\\n            \\"sim\\"\\n          ],\\n          \\"drivable_by\\": [\\n            \\"sim\\"\\n          ]\\n        },\\n        {\\n          \\"name\\": \\"synth\\",\\n          \\"selector\\": \\"synth\\",\\n          \\"flow\\": \\"generic\\",\\n          \\"eda_tool\\": \\"yosys\\",\\n          \\"cocotb_module\\": null,\\n          \\"toplevel\\": \\"alpha\\",\\n          \\"doctor_flows\\": [],\\n          \\"drivable_by\\": [\\n            \\"synth\\"\\n          ]\\n        }\\n      ]\\n    }\\n  ],\\n  \\"warnings\\": []\\n}\\n"]'


def test_human_output_changes_without_machine_or_report_changes(tmp_path):
    result = sim.TargetResult(
        target="top", passed=True, tests=[sim.TestResult(name="smoke", passed=True)]
    )
    assert sim._build_display_lines([result], 61) == ["✓ PASS  1m 01s"], (
        "human output migration missing"
    )
    assert _protected_outputs(tmp_path) == PROTECTED_OUTPUTS


def _completion_fixture(flow, elapsed):
    if flow == "lint":
        return _target_summary_line(LintConfigResult("top", duration_s=elapsed))
    if flow == "synth":
        return object.__new__(AsicSynthesizeFlow)._format_config_line(
            "top", SynthMetrics(elapsed_s=elapsed), None, human=True
        )
    return FpgaImplFlow._format_config_line(
        "top", FpgaMetrics(elapsed_s=elapsed), None, human=True
    )


def test_completion_records_preserve_legacy_stage_bytes(monkeypatch):
    events = []
    monkeypatch.setattr(endpoint_reporting, "_write_display_event", events.append)
    monkeypatch.setattr(terminal_progress, "utc_now_rfc3339", lambda: "2026-10-08T10:00:00Z")
    prefixes = {
        "lint": "[lint] top          [] 0 warnings (target total), ",
        "synth": "[synth] top          -- KGe   -- ps    ",
        "fpga": "[fpga] top             -- LUTs   -- FFs   ",
    }
    for flow, prefix in prefixes.items():
        stream = io.StringIO()
        progress = terminal_progress.TerminalProgress(
            flow, "top", Path("/fixture"), stream=stream, clock=lambda: 1000
        )
        progress.started = 0
        progress.log = io.BytesIO()
        endpoint = SimpleNamespace(name=flow, terminal_progress=progress, _display_identity=None)
        expected_records, expected_log = [], []
        for elapsed in (61.1, 61.9):
            line = _completion_fixture(flow, elapsed)
            endpoint_reporting.emit_completion(endpoint, line)
            legacy = prefix + f"{elapsed:.1f}s" + (" FAIL" if flow == "fpga" else "")
            expected_records.append(
                f"[booley-progress] 2026-10-08T10:00:00Z {flow} Target=top stage={legacy} elapsed=1000s \n"
            )
            progress._consume("stdout", b"", terminal_progress._Lines())
            expected_log.append(f"\n[{flow} Target=top stage={legacy} source=stdout]\n")
            assert "1m 01s" in events[-1]["line"], "completion display/record contract missing"
        assert stream.getvalue() == "".join(expected_records), (
            "completion display/record contract missing"
        )
        assert progress.log.getvalue() == "".join(expected_log).encode(), (
            "completion display/record contract missing"
        )
