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

from booley.dev_support import mutation_lock as lock_mod
from booley.dev_support.development_state import DevelopmentState
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS, McpToolResult
from booley.specialists.mutation_tester import (
    MutationResult,
    MutationSpec,
    MutationSummary,
    MutationTesterSpecialist,
    UnsupportedSimTargetError,
    VerificationOutcome,
    _extract_json,
    _sanitize_json_text,
    find_forbidden_specs,
    generate_results_markdown,
    generate_specs_markdown,
    parse_creator_output,
)
from tests.conftest import require_symlinks

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_creator_specs_cannot_claim_out_of_scope_files(tmp_path: Path) -> None:
    spec = MutationSpec(
        index=1,
        category="operator_change",
        file="rtl/other.sv",
        line=1,
        original_code="a + b",
        mutated_code="a - b",
    )

    outside = MutationTesterSpecialist._specs_outside_scope([spec], ["rtl/owned.sv"], tmp_path)

    assert outside == [spec]


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


def _make_state(
    tmp_path: Path,
    *,
    dut_top_module: str = "design_top",
    dut_files: tuple[str, ...] = ("rtl/design_top.sv",),
    tb_top_module: str = "design_top_tb",
) -> Path:
    """Create a state file with populated dut_info so cold start can run.

    ADR 0022 dec 12-13: DUT/TB file sets and tb_top_module left DutInfo; the DUT
    files are passed to the endpoint via --dut-files (see ``_make_endpoint``).  Only
    ``dut_top_module`` survives on state.  The ``dut_files``/``tb_top_module``
    kwargs are retained so callers stay unchanged.
    """
    state_file = tmp_path / "state.json"
    state = DevelopmentState.load(state_file)
    state.dut_info.dut_top_module = dut_top_module
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
    state_file = _make_state(
        tmp_path,
        dut_top_module=dut_top_module,
        dut_files=dut_files,
        tb_top_module=tb_top,
    )
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
            line=40 + i,
            original_code=f"a + b_{i}",
            mutated_code=f"a - b_{i}",
            detectability_argument="dx",
            mut_id=i,
        )
        for i in range(1, n + 1)
    ]


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
# Forbidden categories
# ---------------------------------------------------------------------------


class TestForbiddenCategories:
    def test_safe_pass(self):
        specs = _sample_specs(3, category="operator_change")
        assert find_forbidden_specs(specs) == []

    def test_module_swap_rejected(self):
        specs = [
            MutationSpec(
                index=1,
                category="module_instantiation_swap",
                file="x.sv",
                line=1,
                original_code="a",
                mutated_code="b",
            )
        ]
        bad = find_forbidden_specs(specs)
        assert len(bad) == 1

    def test_polarity_normalization(self):
        # Hyphenated / mixed-case variants should normalize to the canonical
        # underscore form before lookup.
        specs = [
            MutationSpec(
                index=1,
                category="Clock-Polarity",
                file="x.sv",
                line=1,
                original_code="a",
                mutated_code="b",
            )
        ]
        assert len(find_forbidden_specs(specs)) == 1

    def test_sensitivity_list_rejected(self):
        specs = [
            MutationSpec(
                index=1,
                category="sensitivity_list",
                file="x.sv",
                line=1,
                original_code="a",
                mutated_code="b",
            )
        ]
        assert len(find_forbidden_specs(specs)) == 1


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
        # New flow: harness owns package + reader, agent must not touch them.
        assert "booley_mut_pkg" in p
        assert "MUT_ID" in p
        assert "harness" in p.lower()

    def test_no_revert_instruction(self, tmp_path: Path):
        endpoint = _make_endpoint(tmp_path)
        p = endpoint._build_creator_prompt()
        # Old flow had "Always revert"; new flow says DO NOT revert.
        assert "DO NOT revert" in p

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
            reason="elaboration of muxed RTL failed",
        )
        p = endpoint._build_retry_prompt(outcome)
        assert "Do **not** return JSON again" in p

    def test_zero_detection_path_asks_for_new_json(self):
        summary = MutationSummary(
            specs=_sample_specs(1),
            results=[MutationResult(index=1, detected=False)],
        )
        p = MutationTesterSpecialist._build_zero_detection_retry_prompt(
            summary,
            min_detected=1,
        )
        # SETUP-F-39: "zero mutations" read as "no mutations were applied"; the
        # prompt must say zero *killed* out of N applied.
        assert "ran all 1 mutation(s)" in p
        assert "killed 0 of them" in p
        assert "runtime mutation" in p
        assert "fresh JSON mutation spec list" in p

    def test_zero_detection_prompt_reports_the_missing_evidence(self):
        summary = MutationSummary(
            specs=_sample_specs(2),
            results=[MutationResult(index=1), MutationResult(index=2)],
        )
        p = MutationTesterSpecialist._build_zero_detection_retry_prompt(
            summary,
            min_detected=2,
            evidence={
                "muxes_found": 1,
                "muxes_missing": [2],
                "selector_observed": [],
                "mutations_applied": False,
            },
        )
        assert "no guard for mutation(s) #2" in p
        assert "never echoed +MUT_ID" in p


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
            mutated_rtl_paths={spec.file: "mutated-rtl/rtl/mod (wide).sv"},
        )

        assert "[rtl/mod (wide).sv:12](mutated-rtl/rtl/mod%20%28wide%29.sv)" in md
        assert "`a \\| b` → `a & b`" in md
        assert "not_detected" in md


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


def test_elab_builds_once_and_sim_runs_verilator_binary(tmp_path: Path, monkeypatch):
    """Unit A.3: _run_elab does `make` (build once), _run_sim_pinned runs the
    prebuilt V<top> via verilator_run with the active python + +MUT_ID."""
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
    endpoint._run_sim_pinned("default", tmp_path, build_dir, "tb", mut_id=3)

    assert len(captured) == 2
    elab_cmd, sim_cmd = captured
    # Build half: `make -C <bin dir>` (the resolved build root).
    assert elab_cmd[:2] == ["make", "-C"]
    # _run_elab records the bin dir marker for the run-many loop to reuse.
    assert (build_dir / ".booley_edalize_bindir").exists()
    # Run half: the active python drives verilator_run with +MUT_ID=3, no trace.
    assert sim_cmd[:3] == [sys.executable, "-m", "booley.sim.verilator_run"]
    assert "--plusarg" in sim_cmd and "MUT_ID=3" in sim_cmd
    assert "--top" in sim_cmd and "tb" in sim_cmd
    assert "--trace" not in sim_cmd


def test_sim_uses_first_configured_test_selector(tmp_path: Path, monkeypatch):
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
    endpoint._run_sim_pinned("default", tmp_path, build_dir, "tb", mut_id=1)

    sim_cmd = captured[-1]
    assert "--plusarg=--meminit=ram,coremark.elf" in sim_cmd


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
    endpoint._run_sim_pinned("lib:ip:core#default", tmp_path, build_dir, "tb", mut_id=1)

    sim_cmd = captured[-1]
    assert "--plusarg=--meminit=ram,coremark.elf" in sim_cmd


def test_selected_test_accepts_vlnv_qualified_target(tmp_path: Path, monkeypatch):
    """--test validation must see the qualified Target's declared tests."""
    monkeypatch.setattr(
        "booley.specialists.mutation_tester.project_config.TEST_NAMES",
        {"default": ["test_a", "test_b"]},
    )
    endpoint = _make_endpoint(tmp_path, monkeypatch, extra_args=["--test", "test_b"])
    assert endpoint._selected_test("lib:ip:core#default") == "test_b"


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
    endpoint._run_sim_pinned("default", tmp_path, build_dir, "tb", mut_id=1)

    sim_cmd = captured[-1]
    assert "--pass-sentinel=Correct operation validated." in sim_cmd
    assert "--fail-sentinel=ERROR!" in sim_cmd


def _prepare_scope_files(tmp_path: Path, scope: list[str]) -> None:
    for rel in scope:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(
                "module dummy;\n  initial $finish;\nendmodule\n",
                encoding="utf-8",
            )


class TestWorktreeCleanup:
    def test_restores_ignored_scope_file_bytes(self, tmp_path: Path):
        scope = "rtl/benchmark_copilot_perf_counters.sv"
        path = tmp_path / scope
        path.parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / ".gitignore").write_text("rtl/benchmark_*\n", encoding="utf-8")
        original = "module benchmark_copilot_perf_counters;\n  logic clean_counter;\nendmodule\n"
        path.write_text(original, encoding="utf-8")

        endpoint = _make_endpoint(
            tmp_path,
            scope=scope,
            dut_top_module="benchmark_copilot_perf_counters",
            dut_files=(scope,),
        )
        cleanup_files = endpoint._cleanup_file_set([scope], tmp_path)
        snapshot = endpoint._snapshot_worktree_files(cleanup_files, tmp_path)

        path.write_text(
            "module benchmark_copilot_perf_counters;\n"
            "  assign x = booley_mut_pkg::mut_id;\n"
            "endmodule\n",
            encoding="utf-8",
        )

        endpoint._restore_worktree_snapshot(snapshot)

        assert cleanup_files == [scope]
        assert path.read_text(encoding="utf-8") == original

    def test_scope_and_cleanup_resolve_core_source_symlink(self, tmp_path: Path):
        """A Target resolution link names the same tracked file Git reports."""
        require_symlinks(tmp_path)
        real = tmp_path / "picorv32.v"
        real.write_text("module picorv32; endmodule\n", encoding="utf-8")
        core_dir = tmp_path / ".booley_project" / "cores"
        core_dir.mkdir(parents=True)
        alias = core_dir / "picorv32.v"
        alias.symlink_to("../../picorv32.v")
        alias_rel = ".booley_project/cores/picorv32.v"
        endpoint = _make_endpoint(
            tmp_path,
            scope=alias_rel,
            dut_top_module="picorv32",
            dut_files=(alias_rel,),
        )

        assert endpoint._scope_files() == ["picorv32.v"]
        assert endpoint._dut_files() == ["picorv32.v"]
        keep = endpoint._cleanup_file_set([alias_rel], tmp_path)
        assert keep == ["picorv32.v"]

        _init_repo_with(tmp_path, {"picorv32.v": "module picorv32; endmodule\n"})
        pre_dirty = endpoint._git_modified_tracked(tmp_path)
        real.write_text("module picorv32; /* mutation */ endmodule\n", encoding="utf-8")

        assert endpoint._revert_stray_tracked_edits(tmp_path, pre_dirty, keep) == []
        assert "/* mutation */" in real.read_text(encoding="utf-8")

    def test_removes_file_created_by_mutation_phase(self, tmp_path: Path):
        scope = "rtl/benchmark_generated_mutant.sv"
        path = tmp_path / scope
        endpoint = _make_endpoint(tmp_path, scope=scope)
        snapshot = endpoint._snapshot_worktree_files([scope], tmp_path)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "module benchmark_generated_mutant;\n  assign x = booley_mut_pkg::mut_id;\nendmodule\n",
            encoding="utf-8",
        )

        endpoint._restore_worktree_snapshot(snapshot)

        assert not path.exists()


class TestBuildInputHashes:
    def test_uses_configured_roots_and_include_files(self, tmp_path: Path):
        # ADR 0026: build-input roots come from the .core filesets. An RTL
        # source dir (hw/), an RTL include-header dir (inc/, is_include_file),
        # and a tb-tagged source dir (checks/) are the elaboration inputs. A
        # booley.toml still exists so the config-file hash tracks it.
        (tmp_path / "booley.toml").write_text("# project config\n", encoding="utf-8")
        (tmp_path / "design.core").write_text(
            "CAPI=2:\n"
            "name: ::demo\n"
            "filesets:\n"
            "  rtl:\n"
            "    files:\n"
            "      - hw/mod.sv: {file_type: systemVerilogSource}\n"
            "      - inc/defs.svh: {is_include_file: true}\n"
            "  tb: {files: [checks/tb.sv], tags: [tb]}\n"
            "targets:\n"
            "  sim: {filesets: [rtl, tb], toplevel: tb}\n",
            encoding="utf-8",
        )
        for rel in ("hw/mod.sv", "inc/defs.svh", "checks/tb.sv"):
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"// {rel}\n", encoding="utf-8")
        ignored = tmp_path / "rtl" / "old.sv"
        ignored.parent.mkdir(parents=True, exist_ok=True)
        ignored.write_text("// should not be hashed\n", encoding="utf-8")

        endpoint = _make_endpoint(tmp_path, scope="hw/mod.sv", dut_files=("hw/mod.sv",))
        hashes = endpoint._build_input_hashes(
            work_dir=tmp_path,
            target="lite",
            scope_files=["hw/mod.sv"],
            muxed_hashes={"hw/mod.sv": "sha256:mux"},
        )

        assert "hw/mod.sv" in hashes
        assert "inc/defs.svh" in hashes
        assert "checks/tb.sv" in hashes
        assert "rtl/old.sv" not in hashes
        assert "__config_file__:booley.toml" in hashes


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
        mutated = result.detail["mutated_rtl_files"]
        assert mutated == [
            {
                "source": scope,
                "path": "reports/mutation_tester/1/mutated-rtl/rtl/mod_a.sv",
            }
        ]
        assert (tmp_path / mutated[0]["path"]).is_file()
        results_path = tmp_path / result.detail["artifacts"]["results"]
        assert results_path.is_file()
        results = results_path.read_text(encoding="utf-8")
        assert "[rtl/mod_a.sv:41](mutated-rtl/rtl/mod_a.sv)" in results
        assert "not_detected" in results
        assert "mutated RTL: reports/mutation_tester/1/mutated-rtl/rtl/mod_a.sv" in (
            result.report_text
        )
        criterion_detail = endpoint.state.criteria["mutation_score"].detail
        assert criterion_detail["artifacts"]["results"] == result.detail["artifacts"]["results"]
        assert criterion_detail["mutated_rtl_files"] == mutated

    def test_post_rollback_residue_still_warns(self, tmp_path: Path, monkeypatch):
        endpoint = _make_endpoint(tmp_path, monkeypatch)
        result = McpToolResult(exit_code=EXIT_SUCCESS, report_text="summary\n\nRESULT: PASS")
        monkeypatch.setattr(endpoint, "_verify_clean_worktree", lambda _files: False)

        endpoint._add_residue_warning(result, {"rtl/mod_a.sv", "rtl/design_top.sv"})

        assert "worktree not clean after sim sweep\nRESULT: PASS" in result.report_text

    def test_cold_start_restores_creator_mutated_ignored_rtl(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        scope = "rtl/benchmark_copilot_perf_counters.sv"
        path = tmp_path / scope
        path.parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / ".gitignore").write_text("rtl/benchmark_*\n", encoding="utf-8")
        original = (
            "module benchmark_copilot_perf_counters(input logic clk);\n"
            "  logic [3:0] clean_counter;\n"
            "  always_ff @(posedge clk) clean_counter <= clean_counter + 1;\n"
            "endmodule\n"
        )
        path.write_text(original, encoding="utf-8")

        specs = _sample_specs(1)
        specs[0].file = scope

        def _fake_agent(self, params, on_event=None):
            path.write_text(
                "module benchmark_copilot_perf_counters(input logic clk);\n"
                "  assign x = booley_mut_pkg::mut_id;\n"
                "endmodule\n",
                encoding="utf-8",
            )
            self._last_session_id = "fake-sid"
            return FakeAgentResult(output=_sample_creator_json(specs))

        monkeypatch.setattr(
            "booley.specialists.specialist.Specialist._invoke_agent_with_resume",
            _fake_agent,
        )
        _patch_sim_runner(monkeypatch, sim_returncode=0)

        endpoint = _make_endpoint(
            tmp_path,
            monkeypatch,
            scope=scope,
            count=1,
            min_detected=0,
            dut_top_module="benchmark_copilot_perf_counters",
            dut_files=(scope,),
        )
        with patch(
            "booley.specialists.mutation_tester.hide_opposite_sources",
            side_effect=lambda *a, **k: _NoopCtx(),
        ):
            result = endpoint._run()

        assert result.exit_code == EXIT_SUCCESS
        assert path.read_text(encoding="utf-8") == original

    def test_zero_detection_retries_creator_before_accepting_lock(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        scope = "rtl/mod_a.sv"
        _prepare_scope_files(tmp_path, [scope])
        _write_dut_top(tmp_path)

        specs = _sample_specs(1)
        prompts: list[str] = []
        agent_results = [
            FakeAgentResult(output=_sample_creator_json(specs)),
            FakeAgentResult(output=_sample_creator_json(specs)),
        ]

        def _fake_agent(self, params, on_event=None):
            prompts.append(params.prompt)
            result = agent_results[min(len(prompts) - 1, len(agent_results) - 1)]
            self._last_session_id = result.session_id
            return result

        monkeypatch.setattr(
            "booley.specialists.specialist.Specialist._invoke_agent_with_resume",
            _fake_agent,
        )

        mut_one_calls = {"count": 0}
        original = subprocess.run
        _patch_resolve_target(monkeypatch)

        def _fake_run(cmd, *args, **kwargs):
            joined = " ".join(cmd) if isinstance(cmd, list) else ""
            if "booley.sim.verilator_run" in joined:
                if "MUT_ID=0" in cmd:
                    return _fake_proc(rc=0, stdout="[baseline] ok", stderr="")
                if "MUT_ID=1" in cmd:
                    mut_one_calls["count"] += 1
                    # Round 1 pinned + sweep both pass, proving zero detected.
                    # Round 2 pinned/sweep fail the TB, proving a live mutant.
                    rc = 0 if mut_one_calls["count"] <= 2 else 1
                    return _fake_proc(rc=rc, stdout=f"[mut1] rc={rc}", stderr="")
            if isinstance(cmd, list) and cmd[:2] == ["make", "-C"]:
                return _fake_proc(rc=0, stdout="[elab] ok", stderr="")
            if isinstance(cmd, list) and cmd[:2] == ["git", "checkout"]:
                return _fake_proc(rc=0)
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
            min_detected=1,
            dut_top_module="design_top",
            dut_files=("rtl/design_top.sv",),
        )
        with patch(
            "booley.specialists.mutation_tester.hide_opposite_sources",
            side_effect=lambda *a, **k: _NoopCtx(),
        ):
            result = endpoint._run()

        assert result.exit_code == EXIT_SUCCESS
        assert result.detail["detected"] == 1
        assert result.detail["verification_rounds"] == 2
        assert len(prompts) == 2
        # The scope file carries no ``mut_id == 1`` guard, so the harness cannot
        # confirm the mutation was live — the creator IS the right thing to
        # re-prompt here (contrast the coverage-gap test below).
        assert "killed 0 of them" in prompts[1]

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


class TestCoverageGapDiagnosis:
    """SETUP-F-38: 0 killed is only the creator's fault when the muxes are dead.

    With every mux in the source and the design echoing its ``+MUT_ID``, a
    0-killed sweep means the Target's tests never exercise the scope — the
    run must say so and stop, not re-prompt the creator for three paid rounds.
    """

    MUXED_SOURCE = (
        "module dummy;\n"
        "  logic x;\n"
        "  assign x = (booley_mut_pkg::mut_id == 1) ? 1'b0 : 1'b1;\n"
        "  initial $finish;\n"
        "endmodule\n"
    )

    def _run_zero_kill_cold_start(
        self,
        tmp_path: Path,
        monkeypatch,
        *,
        muxed_source: str,
        echo_selector: bool,
    ) -> tuple[Any, list[str]]:
        """Cold start where every mutant run passes (nothing is ever killed)."""
        scope = "rtl/mod_a.sv"
        _prepare_scope_files(tmp_path, [scope])
        _write_dut_top(tmp_path)
        specs = _sample_specs(1)
        prompts: list[str] = []

        def _fake_agent(self, params, on_event=None):
            prompts.append(params.prompt)
            (tmp_path / scope).write_text(muxed_source, encoding="utf-8")
            self._last_session_id = "fake-sid"
            return FakeAgentResult(output=_sample_creator_json(specs))

        monkeypatch.setattr(
            "booley.specialists.specialist.Specialist._invoke_agent_with_resume",
            _fake_agent,
        )
        _patch_resolve_target(monkeypatch)
        original = subprocess.run
        echo = "[booley_mut] MUT_ID=1 active\n" if echo_selector else ""

        def _fake_run(cmd, *args, **kwargs):
            joined = " ".join(cmd) if isinstance(cmd, list) else ""
            if "booley.sim.verilator_run" in joined:
                if "MUT_ID=0" in cmd:
                    return _fake_proc(rc=0, stdout="[baseline] ok")
                return _fake_proc(rc=0, stdout=f"{echo}[mut1] sim PASSED")
            if isinstance(cmd, list) and cmd[:2] == ["make", "-C"]:
                return _fake_proc(rc=0, stdout="[elab] ok")
            if isinstance(cmd, list) and cmd[:2] == ["git", "checkout"]:
                return _fake_proc(rc=0)
            return original(cmd, *args, **kwargs)

        monkeypatch.setattr("booley.specialists.mutation_tester.subprocess.run", _fake_run)

        endpoint = _make_endpoint(
            tmp_path,
            monkeypatch,
            scope=scope,
            count=1,
            min_detected=1,
            dut_top_module="design_top",
            dut_files=("rtl/design_top.sv",),
        )
        with patch(
            "booley.specialists.mutation_tester.hide_opposite_sources",
            side_effect=lambda *a, **k: _NoopCtx(),
        ):
            return endpoint._run(), prompts

    def test_live_mutations_zero_kills_terminates_as_coverage_gap(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        result, prompts = self._run_zero_kill_cold_start(
            tmp_path,
            monkeypatch,
            muxed_source=self.MUXED_SOURCE,
            echo_selector=True,
        )

        # Graded FAIL (the threshold really was missed), not a creator ERROR.
        assert result.exit_code == EXIT_FAILURE
        assert result.detail["coverage_gap"] is True
        assert result.detail["diagnosis"] == "scope_not_covered_by_target_tests"
        assert result.detail["detected"] == 0
        assert result.detail["evidence"]["mutations_applied"] is True
        # One creator round only — no paid retries on a foredoomed run.
        assert len(prompts) == 1
        assert result.detail["verification_rounds"] == 1
        assert "not covered by the Target's tests" in result.report_text
        assert "--min-detected" in result.report_text

    def test_dead_muxes_still_re_prompt_the_creator(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        # No ``mut_id == 1`` guard anywhere: the harness cannot claim the
        # mutation was live, so blaming the creator is correct here.
        result, prompts = self._run_zero_kill_cold_start(
            tmp_path,
            monkeypatch,
            muxed_source="module dummy;\n  initial $finish;\nendmodule\n",
            echo_selector=True,
        )

        assert result.exit_code == EXIT_ERROR
        assert len(prompts) == MutationTesterSpecialist.MAX_VERIFICATION_ROUNDS
        assert result.detail.get("coverage_gap") is not True

    def test_selector_never_echoed_is_not_a_coverage_gap(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        # Muxes are in the source but the design never saw +MUT_ID — that is a
        # broken plusarg path, not a testbench gap.
        result, prompts = self._run_zero_kill_cold_start(
            tmp_path,
            monkeypatch,
            muxed_source=self.MUXED_SOURCE,
            echo_selector=False,
        )

        assert result.exit_code == EXIT_ERROR
        assert len(prompts) == MutationTesterSpecialist.MAX_VERIFICATION_ROUNDS
        assert "never echoed +MUT_ID" in result.detail["log_tail"]

    def test_failed_run_detail_carries_the_tally_and_mutation_list(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        # SETUP-F-39: a failed run used to persist ``detail: {}``.
        result, _prompts = self._run_zero_kill_cold_start(
            tmp_path,
            monkeypatch,
            muxed_source="module dummy;\n  initial $finish;\nendmodule\n",
            echo_selector=True,
        )

        assert result.detail["failed"] is True
        assert result.detail["phase"] == "cold_verification"
        assert result.detail["detected"] == 0
        assert result.detail["not_detected"] == 1
        assert [m["index"] for m in result.detail["mutations"]] == [1]
        assert result.detail["classified"][0]["status"] == "not_detected"

    def test_creator_no_specs_failure_still_reports_detail(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
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
        assert result.detail["failed"] is True
        assert result.detail["phase"] == "creator_output"
        assert result.detail["mutations"] == []


class TestMutationEvidence:
    def _endpoint(self, tmp_path: Path) -> MutationTesterSpecialist:
        _prepare_scope_files(tmp_path, ["rtl/mod_a.sv"])
        _write_dut_top(tmp_path)
        return _make_endpoint(
            tmp_path,
            scope="rtl/mod_a.sv",
            dut_files=("rtl/design_top.sv",),
        )

    @pytest.mark.parametrize(
        "guard",
        ["mut_id == 3", "booley_mut_pkg::mut_id  ===  3", "mut_id == 32'd3"],
    )
    def test_recognises_every_sanctioned_guard_form(self, tmp_path: Path, guard: str):
        endpoint = self._endpoint(tmp_path)
        (tmp_path / "rtl/mod_a.sv").write_text(
            f"module dummy;\n  assign x = ({guard}) ? 0 : 1;\nendmodule\n",
            encoding="utf-8",
        )
        spec = MutationSpec(
            index=3,
            category="operator_change",
            file="rtl/mod_a.sv",
            line=2,
            original_code="a",
            mutated_code="b",
            mut_id=3,
        )
        summary = MutationSummary(
            specs=[spec],
            results=[MutationResult(index=3, selector_observed=True)],
        )

        evidence = endpoint._mutation_evidence(
            [spec],
            summary,
            ["rtl/mod_a.sv"],
            tmp_path,
        )

        assert evidence["muxes_missing"] == []
        assert evidence["mutations_applied"] is True

    def test_neighbouring_index_does_not_count_as_a_guard(self, tmp_path: Path):
        endpoint = self._endpoint(tmp_path)
        (tmp_path / "rtl/mod_a.sv").write_text(
            "module dummy;\n  assign x = (mut_id == 30) ? 0 : 1;\nendmodule\n",
            encoding="utf-8",
        )
        specs = _sample_specs(3)[2:]  # index/mut_id 3
        summary = MutationSummary(
            specs=specs,
            results=[MutationResult(index=3, selector_observed=True)],
        )

        evidence = endpoint._mutation_evidence(specs, summary, ["rtl/mod_a.sv"], tmp_path)

        assert evidence["muxes_missing"] == [3]
        assert evidence["mutations_applied"] is False

    def test_a_single_echo_does_not_vouch_for_the_whole_sweep(self, tmp_path: Path):
        # Every mux is in the source, but only mutant #1 ever echoed: the other
        # two died on [SIM_INFRA_ERROR] and were graded invalid, so they never
        # reached the design.  Certifying a coverage gap off one run would put
        # the wrong diagnosis on a sweep that simply fell over mid-way.
        specs = _sample_specs(3)
        endpoint = self._endpoint(tmp_path)
        (tmp_path / "rtl/mod_a.sv").write_text(
            "module dummy;\n"
            + "".join(f"  assign x{i} = (mut_id == {i}) ? 0 : 1;\n" for i in (1, 2, 3))
            + "endmodule\n",
            encoding="utf-8",
        )
        summary = MutationSummary(
            specs=specs,
            results=[
                MutationResult(index=1, selector_observed=True),
                MutationResult(index=2, invalid=True, sim_output_snippet="sim infra error"),
                MutationResult(index=3, invalid=True, sim_output_snippet="sim infra error"),
            ],
        )

        evidence = endpoint._mutation_evidence(specs, summary, ["rtl/mod_a.sv"], tmp_path)

        assert evidence["muxes_missing"] == []
        assert evidence["selector_observed"] == [1]
        assert evidence["mutations_applied"] is False
        # ...and the verdict that rides on it stays off, even though the one
        # surviving run makes this look like a 0-killed sweep.
        assert endpoint._is_coverage_gap(summary, min_detected=1, evidence=evidence) is False

    def test_every_mutant_echoing_still_certifies_the_gap(self, tmp_path: Path):
        specs = _sample_specs(3)
        endpoint = self._endpoint(tmp_path)
        (tmp_path / "rtl/mod_a.sv").write_text(
            "module dummy;\n"
            + "".join(f"  assign x{i} = (mut_id == {i}) ? 0 : 1;\n" for i in (1, 2, 3))
            + "endmodule\n",
            encoding="utf-8",
        )
        summary = MutationSummary(
            specs=specs,
            results=[MutationResult(index=i, selector_observed=True) for i in (1, 2, 3)],
        )

        evidence = endpoint._mutation_evidence(specs, summary, ["rtl/mod_a.sv"], tmp_path)

        assert evidence["selector_observed"] == [1, 2, 3]
        assert evidence["mutations_applied"] is True
        assert endpoint._is_coverage_gap(summary, min_detected=1, evidence=evidence) is True


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
            # Creator ignores scope and strays into a DIFFERENT tracked file
            # (the QA-11 defect), leaving muxes with no package definition.
            (tmp_path / "rtl/off_scope.sv").write_text(
                "module off_scope;\n"
                "  assign x = (booley_mut_pkg::mut_id == 1) ? 0 : 1;\n"
                "endmodule\n",
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
        assert "booley_mut_pkg" not in (tmp_path / "rtl/off_scope.sv").read_text()
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


class TestWarmReuse:
    def _prepare_valid_lock(self, tmp_path: Path, scope: list[str]) -> Any:
        from booley.dev_support import mutation_lock as lm

        # Write scope files first so we can hash them.
        _prepare_scope_files(tmp_path, scope)
        _write_dut_top(tmp_path)
        hashes = lm.compute_scope_hashes(scope, tmp_path)
        # Copy each scope file into the lock dir as its muxed counterpart.
        ld = lm.lock_dir()
        ld.mkdir(parents=True, exist_ok=True)
        for rel in scope:
            muxed_path = lm.muxed_path(rel)
            muxed_path.parent.mkdir(parents=True, exist_ok=True)
            muxed_path.write_text(
                (tmp_path / rel).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        specs = _sample_specs(2)
        meta = lm.LockMeta(
            schema_version=lm.LOCK_SCHEMA_VERSION,
            created_at=lm.now_iso(),
            scope=list(scope),
            scope_hashes=hashes,
            count=len(specs),
            host_file=scope[0],
            mutations=[s.to_dict() for s in specs],
            muxed_files=[lm.muxed_path(r).name for r in scope],
            pkg_file=lm.MUT_PKG_FILENAME,
            docker_digest="sha256:test",
        )
        lm.save_lock(meta)
        # Pre-seed build_meta so the warm path skips rebuild.
        muxed_hashes = {r: lm._hash_file(lm.muxed_path(r)) for r in scope}
        # Patch get_docker_digest via the docker_digest field stored above.
        return muxed_hashes

    def test_warm_reuse_skip_rebuild(self, tmp_path: Path, monkeypatch):
        scope = ["rtl/mod_a.sv"]
        # Set logs dir up front so the lock-prep helper writes into tmp.
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
        (tmp_path / "logs").mkdir(exist_ok=True)
        muxed_hashes = self._prepare_valid_lock(tmp_path, scope)
        from booley.dev_support import mutation_lock as lm

        # Force a known digest so cache validity check passes deterministically.
        monkeypatch.setattr(lm, "get_docker_digest", lambda: "sha256:test")
        monkeypatch.setattr(
            MutationTesterSpecialist,
            "_build_input_hashes",
            lambda self, **kwargs: {"inputs": "same"},
        )
        lm.save_build_meta(muxed_hashes, "sha256:test", {"inputs": "same"})

        _patch_sim_runner(monkeypatch, sim_returncode=0)
        # The cold path's _invoke_agent must not run in warm reuse — assert
        # it isn't called.
        invoked = {"count": 0}

        def _fake_agent(self, *a, **k):
            invoked["count"] += 1
            return FakeAgentResult()

        monkeypatch.setattr(
            "booley.specialists.specialist.Specialist._invoke_agent_with_resume",
            _fake_agent,
        )

        endpoint = _make_endpoint(
            tmp_path,
            monkeypatch,
            scope=",".join(scope),
            count=2,
            min_detected=0,
        )
        with patch(
            "booley.specialists.mutation_tester.hide_opposite_sources",
            side_effect=lambda *a, **k: _NoopCtx(),
        ):
            result = endpoint._run()

        assert invoked["count"] == 0  # no agent during warm reuse
        assert result.detail["reused_lock"] is True
        assert result.detail["build_cached"] is True
        assert result.exit_code == EXIT_SUCCESS

    def test_warm_reuse_rebuilds_when_build_inputs_change(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        scope = ["rtl/mod_a.sv"]
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
        (tmp_path / "logs").mkdir(exist_ok=True)
        muxed_hashes = self._prepare_valid_lock(tmp_path, scope)
        from booley.dev_support import mutation_lock as lm

        monkeypatch.setattr(lm, "get_docker_digest", lambda: "sha256:test")
        monkeypatch.setattr(
            MutationTesterSpecialist,
            "_build_input_hashes",
            lambda self, **kwargs: {"tb/verifier.sv": "sha256:new"},
        )
        lm.save_build_meta(
            muxed_hashes,
            "sha256:test",
            {"tb/verifier.sv": "sha256:old"},
        )

        calls = {"elab": 0, "sim": 0}
        original = subprocess.run
        _patch_resolve_target(monkeypatch)

        def _fake(cmd, *args, **kwargs):
            joined = " ".join(cmd) if isinstance(cmd, list) else ""
            if "booley.sim.verilator_run" in joined:
                calls["sim"] += 1
                return _fake_proc(rc=0, stdout="[sim] ok", stderr="")
            if isinstance(cmd, list) and cmd[:2] == ["make", "-C"]:
                calls["elab"] += 1
                return _fake_proc(rc=0, stdout="[make] ok", stderr="")
            if isinstance(cmd, list) and cmd[:2] == ["git", "checkout"]:
                return _fake_proc(rc=0)
            return original(cmd, *args, **kwargs)

        monkeypatch.setattr(
            "booley.specialists.mutation_tester.subprocess.run",
            _fake,
        )
        monkeypatch.setattr(
            "booley.specialists.specialist.Specialist._invoke_agent_with_resume",
            lambda *a, **k: FakeAgentResult(),
        )

        endpoint = _make_endpoint(
            tmp_path,
            monkeypatch,
            scope=",".join(scope),
            count=2,
            min_detected=0,
        )
        with patch(
            "booley.specialists.mutation_tester.hide_opposite_sources",
            side_effect=lambda *a, **k: _NoopCtx(),
        ):
            result = endpoint._run()

        assert calls["elab"] == 1
        assert calls["sim"] > 0
        assert result.detail["reused_lock"] is True
        assert result.detail["build_cached"] is False
        assert result.exit_code == EXIT_SUCCESS

    def test_regen_lock_wipes_and_falls_through_to_cold(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        scope = ["rtl/mod_a.sv"]
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
        (tmp_path / "logs").mkdir(exist_ok=True)
        self._prepare_valid_lock(tmp_path, scope)
        from booley.dev_support import mutation_lock as lm

        assert lm.load_lock() is not None

        specs = _sample_specs(1)
        _patch_invoke_agent(
            monkeypatch,
            [
                FakeAgentResult(output=_sample_creator_json(specs)),
            ],
        )
        _patch_sim_runner(monkeypatch, sim_returncode=0)
        monkeypatch.setattr(lm, "get_docker_digest", lambda: "sha256:test")

        endpoint = _make_endpoint(
            tmp_path,
            monkeypatch,
            scope=",".join(scope),
            count=1,
            min_detected=0,
            regen_lock=True,
        )
        with patch(
            "booley.specialists.mutation_tester.hide_opposite_sources",
            side_effect=lambda *a, **k: _NoopCtx(),
        ):
            result = endpoint._run()
        # Cold start succeeds → exit success and reused_lock=False.
        assert result.exit_code == EXIT_SUCCESS
        assert result.detail["reused_lock"] is False


# ---------------------------------------------------------------------------
# _validate_scope_against_target — reject a --scope that isn't a Target source
# ---------------------------------------------------------------------------

# A real .core so target_source_files('sim') resolves. Mirrors the sim Target
# shape from tests/test_fusesoc_registry.py (_CORE_TEXT): rtl sources
# rtl/counter_pkg.sv + rtl/counter.sv, tb-tagged tb/tb_counter.sv.
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
    "  default:\n"
    "    filesets: [rtl]\n"
    "  sim:\n"
    "    default_tool: verilator\n"
    "    flow: sim\n"
    "    flow_options: {tool: verilator}\n"
    "    filesets: [rtl, tb]\n"
    "    toplevel: tb_counter\n"
)


def _author_scope_core(tmp_path: Path) -> None:
    """Author the sim `.core` + touch its declared sources under *tmp_path*."""
    (tmp_path / "design.core").write_text(_SCOPE_CORE_TEXT, encoding="utf-8")
    for rel in ("rtl/counter_pkg.sv", "rtl/counter.sv", "tb/tb_counter.sv"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("// stub\n", encoding="utf-8")


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
        """A Cocotb Target must NOT be exec'd as a bare V<top>: without cocotb's
        MODULE/filter environment nothing runs, so MUT_ID=0 is not a baseline."""
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
        endpoint._run_sim_pinned("default", tmp_path, build_dir, "tb", mut_id=3)

        sim_cmd = captured[-1]
        assert sim_cmd[:3] == [sys.executable, "-m", "booley.sim.cocotb_run"]
        assert "--cocotb-module" in sim_cmd and "tb.test_ravenoc" in sim_cmd
        assert "--tool" in sim_cmd and "verilator" in sim_cmd
        assert "--plusarg" in sim_cmd and "MUT_ID=3" in sim_cmd
        # Sentinels do not apply to Cocotb Targets (ADR 0034 decision 6).
        assert not any(c.startswith("--pass-sentinel") for c in sim_cmd)
        assert "--top" not in sim_cmd

    def test_cocotb_run_defaults_to_whole_module(self, tmp_path: Path, monkeypatch):
        """No --test => run every cocotb test (one batched process), rather
        than the classic path's 'first declared test' plusarg."""
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
        endpoint._run_sim_pinned("default", tmp_path, build_dir, "tb", mut_id=1)

        assert "--test" not in captured[-1]

    def test_explicit_test_is_forwarded_to_cocotb(self, tmp_path: Path, monkeypatch):
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

        endpoint = _make_endpoint(tmp_path, monkeypatch, extra_args=["--test", "test_b"])
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        endpoint._run_elab("default", tmp_path, build_dir)
        endpoint._run_sim_pinned("default", tmp_path, build_dir, "tb", mut_id=1)

        sim_cmd = captured[-1]
        assert "--test=test_b" in sim_cmd  # `=` form (F-12)

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
        endpoint._run_sim_pinned("default", tmp_path, build_dir, "tb", mut_id=2)

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
        endpoint._run_sim_pinned("default", tmp_path, build_dir, "tb", mut_id=2)

        sim_cmd = captured[-1]
        assert sim_cmd[:2] == [sys.executable, "-m"]
        assert sim_cmd[2].endswith(".sim.iverilog_run")
        assert sim_cmd[sim_cmd.index("--build-dir") + 1] == "build"
        assert "--plusarg" in sim_cmd and "MUT_ID=2" in sim_cmd
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


class TestInfraFailureHandling:
    def _endpoint_with_sim(self, tmp_path, monkeypatch, *, sim_stdout: str, rc: int):
        _patch_resolve_target(monkeypatch)
        _patch_cocotb_target(monkeypatch, module=None)

        def _fake(cmd, *a, **k):
            joined = " ".join(cmd) if isinstance(cmd, list) else ""
            if "booley.sim." in joined:
                return _fake_proc(rc=rc, stdout=sim_stdout)
            return _fake_proc(rc=0, stdout="[make] ok")

        monkeypatch.setattr("booley.specialists.mutation_tester.subprocess.run", _fake)
        return _make_endpoint(tmp_path, monkeypatch)

    def test_missing_binary_is_not_blamed_on_the_creator(self, tmp_path: Path, monkeypatch):
        endpoint = self._endpoint_with_sim(tmp_path, monkeypatch, sim_stdout=_INFRA_OUT, rc=1)
        build_dir = tmp_path / "build"
        outcome = endpoint._verify_round(_sample_specs(2), "default", tmp_path, build_dir, 1)

        assert outcome.infra_error
        assert "harness failure" in outcome.reason
        assert "default branches incorrect" not in outcome.reason

    def test_infra_failure_grades_fail_closed(self, tmp_path: Path, monkeypatch):
        """pinned_passed used to be True on a missing-exe error — an infra
        crash would have scored as a kill in a real sweep."""
        endpoint = self._endpoint_with_sim(tmp_path, monkeypatch, sim_stdout=_INFRA_OUT, rc=1)
        build_dir = tmp_path / "build"
        outcome = endpoint._verify_round(_sample_specs(2), "default", tmp_path, build_dir, 1)

        assert outcome.pinned_passed is False
        assert outcome.baseline_passed is False
        assert outcome.ok is False

    def test_sweep_marks_infra_runs_invalid_not_detected(self, tmp_path: Path, monkeypatch):
        endpoint = self._endpoint_with_sim(tmp_path, monkeypatch, sim_stdout=_INFRA_OUT, rc=1)
        specs = _sample_specs(3)
        results, _ = endpoint._run_sim_sweep(specs, "default", tmp_path, tmp_path / "build", "tb")

        summary = MutationSummary(specs=specs, results=results)
        assert summary.invalid_count == 3
        assert summary.detected_count == 0
        assert summary.not_detected_count == 0

    def test_sweep_persists_a_log_per_mutant(self, tmp_path: Path, monkeypatch):
        """Each mutant's full sim output survives to disk, keyed by MUT_ID.

        Every mutant re-invokes the same prebuilt binary in the same build dir,
        so its run.log is overwritten by the next mutant a tenth of a second
        later. A surviving mutant leaves a clean passing run and no error text,
        which makes its log the only record of what the design actually did.
        """
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))
        endpoint = self._endpoint_with_sim(
            tmp_path,
            monkeypatch,
            sim_stdout="[SIM_RESULT] PASSED\nmutant ran clean\n",
            rc=0,
        )
        specs = _sample_specs(2)
        results, _ = endpoint._run_sim_sweep(specs, "default", tmp_path, tmp_path / "build", "tb")

        # Survivors (rc 0 = the TB failed to kill the mutant) — the actionable case.
        assert all(not r.detected for r in results)
        for spec, result in zip(specs, results, strict=True):
            assert result.log_path, "every mutant run must cite a log"
            log = tmp_path / result.log_path
            assert log.name == f"mutant_{spec.mut_id or spec.index}.log"
            assert "mutant ran clean" in log.read_text(encoding="utf-8")

    def test_classified_entries_cite_their_mutant_log(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))
        endpoint = self._endpoint_with_sim(
            tmp_path,
            monkeypatch,
            sim_stdout="[SIM_RESULT] PASSED\n",
            rc=0,
        )
        specs = _sample_specs(1)
        results, _ = endpoint._run_sim_sweep(specs, "default", tmp_path, tmp_path / "build", "tb")

        entry = MutationSummary(specs=specs, results=results).classify()[0]
        assert entry["status"] == "not_detected"
        assert entry["log"].endswith("mutant_1.log")

    def test_mutant_log_is_capped(self, tmp_path: Path, monkeypatch):
        """A chatty TB times the mutant count times up to 3 rounds — the cap
        is what keeps that off the 20 GB-trace path. Tail kept: the verdict
        and the last thing the design did live there."""
        from booley.specialists.mutation_tester import _MUTANT_LOG_MAX_BYTES

        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))
        chatty = "noise line\n" * 40_000 + "[SIM_RESULT] PASSED\n"
        assert len(chatty) > _MUTANT_LOG_MAX_BYTES
        endpoint = self._endpoint_with_sim(tmp_path, monkeypatch, sim_stdout=chatty, rc=0)
        specs = _sample_specs(1)
        results, _ = endpoint._run_sim_sweep(specs, "default", tmp_path, tmp_path / "build", "tb")

        log = tmp_path / results[0].log_path
        text = log.read_text(encoding="utf-8")
        assert log.stat().st_size <= _MUTANT_LOG_MAX_BYTES
        assert "[SIM_RESULT] PASSED" in text, "the tail is the half worth keeping"
        assert "TRUNCATED" in text, "truncation must be explicit, never silent"

    def test_sweep_clears_the_previous_generation_of_logs(self, tmp_path: Path, monkeypatch):
        """A re-run with a smaller --count must not leave the earlier run's
        higher-numbered logs behind: the ``mutant_logs`` key advertises the
        whole directory, so a survivor there is a present-but-wrong pointer."""
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))
        endpoint = self._endpoint_with_sim(
            tmp_path, monkeypatch, sim_stdout="[SIM_RESULT] PASSED\n", rc=0
        )
        endpoint._run_sim_sweep(_sample_specs(4), "default", tmp_path, tmp_path / "build", "tb")
        log_dir = lock_mod.mutant_logs_dir()
        assert len(list(log_dir.glob("mutant_*.log"))) == 4

        endpoint._run_sim_sweep(_sample_specs(2), "default", tmp_path, tmp_path / "build", "tb")

        assert sorted(p.name for p in log_dir.glob("mutant_*.log")) == [
            "mutant_1.log",
            "mutant_2.log",
        ]

    def test_log_write_failure_never_fails_the_sweep(self, tmp_path: Path, monkeypatch):
        """Best-effort: losing a log must not cost a run its verdict."""
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))
        endpoint = self._endpoint_with_sim(
            tmp_path,
            monkeypatch,
            sim_stdout="[SIM_RESULT] FAILED\n",
            rc=1,
        )
        monkeypatch.setattr(
            "booley.specialists.mutation_tester.lock_mod.mutant_logs_dir",
            lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs")),
        )
        specs = _sample_specs(2)
        results, _ = endpoint._run_sim_sweep(specs, "default", tmp_path, tmp_path / "build", "tb")

        assert [r.detected for r in results] == [True, True]
        assert all(r.log_path == "" for r in results)

    def test_real_fail_verdict_still_counts_as_detected(self, tmp_path: Path, monkeypatch):
        endpoint = self._endpoint_with_sim(
            tmp_path,
            monkeypatch,
            sim_stdout="[SIM_RESULT] FAILED\n",
            rc=1,
        )
        specs = _sample_specs(2)
        results, _ = endpoint._run_sim_sweep(specs, "default", tmp_path, tmp_path / "build", "tb")

        summary = MutationSummary(specs=specs, results=results)
        assert summary.detected_count == 2
        assert summary.invalid_count == 0

    def test_cold_run_aborts_immediately_instead_of_re_prompting(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        """The $8.71 / 3-round re-prompt loop: one creator call, then abort."""
        scope = "rtl/mod_a.sv"
        _prepare_scope_files(tmp_path, [scope])
        _write_dut_top(tmp_path)
        _patch_invoke_agent(
            monkeypatch, [FakeAgentResult(output=_sample_creator_json(_sample_specs(2)))]
        )
        _patch_resolve_target(monkeypatch)
        _patch_cocotb_target(monkeypatch, module=None)

        def _fake(cmd, *a, **k):
            joined = " ".join(cmd) if isinstance(cmd, list) else ""
            if "booley.sim." in joined:
                return _fake_proc(rc=1, stdout=_INFRA_OUT)
            return _fake_proc(rc=0, stdout="[make] ok")

        monkeypatch.setattr("booley.specialists.mutation_tester.subprocess.run", _fake)

        endpoint = _make_endpoint(tmp_path, monkeypatch, scope=scope, count=2, min_detected=0)
        with patch(
            "booley.specialists.mutation_tester.hide_opposite_sources",
            side_effect=lambda *a, **k: _NoopCtx(),
        ):
            result = endpoint._run()

        assert result.exit_code == EXIT_ERROR
        assert "not a defect in the generated mutations" in result.report_text
        assert "round 1" in result.report_text
