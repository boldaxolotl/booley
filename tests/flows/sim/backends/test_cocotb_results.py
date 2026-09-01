"""Unit tests for the cocotb results.xml parser (booley.flows.sim.backends.cocotb_results, G2).

Table-driven from the spike S4 crash-shape inventory: pass/fail/skip
suites, failure-text extraction,
missing file, truncated XML, empty suite, duplicate test names — plus the
selected-set reconciliation (C2) and the [COCOTB_RESULTS] line round-trip.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.flows.sim.backends import cocotb_results as cr


def test_timeout_progress_preserves_pass_active_and_not_run_verdicts():
    output = """\
0.00ns INFO cocotb.regression running test_a (1/3)
1.00ns INFO cocotb.regression test_a passed
1.00ns INFO cocotb.regression running test_b (2/3)
"""

    results = cr.recover_timeout_progress(output, ["test_a", "test_b", "test_c"])
    verdicts = cr.reconcile(["test_a", "test_b", "test_c"], results)

    assert verdicts == [
        ("test_a", cr.VERDICT_PASS, ""),
        ("test_b", cr.VERDICT_FAIL, cr.TIMEOUT_ACTIVE_DETAIL),
        ("test_c", cr.VERDICT_INCONCLUSIVE, cr.TIMEOUT_NOT_RUN_DETAIL),
    ]


# A results.xml shaped exactly like cocotb 2.0.1's writer (spike S4).
_XML_MIXED = """\
<testsuites name="results">
  <testsuite name="all" package="all">
    <property name="random_seed" value="1783871502" />
    <testcase name="test_reset" classname="test_counter" file="/b/test_counter.py" lineno="19" time="0.0005" sim_time_ns="30.0" ratio_time="5.0" />
    <testcase name="test_fail" classname="test_counter" file="/b/test_counter.py" lineno="36" time="0.0013" sim_time_ns="30.0" ratio_time="2.0">
      <failure error_type="AssertionError" error_msg="deliberate failure: count should be 0" />
    </testcase>
    <testcase name="test_skipped" classname="test_counter" file="/b/test_counter.py" lineno="43" time="0" sim_time_ns="0" ratio_time="0">
      <skipped />
    </testcase>
  </testsuite>
</testsuites>
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "results.xml"
    p.write_text(text, encoding="utf-8")
    return p


class TestParseResultsXml:
    def test_mixed_suite_parses_all_states(self, tmp_path: Path):
        res = cr.parse_results_xml(_write(tmp_path, _XML_MIXED))
        assert res.state == cr.STATE_OK
        by_name = {t.name: t for t in res.tests}
        assert by_name["test_reset"].status == cr.VERDICT_PASS
        assert by_name["test_reset"].module == "test_counter"
        assert by_name["test_reset"].elapsed_s == 0.0005
        assert by_name["test_fail"].status == cr.VERDICT_FAIL
        assert by_name["test_skipped"].status == "skipped"

    def test_failure_text_carries_type_and_message(self, tmp_path: Path):
        res = cr.parse_results_xml(_write(tmp_path, _XML_MIXED))
        fail = next(t for t in res.tests if t.name == "test_fail")
        assert fail.failure_text == ("AssertionError: deliberate failure: count should be 0")

    def test_missing_file_is_missing_never_pass(self, tmp_path: Path):
        res = cr.parse_results_xml(tmp_path / "nope.xml")
        assert res.state == cr.STATE_MISSING
        assert not res.tests

    def test_truncated_xml_is_unparseable(self, tmp_path: Path):
        # A SIGKILL mid-write leaves a torn file (S4).
        res = cr.parse_results_xml(_write(tmp_path, _XML_MIXED[: len(_XML_MIXED) // 2]))
        assert res.state == cr.STATE_UNPARSEABLE
        assert "truncated or malformed" in res.detail

    def test_empty_testsuite_is_empty(self, tmp_path: Path):
        res = cr.parse_results_xml(
            _write(
                tmp_path,
                '<testsuites name="results"><testsuite name="all" package="all">'
                "</testsuite></testsuites>",
            )
        )
        assert res.state == cr.STATE_EMPTY

    def test_not_a_file_is_missing(self, tmp_path: Path):
        assert cr.parse_results_xml(tmp_path).state == cr.STATE_MISSING

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
    def _ok(self, tmp_path: Path) -> cr.CocotbResults:
        return cr.parse_results_xml(_write(tmp_path, _XML_MIXED))

    def test_pass_fail_map_directly(self, tmp_path: Path):
        verdicts = {
            n: v for n, v, _ in cr.reconcile(["test_reset", "test_fail"], self._ok(tmp_path))
        }
        assert verdicts == {
            "test_reset": cr.VERDICT_PASS,
            "test_fail": cr.VERDICT_FAIL,
        }

    def test_absent_expected_name_is_inconclusive_with_actionable_message(
        self,
        tmp_path: Path,
    ):
        # decision 7: a tests.toml name with no matching @cocotb.test never
        # reaches the XML — reactive validation, actionable wording.
        ((_name, verdict, detail),) = cr.reconcile(["test_typo"], self._ok(tmp_path))
        assert verdict == cr.VERDICT_INCONCLUSIVE
        assert "no matching @cocotb.test" in detail

    def test_selected_but_skipped_is_inconclusive(self, tmp_path: Path):
        # The zero-match-filter shape (S4): file written, tests skipped, rc=0.
        ((_, verdict, detail),) = cr.reconcile(["test_skipped"], self._ok(tmp_path))
        assert verdict == cr.VERDICT_INCONCLUSIVE
        assert "skipped" in detail

    def test_non_ok_state_maps_every_test_inconclusive(self, tmp_path: Path):
        res = cr.parse_results_xml(tmp_path / "gone.xml")
        verdicts = cr.reconcile(["a", "b"], res)
        assert all(v == cr.VERDICT_INCONCLUSIVE for _, v, _ in verdicts)
        assert all("results.xml not found" in d for _, _, d in verdicts)

    def test_extra_xml_entries_are_not_verdict_bearing(self, tmp_path: Path):
        res = self._ok(tmp_path)
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
        assert "session down" in detail  # the live session is on the stale image

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
    def test_round_trip_preserves_everything(self, tmp_path: Path):
        res = cr.parse_results_xml(_write(tmp_path, _XML_MIXED))
        line = cr.format_results_line(res)
        assert line.startswith(cr.COCOTB_RESULTS_PREFIX)
        parsed = cr.parse_results_line(f"noise\n{line}\ntrailing")
        assert parsed == res

    def test_missing_line_returns_none(self):
        assert cr.parse_results_line("no sentinel here\n") is None

    def test_malformed_line_returns_none_not_pass(self):
        assert cr.parse_results_line(f"{cr.COCOTB_RESULTS_PREFIX}{{broken") is None

    def test_non_object_or_non_list_payload_returns_none(self):
        for payload in ("[]", '"payload"', '{"tests": {}}'):
            assert cr.parse_results_line(cr.COCOTB_RESULTS_PREFIX + payload) is None

    def test_non_object_test_entries_reject_the_transport(self):
        payload = '{"state":"ok","tests":[null,{"name":"kept","status":"pass"}]}'
        assert cr.parse_results_line(cr.COCOTB_RESULTS_PREFIX + payload) is None

    @pytest.mark.parametrize(
        "testcase",
        [
            '{"name":{},"module":"m","status":"pass"}',
            '{"name":"bad","module":[],"status":"pass"}',
            '{"name":"bad","module":"m","status":true}',
            '{"name":"bad","module":"m","status":"pass","failure":7}',
            '{"name":"bad","module":"m","status":"pass","elapsed_s":true}',
            '{"name":"bad","module":"m","status":"pass","elapsed_s":-1}',
            '{"name":"bad","module":"m","status":"pass","elapsed_s":1e999}',
        ],
    )
    def test_invalid_test_fields_reject_the_transport(self, testcase):
        payload = f'{{"state":"ok","tests":[{testcase}]}}'
        assert cr.parse_results_line(cr.COCOTB_RESULTS_PREFIX + payload) is None

    @pytest.mark.parametrize(
        "payload",
        [
            '{"state":7,"tests":[]}',
            '{"state":"unknown","tests":[]}',
            '{"detail":{},"tests":[]}',
            '{"skipped_unselected":true,"tests":[]}',
            '{"skipped_unselected":-1,"tests":[]}',
        ],
    )
    def test_invalid_result_fields_reject_the_transport(self, payload):
        assert cr.parse_results_line(cr.COCOTB_RESULTS_PREFIX + payload) is None

    def test_last_line_wins(self, tmp_path: Path):
        res = cr.parse_results_xml(_write(tmp_path, _XML_MIXED))
        older = cr.format_results_line(cr.CocotbResults(state=cr.STATE_EMPTY))
        newer = cr.format_results_line(res)
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
        parsed = cr.parse_results_line(
            cr.format_results_line(res, selected=["selected"], verbosity="compact")
        )
        assert parsed is not None
        assert [test.name for test in parsed.tests] == ["selected", "unexpected"]
        assert parsed.skipped_unselected == 2

    def test_full_line_retains_unselected_skips(self):
        res = cr.CocotbResults(
            state=cr.STATE_OK,
            tests=(
                cr.CocotbTest("selected", "m", "pass"),
                cr.CocotbTest("skip_a", "m", "skipped"),
            ),
        )
        parsed = cr.parse_results_line(
            cr.format_results_line(res, selected=["selected"], verbosity="full")
        )
        assert parsed == res


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

    def test_cocotb1_message_attribute(self, tmp_path: Path):
        res = cr.parse_results_xml(
            _write(
                tmp_path,
                _xml_with_failure('<failure message="Test failed with RANDOM_SEED=1783871502" />'),
            )
        )
        assert res.tests[0].failure_text == "Test failed with RANDOM_SEED=1783871502"

    def test_cocotb2_init_failure_msg_attribute(self, tmp_path: Path):
        res = cr.parse_results_xml(
            _write(tmp_path, _xml_with_failure('<failure msg="Test initialization failed" />'))
        )
        assert res.tests[0].failure_text == "Test initialization failed"

    def test_plain_junit_type_and_text(self, tmp_path: Path):
        res = cr.parse_results_xml(
            _write(
                tmp_path,
                _xml_with_failure('<failure type="AssertionError">assert 1 == 0</failure>'),
            )
        )
        assert res.tests[0].failure_text == "AssertionError: assert 1 == 0"

    def test_error_msg_still_wins_when_present(self, tmp_path: Path):
        res = cr.parse_results_xml(
            _write(
                tmp_path,
                _xml_with_failure(
                    '<failure error_type="AssertionError" error_msg="count should be 0" '
                    'message="Test failed with RANDOM_SEED=1" />'
                ),
            )
        )
        assert res.tests[0].failure_text == "AssertionError: count should be 0"

    def test_bare_failure_element_still_yields_empty_text(self, tmp_path: Path):
        # Honest limit: nothing to attribute, and we must not invent any.
        res = cr.parse_results_xml(_write(tmp_path, _xml_with_failure("<failure />")))
        assert res.tests[0].status == cr.VERDICT_FAIL
        assert res.tests[0].failure_text == ""


def test_failure_text_survives_the_results_line_round_trip(tmp_path: Path):
    """The [COCOTB_RESULTS] consumer sees the same attribution the XML had."""
    res = cr.parse_results_xml(
        _write(tmp_path, _xml_with_failure('<failure message="Test failed with RANDOM_SEED=7" />'))
    )
    parsed = cr.parse_results_line(cr.format_results_line(res))
    assert parsed is not None
    assert parsed.tests[0].failure_text == "Test failed with RANDOM_SEED=7"
