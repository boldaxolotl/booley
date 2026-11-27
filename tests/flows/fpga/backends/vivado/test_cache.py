from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from booley.core.build_paths import work_root_for
from booley.flows.fpga.backends.vivado import cache as fpga_cache
from booley.flows.fpga.flow import FpgaImplFlow
from booley.fusesoc.fusesoc_registry import ResolvedFile, ResolvedTarget
from booley.mcp.base import EXIT_ERROR, EXIT_SUCCESS
from tests.flows.fpga.vivado_fake import (
    EDALIZE_LEAKS_HANDLE,
    FakeVivado,
    run_fpga,
    write_fpga_project,
)


def _artifacts(root: Path, *, bitstream: bool = True) -> None:
    impl = root / "demo.runs" / "impl_1"
    impl.mkdir(parents=True)
    for name, text in (
        ("top_utilization_placed.rpt", "util"),
        ("top_timing_summary_routed.rpt", "timing"),
        ("top_drc_routed.rpt", "drc"),
        ("runme.log", "route_design completed successfully"),
    ):
        (impl / name).write_text(text, encoding="utf-8")
    if bitstream:
        (impl / "top.bit").write_bytes(b"bitstream")


def _run_evidence(run_id: str = "producer-1") -> dict:
    return {
        "version": 1,
        "run_id": run_id,
        "source_revision": "a" * 40,
        "source_sha256": "b" * 64,
        "recipe_sha256": "c" * 64,
    }


def _resolved(root: Path) -> ResolvedTarget:
    (root / "rtl.sv").write_text("module top; endmodule\n", encoding="utf-8")
    (root / "timing.xdc").write_text("create_clock -period 10 [get_ports clk]\n")
    return ResolvedTarget(
        name="fpga_demo",
        vlnv="::demo:0",
        toplevel="top",
        eda_tool="vivado",
        files=(
            ResolvedFile(name="rtl.sv", file_type="systemVerilogSource"),
            ResolvedFile(name="timing.xdc", file_type="xdc"),
        ),
        parameters={"WIDTH": {"datatype": "int", "paramtype": "vlogparam", "default": 8}},
        build_root=root,
        edam_path=root / "demo.eda.yml",
        flow_options={"tool": "vivado", "part": "xc7a35tcpg236-1"},
    )


@pytest.mark.parametrize("change", ["source", "parameter", "recipe"])
def test_input_fingerprint_changes_with_each_input(tmp_path: Path, change: str) -> None:
    resolved = _resolved(tmp_path)
    edam = {"name": "fpga_demo", "toplevel": "top", "tool_options": {"part": "p"}}
    before = fpga_cache.input_fingerprint(resolved, edam, out_of_context=False, recipe_sha256="a")
    recipe = "b" if change == "recipe" else "a"
    if change == "source":
        (tmp_path / "rtl.sv").write_text("module top; wire changed; endmodule\n", encoding="utf-8")
    if change == "parameter":
        width = {"datatype": "int", "paramtype": "vlogparam", "default": 16}
        resolved = replace(resolved, parameters={"WIDTH": width})
    after = fpga_cache.input_fingerprint(
        resolved, edam, out_of_context=False, recipe_sha256=recipe
    )
    assert after != before


def test_cache_hit_requires_exact_artifact_bytes(tmp_path: Path) -> None:
    _artifacts(tmp_path)
    evidence = _run_evidence()
    assert fpga_cache.store(tmp_path, "a" * 64, require_bitstream=True, producer_evidence=evidence)
    assert fpga_cache.load(tmp_path, "a" * 64, require_bitstream=True) is not None
    assert fpga_cache.load(tmp_path, "b" * 64, require_bitstream=True) is None

    report = tmp_path / "demo.runs" / "impl_1" / "top_drc_routed.rpt"
    report.write_text("changed", encoding="utf-8")
    assert fpga_cache.load(tmp_path, "a" * 64, require_bitstream=True) is None


def test_cache_rejects_non_mapping_metadata_and_boolean_producer_version(tmp_path: Path) -> None:
    _artifacts(tmp_path)
    (tmp_path / fpga_cache.CACHE_FILE).write_text("[]", encoding="utf-8")
    assert fpga_cache.load(tmp_path, "a" * 64, require_bitstream=True) is None
    boolean_version = {**_run_evidence(), "version": True}
    assert not fpga_cache.store(
        tmp_path, "a" * 64, require_bitstream=True, producer_evidence=boolean_version
    )


@pytest.mark.parametrize(
    ("bitstream", "require_bitstream", "stale", "stored"),
    [(False, True, False, False), (False, False, False, True), (True, True, True, False)],
    ids=["bitstream-required", "out-of-context", "predates-dispatch"],
)
def test_store_accepts_only_fresh_complete_artifacts(
    tmp_path: Path, bitstream: bool, require_bitstream: bool, stale: bool, stored: bool
) -> None:
    _artifacts(tmp_path, bitstream=bitstream)
    if stale:
        old = time.time() - 3600
        for path in tmp_path.rglob("*"):
            os.utime(path, (old, old))
    assert (
        fpga_cache.store(
            tmp_path,
            "a" * 64,
            require_bitstream=require_bitstream,
            producer_evidence=_run_evidence(),
            min_mtime=time.time() if stale else None,
        )
        is stored
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return write_fpga_project(tmp_path / "project")


@pytest.fixture
def vivado(project: Path, monkeypatch: pytest.MonkeyPatch) -> FakeVivado:
    return FakeVivado.install(monkeypatch, project)


@EDALIZE_LEAKS_HANDLE
@pytest.mark.parametrize(
    ("args", "cached", "calls"),
    [((), True, [["-B"]]), (("--no-cache",), False, [["-B"], ["-B"]])],
    ids=["reuse", "no-cache"],
)
def test_second_run_reuses_the_verified_route_unless_told_not_to(
    project: Path, vivado: FakeVivado, args: tuple[str, ...], cached: bool, calls: list
) -> None:
    """A cache miss forces Make to rebuild; a hit keeps its producer's run evidence."""
    _, first = run_fpga(project)
    outcome, second = run_fpga(project, *args)

    assert outcome.exit_code == EXIT_SUCCESS
    assert second["cached"] is cached
    assert vivado.calls() == calls
    assert second["recipe_fingerprint"] == first["recipe_fingerprint"] != ""
    producer = first["run_evidence"]["run_id"]
    assert (second["run_evidence"]["run_id"] == producer) is cached
    assert (second["cache_consumer_run_id"] not in {None, producer}) is cached


@EDALIZE_LEAKS_HANDLE
def test_cached_reports_cannot_replace_missing_route_witness(
    project: Path, vivado: FakeVivado
) -> None:
    """A cache whose run log lost the route witness runs Vivado again (#1075)."""
    run_fpga(project)
    work_root = work_root_for(project, "fpga", "fpga")
    impl = work_root / "top.runs" / "impl_1"
    (impl / "runme.log").write_bytes(b"no route occurred\n")
    (impl / "top_drc_routed.rpt").write_bytes(b"route_design completed successfully\n")
    stored = json.loads((work_root / fpga_cache.CACHE_FILE).read_text(encoding="utf-8"))
    assert fpga_cache.store(
        work_root,
        stored["fingerprint"],
        require_bitstream=True,
        producer_evidence=stored["producer_evidence"],
    )

    _, report = run_fpga(project)

    assert report["cached"] is False
    assert len(vivado.calls()) == 2


@pytest.mark.parametrize(
    ("completion_log", "returncode", "passes"),
    [
        ("ERROR: post-route optimization failed", 1, False),
        ("", 0, False),
        ('puts "BOOLEY_POST_ROUTE_PHYS_OPT_COMPLETE"', 1, False),
        ("BOOLEY_POST_ROUTE_PHYS_OPT_COMPLETE", 0, True),
        ("BOOLEY_POST_ROUTE_PHYS_OPT_COMPLETE\nERROR: bitstream failed", 1, True),
    ],
)
def test_post_route_completion_gates_success_and_cache(
    project: Path, vivado: FakeVivado, completion_log: str, returncode: int, passes: bool
) -> None:
    vivado.answer(stdout=completion_log + "\n", returncode=returncode, route=True)

    outcome, _ = run_fpga(project, "--ppa-profile", "max_frequency")

    assert outcome.exit_code == (EXIT_SUCCESS if passes else EXIT_ERROR)
    cache_file = work_root_for(project, "fpga", "fpga") / fpga_cache.CACHE_FILE
    assert cache_file.exists() is passes
    assert ("post-route" in outcome.report_text.lower()) is not passes


@pytest.mark.parametrize("datatype", ["str", "file"])
def test_string_parameter_is_refused_before_vivado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, datatype: str
) -> None:
    project = write_fpga_project(tmp_path / "project", width=datatype, default="MAC")
    vivado = FakeVivado.install(monkeypatch, project)

    outcome, _ = run_fpga(project)

    assert outcome.exit_code == EXIT_ERROR
    assert "Vivado does not support string/file" in outcome.report_text
    assert vivado.calls() == []
    assert not list(work_root_for(project, "fpga", "fpga").glob("*.tcl"))
    # Private: a run refuses the recipe while planning, so only preparation, which a
    # baseline re-resolves at execution, shows the refusal precedes the cache and EDAM.
    flow = FpgaImplFlow()
    flow.parse_args(["--target", "fpga", "--project", str(project)])
    with pytest.raises(ValueError, match="Vivado does not support string/file"):
        flow._prepare_fpga_command("fpga")


def test_numbered_log_is_skipped_without_a_report_directory(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Private: every Flow entry point sets a report directory before it runs (the
    # Booley Directory default), so only the publisher shows that a run without one
    # skips its numbered log instead of failing.
    monkeypatch.setenv("BOOLEY_DIR", str(project / ".booley"))
    flow = FpgaImplFlow()
    flow.parse_args(["--target", "fpga", "--project", str(project)])
    assert flow._publish_numbered_fpga_log("fpga", "log") == ""


def _reachable_target(tmp_path: Path) -> ResolvedTarget:
    """A Target whose build root holds one listed source and one export folder."""
    build = tmp_path / "build"
    (build / "src" / "inc" / "deep").mkdir(parents=True)
    (build / "rtl.sv").write_text("module top; endmodule\n", encoding="utf-8")
    return ResolvedTarget(
        name="t",
        vlnv="::demo:0",
        toplevel="top",
        eda_tool="vivado",
        files=(ResolvedFile(name="rtl.sv", file_type="systemVerilogSource"),),
        parameters={},
        build_root=build,
        edam_path=build / "t.eda.yml",
        flow_options={"tool": "vivado", "part": "xc7a35tcpg236-1"},
    )


def _forbidden_walk(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
    raise AssertionError("non-isolated fingerprint must not walk reachable dirs")


def test_default_payload_has_no_reachable_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without reachable_dirs the key equals a call that never heard of them."""
    resolved = _reachable_target(tmp_path)
    (resolved.build_root / "src" / "inc" / "extra.vh").write_text("// x\n", encoding="utf-8")
    with monkeypatch.context() as guard:
        # Private: the hashed payload is not observable, so only the walk's absence
        # shows that a Project without reachable folders keeps its earlier cache key.
        guard.setattr(fpga_cache, "_reachable_file_evidence", _forbidden_walk)
        plain = fpga_cache.input_fingerprint(resolved, {}, out_of_context=False)
        assert plain == fpga_cache.input_fingerprint(
            resolved, {}, out_of_context=False, reachable_dirs=()
        )
    assert plain != fpga_cache.input_fingerprint(
        resolved,
        {},
        out_of_context=False,
        reachable_dirs=(resolved.build_root / "src" / "inc",),
    )


def test_reachable_files_walk_is_bounded_outside_build_root(tmp_path: Path) -> None:
    """Inside the build root the walk is recursive; a user folder counts only its own files."""
    resolved = _reachable_target(tmp_path)
    inside = resolved.build_root / "src" / "inc"
    user = tmp_path / "user"
    (user / "deep").mkdir(parents=True)
    (user / "top.vh").write_text("// top v1\n", encoding="utf-8")
    (user / "deep" / "buried.vh").write_text("// buried v1\n", encoding="utf-8")
    (inside / "deep" / "buried.vh").write_text("// inside v1\n", encoding="utf-8")

    def key() -> str:
        return fpga_cache.input_fingerprint(
            resolved, {}, out_of_context=False, reachable_dirs=(inside, user)
        )

    base = key()
    (user / "deep" / "buried.vh").write_text("// buried v2\n", encoding="utf-8")
    assert key() == base  # accepted limit (plan R3): user subfolders are not walked
    (inside / "deep" / "buried.vh").write_text("// inside v2\n", encoding="utf-8")
    changed = key()
    assert changed != base  # a subfolder of the build root is
    (user / "top.vh").write_text("// top v2\n", encoding="utf-8")
    assert key() != changed  # a user folder's own file is


def test_reachable_files_tolerate_missing_user_folder(tmp_path: Path) -> None:
    """A reachable folder that cannot be listed adds nothing, like an empty one."""
    resolved = _reachable_target(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    gone = tmp_path / "does-not-exist"
    assert fpga_cache.input_fingerprint(
        resolved, {}, out_of_context=False, reachable_dirs=(gone,)
    ) == fpga_cache.input_fingerprint(resolved, {}, out_of_context=False, reachable_dirs=(empty,))


def test_reachable_file_unreadable_is_keyed_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reachable file that cannot be hashed still enters the key, flagged missing."""
    resolved = _reachable_target(tmp_path)
    user = tmp_path / "user"
    user.mkdir()
    (user / "locked.vh").write_text("// v1\n", encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()
    readable = fpga_cache.input_fingerprint(
        resolved, {}, out_of_context=False, reachable_dirs=(user,)
    )
    without_file = fpga_cache.input_fingerprint(
        resolved, {}, out_of_context=False, reachable_dirs=(empty,)
    )
    # Private: no portable file is listed as a regular file yet fails to open
    # (root and Windows ignore a chmod 000), so the one hashing call is replaced.
    real_hash = fpga_cache._hash_file

    def _locked_only(path: Path) -> tuple[str, int]:
        if path.name == "locked.vh":
            raise OSError("permission denied")
        return real_hash(path)

    monkeypatch.setattr(fpga_cache, "_hash_file", _locked_only)
    with_locked = fpga_cache.input_fingerprint(
        resolved, {}, out_of_context=False, reachable_dirs=(user,)
    )
    assert with_locked != without_file  # the unreadable file is still in the key
    assert with_locked != readable  # and its entry differs from the readable one
