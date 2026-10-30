"""End-of-run Simulation summary: aligned table, plain record, human terminal form (#2429)."""

from __future__ import annotations

import io
import re
from contextlib import redirect_stderr, redirect_stdout

import pytest

from booley.flows import endpoint_reporting
from booley.flows.sim import flow as sim
from booley.harness import colors
from booley.mcp.report_encoding import ReportEncoder
from booley.runtime.endpoint_execution import EndpointOutcome

HEADER = re.compile(r"^\s+Test\s+Result\s+Cycles\s+Time\s+Build$", re.MULTILINE)
LONG_NAME = "test_" + "x" * 55


class _Tty(io.StringIO):
    """A stdout that claims to be a terminal, whatever the host is."""

    def isatty(self) -> bool:
        return True


def _flow() -> sim.SimulateFlow:
    flow = object.__new__(sim.SimulateFlow)
    flow._build_context_lines = lambda _target: ["build: fixture"]
    flow._args = None
    flow._stdout_witness = None
    flow._console_publication_requested = True
    flow._automatic_target_notice = ""
    flow.terminal_progress = None
    return flow


def _results() -> list[sim.TargetResult]:
    passing = sim.TargetResult(
        target="lite",
        passed=True,
        elapsed_s=61,
        tests=[
            sim.TestResult(name="smoke", passed=True, elapsed_s=1.0, cycles=4242),
            sim.TestResult(name=LONG_NAME, passed=True, elapsed_s=0.012, cycles=7),
        ],
    )
    failing = sim.TargetResult(
        target="full",
        passed=False,
        elapsed_s=2.0,
        tests=[
            sim.TestResult(
                name="smoke", passed=False, elapsed_s=2.0, build_s=5, sva_errors=2, timed_out=True
            )
        ],
    )
    return [passing, failing]


def _publish(flow, stdout, *, results=None) -> tuple[str, str, str]:
    """Run the real summary + publication sequence; return (stdout, stderr, report_text)."""
    results = results or _results()
    overall = all(r.passed for r in results)
    witness = endpoint_reporting._StdoutWitness(stdout)
    flow._stdout_witness = witness
    stderr = io.StringIO()
    with redirect_stdout(witness), redirect_stderr(stderr):
        text = flow._format_summary(results, ["[sim] lite"], overall)
        endpoint_reporting._publish_console_report(
            flow, EndpointOutcome(exit_code=0 if overall else 1, report_text=text)
        )
    return stdout.getvalue(), stderr.getvalue(), text


@pytest.fixture
def colored(monkeypatch):
    monkeypatch.setattr(colors, "COLORS_ENABLED", True)


def test_tty_summary_is_an_aligned_table_with_capped_names(colored):
    out, _err, _text = _publish(_flow(), _Tty())
    plain = colors.strip_ansi(out)

    assert HEADER.search(plain)
    assert "…" in plain and LONG_NAME not in plain
    capped = next(ln for ln in plain.splitlines() if "…" in ln).split()[0]
    assert len(capped) == 40 and capped.endswith("…")
    # In each Target's table the Result column starts at one visible offset.
    lines = plain.splitlines()
    first = lines.index("--- summary (Sandbox) ---")
    headers = [i for i, ln in enumerate(lines) if i > first and HEADER.match(ln)]
    assert len(headers) == 2
    for start, size in zip(headers, (2, 1), strict=True):
        body = lines[start + 2 : start + 2 + size]
        assert {ln.index(ln.split()[1]) for ln in body} == {lines[start].index("Result")}
    assert "\x1b" in out


def test_tty_summary_keeps_milliseconds_and_colors_verdicts(colored):
    out, _err, _text = _publish(_flow(), _Tty())
    plain = colors.strip_ansi(out)

    # F-39: a sub-second test is never flattened to "0.0s"; minutes read "1m 01s".
    long_row = next(ln for ln in plain.splitlines() if "…" in ln)
    assert "12ms" in long_row and "0.0s" not in plain
    assert "[sim] lite: PASS (2/2 tests, 1m 01s)" in plain
    # PASS green, FAIL red, and the closing RESULT line bold red on a failing run.
    assert colors.green("PASS") in out
    assert colors.red("FAIL") in out
    assert colors.bold_red("RESULT: FAIL (1/2 targets)") in out


@pytest.mark.parametrize("terminal", ["tty", "pipe"])
def test_summary_spells_out_inconclusive_and_hides_zero_sva_errors(colored, terminal):
    flow = _flow()
    inconclusive = sim.TargetResult(
        target="odd",
        passed=False,
        inconclusive=True,
        tests=[sim.TestResult(name="t", passed=False, inconclusive=True)],
    )
    empty = sim.TargetResult(target="none", passed=True)
    results = [*_results(), inconclusive, empty]
    stdout = _Tty() if terminal == "tty" else io.StringIO()
    out, _err, text = _publish(flow, stdout, results=results)

    status = sim._test_status_line(inconclusive.tests[0])
    for blob in (colors.strip_ansi(out), text, status):
        assert not re.search(r"\bINCO\b", blob)
        assert "sva_errors=0" not in blob
        assert "INCONCLUSIVE" in blob
    assert "sva_errors=2" in text
    # A Target without tests renders its header and no table of its own.
    after = text.split("[sim] none:", 1)[1]
    assert "Result" not in after


@pytest.mark.parametrize("terminal", ["tty", "pipe"])
def test_agent_summary_is_plain_and_ends_stdout(colored, terminal):
    stdout = _Tty() if terminal == "tty" else io.StringIO()
    out, _err, text = _publish(_flow(), stdout)

    assert "\x1b" not in text
    assert HEADER.search(text) and LONG_NAME in text
    if terminal == "pipe":
        # FORCE_COLOR case: still the plain record, printed once, as the last bytes.
        assert out == text + "\n"
        return
    assert "\x1b" in out
    assert colors.strip_ansi(out).rstrip().splitlines()[-1] == text.splitlines()[-1]
    assert colors.strip_ansi(out).count("RESULT:") == 1


def test_mcp_tail_keeps_the_sim_summary_once(monkeypatch):
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)
    stdout = io.StringIO()
    witness = endpoint_reporting._StdoutWitness(stdout)
    flow = _flow()
    flow._stdout_witness = witness
    with redirect_stdout(witness):
        for index in range(300):
            print(f"noise {index:03d}")
        text = flow._format_summary(_results(), [], overall_pass=False)
    monkeypatch.setenv("BOOLEY_MCP_MAX_STDOUT_BYTES", str(len(text) + 64))

    encoded = ReportEncoder.text_result(1, stdout.getvalue(), "", {"report_text": text})

    assert "--- summary (Sandbox) ---" in encoded
    assert len(HEADER.findall(encoded)) == 2
    assert encoded.count("RESULT:") == 1
    assert "--- report ---" not in encoded
    assert "noise 000" not in encoded


def test_deferred_summary_falls_back_when_the_report_changed(colored):
    flow = _flow()
    stdout, stderr = _Tty(), io.StringIO()
    witness = endpoint_reporting._StdoutWitness(stdout)
    flow._stdout_witness = witness
    results = _results()
    with redirect_stdout(witness), redirect_stderr(stderr):
        text = flow._format_summary(results, [], overall_pass=False)
        # A completion diagnosis keeps the human text on stdout and itself on stderr.
        endpoint_reporting._publish_console_report(
            flow,
            EndpointOutcome(exit_code=1, report_text=text + "\nCompletion failure (boom)"),
        )
    assert "\x1b" in stdout.getvalue()
    assert stderr.getvalue() == "Completion failure (boom)\n"

    # A report rewritten after the summary was built is printed as-is, to stderr.
    stdout, stderr = _Tty(), io.StringIO()
    flow._stdout_witness = endpoint_reporting._StdoutWitness(stdout)
    with redirect_stdout(flow._stdout_witness), redirect_stderr(stderr):
        endpoint_reporting._publish_console_report(
            flow, EndpointOutcome(exit_code=1, report_text="pre-sim line\n" + text)
        )
    assert "\x1b" not in stderr.getvalue() and stderr.getvalue().startswith("pre-sim line")
    assert stdout.getvalue() == ""
