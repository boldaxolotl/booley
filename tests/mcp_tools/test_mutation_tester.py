"""Tests for MutationTesterSpecialist — lock-based creator + sim sweep.

Covers parsing, prompts, forbidden-category gating, argparse, and the
cold/warm development with mocked agent + sim subprocess.  The old
two-phase apply/revert tests were deleted along with their helpers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from booley.dev_support.development_state import DevelopmentState
from booley.flows.sim.target_tests import NoRunnableTestsError
from booley.mcp.base import EXIT_ERROR, EXIT_SUCCESS, McpToolResult
from booley.sim.cocotb_run import _parse_args as parse_cocotb_run_args
from booley.specialists.mutation_tester import (
    MutationResult,
    MutationSpec,
    MutationSummary,
    MutationTesterSpecialist,
    UnsupportedSimTargetError,
    VerificationOutcome,
    _extract_json,
    _sanitize_json_text,
    generate_results_markdown,
    generate_specs_markdown,
    parse_creator_output,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_dut_top_is_not_derived_by_parsing_hdl(tmp_path: Path, monkeypatch) -> None:
    rtl = tmp_path / "rtl"
    rtl.mkdir()
    (rtl / "design.sv").write_text(
        "// module stale_line;\n/* module stale_block; */\nmodule actual_dut; endmodule\n",
        encoding="utf-8",
    )
    endpoint = _make_endpoint(
        tmp_path,
        monkeypatch,
        scope="rtl/design.sv",
        dut_top_module="",
    )

    assert endpoint._dut_top_module() == ""


def _env_with_state(
    state_file: Path,
    logs_dir: Path,
    slug: str = "test-ticket",
) -> dict[str, str]:
    env = os.environ.copy()
    env["BOOLEY_SLUG"] = slug
    env["BOOLEY_STATE_FILE"] = str(state_file)
    env["BOOLEY_LOGS_DIR"] = str(logs_dir)
    return env


def _make_state(tmp_path: Path) -> Path:
    """Create the state file used by mutation endpoint tests."""
    state_file = tmp_path / "state.json"
    state = DevelopmentState.load(state_file)
    state.save()
    return state_file


def _write_dut_top(tmp_path: Path, top: str = "design_top") -> Path:
    """Drop a minimal SV file containing ``module <top>``."""
    rtl_dir = tmp_path / "rtl"
    rtl_dir.mkdir(parents=True, exist_ok=True)
    f = rtl_dir / f"{top}.sv"
    f.write_text(
        f"module {top}(input logic clk, input logic rst);\n"
        f"  logic [3:0] r;\n"
        f"  always_ff @(posedge clk) r <= r + 1;\n"
        f"endmodule\n",
        encoding="utf-8",
    )
    return f


def _make_endpoint(
    tmp_path: Path,
    monkeypatch=None,
    *,
    target: str = "lite",
    scope: str = "rtl/mod_a.sv,rtl/mod_b.sv",
    tb_top: str = "design_top_tb",
    count: int = 10,
    min_detected: int | None = None,
    steer: str | None = None,
    regen_lock: bool = False,
    dut_top_module: str = "design_top",
    dut_files: tuple[str, ...] = ("rtl/design_top.sv",),
    extra_args: list[str] | None = None,
) -> MutationTesterSpecialist:
    """Build a endpoint with parsed args and loaded state.

    When *monkeypatch* is supplied, the BOOLEY_LOGS_DIR env var is set for
    the lifetime of the test so mutation_lock writes land in tmp_path.
    """
    state_file = _make_state(tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(exist_ok=True)
    report_dir = tmp_path / "reports"
    argv = [
        "--work-dir",
        str(tmp_path),
        "--report-dir",
        str(report_dir),
        "--target",
        target,
        "--scope",
        scope,
        "--tb-top",
        tb_top,
        "--dut-top",
        dut_top_module,
        "--count",
        str(count),
        # ADR 0022 dec 13: Ticket Mode now derives DUT files from the resolved
        # sim Target's .core; these unit tests author no .core, so pass them as
        # the explicit Interactive-Mode arg (which still wins in _dut_files).
        "--dut-files",
        *dut_files,
    ]
    if min_detected is not None:
        argv.extend(["--min-detected", str(min_detected)])
    if steer:
        argv.extend(["--steer", steer])
    if regen_lock:
        argv.append("--regen-lock")
    if extra_args:
        argv.extend(extra_args)
    env = _env_with_state(state_file, logs_dir)
    if monkeypatch is not None:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    endpoint = MutationTesterSpecialist()
    with patch.dict(os.environ, env):
        endpoint.parse_args(argv)
    endpoint.read_state()
    return endpoint


def _sample_specs(n: int = 3, category: str = "operator_change") -> list[MutationSpec]:
    return [
        MutationSpec(
            index=i,
            category=category,
            file="rtl/mod_a.sv",
            line=i + 1,
            original_code=f"a + b_{i}",
            mutated_code=f"a - b_{i}",
            detectability_argument="dx",
        )
        for i in range(1, n + 1)
    ]


def test_run_rejects_target_with_every_test_skipped(tmp_path: Path, monkeypatch) -> None:
    endpoint = _make_endpoint(tmp_path, monkeypatch)
    with (
        patch.object(endpoint, "_validate_scope_against_target", return_value=None),
        patch.object(endpoint, "_validate_target_runner"),
        patch.object(endpoint, "cocotb_target", return_value=None),
        patch(
            "booley.specialists.mutation_tester.require_runnable_target_test_suite",
            side_effect=NoRunnableTestsError("sim", ("smoke", "corner")),
        ),
    ):
        result = endpoint._run()

    assert result.exit_code == EXIT_ERROR
    assert "no runnable tests" in result.report_text


def _sample_creator_json(specs: list[MutationSpec]) -> str:
    return json.dumps({"mutations": [s.to_dict() for s in specs]})


@dataclass
class FakeAgentResult:
    output: str = ""
    structured: dict[str, Any] | None = None
    session_id: str | None = "fake-sid"
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0


def _fake_proc(rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestCreatorOutputParsing:
    def test_valid_json(self):
        specs = _sample_specs(3)
        out = _sample_creator_json(specs)
        parsed = parse_creator_output(out)
        assert len(parsed) == 3
        assert parsed[0].index == 1

    def test_json_in_code_fence(self):
        raw = _sample_creator_json(_sample_specs(2))
        out = f"Here you go:\n```json\n{raw}\n```\nDone."
        assert len(parse_creator_output(out)) == 2

    def test_empty_output(self):
        assert parse_creator_output("") == []

    def test_empty_mutations(self):
        assert parse_creator_output('{"mutations": []}') == []


class TestExtractJson:
    def test_direct(self):
        assert _extract_json('{"key": "value"}') == {"key": "value"}

    def test_code_fence(self):
        assert _extract_json('```json\n{"k": 42}\n```') == {"k": 42}

    def test_trailing_comma(self):
        assert _extract_json('{"key": "v",}') == {"key": "v"}

    def test_no_json(self):
        assert _extract_json("no json") is None


class TestSanitizeJson:
    def test_removes_trailing_comma_brace(self):
        assert _sanitize_json_text('{"a": 1,}') == '{"a": 1}'

    def test_removes_trailing_comma_bracket(self):
        assert _sanitize_json_text("[1, 2,]") == "[1, 2]"


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


class TestMutationSummary:
    def test_counts(self):
        s = MutationSummary(
            specs=_sample_specs(3),
            results=[
                MutationResult(index=1, detected=True),
                MutationResult(index=2, detected=False),
                MutationResult(index=3, detected=False, invalid=True),
            ],
        )
        assert s.detected_count == 1
        assert s.not_detected_count == 1
        assert s.invalid_count == 1

    def test_classify(self):
        s = MutationSummary(
            specs=_sample_specs(2),
            results=[MutationResult(index=1, detected=True)],
        )
        c = s.classify()
        assert c[0]["status"] == "detected"
        assert c[1]["status"] == "untested"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


class TestCreatorPrompt:
    def test_includes_scope(self, tmp_path: Path):
        endpoint = _make_endpoint(tmp_path, scope="rtl/mod_a.sv,rtl/mod_b.sv")
        p = endpoint._build_creator_prompt()
        assert "rtl/mod_a.sv" in p and "rtl/mod_b.sv" in p

    def test_includes_count(self, tmp_path: Path):
        endpoint = _make_endpoint(tmp_path, count=7)
        p = endpoint._build_creator_prompt()
        assert "7" in p

    def test_includes_harness_section(self, tmp_path: Path):
        endpoint = _make_endpoint(tmp_path)
        p = endpoint._build_creator_prompt()
        assert "exact source" in p
        assert "read-only" in p

    def test_no_revert_instruction(self, tmp_path: Path):
        endpoint = _make_endpoint(tmp_path)
        p = endpoint._build_creator_prompt()
        assert "do not modify the source" in p

    def test_steer_injected(self, tmp_path: Path):
        endpoint = _make_endpoint(tmp_path, steer="Focus on FSMs")
        p = endpoint._build_creator_prompt()
        assert "Focus on FSMs" in p
        assert "Developer Agent Context" in p

    def test_no_steer_omits_section(self, tmp_path: Path):
        endpoint = _make_endpoint(tmp_path)
        p = endpoint._build_creator_prompt()
        assert "Developer Agent Context" not in p

    def test_uses_configured_testbench_dirs_for_boundary(self, tmp_path: Path):
        # ADR 0026: the read-boundary TB dirs come from the .core tags:[tb]
        # partition — the tb fileset's source dir (verif/) plus its include
        # header dir (checks/, an is_include_file entry).
        (tmp_path / "design.core").write_text(
            "CAPI=2:\n"
            "name: ::demo\n"
            "filesets:\n"
            "  rtl: {files: [rtl/mod_a.sv]}\n"
            "  tb:\n"
            "    files:\n"
            "      - verif/tb_top.sv: {file_type: systemVerilogSource}\n"
            "      - checks/asserts.svh: {is_include_file: true}\n"
            "    tags: [tb]\n"
            "targets:\n"
            "  sim: {filesets: [rtl, tb], toplevel: tb_top}\n",
            encoding="utf-8",
        )

        endpoint = _make_endpoint(tmp_path, scope="rtl/mod_a.sv")
        p = endpoint._build_creator_prompt()

        assert "verif/" in p
        assert "checks/" in p
        assert "tb/ or any configured" not in p

    def test_flat_repo_boundary_names_testbench_file_not_directory(self, tmp_path: Path):
        (tmp_path / "picorv32.v").write_text("module picorv32; endmodule\n")
        (tmp_path / "testbench.v").write_text("module testbench; endmodule\n")
        (tmp_path / "design.core").write_text(
            "CAPI=2:\n"
            "name: ::demo\n"
            "filesets:\n"
            "  rtl: {files: [picorv32.v]}\n"
            "  tb: {files: [testbench.v], tags: [tb]}\n"
            "targets:\n"
            "  sim: {filesets: [rtl, tb], toplevel: testbench}\n"
        )

        endpoint = _make_endpoint(tmp_path, scope="picorv32.v")
        prompt = endpoint._build_creator_prompt()

        assert "testbench.v" in prompt
        assert "testbench.v/" not in prompt


class TestRetryPrompt:
    def test_forbidden_path_asks_for_new_json(self, tmp_path: Path):
        endpoint = _make_endpoint(tmp_path)
        outcome = VerificationOutcome(
            ok=False,
            baseline_passed=False,
            pinned_passed=False,
            log_tail="",
            reason="forbidden category in spec list",
        )
        p = endpoint._build_retry_prompt(outcome)
        assert "fresh JSON" in p or "JSON spec list" in p

    def test_sim_failure_says_no_json(self, tmp_path: Path):
        endpoint = _make_endpoint(tmp_path)
        outcome = VerificationOutcome(
            ok=False,
            baseline_passed=False,
            pinned_passed=True,
            log_tail="some elab error",
            reason="isolated variant did not elaborate",
        )
        p = endpoint._build_retry_prompt(outcome)
        assert "Return a complete fresh JSON mutation list" in p


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------


class TestArgparse:
    def test_defaults(self, tmp_path: Path):
        endpoint = _make_endpoint(tmp_path)
        assert endpoint.args.count == 10
        assert endpoint.args.min_detected is None
        assert endpoint.args.steer is None
        assert endpoint.args.regen_lock is False

    def test_regen_lock_flag(self, tmp_path: Path):
        endpoint = _make_endpoint(tmp_path, regen_lock=True)
        assert endpoint.args.regen_lock is True


# ---------------------------------------------------------------------------
# Endpoint metadata
# ---------------------------------------------------------------------------


class TestToolMetadata:
    def test_name(self):
        assert MutationTesterSpecialist().name == "mutation_tester"

    def test_code_modifying_false(self):
        assert MutationTesterSpecialist().code_modifying is False

    def test_satisfies(self):
        assert "mutation_score" in MutationTesterSpecialist().satisfies


# ---------------------------------------------------------------------------
# Artifact generation
# ---------------------------------------------------------------------------


class TestArtifacts:
    def test_specs_markdown(self):
        md = generate_specs_markdown(_sample_specs(2))
        assert "# Mutation Specifications" in md
        assert "operator_change" in md

    def test_results_link_mutated_rtl_and_escape_rtl_operators(self):
        spec = MutationSpec(
            index=1,
            category="operator_change",
            file="rtl/mod (wide).sv",
            line=12,
            original_code="a | b",
            mutated_code="a & b",
        )
        summary = MutationSummary(
            specs=[spec],
            results=[MutationResult(index=1, detected=False)],
        )

        md = generate_results_markdown(
            summary,
            1,
            variant_paths={1: "variants/mutant_1/rtl/mod (wide).sv"},
        )

        assert "[rtl/mod (wide).sv:12](variants/mutant_1/rtl/mod%20%28wide%29.sv)" in md
        assert "`a \\| b` → `a & b`" in md
        assert "not_detected" in md

    def test_results_markdown_names_the_first_killing_test(self):
        spec = _sample_specs(1)[0]
        summary = MutationSummary(
            specs=[spec],
            results=[
                MutationResult(
                    index=1,
                    detected=True,
                    first_killing_test="test_rx_overrun",
                )
            ],
        )

        md = generate_results_markdown(summary, 1)

        assert "test_rx_overrun" in md


# ---------------------------------------------------------------------------
# Cold-start development with mocked agent + sim
# ---------------------------------------------------------------------------


def _patch_invoke_agent(monkeypatch, results: list[FakeAgentResult]):
    """Make _invoke_agent_with_resume return successive FakeAgentResults."""
    state = {"i": 0}

    def _fake(self, params, on_event=None):
        i = state["i"]
        state["i"] = i + 1
        r = results[min(i, len(results) - 1)]
        # Mimic Specialist._invoke_agent's side-effect.
        self._last_session_id = r.session_id
        return r

    monkeypatch.setattr(
        "booley.specialists.specialist.Specialist._invoke_agent_with_resume",
        _fake,
    )


def _patch_resolve_target(monkeypatch, *, eda_tool: str | None = None):
    """Stub fusesoc_registry.resolve_target — no real FuseSoC (Unit A.3).

    Returns a fake ResolvedTarget whose ``build_root`` is the requested build
    dir, so ``_run_elab`` derives the make/bin-dir from it and writes its
    marker without spawning the FuseSoC CLI.
    """
    import types

    def _fake_resolve(target, *, project_root, build_root, **kwargs):
        return types.SimpleNamespace(
            build_root=Path(build_root),
            toplevel="tb",
            eda_tool=eda_tool,
        )

    monkeypatch.setattr(
        "booley.fusesoc.fusesoc_registry.resolve_target",
        _fake_resolve,
    )


def _patch_sim_runner(monkeypatch, sim_returncode: int = 0):
    """Stub the edalize build+run mutation_tester drives (Unit A.3).

    Behaviour:
      * ``resolve_target`` → fake (no FuseSoC CLI).
      * a ``make`` build returns rc=0.
      * a ``verilator_run`` per-mutant invocation returns the chosen rc, so
        baseline + pinned + sweep all "pass" by default.
      * ``git checkout`` is forwarded as a no-op success.
    """
    _patch_resolve_target(monkeypatch)
    original = subprocess.run

    def _fake(cmd, *args, **kwargs):
        joined = " ".join(cmd) if isinstance(cmd, list) else ""
        if "booley.sim.verilator_run" in joined:
            return _fake_proc(rc=sim_returncode, stdout="[sim] ok", stderr="")
        if isinstance(cmd, list) and cmd[:2] == ["make", "-C"]:
            return _fake_proc(rc=0, stdout="[make] ok", stderr="")
        if isinstance(cmd, list) and cmd[:2] == ["git", "checkout"]:
            return _fake_proc(rc=0)
        return original(cmd, *args, **kwargs)

    monkeypatch.setattr(
        "booley.specialists.mutation_tester.subprocess.run",
        _fake,
    )


def test_elab_and_sim_run_verilator_binary(tmp_path: Path, monkeypatch):
    """Each source variant is built, then run through the configured simulator."""
    captured: list[list[str]] = []

    def _fake_run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return _fake_proc(rc=0, stdout="[ok]", stderr="")

    _patch_resolve_target(monkeypatch)
    monkeypatch.setattr(
        "booley.specialists.mutation_tester.subprocess.run",
        _fake_run,
    )

    endpoint = _make_endpoint(tmp_path, monkeypatch)
    build_dir = tmp_path / "build"
    build_dir.mkdir()

    endpoint._run_elab("default", tmp_path, build_dir)
    endpoint._run_sim_pinned("default", tmp_path, build_dir, "tb")

    assert len(captured) == 2
    elab_cmd, sim_cmd = captured
    # Build half: `make -C <bin dir>` (the resolved build root).
    assert elab_cmd[:2] == ["make", "-C"]
    # _run_elab records the bin dir marker for the run-many loop to reuse.
    assert (build_dir / ".booley_edalize_bindir").exists()
    # Run half: the active Python drives verilator_run with no mutation selector.
    assert sim_cmd[:3] == [sys.executable, "-m", "booley.sim.verilator_run"]
    assert not any("MUT_ID" in arg for arg in sim_cmd)
    assert "--top" in sim_cmd and "tb" in sim_cmd
    assert "--trace" not in sim_cmd


def test_sim_runs_every_configured_test_selector(tmp_path: Path, monkeypatch):
    captured: list[list[str]] = []

    def _fake_run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return _fake_proc(rc=0, stdout="[ok]", stderr="")

    _patch_resolve_target(monkeypatch)
    monkeypatch.setattr(
        "booley.specialists.mutation_tester.subprocess.run",
        _fake_run,
    )
    monkeypatch.setattr(
        "booley.specialists.mutation_tester.project_config.TEST_NAMES",
        {"default": ["coremark.elf", "smoke.elf"]},
    )
    monkeypatch.setattr(
        "booley.specialists.mutation_tester.project_config.render_test_selector",
        lambda target, index, name: f"--meminit=ram,{name}",
    )

    endpoint = _make_endpoint(tmp_path, monkeypatch)
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    endpoint._run_elab("default", tmp_path, build_dir)
    runs = endpoint._run_target_test_suite("default", tmp_path, build_dir, "tb")

    sim_cmds = captured[1:]
    assert [run.test_name for run in runs] == ["coremark.elf", "smoke.elf"]
    assert "--plusarg=--meminit=ram,coremark.elf" in sim_cmds[0]
    assert "--plusarg=--meminit=ram,smoke.elf" in sim_cmds[1]


def test_sim_selector_resolves_vlnv_qualified_target(tmp_path: Path, monkeypatch):
    """A VLNV-qualified --target still finds the bare tests.toml section.

    Regression for the raw ``TEST_NAMES.get(target)`` lookup: a qualified
    target (``lib:ip:core#default``) missed the bare ``[default]`` section, so
    no selector was rendered and every mutant ran the TB default test —
    the sweep read "survived" across the board with nothing said.
    """
    captured: list[list[str]] = []

    def _fake_run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return _fake_proc(rc=0, stdout="[ok]", stderr="")

    _patch_resolve_target(monkeypatch)
    monkeypatch.setattr(
        "booley.specialists.mutation_tester.subprocess.run",
        _fake_run,
    )
    monkeypatch.setattr(
        "booley.specialists.mutation_tester.project_config.TEST_NAMES",
        {"default": ["coremark.elf", "smoke.elf"]},
    )
    monkeypatch.setattr(
        "booley.specialists.mutation_tester.project_config.render_test_selector",
        lambda target, index, name: f"--meminit=ram,{name}",
    )

    endpoint = _make_endpoint(tmp_path, monkeypatch)
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    endpoint._run_elab("lib:ip:core#default", tmp_path, build_dir)
    endpoint._run_target_test_suite("lib:ip:core#default", tmp_path, build_dir, "tb")

    sim_cmds = captured[1:]
    assert "--plusarg=--meminit=ram,coremark.elf" in sim_cmds[0]
    assert "--plusarg=--meminit=ram,smoke.elf" in sim_cmds[1]


def test_target_suite_accepts_vlnv_qualified_target(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "booley.specialists.mutation_tester.project_config.TEST_NAMES",
        {"default": ["test_a", "test_b"]},
    )
    endpoint = _make_endpoint(tmp_path, monkeypatch)
    assert endpoint._target_test_suite("lib:ip:core#default").tests == (
        "test_a",
        "test_b",
    )


def test_sim_forwards_project_verdict_sentinels(tmp_path: Path, monkeypatch):
    captured: list[list[str]] = []

    def _fake_run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return _fake_proc(rc=0, stdout="[ok]", stderr="")

    _patch_resolve_target(monkeypatch)
    monkeypatch.setattr(
        "booley.specialists.mutation_tester.subprocess.run",
        _fake_run,
    )
    monkeypatch.setattr(
        "booley.specialists.mutation_tester._resolve_sim_sentinels",
        lambda work_dir: (["Correct operation validated."], ["ERROR!"]),
    )

    endpoint = _make_endpoint(tmp_path, monkeypatch)
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    endpoint._run_elab("default", tmp_path, build_dir)
    endpoint._run_sim_pinned("default", tmp_path, build_dir, "tb")

    sim_cmd = captured[-1]
    assert "--pass-sentinel=Correct operation validated." in sim_cmd
    assert "--fail-sentinel=ERROR!" in sim_cmd


def _prepare_scope_files(tmp_path: Path, scope: list[str]) -> None:
    for rel in scope:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(
                "module dummy;\n"
                + "".join(f"  assign x_{i} = a + b_{i};\n" for i in range(1, 31))
                + "endmodule\n",
                encoding="utf-8",
            )


def _assert_durable_campaign(tmp_path: Path, result: McpToolResult) -> None:
    """Assert that a successful campaign is self-contained and fully published."""
    results_path = tmp_path / result.detail["artifacts"]["results"]
    assert results_path.is_file()
    results = results_path.read_text(encoding="utf-8")
    assert "[rtl/mod_a.sv:2](variants/mutant_1/rtl/mod_a.sv)" in results
    assert "not_detected" in results
    assert "campaign/mutant-logs/mutant_1.log" in results
    assert result.detail["classified"][0]["log"].endswith("campaign/mutant-logs/mutant_1.log")

    manifest_path = tmp_path / result.detail["artifacts"]["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["target"] == "lite"
    assert manifest["eda_tool"] == "verilator"
    assert manifest["baseline"]["status"] == "passed"
    assert len(manifest["mutants"]) == 2
    assert all(row["status"] == "not_detected" for row in manifest["mutants"])
    campaign_dir = manifest_path.parent
    assert all((campaign_dir / row["log"]).is_file() for row in manifest["mutants"])
    assert all((campaign_dir / row["variant"]).is_file() for row in manifest["mutants"])
    assert not any(path.name.endswith(".tmp") for path in manifest_path.parents[1].iterdir())


class TestColdStart:
    def test_happy_path_writes_lock(self, tmp_path: Path, monkeypatch):
        scope = "rtl/mod_a.sv"
        _prepare_scope_files(tmp_path, [scope])
        _write_dut_top(tmp_path)

        specs = _sample_specs(2)
        for spec in specs:
            spec.file = f"./{scope}"
        _patch_invoke_agent(
            monkeypatch,
            [
                FakeAgentResult(output=_sample_creator_json(specs)),
            ],
        )
        _patch_sim_runner(monkeypatch, sim_returncode=0)

        endpoint = _make_endpoint(
            tmp_path,
            monkeypatch,
            scope=scope,
            count=2,
            min_detected=0,
            dut_top_module="design_top",
            dut_files=("rtl/design_top.sv",),
        )
        # Avoid hide_opposite_sources side effects in tests.
        with patch(
            "booley.specialists.mutation_tester.hide_opposite_sources",
            side_effect=lambda *a, **k: _NoopCtx(),
        ):
            result = endpoint._run()

        from booley.dev_support import mutation_lock as lm

        assert lm.load_lock() is not None
        # When the sim returns rc=0, baseline + pinned + sweep all "pass" —
        # which means *nothing* is detected in the sweep.  min_detected=0
        # keeps the run a PASS.
        assert result.exit_code == EXIT_SUCCESS
        assert result.detail["reused_lock"] is False
        assert result.detail["verification_rounds"] == 1
        assert "worktree not clean after sim sweep" not in result.report_text
        variants = result.detail["variant_files"]
        assert variants == [
            {
                "index": 1,
                "path": "reports/mutation_tester/1/campaign/variants/mutant_1/rtl/mod_a.sv",
            },
            {
                "index": 2,
                "path": "reports/mutation_tester/1/campaign/variants/mutant_2/rtl/mod_a.sv",
            },
        ]
        assert all((tmp_path / row["path"]).is_file() for row in variants)
        _assert_durable_campaign(tmp_path, result)
        assert "mutation variant: reports/mutation_tester/1/campaign/variants/" in (
            result.report_text
        )
        assert result.criterion_key == "mutation_score_lite"
        criterion_detail = endpoint.state.criteria[result.criterion_key].detail
        assert criterion_detail["artifacts"]["results"] == result.detail["artifacts"]["results"]
        assert criterion_detail["variant_files"] == variants

    def test_warm_lock_reuses_only_proposals_and_rebuilds_variants(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        scope = "rtl/mod_a.sv"
        _prepare_scope_files(tmp_path, [scope])
        _write_dut_top(tmp_path)
        _patch_invoke_agent(
            monkeypatch,
            [FakeAgentResult(output=_sample_creator_json(_sample_specs(2)))],
        )
        build_calls: list[list[str]] = []
        _patch_resolve_target(monkeypatch)
        original = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            joined = " ".join(cmd) if isinstance(cmd, list) else ""
            if isinstance(cmd, list) and cmd[:2] == ["make", "-C"]:
                build_calls.append(cmd)
                return _fake_proc(rc=0)
            if "booley.sim.verilator_run" in joined:
                return _fake_proc(rc=0)
            return original(cmd, *args, **kwargs)

        monkeypatch.setattr("booley.specialists.mutation_tester.subprocess.run", fake_run)
        endpoint = _make_endpoint(tmp_path, monkeypatch, scope=scope, count=2, min_detected=0)
        with patch(
            "booley.specialists.mutation_tester.hide_opposite_sources",
            side_effect=lambda *args, **kwargs: _NoopCtx(),
        ):
            cold = endpoint._run()
        assert cold.detail["reused_lock"] is False
        cold_builds = len(build_calls)

        monkeypatch.setattr(
            "booley.specialists.specialist.Specialist._invoke_agent_with_resume",
            lambda *_args, **_kwargs: pytest.fail("warm reuse invoked the creator"),
        )
        warm_endpoint = _make_endpoint(tmp_path, monkeypatch, scope=scope, count=2, min_detected=0)
        warm = warm_endpoint._run()

        assert warm.exit_code == EXIT_SUCCESS
        assert warm.detail["reused_lock"] is True
        assert warm.detail["build_cached"] is False
        assert len(build_calls) == cold_builds + 3  # pristine + two isolated variants

    def test_post_rollback_residue_still_warns(self, tmp_path: Path, monkeypatch):
        endpoint = _make_endpoint(tmp_path, monkeypatch)
        result = McpToolResult(exit_code=EXIT_SUCCESS, report_text="summary\n\nRESULT: PASS")
        monkeypatch.setattr(endpoint, "_verify_clean_worktree", lambda _files: False)

        endpoint._add_residue_warning(result, {"rtl/mod_a.sv", "rtl/design_top.sv"})

        assert "worktree not clean after sim sweep\nRESULT: PASS" in result.report_text

    def test_creator_no_specs_errors(self, tmp_path: Path, monkeypatch):
        scope = "rtl/mod_a.sv"
        _prepare_scope_files(tmp_path, [scope])
        _write_dut_top(tmp_path)
        _patch_invoke_agent(monkeypatch, [FakeAgentResult(output="no json here")])
        _patch_sim_runner(monkeypatch, sim_returncode=0)
        endpoint = _make_endpoint(tmp_path, monkeypatch, scope=scope, count=2)
        with patch(
            "booley.specialists.mutation_tester.hide_opposite_sources",
            side_effect=lambda *a, **k: _NoopCtx(),
        ):
            result = endpoint._run()
        assert result.exit_code == EXIT_ERROR

    def test_forbidden_category_eventually_fails(self, tmp_path: Path, monkeypatch):
        scope = "rtl/mod_a.sv"
        _prepare_scope_files(tmp_path, [scope])
        _write_dut_top(tmp_path)
        # All 3 attempts return forbidden categories.
        bad = [
            MutationSpec(
                index=1,
                category="module_instantiation_swap",
                file="rtl/mod_a.sv",
                line=1,
                original_code="a",
                mutated_code="b",
            )
        ]
        bad_json = json.dumps({"mutations": [s.to_dict() for s in bad]})
        _patch_invoke_agent(
            monkeypatch,
            [
                FakeAgentResult(output=bad_json),
            ]
            * 3,
        )
        _patch_sim_runner(monkeypatch, sim_returncode=0)
        endpoint = _make_endpoint(tmp_path, monkeypatch, scope=scope, count=1)
        with patch(
            "booley.specialists.mutation_tester.hide_opposite_sources",
            side_effect=lambda *a, **k: _NoopCtx(),
        ):
            result = endpoint._run()
        assert result.exit_code == EXIT_ERROR


class _NoopCtx:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# QA-11: git rollback net + scope boundary enforcement
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo_with(tmp_path: Path, files: dict[str, str]) -> None:
    """Init a git repo in *tmp_path* and commit *files* (rel path -> content)."""
    _git("init", cwd=tmp_path)
    _git("config", "user.email", "t@example.com", cwd=tmp_path)
    _git("config", "user.name", "Tester", cwd=tmp_path)
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-m", "init", cwd=tmp_path)


class TestGitRollbackNet:
    def test_modified_tracked_lists_only_tracked_changes(self, tmp_path: Path):
        endpoint = _make_endpoint(tmp_path, scope="rtl/mod_a.sv")
        _init_repo_with(tmp_path, {"rtl/mod_a.sv": "module a; endmodule\n"})
        assert endpoint._git_modified_tracked(tmp_path) == set()
        # Modify the tracked file + drop an untracked one.
        (tmp_path / "rtl/mod_a.sv").write_text("module a; wire x; endmodule\n")
        (tmp_path / "rtl/untracked.sv").write_text("module u; endmodule\n")
        modified = endpoint._git_modified_tracked(tmp_path)
        assert modified == {"rtl/mod_a.sv"}  # untracked excluded

    def test_reverts_stray_out_of_scope_tracked_file(self, tmp_path: Path):
        """The QA-11 blast radius: creator strays into a tracked file off-scope."""
        endpoint = _make_endpoint(tmp_path, scope="rtl/mod_a.sv")
        _init_repo_with(
            tmp_path,
            {
                "rtl/mod_a.sv": "module a; endmodule\n",
                "rtl/mod_x.sv": "module x; endmodule\n",
            },
        )
        pre_dirty = endpoint._git_modified_tracked(tmp_path)
        # Simulate the creator dirtying the scope file AND an out-of-scope file.
        (tmp_path / "rtl/mod_a.sv").write_text("module a; /*mut*/ endmodule\n")
        (tmp_path / "rtl/mod_x.sv").write_text(
            "module x; import booley_mut_pkg::*; endmodule\n",
        )
        reverted = endpoint._revert_stray_tracked_edits(
            tmp_path,
            pre_dirty,
            keep=["rtl/mod_a.sv"],
        )
        assert reverted == ["rtl/mod_x.sv"]
        # Stray rolled back; the kept scope file left untouched.
        assert "booley_mut_pkg" not in (tmp_path / "rtl/mod_x.sv").read_text()
        assert "/*mut*/" in (tmp_path / "rtl/mod_a.sv").read_text()

    def test_full_rollback_keep_empty_reverts_everything_this_run_dirtied(
        self,
        tmp_path: Path,
    ):
        endpoint = _make_endpoint(tmp_path, scope="rtl/mod_a.sv")
        _init_repo_with(
            tmp_path,
            {
                "rtl/mod_a.sv": "module a; endmodule\n",
                "rtl/mod_x.sv": "module x; endmodule\n",
            },
        )
        pre_dirty = endpoint._git_modified_tracked(tmp_path)
        (tmp_path / "rtl/mod_a.sv").write_text("module a; changed endmodule\n")
        (tmp_path / "rtl/mod_x.sv").write_text("module x; changed endmodule\n")
        reverted = endpoint._revert_stray_tracked_edits(tmp_path, pre_dirty, keep=[])
        assert reverted == ["rtl/mod_a.sv", "rtl/mod_x.sv"]
        assert "changed" not in (tmp_path / "rtl/mod_a.sv").read_text()
        assert "changed" not in (tmp_path / "rtl/mod_x.sv").read_text()

    def test_preserves_pre_existing_wip(self, tmp_path: Path):
        """Pre-run dirty files (unrelated WIP) are never clobbered by the net."""
        endpoint = _make_endpoint(tmp_path, scope="rtl/mod_a.sv")
        _init_repo_with(
            tmp_path,
            {
                "rtl/mod_a.sv": "module a; endmodule\n",
                "rtl/wip.sv": "module wip; endmodule\n",
            },
        )
        # Pre-existing uncommitted edit — captured as the baseline.
        (tmp_path / "rtl/wip.sv").write_text("module wip; my_wip endmodule\n")
        pre_dirty = endpoint._git_modified_tracked(tmp_path)
        assert "rtl/wip.sv" in pre_dirty
        # The run then strays into mod_a.
        (tmp_path / "rtl/mod_a.sv").write_text("module a; stray endmodule\n")
        reverted = endpoint._revert_stray_tracked_edits(tmp_path, pre_dirty, keep=[])
        assert reverted == ["rtl/mod_a.sv"]
        # WIP survived; stray reverted.
        assert "my_wip" in (tmp_path / "rtl/wip.sv").read_text()
        assert "stray" not in (tmp_path / "rtl/mod_a.sv").read_text()

    def test_no_git_is_graceful_noop(self, tmp_path: Path):
        """Outside a git repo the net degrades to the content snapshot alone."""
        endpoint = _make_endpoint(tmp_path, scope="rtl/mod_a.sv")
        assert endpoint._git_modified_tracked(tmp_path) is None
        assert endpoint._revert_stray_tracked_edits(tmp_path, None, keep=[]) == []


class TestColdScopeEnforcement:
    def test_cold_run_reverts_out_of_scope_creator_edits(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        """End-to-end: a creator that mutates a tracked file OUTSIDE --scope has
        that edit reverted (scope guard + rollback net), leaving a clean tree."""
        scope = "rtl/mod_a.sv"
        _init_repo_with(
            tmp_path,
            {
                "rtl/mod_a.sv": "module mod_a; endmodule\n",
                "rtl/off_scope.sv": "module off_scope; endmodule\n",
                "rtl/design_top.sv": (
                    "module design_top(input logic clk);\n"
                    "  logic [3:0] r;\n"
                    "  always_ff @(posedge clk) r <= r + 1;\n"
                    "endmodule\n"
                ),
            },
        )

        specs = _sample_specs(1)
        specs[0].file = scope

        def _fake_agent(self, params, on_event=None):
            # Creator ignores scope and strays into a DIFFERENT tracked file.
            (tmp_path / "rtl/off_scope.sv").write_text(
                "module off_scope; /* unauthorized edit */ endmodule\n",
                encoding="utf-8",
            )
            self._last_session_id = "fake-sid"
            return FakeAgentResult(output=_sample_creator_json(specs))

        monkeypatch.setattr(
            "booley.specialists.specialist.Specialist._invoke_agent_with_resume",
            _fake_agent,
        )

        # Sim/make fakes, but forward real git so the rollback actually runs.
        _patch_resolve_target(monkeypatch)
        original = subprocess.run

        def _fake_run(cmd, *args, **kwargs):
            joined = " ".join(cmd) if isinstance(cmd, list) else ""
            if "booley.sim.verilator_run" in joined:
                return _fake_proc(rc=0, stdout="[sim] ok")
            if isinstance(cmd, list) and cmd[:2] == ["make", "-C"]:
                return _fake_proc(rc=0, stdout="[make] ok")
            return original(cmd, *args, **kwargs)

        monkeypatch.setattr(
            "booley.specialists.mutation_tester.subprocess.run",
            _fake_run,
        )

        endpoint = _make_endpoint(
            tmp_path,
            monkeypatch,
            scope=scope,
            count=1,
            min_detected=0,
            dut_top_module="design_top",
            dut_files=("rtl/design_top.sv",),
        )
        with patch(
            "booley.specialists.mutation_tester.hide_opposite_sources",
            side_effect=lambda *a, **k: _NoopCtx(),
        ):
            endpoint._run()

        # The stray out-of-scope edit must be gone; the working tree clean.
        assert "unauthorized edit" not in (tmp_path / "rtl/off_scope.sv").read_text()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert status.stdout.strip() == "", f"tree left dirty: {status.stdout!r}"


# ---------------------------------------------------------------------------
# Warm reuse
# ---------------------------------------------------------------------------


_SCOPE_CORE_TEXT = (
    "CAPI=2:\n"
    "name: ::demo_core:0\n"
    "filesets:\n"
    "  rtl:\n"
    "    files:\n"
    "      - rtl/counter_pkg.sv: {file_type: systemVerilogSource}\n"
    "      - rtl/counter.sv: {file_type: systemVerilogSource}\n"
    "  tb:\n"
    "    files:\n"
    "      - tb/tb_counter.sv: {file_type: systemVerilogSource}\n"
    "    tags: [tb]\n"
    "targets:\n"
    "  sim:\n"
    "    default_tool: verilator\n"
    "    flow: sim\n"
    "    flow_options: {tool: verilator}\n"
    "    filesets: [rtl, tb]\n"
    "    toplevel: tb_counter\n"
)


def _author_scope_core(tmp_path: Path) -> None:
    (tmp_path / "design.core").write_text(_SCOPE_CORE_TEXT, encoding="utf-8")
    for rel in ("rtl/counter_pkg.sv", "rtl/counter.sv", "tb/tb_counter.sv"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// stub\n", encoding="utf-8")


class TestValidateScopeAgainstTarget:
    def test_resolved_source_is_accepted(self, tmp_path: Path):
        _author_scope_core(tmp_path)
        endpoint = _make_endpoint(tmp_path, target="sim")
        # rtl/counter.sv IS a resolved source of the sim Target → accepted.
        assert endpoint._validate_scope_against_target(["rtl/counter.sv"]) is None

    def test_stealth_mirror_path_rejected_with_basename_hint(self, tmp_path: Path):
        _author_scope_core(tmp_path)
        endpoint = _make_endpoint(tmp_path, target="sim")
        # The stealth-cores mirror path is NOT a resolved source; its basename
        # (counter.sv) matches rtl/counter.sv, so a "did you mean" hint fires.
        result = endpoint._validate_scope_against_target(
            [".booley_project/cores/rtl/counter.sv"],
        )
        assert result is not None
        assert result.exit_code == 1
        assert "does not match" in result.report_text
        assert "Did you mean 'rtl/counter.sv'" in result.report_text

    def test_fails_open_when_target_unresolvable(self, tmp_path: Path):
        # No .core authored → sim can't resolve → the guard must not block an
        # Interactive-Mode --dut-files run.
        endpoint = _make_endpoint(tmp_path, target="sim")
        assert endpoint._validate_scope_against_target(["anything.sv"]) is None


# ---------------------------------------------------------------------------
# SETUP-F-40 — Cocotb Targets run through the cocotb run-half
# ---------------------------------------------------------------------------


def _patch_cocotb_target(monkeypatch, *, module: str | None, eda_tool: str = "verilator"):
    """Make the .core reads report a Cocotb (or classic) Target."""
    monkeypatch.setattr(
        "booley.fusesoc.fusesoc_registry.target_cocotb_modules",
        lambda work_dir: {"default": module},
    )
    monkeypatch.setattr(
        "booley.fusesoc.fusesoc_registry.target_eda_tools",
        lambda work_dir: {"default": eda_tool},
    )


class TestCocotbSimDispatch:
    def test_cocotb_target_runs_cocotb_run_half(self, tmp_path: Path, monkeypatch):
        """A Cocotb Target must run with Cocotb's module/filter environment."""
        captured: list[list[str]] = []
        monkeypatch.setattr(
            "booley.specialists.mutation_tester.subprocess.run",
            lambda cmd, *a, **k: (captured.append(list(cmd)), _fake_proc(rc=0))[1],
        )
        _patch_resolve_target(monkeypatch)
        _patch_cocotb_target(monkeypatch, module="tb.test_ravenoc")

        endpoint = _make_endpoint(tmp_path, monkeypatch)
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        endpoint._run_elab("default", tmp_path, build_dir)
        endpoint._run_sim_pinned("default", tmp_path, build_dir, "tb")

        sim_cmd = captured[-1]
        assert sim_cmd[:3] == [sys.executable, "-m", "booley.sim.cocotb_run"]
        parsed = parse_cocotb_run_args(sim_cmd[3:])
        assert "--cocotb-module" in sim_cmd and "tb.test_ravenoc" in sim_cmd
        assert parsed.eda_tool == "verilator"
        assert not any("MUT_ID" in arg for arg in sim_cmd)
        # Sentinels do not apply to Cocotb Targets (ADR 0034 decision 6).
        assert not any(c.startswith("--pass-sentinel") for c in sim_cmd)
        assert "--top" not in sim_cmd

    def test_cocotb_run_batches_whole_target_suite(self, tmp_path: Path, monkeypatch):
        captured: list[list[str]] = []
        monkeypatch.setattr(
            "booley.specialists.mutation_tester.subprocess.run",
            lambda cmd, *a, **k: (captured.append(list(cmd)), _fake_proc(rc=0))[1],
        )
        _patch_resolve_target(monkeypatch)
        _patch_cocotb_target(monkeypatch, module="tb.test_noc")
        monkeypatch.setattr(
            "booley.specialists.mutation_tester.project_config.TEST_NAMES",
            {"default": ["test_a", "test_b"]},
        )

        endpoint = _make_endpoint(tmp_path, monkeypatch)
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        endpoint._run_elab("default", tmp_path, build_dir)
        endpoint._run_target_test_suite("default", tmp_path, build_dir, "tb")

        assert "--test=test_a" in captured[-1]
        assert "--test=test_b" in captured[-1]

    def test_individual_test_flag_was_removed(self, tmp_path: Path, monkeypatch):
        with pytest.raises(SystemExit):
            _make_endpoint(tmp_path, monkeypatch, extra_args=["--test", "test_b"])

    def test_classic_target_still_uses_verilator_run(self, tmp_path: Path, monkeypatch):
        captured: list[list[str]] = []
        monkeypatch.setattr(
            "booley.specialists.mutation_tester.subprocess.run",
            lambda cmd, *a, **k: (captured.append(list(cmd)), _fake_proc(rc=0))[1],
        )
        _patch_resolve_target(monkeypatch)
        _patch_cocotb_target(monkeypatch, module=None)

        endpoint = _make_endpoint(tmp_path, monkeypatch)
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        endpoint._run_elab("default", tmp_path, build_dir)
        endpoint._run_sim_pinned("default", tmp_path, build_dir, "tb")

        assert captured[-1][:3] == [sys.executable, "-m", "booley.sim.verilator_run"]

    def test_classic_icarus_target_uses_iverilog_run(self, tmp_path: Path, monkeypatch):
        """F-6: a classic Icarus build must be executed as a vvp image."""
        captured: list[list[str]] = []
        monkeypatch.setattr(
            sys.modules[MutationTesterSpecialist.__module__].subprocess,
            "run",
            lambda cmd, *a, **k: (captured.append(list(cmd)), _fake_proc(rc=0))[1],
        )
        _patch_resolve_target(monkeypatch, eda_tool="icarus")
        _patch_cocotb_target(monkeypatch, module=None, eda_tool="icarus")

        endpoint = _make_endpoint(tmp_path, monkeypatch)
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        endpoint._run_elab("default", tmp_path, build_dir)
        endpoint._run_sim_pinned("default", tmp_path, build_dir, "tb")

        sim_cmd = captured[-1]
        assert sim_cmd[:2] == [sys.executable, "-m"]
        assert sim_cmd[2].endswith(".sim.iverilog_run")
        assert sim_cmd[sim_cmd.index("--build-dir") + 1] == "build"
        assert not any("MUT_ID" in arg for arg in sim_cmd)
        assert "--top" not in sim_cmd

    def test_commercial_cocotb_target_fails_fast_with_reason(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        """Unsupported => a typed error naming the reason, never a silent run."""
        _patch_cocotb_target(monkeypatch, module="tb.test_noc", eda_tool="xcelium")
        endpoint = _make_endpoint(tmp_path, monkeypatch, target="default")

        with pytest.raises(UnsupportedSimTargetError) as exc:
            endpoint.cocotb_target("default", tmp_path)
        assert "icarus and verilator only" in str(exc.value)

    def test_unsupported_target_aborts_before_any_agent_call(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        scope = "rtl/mod_a.sv"
        _prepare_scope_files(tmp_path, [scope])
        _write_dut_top(tmp_path)
        _patch_cocotb_target(monkeypatch, module="tb.test_noc", eda_tool="vcs")
        calls: list[int] = []
        monkeypatch.setattr(
            "booley.specialists.specialist.Specialist._invoke_agent_with_resume",
            lambda self, params, on_event=None: calls.append(1),
        )

        endpoint = _make_endpoint(tmp_path, monkeypatch, target="default", scope=scope, count=1)
        result = endpoint._run()

        assert result.exit_code == EXIT_ERROR
        assert "vcs" in result.report_text
        assert calls == []  # the creator agent was never invoked

    def test_tb_top_not_required_for_cocotb_target(self, tmp_path: Path, monkeypatch):
        _patch_cocotb_target(monkeypatch, module="tb.test_noc")
        endpoint = _make_endpoint(tmp_path, monkeypatch, target="default")
        endpoint.args.tb_top = None
        assert endpoint._validate_interactive_args() is None

    def test_tb_top_still_required_for_classic_target(self, tmp_path: Path, monkeypatch):
        _patch_cocotb_target(monkeypatch, module=None)
        endpoint = _make_endpoint(tmp_path, monkeypatch, target="default")
        endpoint.args.tb_top = None
        result = endpoint._validate_interactive_args()
        assert result is not None
        assert "--tb-top is required" in result.report_text


# ---------------------------------------------------------------------------
# SETUP-F-41 — infra failures are not the creator's fault, and never a kill
# ---------------------------------------------------------------------------

_INFRA_OUT = (
    "ERROR: Verilator executable Vtb not found in build\n"
    "[SIM_INFRA_ERROR] Verilator executable Vtb not found in build\n"
)
