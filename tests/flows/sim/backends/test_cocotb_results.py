"""Unit tests for the cocotb results.xml parser (booley.flows.sim.backends.cocotb_results, G2).

Table-driven from the spike S4 crash-shape inventory: pass/fail/skip
suites, failure-text extraction,
missing file, truncated XML, empty suite, duplicate test names — plus the
selected-set reconciliation (C2), the payload encoder/decoder and the [COCOTB_RESULTS] line.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from booley.core.boundary import BoundaryError
from booley.flows.sim.backends import cocotb_results as cr
from booley.runtime.sandbox_apply import UNKNOWN_SANDBOX_STEPS


def _transported(results: cr.CocotbResults, selected: list[str] | None = None):
    """Results as the decoder sees them after the encoder (compacted if *selected*)."""
    carried = results if selected is None else cr.compact_results(results, selected)
    return cr.decode_results_payload(cr.results_payload(carried))


@pytest.mark.parametrize("prefix", ["", "counter.", "tests.counter."])
def test_timeout_progress_preserves_pass_active_and_not_run_verdicts(prefix):
    # Cocotb 1.x logs bare names; 2.x logs <module>.<test>.
    output = f"""\
0.00ns INFO cocotb.regression running {prefix}test_a (1/3)
1.00ns INFO cocotb.regression {prefix}test_a passed
1.00ns INFO cocotb.regression running {prefix}test_b (2/3)
"""

    results = cr.recover_timeout_progress(output, ["test_a", "test_b", "test_c"])
    verdicts = cr.reconcile(["test_a", "test_b", "test_c"], results)

    assert verdicts == [
        ("test_a", cr.VERDICT_PASS, ""),
        ("test_b", cr.VERDICT_FAIL, cr.TIMEOUT_ACTIVE_DETAIL),
        ("test_c", cr.VERDICT_INCONCLUSIVE, cr.TIMEOUT_NOT_RUN_DETAIL),
    ]


@pytest.mark.parametrize("with_xml", [False, True])
def test_timeout_progress_discovers_bare_names_without_duplicate_xml_entries(with_xml):
    output = """\
0.00ns INFO cocotb.regression running tests.counter.test_a (1/3)
1.00ns INFO cocotb.regression tests.counter.test_a passed
1.00ns INFO cocotb.regression running tests.counter.test_b (2/3)
"""
    parsed = (
        cr.CocotbResults(
            state=cr.STATE_OK,
            tests=(cr.CocotbTest(name="test_a", module="tests.counter", status="pass"),),
        )
        if with_xml
        else None
    )

    results = cr.recover_timeout_progress(output, [], parsed)

    assert [test.name for test in results.tests] == ["test_a", "test_b"]
    assert cr.reconcile(["test_a", "test_b"], results) == [
        ("test_a", cr.VERDICT_PASS, ""),
        ("test_b", cr.VERDICT_FAIL, cr.TIMEOUT_ACTIVE_DETAIL),
    ]


def test_timeout_progress_preserves_qualified_failure_and_matches_exact_test_names():
    output = """\
0.00ns INFO cocotb.regression running tests.counter.test_failed (1/4)
1.00ns WARNING cocotb.regression tests.counter.test_failed failed
1.00ns INFO cocotb.regression running tests.counter.test_active (2/4)
1.00ns INFO cocotb.regression tests.counter.not_test_later passed
1.00ns INFO cocotb.regression tests.counter.test_later_extra passed
1.00ns INFO cocotb.regression tests.counter.testXcase passed
"""
    selected = ["test_failed", "test_active", "test_later", "test.case"]

    results = cr.recover_timeout_progress(output, selected)

    assert cr.reconcile(selected, results) == [
        ("test_failed", cr.VERDICT_FAIL, "reported failed before the cocotb batch timed out"),
        ("test_active", cr.VERDICT_FAIL, cr.TIMEOUT_ACTIVE_DETAIL),
        ("test_later", cr.VERDICT_INCONCLUSIVE, cr.TIMEOUT_NOT_RUN_DETAIL),
        ("test.case", cr.VERDICT_INCONCLUSIVE, cr.TIMEOUT_NOT_RUN_DETAIL),
    ]


# Console output of real batches killed mid-run; provenance lives beside them.
_TIMEOUT_LOGS = Path(__file__).parents[3] / "fixtures/cocotb_timeout_progress/2.1.0"
_MIXED_TESTS = ["test_done", "test_bad", "test_hang", "test_later"]


@pytest.mark.parametrize("selected", [_MIXED_TESTS, []])
def test_timeout_progress_recovers_captured_producer_batch(selected):
    output = (_TIMEOUT_LOGS / "mixed.log").read_text(encoding="utf-8")

    results = cr.recover_timeout_progress(output, list(selected))

    verdicts = [
        ("test_done", cr.VERDICT_PASS, ""),
        ("test_bad", cr.VERDICT_FAIL, "reported failed before the cocotb batch timed out"),
        ("test_hang", cr.VERDICT_FAIL, cr.TIMEOUT_ACTIVE_DETAIL),
        ("test_later", cr.VERDICT_INCONCLUSIVE, cr.TIMEOUT_NOT_RUN_DETAIL),
    ]
    # An unfiltered run can only discover the tests that started.
    expected = verdicts if selected else verdicts[:3]
    names = [name for name, _, _ in expected]
    assert [test.name for test in results.tests] == names
    assert cr.reconcile(names, results) == expected


@pytest.mark.parametrize("filtered", [True, False])
def test_timeout_progress_keeps_dots_inside_captured_parametrized_names(filtered):
    output = (_TIMEOUT_LOGS / "parametrized.log").read_text(encoding="utf-8")
    names = ["test_gain/gain=0.5", "test_gain/gain=1.5"]

    results = cr.recover_timeout_progress(output, list(names) if filtered else [])

    assert [test.name for test in results.tests] == names
    assert cr.reconcile(names, results) == [
        (names[0], cr.VERDICT_PASS, ""),
        (names[1], cr.VERDICT_FAIL, cr.TIMEOUT_ACTIVE_DETAIL),
    ]


# Captured from the pinned producer; provenance and reproduction live beside it.
_RESULTS_XML = Path(__file__).parents[3] / "fixtures/cocotb_results/2.1.0/results.xml"
_NO_MATCH_XML = Path(__file__).parents[3] / "fixtures/cocotb_results/2.1.0/results-no-match.xml"


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "results.xml"
    p.write_text(text, encoding="utf-8")
    return p


def _parse(tmp_path: Path, text: str) -> cr.CocotbResults:
    return cr.parse_results_xml(_write(tmp_path, text))


_RULE = (
    "tests.toml must list each test by its cocotb test ID: a plain test's function name, "
    'or a parametrized or generated test\'s rendered ID, such as "name/param=value".'
)


class TestParseResultsXml:
    def test_mixed_suite_parses_all_states(self):
        res = cr.parse_results_xml(_RESULTS_XML)
        assert res.state == cr.STATE_OK
        assert len(res.tests) == 3
        by_name = {t.name: t for t in res.tests}
        assert by_name["test_reset"].status == cr.VERDICT_PASS
        assert by_name["test_reset"].module == "capture_cases"
        assert by_name["test_reset"].elapsed_s == 0.0
        assert by_name["test_fail"].status == cr.VERDICT_FAIL
        assert by_name["test_skipped"].status == "skipped"

    @pytest.mark.parametrize(
        ("content", "state"),
        [
            (None, cr.STATE_MISSING),
            # A SIGKILL mid-write leaves a torn file (S4).
            ('<testsuites><testsuite><testcase name="interrupted"', cr.STATE_UNPARSEABLE),
            ("DIRECTORY", cr.STATE_MISSING),
        ],
        ids=["missing-file", "truncated", "not-a-file"],
    )
    def test_unusable_results_file_never_passes(self, tmp_path: Path, content, state):
        if content is None:
            path = tmp_path / "nope.xml"
        elif content == "DIRECTORY":
            path = tmp_path
        else:
            path = _write(tmp_path, content)
        res = cr.parse_results_xml(path)
        assert res.state == state
        assert not res.tests
        if state == cr.STATE_UNPARSEABLE:
            assert "truncated or malformed" in res.detail

    def test_duplicate_names_fail_wins(self, tmp_path: Path):
        res = cr.parse_results_xml(
            _write(
                tmp_path,
                """\
<testsuites><testsuite>
  <testcase name="dup" classname="m" time="0" />
  <testcase name="dup" classname="m" time="0"><failure error_type="X" error_msg="boom"/></testcase>
</testsuite></testsuites>
""",
            )
        )
        assert [v for _, v, _ in cr.reconcile(["dup"], res)] == [cr.VERDICT_FAIL]

    def test_failure_text_capped(self, tmp_path: Path):
        res = cr.parse_results_xml(
            _write(
                tmp_path,
                f"""\
<testsuites><testsuite>
  <testcase name="t" classname="m"><failure error_type="E" error_msg="{"x" * 2000}"/></testcase>
</testsuite></testsuites>
""",
            )
        )
        assert len(res.tests[0].failure_text) <= 500


class TestReconcile:
    def _ok(self) -> cr.CocotbResults:
        return cr.parse_results_xml(_RESULTS_XML)

    def test_selected_producer_cases_reconcile_with_focused_details(self):
        selected = ["test_reset", "test_fail", "test_skipped"]
        verdicts = cr.reconcile(selected, self._ok())
        assert [name for name, _, _ in verdicts] == selected
        assert [verdict for _, verdict, _ in verdicts] == [
            cr.VERDICT_PASS,
            cr.VERDICT_FAIL,
            cr.VERDICT_INCONCLUSIVE,
        ]
        assert verdicts[0][2] == ""
        assert verdicts[1][2] == (
            "AssertionError: deliberate failure: compatibility sample\nassert 1 == 0"
        )
        assert "skipped" in verdicts[2][2]

    def test_absent_expected_name_is_inconclusive_with_actionable_message(self):
        # decision 7: a tests.toml name with no matching @cocotb.test never
        # reaches the XML — reactive validation, actionable wording.
        ((_name, verdict, detail),) = cr.reconcile(["test_typo"], self._ok())
        assert verdict == cr.VERDICT_INCONCLUSIVE
        assert "no matching test ID" in detail
        assert "test_typo" in detail
        assert all(name in detail for name in ("test_reset", "test_fail", "test_skipped"))

    @pytest.mark.parametrize(
        ("xml", "selected", "expected"),
        [
            pytest.param(
                None,
                ["test_param", "test_other"],
                [
                    'no matching test ID "test_param" in cocotb results: no cocotb test ID '
                    "matched the selected tests, so cocotb ran none. " + _RULE,
                    'no matching test ID "test_other" in cocotb results: no cocotb test ID '
                    "matched the selected tests, so cocotb ran none. " + _RULE,
                ],
                id="cocotb-2.1.0",
            ),
            pytest.param(
                # The cocotb 2.0.1 shape: excluded tests are listed as skipped.
                '<testsuites><testsuite name="all">'
                '<testcase name="test_param/width=1" classname="m"><skipped/></testcase>'
                '<testcase name="test_param/width=2" classname="m"><skipped/></testcase>'
                "</testsuite></testsuites>",
                ["test_param"],
                [
                    'no matching test ID "test_param" in cocotb results. cocotb reported '
                    'test IDs: "test_param/width=1", "test_param/width=2". ' + _RULE
                ],
                id="cocotb-2.0.1",
            ),
        ],
    )
    def test_selected_ids_matching_no_test_are_inconclusive_with_the_rendered_id_rule(
        self, tmp_path: Path, xml: str | None, selected: list[str], expected: list[str]
    ):
        res = cr.parse_results_xml(_NO_MATCH_XML) if xml is None else _parse(tmp_path, xml)
        assert res.state == (cr.STATE_EMPTY if xml is None else cr.STATE_OK)
        verdicts = cr.reconcile(selected, res)
        assert [(n, v) for n, v, _ in verdicts] == [(n, cr.VERDICT_INCONCLUSIVE) for n in selected]
        assert [d for _, _, d in verdicts] == expected

    def test_non_ok_state_maps_every_test_inconclusive(self, tmp_path: Path):
        res = cr.parse_results_xml(tmp_path / "gone.xml")
        verdicts = cr.reconcile(["a", "b"], res)
        assert all(v == cr.VERDICT_INCONCLUSIVE for _, v, _ in verdicts)
        assert all("results.xml not found" in d for _, _, d in verdicts)

    def test_extra_xml_entries_are_not_verdict_bearing(self):
        res = self._ok()
        verdicts = cr.reconcile(["test_reset"], res)
        assert len(verdicts) == 1
        # test_fail ran but was not selected → surfaced as an extra, and the
        # skipped entry (not run) is not an extra at all.
        assert cr.extra_tests(["test_reset"], res) == ["test_fail"]


class TestFindImportFailure:
    """F-6: an import failure writes no XML and exits 0, so 'results.xml not
    found' is all the parser can say. The real cause is on the console."""

    # cocotb's actual wording, as seen in the taxi port's run.log.
    _CRITICAL = (
        "     0.00ns CRITICAL cocotb.regression                  "
        "Failed to import module test_taxi_eth_mac_10g: No module named 'cocotb_test'"
    )

    def test_names_the_module_and_the_missing_dependency(self):
        detail = cr.find_import_failure(f"some noise\n{self._CRITICAL}\nmore noise")
        assert "test_taxi_eth_mac_10g" in detail
        assert "cocotb_test" in detail

    def test_missing_dependency_gets_the_remedy(self):
        detail = cr.find_import_failure(self._CRITICAL)
        assert "pip_requirements" in detail
        assert "then recreate the Sandbox so it runs the new image: " in detail
        assert detail.endswith(UNKNOWN_SANDBOX_STEPS + ".")
        assert "session down" not in detail.replace(UNKNOWN_SANDBOX_STEPS, "")

    def test_non_import_error_is_echoed_without_the_pip_remedy(self):
        """A syntax error in the TB is an import failure too — but pip won't fix it."""
        detail = cr.find_import_failure(
            "0.00ns CRITICAL cocotb.regression  Failed to import module test_x: "
            "invalid syntax (test_x.py, line 4)"
        )
        assert "invalid syntax" in detail
        assert "pip_requirements" not in detail

    def test_clean_output_finds_nothing(self):
        assert cr.find_import_failure("0.00ns INFO cocotb  Running on Icarus\n") == ""

    def test_promoted_detail_reaches_every_selected_test(self, tmp_path: Path):
        """The end-to-end point: what reconcile() stamps on each test is the
        cause, not the symptom."""
        from dataclasses import replace

        res = cr.parse_results_xml(tmp_path / "gone.xml")
        res = replace(res, detail=cr.find_import_failure(self._CRITICAL))
        verdicts = cr.reconcile(["t1", "t2"], res)
        assert all(v == cr.VERDICT_INCONCLUSIVE for _, v, _ in verdicts)
        assert all("could not import the test module" in d for _, _, d in verdicts)
        assert not any("results.xml not found" in d for _, _, d in verdicts)


class TestResultsLineRoundTrip:
    @pytest.mark.parametrize("retained", [(), (cr.CocotbTest("plain", "m", "pass"),)])
    def test_absent_detail_is_identical_through_full_and_compact_transport(self, retained):
        res = cr.CocotbResults(
            state=cr.STATE_OK,
            tests=(
                cr.CocotbTest("run_test/ifg=12", "m", "skipped"),
                *retained,
                cr.CocotbTest("run_test/ifg=0", "m", "skipped"),
                cr.CocotbTest("run_test/ifg=12", "other", "skipped"),
            ),
        )
        selected = ["run_test"]
        compact = _transported(res, selected)
        full = _transported(res)
        assert cr.reconcile(selected, compact) == cr.reconcile(selected, full)
        detail = cr.reconcile(selected, compact)[0][2]
        assert detail.count('"run_test/ifg=12"') == 1
        assert '"run_test/ifg=0"' in detail
        assert detail.index("ifg=12") < detail.index("ifg=0")
        assert _transported(compact, selected) == compact

    def test_legacy_compact_transport_discloses_missing_candidate_names(self):
        parsed = cr.decode_results_payload({"state": "ok", "tests": [], "skipped_unselected": 2})
        detail = cr.reconcile(["run_test"], parsed)[0][2]
        assert "names were not transported" in detail
        assert "cocotb_results.json" in detail
        repeated = _transported(parsed, ["run_test"])
        assert cr.reconcile(["run_test"], repeated)[0][2] == detail

    def test_diagnostic_names_cannot_fabricate_a_verdict(self):
        res = cr.CocotbResults(state=cr.STATE_OK, reported_names=("test_reset",))
        assert cr.reconcile(["test_reset"], res)[0][1] == cr.VERDICT_INCONCLUSIVE

    def test_long_single_candidate_is_bounded_with_full_artifact_pointer(self):
        res = cr.CocotbResults(
            state=cr.STATE_OK, tests=(cr.CocotbTest("界" * 4000, "m", "skipped"),)
        )
        detail = cr.reconcile(["a" * 4000], res)[0][2]
        assert len(detail) <= 1200
        assert "long IDs abbreviated" in detail
        assert "cocotb_results.json" in detail

    def test_large_candidate_detail_is_safe_for_simulation_record_evidence(self):
        from booley.flows.sim.record.codec import MAX_DETAIL_BYTES

        res = cr.CocotbResults(
            state=cr.STATE_OK,
            tests=tuple(
                cr.CocotbTest(f"test_{i}/value=界\n\x1b[31m", "m", "skipped") for i in range(200)
            ),
        )
        compact = _transported(res, ["absent"])
        detail = cr.reconcile(["absent"], compact)[0][2]
        assert len(detail) <= 1200
        assert len(json.dumps({"reason": detail}).encode()) < MAX_DETAIL_BYTES
        assert "\n" not in detail and "\x1b" not in detail
        assert "more" in detail and "cocotb_results.json" in detail

    def test_round_trip_preserves_everything(self):
        res = cr.parse_results_xml(_RESULTS_XML)
        payload = cr.results_payload(res)
        assert cr.decode_results_payload(payload) == res
        line = cr.format_results_line(payload)
        assert line.startswith(cr.COCOTB_RESULTS_PREFIX)
        assert cr.parse_results_line(f"noise\n{line}\ntrailing") == res

    @pytest.mark.parametrize("line", ["no sentinel here\n", f"{cr.COCOTB_RESULTS_PREFIX}{{broken"])
    def test_missing_or_malformed_line_returns_none_not_pass(self, line):
        assert cr.parse_results_line(line) is None

    @pytest.mark.parametrize(
        "payload",
        [
            "[]",
            '"payload"',
            '{"tests": {}}',
            '{"state":"ok","tests":[null,{"name":"kept","status":"pass"}]}',
            '{"state":"ok","tests":[{"name":{},"module":"m","status":"pass"}]}',
            '{"state":"ok","tests":[{"name":"b","module":[],"status":"pass"}]}',
            '{"state":"ok","tests":[{"name":"b","module":"m","status":true}]}',
            '{"state":"ok","tests":[{"name":"b","module":"m","status":"pass","failure":7}]}',
            '{"state":"ok","tests":[{"name":"b","module":"m","status":"pass","elapsed_s":true}]}',
            '{"state":"ok","tests":[{"name":"b","module":"m","status":"pass","elapsed_s":-1}]}',
            '{"state":"ok","tests":[{"name":"b","module":"m","status":"pass","elapsed_s":1e999}]}',
            '{"state":7,"tests":[]}',
            '{"state":"unknown","tests":[]}',
            '{"detail":{},"tests":[]}',
            '{"skipped_unselected":true,"tests":[]}',
            '{"skipped_unselected":-1,"tests":[]}',
            '{"state":"ok","tests":[],"reported_names":null}',
            '{"state":"ok","tests":[],"reported_names":"test"}',
            '{"state":"ok","tests":[],"reported_names":{}}',
            '{"state":"ok","tests":[],"reported_names":[1]}',
            '{"state":"ok","tests":[],"reported_names":["test",false]}',
        ],
    )
    def test_invalid_payload_rejects_the_transport(self, payload):
        with pytest.raises(BoundaryError):
            cr.decode_results_payload(json.loads(payload))

    def test_a_rejected_line_payload_is_no_results(self):
        # parse_results_line keeps its fail-safe: a rejected payload is "no results".
        assert cr.parse_results_line(cr.COCOTB_RESULTS_PREFIX + "[]") is None

    def test_last_line_wins(self):
        res = cr.parse_results_xml(_RESULTS_XML)
        older = cr.format_results_line(cr.results_payload(cr.CocotbResults(state=cr.STATE_EMPTY)))
        newer = cr.format_results_line(cr.results_payload(res))
        assert cr.parse_results_line(f"{older}\n{newer}") == res

    def test_compact_line_keeps_selected_and_aggregates_other_skips(self):
        res = cr.CocotbResults(
            state=cr.STATE_OK,
            tests=(
                cr.CocotbTest("selected", "m", "pass"),
                cr.CocotbTest("skip_a", "m", "skipped"),
                cr.CocotbTest("skip_b", "m", "skipped"),
                cr.CocotbTest("unexpected", "m", "fail", "boom"),
            ),
        )
        parsed = _transported(res, ["selected"])
        assert [test.name for test in parsed.tests] == ["selected", "unexpected"]
        assert parsed.skipped_unselected == 2


# ---------------------------------------------------------------------------
# F-36 — the <failure> attribute dialect varies by cocotb generation
# ---------------------------------------------------------------------------


def _xml_with_failure(child: str) -> str:
    return (
        '<testsuites name="results">\n'
        '  <testsuite name="all" package="all">\n'
        '    <testcase name="t" classname="m" time="0.012">\n'
        f"      {child}\n"
        "    </testcase>\n"
        "  </testsuite>\n"
        "</testsuites>\n"
    )


class TestFailureMessageDialects:
    """Reading only ``error_msg`` left the structured field empty (F-36).

    Verified against the shipped writers: cocotb 2.x uses error_type/error_msg
    on a normal failure and ``msg`` on its test-initialization path, while
    cocotb 1.x writes ``message="Test failed with RANDOM_SEED=…"``.
    """

    @pytest.mark.parametrize(
        ("child", "text"),
        [
            (
                '<failure message="Test failed with RANDOM_SEED=1783871502" />',
                "Test failed with RANDOM_SEED=1783871502",
            ),
            ('<failure msg="Test initialization failed" />', "Test initialization failed"),
            (
                '<failure type="AssertionError">assert 1 == 0</failure>',
                "AssertionError: assert 1 == 0",
            ),
            (
                '<failure error_type="AssertionError" error_msg="count should be 0" '
                'message="Test failed with RANDOM_SEED=1" />',
                "AssertionError: count should be 0",
            ),
            # Honest limit: nothing to attribute, and we must not invent any.
            ("<failure />", ""),
        ],
        ids=["cocotb1-message", "cocotb2-init-msg", "junit-type-text", "error-msg-wins", "bare"],
    )
    def test_failure_text_by_dialect(self, tmp_path: Path, child, text):
        res = _parse(tmp_path, _xml_with_failure(child))
        assert res.tests[0].status == cr.VERDICT_FAIL
        assert res.tests[0].failure_text == text
        assert res.tests[0].elapsed_s == 0.012
