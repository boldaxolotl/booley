"""Coverage source gaps: a lexical Preflight scan compared with all native records."""

from dataclasses import replace

from booley.flows.sim.coverage_campaign import freeze_coverage_mapping
from booley.flows.sim.coverage_campaign_store import load_coverage_campaign
from booley.flows.sim.coverage_source_gaps import GAP_CODE, source_gap_report_lines
from booley.flows.sim.coverage_transaction import run_coverage_target
from tests.flows.sim.test_coverage_transaction import NativeExecution, Progress, gap_plan


def _gap_paths(outcome):
    """Closure paths named by the published gap findings, resolved from their pointers."""
    campaign = load_coverage_campaign(outcome.campaign_path).campaign
    rtl = campaign.source_closure["rtl"]
    gaps = [f for f in campaign.findings if f.code == GAP_CODE]
    return campaign, [rtl[int(f.pointer.split("/")[3])]["path"] for f in gaps]


def test_absent_points_warns_only_for_module_bearing_non_include_rtl(tmp_path):
    sources = {
        "rtl/unused.sv": "module unused; endmodule\n",
        "rtl/pkg.sv": "package p; endpackage\n",
        "rtl/iface.sv": "interface i; endinterface\n",
        "rtl/header.svh": "module h; endmodule\n",
        # `module` only in a line comment, a block comment and a continued string.
        "rtl/hidden.sv": '// module a\n/* module b */\nlocalparam string S = "x\\\nmodule c";\n',
    }
    plan = gap_plan(
        tmp_path,
        sources,
        includes=("rtl/header.svh",),
        testbench={"tb/bench.sv": "module bench; endmodule\n"},
    )
    assert plan.rtl_candidates == ("rtl/counter.sv", "rtl/unused.sv")
    _campaign, paths = _gap_paths(run_coverage_target(plan, NativeExecution(), Progress()))
    assert paths == ["rtl/unused.sv"]


class _ExtraRecordsExecution(NativeExecution):
    """Native output that adds an fsm and an unknown-type record, neither scored."""

    def payload(self, hits):
        extra = "".join(
            f"C '\x01f\x02rtl/{name}.sv\x01l\x021\x01n\x021\x01h\x02TOP.x\x01t\x02{kind}' {hits}\n"
            for name, kind in (("fsm_only", "fsm"), ("future_only", "future"))
        )
        return super().payload(hits) + extra


def test_presence_counts_even_when_no_scored_point_exists(tmp_path):
    sources = {
        f"rtl/{name}.sv": f"module {name}; endmodule\n"
        for name in ("fsm_only", "future_only", "absent")
    }
    outcome = run_coverage_target(
        gap_plan(tmp_path, sources), _ExtraRecordsExecution(), Progress()
    )
    campaign, paths = _gap_paths(outcome)
    assert campaign.collection["status"] == "complete"
    # absent.sv is the positive control: it has no record of any type.
    assert paths == ["rtl/absent.sv"]


def test_line_directives_and_token_joins_do_not_suppress_source_gaps(tmp_path):
    sources = {
        "rtl/remapped.sv": '`line 100 "logical.sv" 0\nmodule remapped; endmodule\n',
        "rtl/joined.sv": "`define J(a) a``_x\nmodule joined; endmodule\n",
    }
    outcome = run_coverage_target(gap_plan(tmp_path, sources), NativeExecution(), Progress())
    campaign, paths = _gap_paths(outcome)
    assert sorted(paths) == ["rtl/joined.sv", "rtl/remapped.sv"]
    assert not any(f.code == "COV_RTL_SOURCE_DISCOVERY_INCOMPLETE" for f in campaign.findings)


def test_presentation_bounds_escaped_paths_and_omissions():
    from tests.mcp_tools.test_coverage_evidence import _campaign_with_source_gaps

    campaign, paths = _campaign_with_source_gaps(8)
    lines = source_gap_report_lines(campaign)
    assert lines[0] == "RTL sources without coverage points: 8"
    assert len(lines) == 5
    assert all(paths[i] in lines[i + 1] for i in range(3))
    assert "5 more" in lines[-1]
    records = list(campaign.source_closure["rtl"])
    records[0] = {"path": 'rtl/aaa_quote"\n.sv', "sha256": "sha256:" + "a" * 64}
    escaped = replace(
        campaign, source_closure=freeze_coverage_mapping({"rtl": records, "testbench": []})
    )
    text = "\n".join(source_gap_report_lines(escaped))
    assert "\\n.sv" in text and '\\"' in text
    assert source_gap_report_lines(replace(campaign, findings=())) == []
