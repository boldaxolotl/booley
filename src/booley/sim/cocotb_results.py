"""Parse cocotb's ``results.xml`` into per-test verdicts (ADR 0034 decision 6).

A Cocotb Target has no Simulation Sentinel: the canonical verdict source is the
JUnit-shaped ``results.xml`` cocotb writes at the path Booley pins via
``COCOTB_RESULTS_FILE`` (the run-half sets it explicitly — the location is
never guessed). The simulator exit code is untrustworthy in both directions
(spike S4): a failing test still exits 0, and a crashed interpreter can exit 0
having written no XML at all. Everything that is not a well-formed verdict —
missing file, truncated/unparseable XML, empty testsuite — therefore maps to
**inconclusive, never pass**.

The run-half (:mod:`booley.sim.cocotb_run`) parses the XML in-sandbox and
re-emits it as a single ``[COCOTB_RESULTS]`` JSON line beside the usual
``[SIM_SUMMARY]``; ``simulate`` parses that line back and fans the per-test
verdicts into its existing report shapes (``_test_report_entry``). Both halves
share this module so the shapes cannot drift.

Empirical XML states this parser is frozen against (spike S4, cocotb 2.0.1):

* pass — ``<testcase>`` with no child element;
* fail — ``<failure …/>`` child (cocotb assertion, Python exception, or
  ``SimFailure`` after the simulator died mid-test). The attribute dialect
  varies by cocotb generation — ``error_type``/``error_msg`` on 2.x, ``msg`` on
  its init-failure path, ``message`` on 1.x — so the message is read through
  :func:`_failure_message` rather than one hard-coded attribute (F-36);
* skipped — ``<skipped/>`` child (test excluded by ``COCOTB_TEST_FILTER``; a
  zero-match filter yields a file where *every* test is skipped, at rc=0);
* absent — a ``tests.toml`` name with no matching ``@cocotb.test`` function
  never appears in the XML (reactive validation, decision 7);
* no file — SIGKILL/segfault, ``ModuleNotFoundError`` (also rc=0!), or any
  crash before cocotb's shutdown handler ran.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from xml.etree import ElementTree

from booley.core.boundary import (
    BoundaryError,
    as_str,
    require_dict,
    require_finite_number,
    require_int,
    require_list,
)

# Verdict vocabulary for a reconciled test — a strict subset of simulate's
# per-test verdict enum (``_test_verdict``): "pass" / "fail" map directly,
# "inconclusive" sets the TestResult inconclusive flag.
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_INCONCLUSIVE = "inconclusive"

TIMEOUT_ACTIVE_DETAIL = "timed out while this test was running"
TIMEOUT_NOT_RUN_DETAIL = "did not run before the cocotb batch timed out"

# XML-file states. Everything except OK is inconclusive-bearing.
STATE_OK = "ok"
STATE_MISSING = "missing"
STATE_UNPARSEABLE = "unparseable"
STATE_EMPTY = "empty"
_RESULT_STATES = frozenset({STATE_OK, STATE_MISSING, STATE_UNPARSEABLE, STATE_EMPTY})
_TEST_STATUSES = frozenset({VERDICT_PASS, VERDICT_FAIL, "skipped", "not_run"})

# Structured per-test results line the cocotb run-half prints next to
# [SIM_SUMMARY]; simulate scrapes it to fan out per-test report entries.
COCOTB_RESULTS_PREFIX = "[COCOTB_RESULTS] "

# Cap per-test failure text crossing the stdout boundary — one verbose
# exception must not blow the [COCOTB_RESULTS] line (full text survives in
# run.log next to result.json).
_MAX_FAILURE_CHARS = 500

# The actionable message for a tests.toml name that never reached the XML
# (decision 7 — reactive validation, no proactive decorator scanning).
ABSENT_TEST_MESSAGE = (
    "no matching @cocotb.test function in the module — check the name in "
    "tests.toml against the cocotb module's test functions"
)

# cocotb's own console cry when it cannot import the test module, e.g.
#     0.00ns CRITICAL cocotb.regression  Failed to import module test_eth_mac:
#                                        No module named 'cocotb_test'
# It writes NO results.xml in this case and still exits 0, so every downstream
# signal Booley has says only "file missing" — the true cause exists on the
# console and nowhere else. Scanning for it turns a log-diving exercise into a
# one-line diagnosis (F-6, taxi port).
_IMPORT_FAILURE_RE = re.compile(
    r"Failed to import module\s+(?P<module>[\w.]+)\s*:\s*(?P<error>.+?)\s*$",
    re.MULTILINE,
)

# The specific import error worth a remedy, rather than just an echo: a
# dependency that simply is not in the sandbox image.
_MISSING_MODULE_RE = re.compile(r"No module named ['\"](?P<name>[\w.]+)['\"]")


def find_import_failure(output: str) -> str:
    """Explain a cocotb test-module import failure found in *output*, or ``""``.

    Returned as a ready-to-show ``detail`` string: the caller substitutes it for
    the parser's "results.xml not found", which is a true statement about a
    symptom and a useless one about the cause.
    """
    m = _IMPORT_FAILURE_RE.search(output)
    if not m:
        return ""
    module = m.group("module")
    error = m.group("error").strip()
    detail = f"cocotb could not import the test module '{module}': {error}"

    missing = _MISSING_MODULE_RE.search(error)
    if missing:
        # The trap this hint exists for: testbenches import `cocotb_test` and
        # `pytest` at module scope for a pytest entry point Booley never calls,
        # so they look like droppable test-runner deps — but they run at import
        # time, and dropping them breaks the import outright.
        detail += (
            f" — '{missing.group('name')}' is not installed in the sandbox image. "
            "Bake the project's FULL pinned test stack (not just the imports the "
            "testbench appears to use at run time) via [sandbox].pip_requirements, "
            "re-run `booley init`, then `booley session down && booley session up` "
            "so the live session picks up the new image."
        )
    return detail


@dataclass(frozen=True)
class CocotbTest:
    """One ``<testcase>`` from results.xml."""

    name: str
    """Bare test-function name (the XML ``name`` attribute)."""

    module: str
    """The declaring Python module (the XML ``classname`` attribute)."""

    status: str
    """``"pass"`` / ``"fail"`` / ``"skipped"`` / ``"not_run"`` — the raw state, before
    selected-set reconciliation (a *selected* skipped test is inconclusive)."""

    failure_text: str = ""
    """``error_type: error_msg`` from the ``<failure>`` child, "" otherwise."""

    elapsed_s: float = 0.0
    """Wall-clock seconds cocotb reports for the test (the ``time`` attribute)."""


@dataclass(frozen=True)
class CocotbResults:
    """A parsed results.xml: its file state plus every testcase found."""

    state: str
    """:data:`STATE_OK` / :data:`STATE_MISSING` / :data:`STATE_UNPARSEABLE` /
    :data:`STATE_EMPTY`. Anything but OK carries no trustworthy verdicts."""

    tests: tuple[CocotbTest, ...] = ()

    detail: str = ""
    """Human-readable note for a non-OK state (e.g. the parse error)."""

    skipped_unselected: int = 0
    """Skipped XML entries omitted from the compact stdout transport."""


def parse_results_xml(path: Path | str) -> CocotbResults:
    """Parse cocotb's results.xml; never raises, never fabricates a pass.

    Missing file, truncated/unparseable XML and an empty testsuite each return
    their explicit non-OK state — the caller maps those to inconclusive
    (decision 6: the exit code alone decides nothing).
    """
    path = Path(path)
    if not path.is_file():
        return CocotbResults(
            state=STATE_MISSING,
            detail=f"results.xml not found at {path}",
        )
    try:
        root = ElementTree.parse(path).getroot()
    except ElementTree.ParseError as exc:
        return CocotbResults(
            state=STATE_UNPARSEABLE,
            detail=f"results.xml is truncated or malformed: {exc}",
        )
    tests = tuple(_parse_testcase(el) for el in root.iter("testcase"))
    if not tests:
        return CocotbResults(
            state=STATE_EMPTY,
            detail="results.xml contains no testcase entries",
        )
    return CocotbResults(state=STATE_OK, tests=tests)


def recover_timeout_progress(
    output: str,
    selected: list[str],
    parsed: CocotbResults | None = None,
) -> CocotbResults:
    """Recover completed, active, and not-run tests from cocotb progress lines."""
    passed = {
        name
        for name in selected
        if re.search(rf"cocotb\.regression\s+{re.escape(name)}\s+passed\b", output)
    }
    failed = {
        name
        for name in selected
        if re.search(rf"cocotb\.regression\s+{re.escape(name)}\s+failed\b", output)
    }
    started = [
        name
        for name in selected
        if re.search(rf"cocotb\.regression\s+running\s+{re.escape(name)}\s+\(", output)
    ]
    active = next((name for name in reversed(started) if name not in passed | failed), None)
    parsed_by_name = {
        name: entry
        for name in selected
        if parsed is not None and (entry := _status_for_name(parsed, name)) is not None
    }
    if active is None:
        active = next(
            (
                name
                for name in reversed(selected)
                if (entry := parsed_by_name.get(name)) is not None and entry.status == VERDICT_FAIL
            ),
            None,
        )
    tests: list[CocotbTest] = []
    for name in selected:
        parsed_entry = parsed_by_name.get(name)
        if name in passed or (parsed_entry is not None and parsed_entry.status == VERDICT_PASS):
            tests.append(parsed_entry or CocotbTest(name=name, module="", status=VERDICT_PASS))
        elif name == active:
            tests.append(
                CocotbTest(
                    name=name,
                    module="",
                    status=VERDICT_FAIL,
                    failure_text=TIMEOUT_ACTIVE_DETAIL,
                )
            )
        elif name in failed:
            tests.append(
                CocotbTest(
                    name=name,
                    module="",
                    status=VERDICT_FAIL,
                    failure_text="reported failed before the cocotb batch timed out",
                )
            )
        elif parsed_entry is not None and parsed_entry.status == VERDICT_FAIL:
            tests.append(parsed_entry)
        else:
            tests.append(CocotbTest(name=name, module="", status="not_run"))
    return CocotbResults(state=STATE_OK, tests=tuple(tests))


# Attribute names cocotb has used on ``<failure>``, most informative first.
# Reading only ``error_msg`` left the structured ``failure`` field empty for
# every generation but 2.x's normal test-failure path (F-36) — verified
# against the shipped sources:
#   * cocotb 2.x, test failed        → error_type + error_msg (the exception)
#   * cocotb 2.x, test init failed   → msg="Test initialization failed"
#   * cocotb 1.x, any failure        → message="Test failed with RANDOM_SEED=…"
# Note the honest limit: cocotb 1.x never writes the assertion text into
# results.xml at all, so on a 1.x-pinned stack the attribution is the seed
# line and the exception itself remains only in the raw log tail.
_FAILURE_MESSAGE_ATTRS = ("error_msg", "message", "msg")


def _failure_message(failure: ElementTree.Element) -> str:
    """The most informative message text on a ``<failure>`` element, or ""."""
    for attr in _FAILURE_MESSAGE_ATTRS:
        value = (failure.get(attr) or "").strip()
        if value:
            return value
    return (failure.text or "").strip()


def _parse_testcase(el: ElementTree.Element) -> CocotbTest:
    name = el.get("name", "")
    module = el.get("classname", "")
    try:
        elapsed_s = float(el.get("time", 0) or 0)
    except (TypeError, ValueError):
        elapsed_s = 0.0
    failure = el.find("failure")
    if failure is not None:
        error_type = failure.get("error_type") or failure.get("type") or ""
        error_msg = _failure_message(failure)
        text = f"{error_type}: {error_msg}" if error_type else error_msg
        return CocotbTest(
            name=name,
            module=module,
            status=VERDICT_FAIL,
            failure_text=text[:_MAX_FAILURE_CHARS],
            elapsed_s=elapsed_s,
        )
    if el.find("skipped") is not None:
        return CocotbTest(name=name, module=module, status="skipped")
    return CocotbTest(
        name=name,
        module=module,
        status=VERDICT_PASS,
        elapsed_s=elapsed_s,
    )


def _status_for_name(results: CocotbResults, name: str) -> CocotbTest | None:
    """The verdict-bearing entry for *name* (fail > pass > skipped).

    Duplicate test names in one XML (two modules, or a rerun-appended file)
    resolve fail-safe: any failing entry wins, then any passing one, then the
    skip. Absent → ``None``.
    """
    entries = [t for t in results.tests if t.name == name]
    if not entries:
        return None
    for status in (VERDICT_FAIL, VERDICT_PASS):
        for t in entries:
            if t.status == status:
                return t
    return entries[0]


def reconcile(
    selected: list[str],
    results: CocotbResults,
) -> list[tuple[str, str, str]]:
    """Map every *selected* tests.toml name to ``(name, verdict, detail)``.

    The selected-set reconciliation of ADR 0034 decision 7: every name Booley
    asked for must come back as a real pass/fail from the XML —

    * non-OK file state → every test inconclusive, carrying the state detail;
    * name absent from the XML → inconclusive with the actionable
      :data:`ABSENT_TEST_MESSAGE` (typo'd / undecorated test function);
    * present but ``skipped`` → inconclusive (the filter Booley built did not
      actually select it — a filter/name mismatch, never a pass);
    * fail → fail with the cocotb failure text;
    * pass → pass.

    Unexpected extra XML entries are **not** verdict-bearing (the caller may
    log them via :func:`extra_tests`).
    """
    out: list[tuple[str, str, str]] = []
    for name in selected:
        if results.state != STATE_OK:
            out.append((name, VERDICT_INCONCLUSIVE, results.detail))
            continue
        entry = _status_for_name(results, name)
        if entry is None:
            out.append((name, VERDICT_INCONCLUSIVE, ABSENT_TEST_MESSAGE))
        elif entry.status == VERDICT_FAIL:
            out.append((name, VERDICT_FAIL, entry.failure_text))
        elif entry.status == "skipped":
            out.append(
                (
                    name,
                    VERDICT_INCONCLUSIVE,
                    "selected but reported skipped by cocotb — the test filter "
                    "did not match it (name/filter mismatch)",
                )
            )
        elif entry.status == "not_run":
            out.append((name, VERDICT_INCONCLUSIVE, TIMEOUT_NOT_RUN_DETAIL))
        else:
            out.append((name, VERDICT_PASS, ""))
    return out


def extra_tests(selected: list[str], results: CocotbResults) -> list[str]:
    """Non-selected names the XML reports as run (pass/fail) — log-only."""
    wanted = set(selected)
    return [t.name for t in results.tests if t.name not in wanted and t.status != "skipped"]


def results_payload(
    results: CocotbResults,
    *,
    selected: list[str] | None = None,
    verbosity: str = "full",
) -> dict[str, object]:
    """Serialize results for transport while retaining selected-test evidence."""
    if verbosity not in {"compact", "full"}:
        raise ValueError(f"unsupported Cocotb result verbosity: {verbosity}")
    tests = results.tests
    skipped_unselected = results.skipped_unselected
    if verbosity == "compact" and selected is not None:
        wanted = set(selected)
        skipped_unselected = sum(
            1 for test in tests if test.status == "skipped" and test.name not in wanted
        )
        tests = tuple(test for test in tests if test.name in wanted or test.status != "skipped")
    payload: dict[str, object] = {
        "state": results.state,
        "detail": results.detail,
        "tests": [
            {
                "name": t.name,
                "module": t.module,
                "status": t.status,
                "failure": t.failure_text,
                "elapsed_s": t.elapsed_s,
            }
            for t in tests
        ],
        "skipped_unselected": skipped_unselected,
    }
    return payload


def format_results_line(
    results: CocotbResults,
    *,
    selected: list[str] | None = None,
    verbosity: str = "full",
) -> str:
    """Serialize *results* as the one-line ``[COCOTB_RESULTS]`` sentinel."""
    payload = results_payload(results, selected=selected, verbosity=verbosity)
    return COCOTB_RESULTS_PREFIX + json.dumps(payload, separators=(",", ":"))


def _transport_string(payload: dict[str, object], key: str, *, default: str = "") -> str:
    """Read one string field without inventing text from another JSON type."""
    value = as_str(payload.get(key, default))
    if value is None:
        raise BoundaryError(f"{key} must be a string")
    return value


def _transport_test(raw_test: object) -> CocotbTest:
    """Validate one test transported across the stdout JSON boundary."""
    test = cast(dict[str, object], require_dict(raw_test, field="test"))
    status = _transport_string(test, "status")
    if status not in _TEST_STATUSES:
        raise BoundaryError(f"unknown test status: {status!r}")
    elapsed_s = require_finite_number(test.get("elapsed_s", 0), field="elapsed_s")
    if elapsed_s < 0:
        raise BoundaryError(f"elapsed_s must be non-negative, got {elapsed_s!r}")
    return CocotbTest(
        name=_transport_string(test, "name"),
        module=_transport_string(test, "module"),
        status=status,
        failure_text=_transport_string(test, "failure"),
        elapsed_s=elapsed_s,
    )


def _transport_results(raw_payload: object) -> CocotbResults:
    """Validate the complete results sentinel payload."""
    payload = cast(dict[str, object], require_dict(raw_payload, field="COCOTB_RESULTS"))
    state = _transport_string(payload, "state", default=STATE_UNPARSEABLE)
    if state not in _RESULT_STATES:
        raise BoundaryError(f"unknown results state: {state!r}")
    tests = tuple(_transport_test(test) for test in require_list(payload.get("tests", [])))
    skipped_unselected = require_int(
        payload.get("skipped_unselected", 0), field="skipped_unselected"
    )
    if skipped_unselected < 0:
        raise BoundaryError("skipped_unselected must be non-negative")
    return CocotbResults(
        state=state,
        tests=tests,
        detail=_transport_string(payload, "detail"),
        skipped_unselected=skipped_unselected,
    )


def parse_results_line(output: str) -> CocotbResults | None:
    """Extract the last ``[COCOTB_RESULTS]`` line from captured output.

    Returns ``None`` when the line is missing (run died before the run-half's
    post-processing — the caller treats that like a missing XML) or malformed
    (same fail-safe: no fabricated verdicts).
    """
    for line in reversed(output.splitlines()):
        if not line.startswith(COCOTB_RESULTS_PREFIX):
            continue
        try:
            raw_payload: object = json.loads(line[len(COCOTB_RESULTS_PREFIX) :])
        except json.JSONDecodeError:
            return None
        try:
            return _transport_results(raw_payload)
        except BoundaryError:
            return None
    return None
