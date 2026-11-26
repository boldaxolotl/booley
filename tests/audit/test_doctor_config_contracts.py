"""Migrated config diagnostic contracts exercised through owning interfaces."""

from __future__ import annotations

from typing import Any

import pytest

from booley.audit import agent_schema, flow_schema, project_schema
from booley.audit.config_common import ConfigFindingSeverity, ConfigTableAudit
from booley.audit.diagnostic_results import Severity
from booley.harness.setup import readiness


def _texts(audit: ConfigTableAudit, severity: ConfigFindingSeverity) -> str:
    """Message and fix of every finding of one severity, one per line."""
    return "\n".join(
        f"{finding.message} {finding.fix}"
        for finding in audit.findings
        if finding.severity is severity
    )


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param(
            {"flows": {"lint": {"default_target": "lint_core"}}},
            ["[flows.lint].default_target is retired"],
            id="default-target",
        ),
        pytest.param(
            {"flows": {"lint": {"target": "ibex_top#lint"}}},
            ["[flows.lint].target is retired", "Flow calls require an explicit target"],
            id="target",
        ),
        pytest.param(
            {"flows": {"sim": {"selftest": {"good": "main", "bad": "known_bad"}}}},
            ["[flows.sim.selftest] is retired", "bad-overlay"],
            id="selftest",
        ),
        pytest.param({"tools": {"builtin": ["sim"]}}, ["[tools] is retired"], id="tools"),
        pytest.param(
            {"flows": {"custom": ["sim"]}},
            ["[flows].custom is retired", "enabled = false"],
            id="custom",
        ),
        pytest.param(
            {"flows": {"elab": {"standalone_frontend": "iverilog"}}},
            ["[flows.elab] is retired", "sim --mode elab-only", "delete standalone_frontend"],
            id="elab",
        ),
        pytest.param(
            {"flows": {"elaborate": {"standalone_frontend": "iverilog"}}},
            ["[flows.elaborate] is retired"],
            id="elaborate",
        ),
        pytest.param(
            {"flows": {"synth": {"flatten": True}}},
            ["Target build input(s): flatten", ".core Target"],
            id="synth-flatten",
        ),
        pytest.param(
            {"flows": {"fpga": {"part": "xc7a35tcsg324-1"}}},
            ["Target build input(s): part", ".core Target"],
            id="fpga-part",
        ),
    ],
)
def test_retired_and_target_owned_flow_settings_fail_with_their_migration(
    data: dict[str, Any], expected: list[str]
) -> None:
    audit = flow_schema.audit_flow_tables(data, ())

    assert not audit.is_valid
    failures = _texts(audit, ConfigFindingSeverity.FAIL)
    for text in expected:
        assert text in failures


@pytest.mark.parametrize(
    ("knob", "reader", "non_reader", "value"),
    [
        ("sim_time_grace_s", "sim", "lint", 180),
        ("build_timeout_ms", "sim", "lint", 3_600_000),
        ("trace_convert_timeout_ms", "sim", "lint", 1_800_000),
        ("fail_on_timing_violation", "synth", "lint", True),
        ("timing_report_paths", "synth", "lint", 10),
        ("warnings_as_errors", "lint", "sim", False),
        ("trace_files", "sim", "lint", ["fpu.vcd"]),
        ("output_dir", "synth", "lint", "util/synth"),
        ("expected_latches", "synth", "lint", 1),
        ("expected_latches", "fpga", "sim", 1),
        ("expected_comb_loops", "synth", "lint", 1),
        ("expected_comb_loops", "fpga", "sim", 1),
        ("pre_run_commands", "sim", "lint", ["make prep CASE=$BOOLEY_TEST_NAME"]),
    ],
)
def test_flow_knob_warns_only_under_a_flow_that_ignores_it(
    knob: str, reader: str, non_reader: str, value: object
) -> None:
    """A knob added to a Flow without a registered reader would be accepted under every Flow."""

    def ignored(flow: str) -> tuple[bool, set[str | None]]:
        audit = flow_schema.audit_flow_table(flow, {knob: value})
        return audit.is_valid, {
            f.subject for f in audit.findings if f.check_id == "config.flow-knob-ignored"
        }

    assert ignored(reader) == (True, set())
    assert ignored(non_reader) == (True, {f"{non_reader}.{knob}"})


@pytest.mark.parametrize("value", ["make prep", ["make prep", ""]])
def test_pre_run_commands_must_be_a_list_of_nonempty_strings(value: object) -> None:
    audit = flow_schema.audit_flow_table("sim", {"pre_run_commands": value})

    assert not audit.is_valid
    assert "pre_run_commands must be a list of strings, none empty" in _texts(
        audit, ConfigFindingSeverity.FAIL
    )


def test_sim_output_dir_is_retired_without_failing_doctor() -> None:
    """``[flows.sim].output_dir`` never changed a run: Doctor warns once, never fails."""
    audit = flow_schema.audit_flow_tables(
        {"flows": {"sim": {"output_dir": "util/sim"}, "lint": {}, "synth": {}}},
        ("sim", "lint", "synth"),
    )

    assert audit.is_valid
    (finding,) = [f for f in audit.findings if f.severity is ConfigFindingSeverity.WARN]
    assert "[flows.sim].output_dir is retired" in finding.message
    assert finding.check_id == "config.flow-key-retired"
    assert finding.subject == "sim.output_dir"
    assert "delete [flows.sim].output_dir" in finding.fix


def test_windows_rejects_host_provisioning_during_config_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.eda.provisioning import configuration as eda_config

    monkeypatch.setattr(eda_config.sys, "platform", "win32")

    audit = project_schema.audit_eda_config({"eda": {"vivado": {"provisioning": "host"}}})

    assert not audit.is_valid
    assert "host provisioning is unsupported on Windows" in _texts(
        audit, ConfigFindingSeverity.FAIL
    )


@pytest.mark.parametrize(
    ("data", "valid", "expected"),
    [
        pytest.param({"agent": {"provider": "cluade"}}, False, ["'cluade'"], id="typo"),
        pytest.param({"agent": "claude"}, False, ["[agent] must be a table"], id="not-a-table"),
        pytest.param(
            {"agent": {"provider": "codex"}},
            True,
            ["booley.toml [agent] default: codex"],
            id="valid",
        ),
        pytest.param({}, True, [], id="absent"),
        pytest.param({"agent": {}}, True, [], id="empty"),
    ],
)
def test_agent_table_fails_only_a_present_and_wrong_value(
    data: dict[str, Any], valid: bool, expected: list[str]
) -> None:
    """The provider may come from BOOLEY_PRIMARY_PROVIDER; only a present wrong value fails."""
    audit = agent_schema.audit_agent_table(data)

    assert audit.is_valid is valid
    messages = [finding.message for finding in audit.findings]
    assert len(messages) == len(expected)
    for text, message in zip(expected, messages, strict=True):
        assert text in message


def test_doctor_stops_on_retired_standalone_frontend_with_its_own_migration(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doctor stops on the retired key with its own migration text, not the EDA fix."""
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_bytes(b'[flows.sim]\nstandalone_frontend = "auto"\n')

    result = readiness.load_project(tmp_path)

    (finding,) = [f for f in result.report.findings if f.severity is Severity.FAIL]
    assert finding.message.startswith("booley.toml [flows.sim].standalone_frontend is retired")
    assert "each Target's simulator" in finding.message
    assert "commercial-EDA" not in finding.fix
    assert result.project is None
