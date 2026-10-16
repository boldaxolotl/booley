"""Scratch deletion requires registered identity, dead ownership and child exclusion."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.runtime.scratch_support import scratch_process_ownership_supported

from booley.core import temporary_workspace as tw

pytestmark = pytest.mark.skipif(
    not scratch_process_ownership_supported(), reason="requires Linux identity/flock"
)


def _abandoned() -> Path:
    script = (
        "import os\n"
        "from booley.core.temporary_workspace import temporary_workspace\n"
        "with temporary_workspace() as root:\n"
        "    print(root, flush=True)\n"
        "    os._exit(0)\n"
    )
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    child = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return Path(child.stdout.strip())


def test_live_context_preserved_and_normal_exit_removed():
    with tw.temporary_workspace() as raw:
        root = Path(raw)
        tw.sweep_stale_workspaces()
        assert root.is_dir()
    assert not root.exists()


def test_dead_owner_removed_without_touching_unmarked_legacy(tmp_path):
    root = _abandoned()
    legacy = tw.registry_root() / "booley-owned-user-directory"
    legacy.mkdir()
    sentinel = legacy / "sentinel"
    sentinel.write_bytes(b"foreign")
    tw.sweep_stale_workspaces()
    assert not root.exists()
    assert sentinel.read_bytes() == b"foreign"


@pytest.mark.parametrize(
    "case",
    [
        "namespace",
        "unknown_kind",
        "schema",
        "uid",
        "root_inode",
        "oversized",
        "malformed",
        "marker_symlink",
        "root_symlink",
        "pending",
        "lost_guardian",
    ],
)
def test_uncertain_markers_preserve_foreign_bytes(tmp_path, case):
    root = _abandoned()
    marker = root / tw._MARKER
    data = json.loads(marker.read_text())
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    sentinel = foreign / "sentinel"
    sentinel.write_bytes(b"do not touch")
    if case == "namespace":
        data["owner"]["namespace"][1] += 1
    elif case == "unknown_kind":
        data["kind"] = "future"
    elif case == "schema":
        data["schema"] = 123
    elif case == "uid":
        data["uid"] += 1
    elif case == "root_inode":
        data["root"][1] += 1
    elif case == "pending":
        data["launches"]["ambiguous"] = {"state": "pending"}
    elif case == "lost_guardian":
        data["launches"]["lost"] = {"state": "running", "supervisor": data["owner"]}
    if case == "oversized":
        marker.write_text(" " * (tw._MAX_MARKER + 1))
    elif case == "malformed":
        marker.write_text("{")
    elif case == "marker_symlink":
        marker.unlink()
        marker.symlink_to(sentinel)
    elif case == "root_symlink":
        moved = root.with_name("retained-original")
        root.rename(moved)
        root.symlink_to(foreign, target_is_directory=True)
    else:
        marker.write_text(json.dumps(data))
    tw.sweep_stale_workspaces()
    assert root.exists()
    assert sentinel.read_bytes() == b"do not touch"


def test_pid_reuse_does_not_keep_an_old_owner():
    root = _abandoned()
    marker = root / tw._MARKER
    data = json.loads(marker.read_text())
    data["owner"] = tw.process_identity(os.getpid())
    data["owner"]["start"] += 1
    marker.write_text(json.dumps(data))
    tw.sweep_stale_workspaces()
    assert not root.exists()


def test_nested_symlink_unlinked_without_following_target(tmp_path):
    root = _abandoned()
    sentinel = tmp_path / "external"
    sentinel.write_bytes(b"external bytes")
    (root / "external-link").symlink_to(sentinel)
    tw.sweep_stale_workspaces()
    assert not root.exists()
    assert sentinel.read_bytes() == b"external bytes"


def test_shared_guard_survives_owner_descriptor_close():
    import fcntl

    root = _abandoned()
    guard = os.open(root / tw._GUARD, os.O_RDWR)
    inherited = os.dup(guard)
    try:
        fcntl.flock(guard, fcntl.LOCK_SH)
        os.close(guard)
        guard = -1
        tw.sweep_stale_workspaces()
        assert root.is_dir()
    finally:
        os.close(inherited)
        if guard >= 0:
            os.close(guard)
    tw.sweep_stale_workspaces()
    assert not root.exists()


def test_replaced_guard_is_preserved():
    root = _abandoned()
    guard = root / tw._GUARD
    guard.rename(root / "original-guard")
    guard.write_bytes(b"foreign guard")
    guard.chmod(0o600)
    tw.sweep_stale_workspaces()
    assert root.is_dir()
    assert guard.read_bytes() == b"foreign guard"


def test_scan_budget_advances_across_fresh_processes():
    parent = tw.registry_root()
    sentinels = []
    for index in range(80):
        legacy = parent / f"booley-owned-legacy-{index}"
        legacy.mkdir()
        sentinel = legacy / "foreign"
        sentinel.write_bytes(b"foreign")
        sentinels.append(sentinel)
    root = _abandoned()
    script = (
        "from booley.core import temporary_workspace as t; t._LIMIT=16; t.sweep_stale_workspaces()"
    )
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    for _ in range(8):
        subprocess.run(
            [sys.executable, "-c", script],
            env=environment,
            check=True,
            timeout=10,
            capture_output=True,
        )
        if not root.exists():
            break
    assert not root.exists()
    assert all(path.read_bytes() == b"foreign" for path in sentinels)


def test_concurrent_sweepers_claim_a_root_once():
    import concurrent.futures

    root = _abandoned()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(tw.sweep_stale_workspaces) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    assert not root.exists()


@pytest.mark.parametrize(
    "threat",
    ["owner_extra", "owner_bool", "owner_namespace", "schema_bool", "launch_extra", "parent_mode"],
)
def test_strict_marker_shapes_preserve_uncertain_ownership(threat):
    root = _abandoned()
    marker = root / tw._MARKER
    data = json.loads(marker.read_text())
    old_mode = root.parent.stat().st_mode & 0o777
    if threat == "owner_extra":
        data["owner"]["future"] = True
    elif threat == "owner_bool":
        data["owner"]["start"] = True
    elif threat == "owner_namespace":
        data["owner"]["namespace"] = [True, 1]
    elif threat == "schema_bool":
        data["schema"] = True
    elif threat == "launch_extra":
        data["launches"]["future"] = {"state": "complete", "future": True}
    else:
        root.parent.chmod(0o777)
    marker.write_text(json.dumps(data))
    try:
        tw.sweep_stale_workspaces()
        assert root.is_dir()
    finally:
        root.parent.chmod(old_mode)


@pytest.mark.parametrize(
    "helper", ["image_source", "image_role", "migration_config", "demo_targets"]
)
@pytest.mark.parametrize("fail", [False, True])
def test_lightweight_helper_allocations_have_markers_and_cleanup(
    tmp_path, monkeypatch, helper, fail
):
    import contextlib
    import importlib
    from types import SimpleNamespace

    modules = {
        "image_source": "booley.runtime.build_stamp",
        "image_role": "booley.harness.image_lifecycle",
        "migration_config": "booley.runtime.directory_migration_git",
        "demo_targets": "booley.dev_support.demo_contract",
    }
    module = importlib.import_module(modules[helper])
    observed = []

    @contextlib.contextmanager
    def recorded(**kwargs):
        with tw.temporary_workspace(**kwargs) as raw:
            root = Path(raw)
            assert (root / tw._MARKER).is_file()
            assert tw.active_guard_fds()
            observed.append(root)
            yield raw
            if fail:
                raise RuntimeError("synthetic helper failure")

    monkeypatch.setattr(module, "temporary_workspace", recorded)
    expectation = (
        pytest.raises(RuntimeError, match="synthetic helper failure")
        if fail
        else contextlib.nullcontext()
    )
    with expectation:
        _invoke_lightweight_helper(module, helper, tmp_path, monkeypatch, SimpleNamespace)
    assert len(observed) == 1
    assert not observed[0].exists()


def _invoke_lightweight_helper(module, helper, tmp_path, monkeypatch, namespace):
    if helper == "image_source":
        import sys

        monkeypatch.setitem(
            sys.modules,
            "booley._build_commit",
            namespace(
                DEVELOPMENT_CONTEXT_SHA256="a" * 64,
                PAYLOAD_FINGERPRINT="b" * 64,
            ),
        )
        monkeypatch.setattr(module, "_extract_development_context", lambda *args, **kwargs: None)
        with module.extracted_development_context() as root:
            assert root.is_dir()
    elif helper == "image_role":
        node = namespace(
            generated_files=(("generated", "synthetic"),),
            recipe=tmp_path / "Dockerfile",
            role=None,
        )
        with module._IncrementalBuildAdapter._role_context(node, tmp_path / "absent") as (_, root):
            assert (root / "generated").read_text() == "synthetic"
    elif helper == "migration_config":
        admin = tmp_path / "admin"
        admin.mkdir()
        (admin / "config").write_text("[core]\nworktree = ../project\n")
        assert module._config_edits(tmp_path, admin, []) == []
    else:
        monkeypatch.setattr(module.TargetCatalog, "build", lambda _: namespace())
        monkeypatch.setattr(
            module, "_resolve_catalog_target", lambda *args: namespace(toplevel="top")
        )
        assert module._validate_targets(tmp_path, ("synthetic",)) == []


@pytest.mark.parametrize("flow_name", ["synth", "fpga"])
@pytest.mark.parametrize("custom_state", [False, True])
def test_dry_flow_plan_keeps_payload_and_registry_in_selected_booley_directory(
    tmp_path, monkeypatch, flow_name, custom_state
):
    import importlib
    from types import SimpleNamespace

    from booley.runtime.booley_dir import reset_cache

    project, selected = _dry_plan_layout(tmp_path, custom_state)
    monkeypatch.setenv("BOOLEY_DIR", str(selected))
    reset_cache()
    module = importlib.import_module(f"booley.flows.{flow_name}.flow")
    flow_type = module.AsicSynthesizeFlow if flow_name == "synth" else module.FpgaImplFlow
    flow = flow_type()
    flow.parse_args(["--target", "default", "--work-dir", str(project), "--dry-run"])
    observed = []

    def resolve(target, *, build_root):
        root = build_root.parent
        assert root.parent == selected / ".runtime" / "owned-scratch"
        assert (root / tw._MARKER).is_file()
        build_root.mkdir()
        (build_root / "relative.txt").write_text("synthetic recipe input")
        observed.append(root)
        return SimpleNamespace(sources=("relative.txt",))

    monkeypatch.setattr(flow, f"_resolve_{flow_name}_recipe", resolve)
    monkeypatch.setattr(flow, f"_{flow_name}_work_unit", _synthetic_work_unit)
    try:
        plan = getattr(flow, f"_plan_{flow_name}_implementation")(["default"])
        assert not plan.aggregate_errors
        assert plan.work_units[0].sources == ("relative.txt",)
        assert observed and all(not root.exists() for root in observed)
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert status.stdout == ""
        assert not (project / ".booley-registry.lock").exists()
        assert (selected / ".runtime/owned-scratch/.booley-registry.lock").is_file()
    finally:
        reset_cache()


def test_missing_native_identity_keeps_isolation_and_preserves_normal_root(monkeypatch):
    monkeypatch.setattr(tw, "process_identity", lambda _: None)
    with tw.temporary_workspace(kind="baseline") as raw:
        root = Path(raw)
        tw.register_baseline(root, {"synthetic": "trusted metadata"})
        assert tw.active_guard_fds() == ()
        result = tw.run_guarded(
            [sys.executable, "-c", 'print("isolated")'], capture_output=True, text=True, timeout=5
        )
        assert result.stdout == "isolated\n"
    assert root.is_dir()
    tw.sweep_stale_workspaces()
    assert root.is_dir()


@pytest.mark.parametrize("threat", ["root_symlink", "parent_symlink", "writable"])
def test_registry_admission_rejects_links_and_foreign_write_access(tmp_path, monkeypatch, threat):
    selected = tmp_path / "selected"
    selected.mkdir(mode=0o700)
    sentinel = selected / "foreign"
    sentinel.write_bytes(b"foreign bytes")
    if threat == "root_symlink":
        link = tmp_path / "registry-link"
        link.symlink_to(selected, target_is_directory=True)
        selected = link
    elif threat == "parent_symlink":
        link = tmp_path / "parent-link"
        link.symlink_to(selected, target_is_directory=True)
        selected = link / "registry"
    else:
        selected.chmod(0o777)
    monkeypatch.setenv(tw.ROOT_ENV, str(selected))
    with pytest.raises(ValueError):
        tw.registry_root()
    assert sentinel.read_bytes() == b"foreign bytes"


def test_root_replacement_during_claim_restores_foreign_path_without_deletion(
    tmp_path, monkeypatch
):
    root = _abandoned()
    original = root.with_name("retained-original")
    foreign = tmp_path / "foreign"
    foreign.mkdir(mode=0o700)
    (foreign / "sentinel").write_bytes(b"foreign bytes")
    real_rename = Path.rename
    replaced = False

    def replace_before_claim(path, target):
        nonlocal replaced
        if path == root and not replaced:
            replaced = True
            real_rename(root, original)
            real_rename(foreign, root)
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", replace_before_claim)
    tw.sweep_stale_workspaces()
    assert replaced
    assert (root / "sentinel").read_bytes() == b"foreign bytes"
    assert (original / tw._MARKER).is_file()


def test_creation_and_sweep_overlap_preserves_live_roots():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2, timeout=5)
    roots = []

    def create():
        for _ in range(8):
            with tw.temporary_workspace() as raw:
                root = Path(raw)
                roots.append(root)
                barrier.wait()
                barrier.wait()
                assert root.is_dir()

    def sweep():
        for _ in range(8):
            barrier.wait()
            tw.sweep_stale_workspaces()
            barrier.wait()

    with ThreadPoolExecutor(max_workers=2) as pool:
        creator, sweeper = pool.submit(create), pool.submit(sweep)
        creator.result(timeout=15)
        sweeper.result(timeout=15)
    assert len(roots) == 8
    assert all(not root.exists() for root in roots)


def test_test_namespace_survives_a_test_owned_monkeypatch_undo(monkeypatch):
    home = os.environ["HOME"]
    registry = os.environ[tw.ROOT_ENV]
    monkeypatch.setenv("BOOLEY_SYNTHETIC_UNDO_PROBE", "synthetic")
    monkeypatch.undo()
    assert os.environ["HOME"] == home
    assert os.environ[tw.ROOT_ENV] == registry
    assert not os.environ.get("BOOLEY_SYNTHETIC_UNDO_PROBE")


def _synthetic_work_unit(target, recipe, *, role, revision):
    from booley.flows.plan import WorkUnitPlan

    return WorkUnitPlan(
        unit_id="candidate",
        role=role,
        revision=revision,
        selector=target,
        target_identity=target,
        test_or_module_scope=(),
        eda_tool="synthetic",
        timeout_ms=1000,
        sources=recipe.sources,
    )


def _dry_plan_layout(tmp_path, custom_state):
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True, timeout=5)
    (project / ".git/info/exclude").write_text("/.booley\n")
    selected = tmp_path / "custom-state" if custom_state else project / ".booley"
    selected.mkdir()
    return project, selected
