"""Parse cocotb's ``results.xml`` into per-test verdicts (ADR 0034 decision 6).

A Cocotb Target has no Simulation Sentinel: the canonical verdict source is the
JUnit-shaped ``results.xml`` cocotb writes at the path Booley pins via
``COCOTB_RESULTS_FILE`` (the run-half sets it explicitly — the location is
never guessed). The simulator exit code is untrustworthy in both directions
(spike S4): a failing test still exits 0, and a crashed interpreter can exit 0
having written no XML at all. Everything that is not a well-formed verdict —
missing file, truncated/unparseable XML, empty testsuite — therefore maps to
**inconclusive, never pass**.

The run-half (:mod:`booley.flows.sim.backends.cocotb`) parses the XML in-sandbox and
re-emits it as a single ``[COCOTB_RESULTS]`` JSON line beside the usual
``[SIM_SUMMARY]``; the Mutation Flow parses that line back, while the
adapter itself publishes the results it already holds and never reads its own
line. Both share this module so the shapes cannot drift.

Empirical XML states this parser is frozen against (spike S4, cocotb 2.0.1 and 2.1.0):

* pass — ``<testcase>`` with no child element;
* fail — ``<failure …/>`` child (cocotb assertion, Python exception, or
  ``SimFailure`` after the simulator died mid-test). The attribute dialect
  varies by cocotb generation — ``error_type``/``error_msg`` on 2.x, ``msg`` on
  its init-failure path, ``message`` on 1.x — so the message is read through
  :func:`_failure_message` rather than one hard-coded attribute (F-36);
* skipped — ``<skipped/>`` child, a ``skip=True`` test on both versions; cocotb
  2.0.1 also lists every test excluded by ``COCOTB_TEST_FILTER`` this way (a
  zero-match filter there gives a file where *every* test is skipped). cocotb
  2.1 writes the fixed message ``Test was skipped`` for every skip; the cause
  and the user's message come from the run log (:func:`attach_skip_causes`):
  ``skipping <module>.<name>: <message>`` for a runtime skip and
  ``skipping <module>.<name> (<n>/<total>)`` for a decorator skip, which a
  filtered 2.1 run never produces because cocotb runs selected decorated tests;
* empty — no ``<testcase>``; cocotb 2.1 omits excluded tests, so a filter
  matching nothing writes ``<testsuites name="cocotb tests" />`` at rc=0;
* absent — a ``tests.toml`` ID not reported by cocotb never appears in the XML
  (including a generating function's base name; reactive validation, decision 7);
* no file — SIGKILL/segfault, ``ModuleNotFoundError`` (also rc=0!), or any
  crash before cocotb's shutdown handler ran.
"""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Final, Literal, cast
from xml.etree import ElementTree

from booley.core.boundary import (
    BoundaryError,
    as_str,
    require_dict,
    require_finite_number,
    require_int,
    require_list,
)
from booley.runtime.sandbox_apply import UNKNOWN_SANDBOX_STEPS

# Verdict vocabulary for a reconciled test — a strict subset of simulate's
# per-test verdict enum: "pass" / "fail" map directly, "inconclusive" marks
# the Test Run inconclusive.
VERDICT_PASS: Final = "pass"
VERDICT_FAIL: Final = "fail"
VERDICT_INCONCLUSIVE: Final = "inconclusive"
ReconciledVerdict = Literal["pass", "fail", "inconclusive"]

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
# [SIM_SUMMARY] for stdout consumers; only format_results_line adds it and only
# parse_results_line strips it.
COCOTB_RESULTS_PREFIX = "[COCOTB_RESULTS] "

# Cap per-test failure text crossing the stdout boundary — one verbose
# exception must not blow the [COCOTB_RESULTS] line (full text survives in
# run.log next to result.json).
_MAX_FAILURE_CHARS = 500
_SKIP_LOG_MAX_LINES = 5_000

RESULTS_PAYLOAD_VERSION: Final = 2
"""Version the encoder writes. Bump it when a reader must reject the new shape;
version 2 only adds skip keys, which a reader of version 1 ignores."""

_SUPPORTED_PAYLOAD_VERSIONS: Final = frozenset({1, RESULTS_PAYLOAD_VERSION})
_SKIP_CAUSES: Final = frozenset({"", "runtime", "decorator"})

# Keep absent-ID details below the record's 1536-character tail and 2048-byte
# canonical JSON detail limits, including escaping when the detail is serialized.
_MAX_ABSENT_DETAIL_CHARS = 1200
_MAX_DISPLAY_NAME_CHARS = 120
_RENDERED_ID_RULE = (
    "tests.toml must list each test by its cocotb test ID: a plain test's "
    "function name, or a parametrized or generated test's rendered ID, "
    'such as "name/param=value".'
)

# cocotb 1.x's own console cry when it cannot import the test module, e.g.
#     0.00ns CRITICAL cocotb.regression  Failed to import module test_eth_mac:
#                                        No module named 'cocotb_test'
# It writes NO results.xml in this case and still exits 0, so every downstream
# signal Booley has says only "file missing" — the true cause exists on the
# console and nowhere else. Scanning for it turns a log-diving exercise into a
# one-line diagnosis (F-6, taxi port). cocotb 2.x prints no such line: see
# _discovery_import_failure.
_IMPORT_FAILURE_RE = re.compile(
    r"Failed to import module\s+(?P<module>[\w.]+)\s*:\s*(?P<error>.+?)\s*$",
    re.MULTILINE,
)

# cocotb 2.x lets the exception out of its test discovery, so the console holds a
# plain traceback and no results.xml (captured: tests/fixtures/cocotb_results/2.1.0/
# import-missing.log):
#     File ".../cocotb/regression.py", line 255, in discover_tests
#       mod = import_module(module_name)
#     ...
#     File "/capture/import_missing_cases.py", line 4, in <module>
#       import qa_no_such_module_xyz
#   ModuleNotFoundError: No module named 'qa_no_such_module_xyz'
# The discovery frame says the traceback is cocotb importing the test module; the
# first `<module>` frame after it is the test module (importlib's frames are never
# `in <module>`); the exception line starts at column 0.
_DISCOVERY_FRAME_RE = re.compile(
    r'^\s*File "[^"]*cocotb/regression\.py", line \d+, in discover_tests$', re.MULTILINE
)
_MODULE_FRAME_RE = re.compile(r'^\s*File "(?P<path>[^"]+)", line \d+, in <module>$', re.MULTILINE)
_IMPORT_ERROR_LINE_RE = re.compile(
    r"^(?P<error>(?:ModuleNotFoundError|ImportError)(?:: .+?)?)\s*$", re.MULTILINE
)

# The specific import error worth a remedy, rather than just an echo: a
# dependency that simply is not in the sandbox image.
_MISSING_MODULE_RE = re.compile(r"No module named ['\"](?P<name>[\w.]+)['\"]")


# Opens the reason of a cocotb import failure; metrics detect it by this prefix.
IMPORT_FAILURE_PREFIX = "cocotb could not import the test module"


def _discovery_import_failure(output: str) -> tuple[str, str] | None:
    """The ``(module, error)`` of a cocotb 2.x discovery import traceback, or ``None``.

    *module* is ``""`` when the test module itself was not found: there is then no
    ``<module>`` frame of it in the traceback.
    """
    anchors = list(_DISCOVERY_FRAME_RE.finditer(output))
    if not anchors:
        return None
    tail_start = anchors[-1].end()
    last = _IMPORT_ERROR_LINE_RE.search(output, tail_start)
    if last is None:
        return None
    frame = _MODULE_FRAME_RE.search(output, tail_start, last.start())
    module = PurePosixPath(frame.group("path")).stem if frame else ""
    return module, last.group("error")


def find_import_failure(output: str) -> str:
    """Explain a cocotb test-module import failure found in *output*, or ``""``.

    Understands cocotb 1.x's ``Failed to import module`` line and cocotb 2.x's
    discovery traceback ending in ``ImportError`` or ``ModuleNotFoundError``.
    Returned as a ready-to-show ``detail`` string: the caller substitutes it for
    the parser's "results.xml not found", which is a true statement about a
    symptom and a useless one about the cause.
    """
    m = _IMPORT_FAILURE_RE.search(output)
    found = (
        (m.group("module"), m.group("error").strip()) if m else _discovery_import_failure(output)
    )
    if found is None:
        return ""
    module, error = found
    named = f" '{module}'" if module else ""
    detail = f"{IMPORT_FAILURE_PREFIX}{named}: {error}"

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
            "then recreate the Sandbox so it runs the new image: "
            f"{UNKNOWN_SANDBOX_STEPS}."
        )
    return detail


@dataclass(frozen=True)
class CocotbTest:
    """One ``<testcase>`` from results.xml."""

    name: str
    """Reported test ID (the XML ``name`` attribute), including generated IDs."""

    module: str
    """The declaring Python module (the XML ``classname`` attribute)."""

    status: str
    """``"pass"`` / ``"fail"`` / ``"skipped"`` / ``"not_run"`` — the raw state, before
    selected-set reconciliation (a *selected* skipped test is inconclusive)."""

    failure_text: str = ""
    """``error_type: error_msg`` from the ``<failure>`` child, "" otherwise."""

    elapsed_s: float = 0.0
    """Wall-clock seconds cocotb reports for the test (the ``time`` attribute)."""

    skip_cause: str = ""
    """``"runtime"`` (the test skipped itself), ``"decorator"``, or ``""`` when the
    run log named no cause. Only a skipped test carries one."""

    skip_message: str = ""
    """The message of a runtime skip (``""`` for a bare skip or a decorator skip)."""


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

    reported_names: tuple[str, ...] = ()
    """Complete reported IDs for absent-name diagnostics after compaction.

    These names carry no verdict evidence; omitted tests remain omitted.
    """


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


# Dotted module path cocotb 2.x prepends to a test name in progress lines.
_MODULE_PREFIX = r"(?:\w+\.)*"


def _bare_progress_name(logged: str) -> str:
    """Strip the leading module path, keeping dots inside the test name itself.

    A parametrized cocotb 2.x test logs ``pkg.mod.test_gain/gain=0.5``. Only
    the identifier segments before the test name are module path, so splitting
    at the last dot would return ``5``.
    """
    return re.sub(rf"^{_MODULE_PREFIX}", "", logged)


def recover_timeout_progress(
    output: str,
    selected: list[str],
    parsed: CocotbResults | None = None,
) -> CocotbResults:
    """Recover completed, active, and not-run tests from cocotb progress lines."""
    # Cocotb 2.x logs <module>.<test>, while XML and selected tests use bare
    # names. Accept the module prefix without widening the test-name match;
    # bare progress names remain valid for cocotb 1.x.
    if not selected:
        xml_names = [test.name for test in parsed.tests] if parsed is not None else []
        running_names = [
            _bare_progress_name(name)
            for name in re.findall(r"cocotb\.regression\s+running\s+(\S+)\s+\(", output)
        ]
        selected = list(dict.fromkeys((*xml_names, *running_names)))
    patterns = {name: rf"{_MODULE_PREFIX}{re.escape(name)}" for name in selected}
    passed = {
        name
        for name in selected
        if re.search(rf"cocotb\.regression\s+{patterns[name]}\s+passed\b", output)
    }
    failed = {
        name
        for name in selected
        if re.search(rf"cocotb\.regression\s+{patterns[name]}\s+failed\b", output)
    }
    started = [
        name
        for name in selected
        if re.search(rf"cocotb\.regression\s+running\s+{patterns[name]}\s+\(", output)
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


def _skip_log_entry(lines: Sequence[str], test: CocotbTest) -> tuple[str, str] | None:
    """The ``(cause, message)`` cocotb logged when it skipped *test*, if any."""
    key = re.escape(f"{test.module}.{test.name}")
    pattern = re.compile(
        rf"^(?P<head>.*?cocotb\.regression\s+)skipping {key}"
        r"(?:(?P<runtime>:)(?: |$)(?P<msg>.*)| \(\d+/\d+\))"
    )
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if match is None:
            continue
        if match.group("runtime") is None:
            return "decorator", ""
        indent = " " * len(match.group("head"))
        parts = [match.group("msg")]
        for follower in lines[index + 1 :]:
            if not follower.startswith(indent):
                break
            parts.append(follower[len(indent) :])
        return "runtime", "\n".join(parts).strip()[:_MAX_FAILURE_CHARS]
    return None


_SKIP_LOG_LINE: Final = re.compile(r"^(?P<head>.*?cocotb\.regression\s+)skipping ")


class SkipLogCollector:
    """Keeps cocotb's ``skipping`` log lines past the run output's tail cap.

    The captured output keeps only its last lines, so a skip logged before a long
    stretch of output would otherwise lose its message.
    """

    def __init__(self) -> None:
        self._lines: deque[str] = deque(maxlen=_SKIP_LOG_MAX_LINES)
        self._indent: str | None = None

    def feed(self, line: str) -> None:
        """Keep *line* when it starts a skip entry or continues the one before it."""
        match = _SKIP_LOG_LINE.match(line)
        if match is not None:
            self._indent = " " * len(match.group("head"))
        elif self._indent is None or not line.startswith(self._indent):
            self._indent = None
            return
        self._lines.append(line)

    def text(self) -> str:
        return "".join(self._lines)


def attach_skip_causes(results: CocotbResults, output: str) -> CocotbResults:
    """Add the skip cause and message the run log names to each skipped test.

    cocotb 2.1 writes a fixed message for every skip, so the cause comes from the
    ``cocotb.regression  skipping <module>.<name>`` log line. A multiline message
    continues on following lines indented to the column where ``skipping`` starts.
    A skipped test with no such line is returned unchanged.
    """
    lines = output.splitlines()
    tests: list[CocotbTest] = []
    for test in results.tests:
        entry = _skip_log_entry(lines, test) if test.status == "skipped" else None
        if entry is None:
            tests.append(test)
        else:
            tests.append(replace(test, skip_cause=entry[0], skip_message=entry[1]))
    return replace(results, tests=tuple(tests))


def _skip_detail(test: CocotbTest) -> str:
    """The inconclusive detail for a selected test cocotb reported as skipped."""
    if test.skip_cause == "decorator":
        return "skipped by its decorator (skip=True)"
    if test.skip_cause == "runtime":
        if test.skip_message:
            return f"skipped at runtime: {test.skip_message}"
        return "skipped at runtime without a reason"
    return "reported skipped by cocotb; the run log names no skip reason"


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


def _reported_names(results: CocotbResults) -> tuple[str, ...]:
    """Deduplicate complete IDs in producer order, before filtering test entries."""
    names = results.reported_names or tuple(test.name for test in results.tests)
    return tuple(dict.fromkeys(names))


def _display_name(name: str) -> str:
    """Escape testbench-supplied control characters and bound one displayed ID."""
    rendered = json.dumps(name, ensure_ascii=True)
    if len(rendered) > _MAX_DISPLAY_NAME_CHARS:
        return rendered[: _MAX_DISPLAY_NAME_CHARS - 4] + '..."'
    return rendered


def _no_test_ran_detail(name: str) -> str:
    """Explain an empty focused run: cocotb 2.1 writes no testcase when its filter matched none."""
    return (
        f"no matching test ID {_display_name(name)} in cocotb results: no cocotb "
        f"test ID matched the selected tests, so cocotb ran none. {_RENDERED_ID_RULE}"
    )


def _absent_test_detail(name: str, results: CocotbResults) -> str:
    """One bounded diagnostic shared by complete XML and compact consumers."""
    prefix = f"no matching test ID {_display_name(name)} in cocotb results. "
    remedy = _RENDERED_ID_RULE
    names = _reported_names(results)
    preview: list[str] = []
    # Reserve room for the omitted count and artifact pointer before adding IDs.
    budget = _MAX_ABSENT_DETAIL_CHARS - len(prefix) - len(remedy) - 180
    for candidate in names:
        rendered = _display_name(candidate)
        if len(", ".join((*preview, rendered))) > budget:
            break
        preview.append(rendered)
    detail = prefix + "cocotb reported test IDs: " + (", ".join(preview) or "(none)")
    if omitted := len(names) - len(preview):
        detail += f"; {omitted} more (full IDs in cocotb_results.json)"
    elif any(
        len(json.dumps(candidate, ensure_ascii=True)) > _MAX_DISPLAY_NAME_CHARS
        for candidate in names
    ):
        detail += "; long IDs abbreviated (full IDs in cocotb_results.json)"
    if results.skipped_unselected and not results.reported_names:
        detail += "; omitted skipped test names were not transported; see cocotb_results.json"
    return detail + ". " + remedy


def reconcile(
    selected: list[str],
    results: CocotbResults,
) -> list[tuple[str, ReconciledVerdict, str]]:
    """Map every *selected* tests.toml name to ``(name, verdict, detail)``.

    The selected-set reconciliation of ADR 0034 decision 7: every name Booley
    asked for must come back as a real pass/fail from the XML —

    * empty file (cocotb 2.1 ran no test the filter matched) → every test
      inconclusive, naming the no-match cause and the rendered-ID rule;
    * other non-OK file state → every test inconclusive, carrying the state detail;
    * name absent from the XML → inconclusive with reported IDs and a
      correction hint (typo'd, undecorated, or generating function base name);
    * present but ``skipped`` → inconclusive with the skip cause and message the
      run log named (:func:`attach_skip_causes`), never a pass;
    * fail → fail with the cocotb failure text;
    * pass → pass.

    Unexpected extra XML entries are **not** verdict-bearing (the caller may
    log them via :func:`extra_tests`).
    """
    out: list[tuple[str, ReconciledVerdict, str]] = []
    for name in selected:
        if results.state == STATE_EMPTY:
            out.append((name, VERDICT_INCONCLUSIVE, _no_test_ran_detail(name)))
            continue
        if results.state != STATE_OK:
            out.append((name, VERDICT_INCONCLUSIVE, results.detail))
            continue
        entry = _status_for_name(results, name)
        if entry is None:
            out.append((name, VERDICT_INCONCLUSIVE, _absent_test_detail(name, results)))
        elif entry.status == VERDICT_FAIL:
            out.append((name, VERDICT_FAIL, entry.failure_text))
        elif entry.status == "skipped":
            out.append((name, VERDICT_INCONCLUSIVE, _skip_detail(entry)))
        elif entry.status == "not_run":
            out.append((name, VERDICT_INCONCLUSIVE, TIMEOUT_NOT_RUN_DETAIL))
        else:
            out.append((name, VERDICT_PASS, ""))
    return out


def extra_tests(selected: list[str], results: CocotbResults) -> list[str]:
    """Non-selected names the XML reports as run (pass/fail) — log-only."""
    wanted = set(selected)
    return [t.name for t in results.tests if t.name not in wanted and t.status != "skipped"]


def compact_results(results: CocotbResults, selected: Sequence[str]) -> CocotbResults:
    """Drop unselected skipped tests from *results*, counting them instead.

    This is what the focused compact transport carries: selected tests and every
    test that ran stay, the rest collapse into ``skipped_unselected`` and, when a
    selected name is absent, ``reported_names`` keeps the candidate IDs.
    """
    wanted = set(selected)
    reported_names = results.reported_names
    if wanted - {test.name for test in results.tests} and (
        reported_names or not results.skipped_unselected
    ):
        reported_names = _reported_names(results)
    return replace(
        results,
        tests=tuple(
            test for test in results.tests if test.name in wanted or test.status != "skipped"
        ),
        skipped_unselected=results.skipped_unselected
        + sum(1 for test in results.tests if test.status == "skipped" and test.name not in wanted),
        reported_names=reported_names,
    )


def _encode_test(test: CocotbTest) -> dict[str, object]:
    encoded: dict[str, object] = {
        "name": test.name,
        "module": test.module,
        "status": test.status,
        "failure": test.failure_text,
        "elapsed_s": test.elapsed_s,
    }
    if test.skip_cause:
        encoded["skip_cause"] = test.skip_cause
        encoded["skip_message"] = test.skip_message
    return encoded


def results_payload(results: CocotbResults) -> dict[str, object]:
    """Encode *results* as the JSON-ready transport payload."""
    payload: dict[str, object] = {
        "version": RESULTS_PAYLOAD_VERSION,
        "state": results.state,
        "detail": results.detail,
        "tests": [_encode_test(t) for t in results.tests],
        "skipped_unselected": results.skipped_unselected,
    }
    if results.reported_names:
        payload["reported_names"] = list(results.reported_names)
    return payload


def format_results_line(payload: Mapping[str, object]) -> str:
    """Print an encoded *payload* as the one-line ``[COCOTB_RESULTS]`` sentinel."""
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
    skip_cause = _transport_string(test, "skip_cause")
    if skip_cause not in _SKIP_CAUSES:
        raise BoundaryError(f"unknown skip_cause: {skip_cause!r}")
    return CocotbTest(
        name=_transport_string(test, "name"),
        module=_transport_string(test, "module"),
        status=status,
        failure_text=_transport_string(test, "failure"),
        elapsed_s=elapsed_s,
        skip_cause=skip_cause,
        skip_message=_transport_string(test, "skip_message"),
    )


def decode_results_payload(raw_payload: object) -> CocotbResults:
    """Validate and decode an encoded results payload; the one payload decoder.

    Raises :class:`BoundaryError` for any payload :func:`results_payload` could
    not have produced (wrong types, unknown state or status, non-finite time, a
    ``version`` other than 1 or 2). A missing ``version`` is 1: a payload from a
    release that did not record skip causes.
    """
    payload = cast(dict[str, object], require_dict(raw_payload, field="COCOTB_RESULTS"))
    version = require_int(payload.get("version", 1), field="version")
    if version not in _SUPPORTED_PAYLOAD_VERSIONS:
        raise BoundaryError(f"unsupported cocotb results payload version: {version}")
    state = _transport_string(payload, "state", default=STATE_UNPARSEABLE)
    if state not in _RESULT_STATES:
        raise BoundaryError(f"unknown results state: {state!r}")
    tests = tuple(_transport_test(test) for test in require_list(payload.get("tests", [])))
    skipped_unselected = require_int(
        payload.get("skipped_unselected", 0), field="skipped_unselected"
    )
    if skipped_unselected < 0:
        raise BoundaryError("skipped_unselected must be non-negative")
    reported_names = require_list(payload.get("reported_names", []), field="reported_names")
    if any(not isinstance(name, str) for name in reported_names):
        raise BoundaryError("reported_names must contain only strings")
    return CocotbResults(
        state=state,
        tests=tests,
        detail=_transport_string(payload, "detail"),
        skipped_unselected=skipped_unselected,
        reported_names=tuple(reported_names),
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
            return decode_results_payload(raw_payload)
        except BoundaryError:
            return None
    return None
