"""Stealth core projection lifecycle tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.fusesoc.core_projection import (
    CoreProjectionError,
    authoritative_cores,
    isolated_core_contents_equivalent,
    isolated_registry_root,
    native_cores_ignored,
    projected_core_path,
    projection_enabled,
    projection_issues,
    reconcile_isolated_registry,
    reconcile_projected_cores,
)

_CORE = "CAPI=2:\nname: booley::demo:0\nfilesets: {}\ntargets: {}\n"


def _project(tmp_path: Path, *, stealth: str = "true") -> tuple[Path, Path]:
    root = tmp_path / "repo"
    cores = root / ".booley_project" / "cores"
    cores.mkdir(parents=True)
    (root / ".booley_project" / "booley.toml").write_text(
        f"[stealth]\nenabled = {stealth}\n", encoding="utf-8"
    )
    core = cores / "demo.core"
    core.write_text(_CORE, encoding="utf-8")
    return root, core


def test_projection_requires_explicit_stealth_true(tmp_path: Path) -> None:
    root, _core = _project(tmp_path, stealth="false")
    assert projection_enabled(root) is False
    (root / ".booley_project" / "booley.toml").write_text("[stealth]\n", encoding="utf-8")
    assert projection_enabled(root) is False


def test_native_core_ignore_requires_explicit_stealth_and_toggle(tmp_path: Path) -> None:
    root, _core = _project(tmp_path)
    assert native_cores_ignored(root) is False
    (root / ".booley_project" / "booley.toml").write_text(
        "[stealth]\nenabled = true\nignore_native_cores = true\n",
        encoding="utf-8",
    )
    assert native_cores_ignored(root) is True
    (root / ".booley_project" / "booley.toml").write_text(
        "[stealth]\nenabled = false\nignore_native_cores = true\n",
        encoding="utf-8",
    )
    assert native_cores_ignored(root) is False


def test_reconcile_writes_root_core_and_is_idempotent(tmp_path: Path) -> None:
    root, core = _project(tmp_path)
    destination = projected_core_path(root, core)

    first = reconcile_projected_cores(root)
    second = reconcile_projected_cores(root)

    assert first.written == (destination,)
    assert second.written == ()
    assert destination.read_text(encoding="utf-8").splitlines()[:3] == [
        "CAPI=2:",
        "# Booley stealth core projection: .booley_project/cores/demo.core",
        "name: booley::demo:0",
    ]
    assert projection_issues(root) == ()


def test_reconcile_refreshes_and_removes_owned_stale_projection(tmp_path: Path) -> None:
    root, core = _project(tmp_path)
    destination = projected_core_path(root, core)
    reconcile_projected_cores(root)
    core.write_text(_CORE.replace("demo:0", "demo:1"), encoding="utf-8")
    stale = root / ".booley-projected-old.core"
    stale.write_text(
        "CAPI=2:\n# Booley stealth core projection: .booley_project/cores/old.core\n",
        encoding="utf-8",
    )

    result = reconcile_projected_cores(root)

    assert result.written == (destination,)
    assert result.removed == (stale,)
    assert "demo:1" in destination.read_text(encoding="utf-8")


def test_reconcile_refuses_foreign_destination(tmp_path: Path) -> None:
    root, core = _project(tmp_path)
    destination = projected_core_path(root, core)
    destination.write_text("user data\n", encoding="utf-8")

    with pytest.raises(CoreProjectionError, match="refusing to overwrite"):
        reconcile_projected_cores(root)
    assert destination.read_text(encoding="utf-8") == "user data\n"


def test_disabling_stealth_removes_only_owned_projections(tmp_path: Path) -> None:
    root, _core = _project(tmp_path)
    result = reconcile_projected_cores(root)
    owned = result.written[0]
    foreign = root / ".booley-projected-foreign.core"
    foreign.write_text("user data\n", encoding="utf-8")
    (root / ".booley_project" / "booley.toml").write_text(
        "[stealth]\nenabled = false\n", encoding="utf-8"
    )

    disabled = reconcile_projected_cores(root)

    assert disabled.removed == (owned,)
    assert foreign.read_text(encoding="utf-8") == "user data\n"
    assert authoritative_cores(root) == (root / ".booley_project" / "cores" / "demo.core",)


def test_isolated_registry_rebases_files_and_excludes_native_cores(tmp_path: Path) -> None:
    root, core = _project(tmp_path)
    core.write_text(
        "CAPI=2:\n"
        "name: booley::demo:0\n"
        "filesets:\n"
        "  rtl:\n"
        "    files:\n"
        "      - rtl/demo.sv: {file_type: systemVerilogSource, include_path: rtl}\n"
        "targets: {}\n",
        encoding="utf-8",
    )
    (root / ".booley_project" / "booley.toml").write_text(
        "[stealth]\nenabled = true\nignore_native_cores = true\n",
        encoding="utf-8",
    )
    native = root / "native.core"
    native.write_text("not valid FuseSoC\n", encoding="utf-8")

    result = reconcile_isolated_registry(root)

    assert len(result.written) == 1
    generated = result.written[0]
    assert generated.parent == isolated_registry_root(root)
    text = generated.read_text(encoding="utf-8")
    assert str(root / "rtl" / "demo.sv") in text
    assert str(root / "rtl") in text
    assert "native.core" not in text
    assert core.read_text(encoding="utf-8").startswith("CAPI=2:\nname: booley::demo:0")


def test_isolated_core_equivalence_normalizes_only_checkout_root(tmp_path: Path) -> None:
    left_root, _left_core = _project(tmp_path / "left")
    right_root, right_core = _project(tmp_path / "right")
    config = "[stealth]\nenabled = true\nignore_native_cores = true\n"
    (left_root / ".booley_project/booley.toml").write_text(config, encoding="utf-8")
    (right_root / ".booley_project/booley.toml").write_text(config, encoding="utf-8")
    left = reconcile_isolated_registry(left_root).written[0]
    right = reconcile_isolated_registry(right_root).written[0]

    assert isolated_core_contents_equivalent(left, right)

    right_core.write_text(_CORE.replace("demo:0", "changed:0"), encoding="utf-8")
    reconcile_isolated_registry(right_root)
    assert not isolated_core_contents_equivalent(left, right)


def test_isolated_registry_rejects_nonliteral_fileset_path(tmp_path: Path) -> None:
    root, core = _project(tmp_path)
    core.write_text(
        "CAPI=2:\n"
        "name: booley::demo:0\n"
        "filesets:\n"
        "  rtl:\n"
        "    files: ['tool_verilator? (rtl/demo.sv)']\n"
        "targets: {}\n",
        encoding="utf-8",
    )
    (root / ".booley_project" / "booley.toml").write_text(
        "[stealth]\nenabled = true\nignore_native_cores = true\n",
        encoding="utf-8",
    )

    with pytest.raises(CoreProjectionError, match="literal fileset paths"):
        reconcile_isolated_registry(root)
