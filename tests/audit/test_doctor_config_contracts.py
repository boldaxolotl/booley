"""Migrated config diagnostic contracts exercised through owning interfaces."""

from __future__ import annotations

import pytest
from tests.diagnostic_helpers import (
    _record_audit,
)

from booley.audit import (
    agent_schema,
    flow_schema,
    project_schema,
)


def test_validate_one_flow_table_rejects_retired_default_target():
    fails: list[str] = []
    warns: list[str] = []

    def _fail(msg: str, fix: str = "") -> None:
        fails.append(msg)

    def _warn(msg: str) -> None:
        warns.append(msg)

    ok = _record_audit(
        flow_schema.audit_flow_table("lint", {"default_target": "lint_core"}),
        warned=_warn,
        failed=_fail,
    )
    assert ok is False
    assert any("[flows.lint].default_target is retired" in m for m in fails)


def test_validate_one_flow_table_rejects_retired_target_key():
    fails: list[str] = []

    ok = _record_audit(
        flow_schema.audit_flow_table("lint", {"target": "ibex_top#lint"}),
        warned=lambda _msg: None,
        failed=lambda msg, fix="": fails.append(f"{msg} {fix}"),
    )

    assert ok is False
    assert any("[flows.lint].target is retired" in message for message in fails)
    assert any("Flow calls require an explicit target" in message for message in fails)


def test_validate_one_flow_table_rejects_retired_selftest_table():
    fails: list[str] = []

    ok = _record_audit(
        flow_schema.audit_flow_table("sim", {"selftest": {"good": "main", "bad": "known_bad"}}),
        warned=lambda _msg: None,
        failed=lambda msg, fix="": fails.append(f"{msg} {fix}"),
    )

    assert ok is False
    assert any("[flows.sim.selftest] is retired" in message for message in fails)
    assert any("bad-overlay" in message for message in fails)


@pytest.mark.parametrize("key", ["builtin", "custom"])
def test_validate_flow_tables_rejects_retired_allowlists(key):
    fails: list[str] = []

    ok = _record_audit(
        flow_schema.audit_flow_tables({"tools": {key: ["sim"]}}, ("sim", "lint", "synth")),
        warned=lambda _msg: None,
        failed=lambda msg, fix="": fails.append(f"{msg} {fix}"),
    )

    assert ok is False
    assert any("retired" in message and "enabled = false" in message for message in fails)


@pytest.mark.parametrize("retired", ["elab", "elaborate"])
def test_doctor_rejects_retired_elaboration_tables_with_migration(retired):
    fails: list[str] = []

    ok = _record_audit(
        flow_schema.audit_flow_tables(
            {"flows": {retired: {"standalone_frontend": "iverilog"}, "sim": {}}},
            ("sim", "lint", "synth"),
        ),
        warned=lambda _msg: None,
        failed=lambda msg, fix="": fails.append(f"{msg} {fix}"),
    )

    assert ok is False
    assert any(
        f"[flows.{retired}] is retired" in message
        and "sim --mode elab-only" in message
        and "[flows.sim].standalone_frontend" in message
        for message in fails
    )


def test_validate_one_flow_table_accepts_lint_timeout_ms():
    """Lint reads the same persistent timeout policy as every other built-in Flow."""
    fails: list[str] = []
    warns: list[str] = []

    ok = _record_audit(
        flow_schema.audit_flow_table("lint", {"timeout_ms": 900000}),
        warned=warns.append,
        failed=lambda msg, fix="": fails.append(msg),
    )
    assert ok is True
    assert warns == []

    # simulate DOES read timeout_ms → no set-but-ignored warning.
    warns.clear()
    _record_audit(
        flow_schema.audit_flow_table("sim", {"timeout_ms": 900000}),
        warned=warns.append,
        failed=lambda msg, fix="": fails.append(msg),
    )
    assert not any("timeout_ms" in m for m in warns)


@pytest.mark.parametrize(
    ("knob", "reader", "non_reader", "value"),
    [
        ("sim_time_grace_s", "sim", "lint", 180),
        ("fail_on_timing_violation", "synth", "lint", True),
        ("warnings_as_errors", "lint", "sim", False),
        # trace_files declares the TB's own dump path; only simulate reads it.
        ("trace_files", "sim", "lint", ["fpu.vcd"]),
    ],
)
def test_selective_knob_is_registered_with_its_reader(knob, reader, non_reader, value):
    """Every selective knob warns under a Flow that ignores it, stays quiet under its own.

    A knob added to a Flow without an entry here is silently accepted anywhere,
    which is the exact failure mode ``_SELECTIVE_FLOW_KNOBS`` exists to prevent.
    """
    fails: list[str] = []
    warns: list[str] = []

    _record_audit(
        flow_schema.audit_flow_table(non_reader, {knob: value}),
        warned=warns.append,
        failed=lambda msg, fix="": fails.append(msg),
    )
    assert any(f"[flows.{non_reader}].{knob}" in m and "ignores it" in m for m in warns)

    warns.clear()
    _record_audit(
        flow_schema.audit_flow_table(reader, {knob: value}),
        warned=warns.append,
        failed=lambda msg, fix="": fails.append(msg),
    )
    assert not any(knob in m and "ignores it" in m for m in warns)


@pytest.mark.parametrize(
    ("flow_name", "knob", "value"),
    [
        ("synth", "flatten", True),
        ("synth", "frontend", "slang"),
        ("synth", "sdc", "constraints/top.sdc"),
        ("fpga", "part", "xc7a35tcsg324-1"),
        ("fpga", "ppa_profile", "compact"),
        ("fpga", "out_of_context", True),
        ("fpga", "strategy", "Flow_PerfOptimized_high"),
    ],
)
def test_target_build_inputs_are_rejected_from_flow_tables(flow_name, knob, value):
    fails: list[str] = []

    ok = _record_audit(
        flow_schema.audit_flow_table(flow_name, {knob: value}),
        warned=lambda _msg: None,
        failed=lambda msg, fix="": fails.append(f"{msg} {fix}"),
    )

    assert ok is False
    assert any(knob in message and ".core Target" in message for message in fails)


def test_validate_one_flow_table_pre_run_commands_shape():
    """[flows.sim].pre_run_commands must be a list of strings (ADR 0039)."""
    fails: list[str] = []
    warns: list[str] = []

    # A scalar (or a list with non-string entries) fails the shape check.
    ok = _record_audit(
        flow_schema.audit_flow_table("sim", {"pre_run_commands": "make prep"}),
        warned=warns.append,
        failed=lambda msg, fix="": fails.append(msg),
    )
    assert ok is False
    assert any("[flows.sim].pre_run_commands must be a" in m for m in fails)

    fails.clear()
    ok = _record_audit(
        flow_schema.audit_flow_table("sim", {"pre_run_commands": ["make prep", 3]}),
        warned=warns.append,
        failed=lambda msg, fix="": fails.append(msg),
    )
    assert ok is False

    # A well-formed list passes, with no inert-knob warning on simulate.
    fails.clear()
    warns.clear()
    ok = _record_audit(
        flow_schema.audit_flow_table(
            "sim", {"pre_run_commands": ["make prep CASE=$BOOLEY_TEST_NAME"]}
        ),
        warned=warns.append,
        failed=lambda msg, fix="": fails.append(msg),
    )
    assert ok is True
    assert fails == []
    assert not any("pre_run_commands" in m for m in warns)

    # Only simulate reads it: set on lint it is inert → warn, not fail.
    ok = _record_audit(
        flow_schema.audit_flow_table("lint", {"pre_run_commands": ["make prep"]}),
        warned=warns.append,
        failed=lambda msg, fix="": fails.append(msg),
    )
    assert ok is True
    assert any("[flows.lint].pre_run_commands" in m and "ignores it" in m for m in warns)


def test_windows_rejects_host_provisioning_during_config_audit(tmp_path, monkeypatch):
    from booley.eda import config as eda_config

    passes: list[str] = []
    warns: list[str] = []
    fails: list[str] = []
    monkeypatch.setattr(eda_config.sys, "platform", "win32")

    valid = _record_audit(
        project_schema.audit_eda_config({"eda": {"vivado": {"provisioning": "host"}}}),
        passed=passes.append,
        warned=warns.append,
        failed=lambda message, fix="": fails.append(message),
    )

    assert valid is False
    assert any("host provisioning is unsupported on Windows" in message for message in fails)


class TestValidateAgentTable:
    """A typo'd provider is fatal, not advisory: `_parse_provider` raises rather
    than run a backend the project never chose, so every agent run dies with a
    BackendConfigError. Doctor must be where that surfaces."""

    @staticmethod
    def _run(agent_section):
        passes: list[str] = []
        fails: list[str] = []
        valid = _record_audit(
            agent_schema.audit_agent_table(
                {"agent": agent_section} if agent_section is not None else {}
            ),
            passed=passes.append,
            failed=lambda msg, fix="": fails.append(msg),
        )
        return valid, passes, fails

    def test_fails_on_an_invalid_provider(self):
        valid, _passes, fails = self._run({"provider": "cluade"})
        assert not valid
        assert any("cluade" in m for m in fails)

    def test_fails_when_agent_is_not_a_table(self):
        valid, _passes, fails = self._run("claude")
        assert not valid
        assert any("[agent] must be a table" in m for m in fails)

    def test_passes_and_names_a_valid_provider(self):
        valid, passes, fails = self._run({"provider": "codex"})
        assert valid and not fails
        assert any("codex" in m for m in passes)

    def test_rejects_the_retired_primary_alias(self):
        valid, _passes, fails = self._run({"primary": "claude"})
        assert not valid
        assert any("retired" in m for m in fails)

    @pytest.mark.parametrize("section", [None, {}])
    def test_absent_or_empty_agent_is_not_an_error(self, section):
        # The provider may legitimately come from BOOLEY_PRIMARY_PROVIDER or the
        # container's BOOLEY_AGENT_APP; only a present-and-wrong value fails.
        valid, passes, fails = self._run(section)
        assert valid and not fails and not passes
