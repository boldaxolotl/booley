"""Stealth core projection lifecycle tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.fusesoc.core_projection import (
    CoreProjectionError,
    authoritative_cores,
    isolated_registry_root,
    native_cores_ignored,
    projected_core_path,
    projection_enabled,
    projection_issues,
    reconcile_isolated_registry,
    reconcile_projected_cores,
    registry_issues,
    render_private_mirror,
)
from tests.conftest import symlink_or_skip
from tests.fusesoc_test_support import symlinked_booley_dir

_CORE = "CAPI=2:\nname: booley::demo:0\nfilesets: {}\ntargets: {}\n"


def _project(tmp_path: Path, *, stealth: str = "true") -> tuple[Path, Path]:
    root = tmp_path / "repo"
    cores = root / ".booley" / "cores"
    cores.mkdir(parents=True)
    (root / ".booley" / "booley.toml").write_text(
        f"[stealth]\nenabled = {stealth}\n", encoding="utf-8"
    )
    core = cores / "demo.core"
    core.write_text(_CORE, encoding="utf-8")
    return root, core


def test_projection_requires_explicit_stealth_true(tmp_path: Path) -> None:
    root, _core = _project(tmp_path, stealth="false")
    assert projection_enabled(root) is False
    (root / ".booley" / "booley.toml").write_text("[stealth]\n", encoding="utf-8")
    assert projection_enabled(root) is False


def test_native_core_ignore_requires_explicit_stealth_and_toggle(tmp_path: Path) -> None:
    root, _core = _project(tmp_path)
    assert native_cores_ignored(root) is False
    (root / ".booley" / "booley.toml").write_text(
        "[stealth]\nenabled = true\nignore_native_cores = true\n",
        encoding="utf-8",
    )
    assert native_cores_ignored(root) is True
    (root / ".booley" / "booley.toml").write_text(
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
        "# Booley stealth core projection: .booley/cores/demo.core",
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
        "CAPI=2:\n# Booley stealth core projection: .booley/cores/old.core\n",
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
    (root / ".booley" / "booley.toml").write_text("[stealth]\nenabled = false\n", encoding="utf-8")

    disabled = reconcile_projected_cores(root)

    assert disabled.removed == (owned,)
    assert foreign.read_text(encoding="utf-8") == "user data\n"
    assert authoritative_cores(root) == (root / ".booley" / "cores" / "demo.core",)


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
    (root / ".booley" / "booley.toml").write_text(
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
    assert any(
        path in text
        for path in (str(root / "rtl" / "demo.sv"), (root / "rtl" / "demo.sv").as_posix())
    )
    assert any(path in text for path in (str(root / "rtl"), (root / "rtl").as_posix()))
    assert "native.core" not in text
    assert core.read_text(encoding="utf-8").startswith("CAPI=2:\nname: booley::demo:0")


def test_isolated_registry_resolves_fileset_symlink_but_preserves_file_parameter(
    tmp_path: Path,
) -> None:
    root, core = _project(tmp_path)
    (root / "constraints").mkdir()
    physical = root / "constraints" / "physical.sdc"
    physical.write_text("create_clock -period 10 clk\n", encoding="utf-8")
    symlink_or_skip(root / "constraints" / "timing.sdc", physical)
    core.write_text(
        "CAPI=2:\n"
        "name: booley::demo:0\n"
        "filesets:\n"
        "  constraints:\n"
        "    files:\n"
        "      - constraints/timing.sdc: {file_type: SDC}\n"
        "parameters:\n"
        "  CONFIG: {datatype: file, paramtype: plusarg, default: config/authored.cfg}\n"
        "targets: {}\n",
        encoding="utf-8",
    )
    (root / ".booley" / "booley.toml").write_text(
        "[stealth]\nenabled = true\nignore_native_cores = true\n",
        encoding="utf-8",
    )

    generated = reconcile_isolated_registry(root).written[0]
    text = generated.read_text(encoding="utf-8")

    assert physical.resolve().as_posix() in text
    assert "default: config/authored.cfg" in text
    assert str(root / "config" / "authored.cfg") not in text


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
    (root / ".booley" / "booley.toml").write_text(
        "[stealth]\nenabled = true\nignore_native_cores = true\n",
        encoding="utf-8",
    )

    with pytest.raises(CoreProjectionError, match="literal fileset paths"):
        reconcile_isolated_registry(root)


def test_isolated_registry_keeps_files_below_symlinked_booley_dir_lexical(
    tmp_path: Path,
) -> None:
    """A fileset path under a linked ``.booley`` renders through the entry, not its target.

    Only the entry itself is followed: a link below it that points outside the
    Project, or a ``..`` route to the entry's target, still renders as its
    physical path, so later checks refuse it.
    """
    root = tmp_path / "repo"
    root.mkdir()
    external = symlinked_booley_dir(root, tmp_path / "external-booley")
    (external / "booley.toml").write_text(
        "[stealth]\nenabled = true\nignore_native_cores = true\n", encoding="utf-8"
    )
    (external / "sim").mkdir()
    (external / "sim" / "main.cc").write_text("int main() {}\n", encoding="utf-8")
    outside = tmp_path / "outside.cc"
    outside.write_text("// outside\n", encoding="utf-8")
    symlink_or_skip(external / "sim" / "escape.cc", outside)
    (external / "cores" / "demo.core").write_text(
        "CAPI=2:\n"
        "name: booley::demo:0\n"
        "filesets:\n"
        "  tb:\n"
        "    files:\n"
        "      - .booley/sim/main.cc: {file_type: cppSource}\n"
        "      - .booley/sim/escape.cc: {file_type: cppSource}\n"
        "      - ../external-booley/sim/main.cc: {file_type: cppSource}\n"
        "targets: {}\n",
        encoding="utf-8",
    )

    text = reconcile_isolated_registry(root).written[0].read_text(encoding="utf-8")

    assert (root.resolve() / ".booley" / "sim" / "main.cc").as_posix() in text
    # The ``..`` route into the target is not the entry: it keeps its physical path.
    assert text.count((external.resolve() / "sim" / "main.cc").as_posix()) == 1
    assert outside.resolve().as_posix() in text


def test_private_mirror_names_in_checkout_files_relatively(tmp_path: Path) -> None:
    root, core = _project(tmp_path)
    (root / "rtl" / "inc").mkdir(parents=True)
    (root / "rtl" / "demo.sv").write_text("module demo; endmodule\n", encoding="utf-8")
    (root / "rtl" / "inc" / "defs.svh").write_text("`define X 1\n", encoding="utf-8")
    outside = tmp_path / "outside.sv"
    outside.write_text("module outside; endmodule\n", encoding="utf-8")
    symlink_or_skip(root / "rtl" / "ext.sv", outside)
    core.write_text(
        "CAPI=2:\n"
        "name: booley::demo:0\n"
        "filesets:\n"
        "  rtl:\n"
        "    files:\n"
        "      - rtl/demo.sv\n"
        "      - rtl/inc/defs.svh: {is_include_file: true, include_path: rtl/inc}\n"
        "      - rtl/ext.sv\n"
        f"      - {outside.as_posix()}\n"
        "      - rtl/missing.sv\n"
        "targets: {}\n",
        encoding="utf-8",
    )
    (root / ".booley" / "booley.toml").write_text(
        "[stealth]\nenabled = true\nignore_native_cores = true\n", encoding="utf-8"
    )
    reconcile_isolated_registry(root)
    shared = {p: p.read_bytes() for p in isolated_registry_root(root).glob("*.core")}
    mirror_root = tmp_path / "mirror"

    mirrored = render_private_mirror(root, mirror_root)

    (mirror_core,) = mirrored.values()
    assert mirrored == {core.resolve(): mirror_core}
    text = mirror_core.read_text(encoding="utf-8")
    assert "- rtl/demo.sv\n" in text
    assert "include_path: rtl/inc" in text
    assert root.resolve().as_posix() not in text
    # In-checkout files are copied; the link out of the checkout stays absolute and uncopied.
    assert (mirror_root / "rtl" / "demo.sv").read_text(encoding="utf-8").startswith("module demo")
    assert (mirror_root / "rtl" / "inc" / "defs.svh").is_file()
    assert not (mirror_root / "rtl" / "ext.sv").exists()
    assert not (mirror_root / "rtl" / "missing.sv").exists()
    # Both the link and the literal absolute name keep pointing outside.
    assert text.count(outside.resolve().as_posix()) == 2
    # The shared registry is left exactly as it was.
    assert {p: p.read_bytes() for p in isolated_registry_root(root).glob("*.core")} == shared


def test_private_mirror_refuses_an_existing_mirror_root(tmp_path: Path) -> None:
    root, _core = _project(tmp_path)
    (tmp_path / "mirror").mkdir()
    with pytest.raises(FileExistsError):
        render_private_mirror(root, tmp_path / "mirror")


def test_registry_issues_describe_what_reconcile_would_write_without_writing(
    tmp_path: Path,
) -> None:
    root, core = _project(tmp_path)
    assert registry_issues(root) == ()  # Native cores are not ignored: no private registry.
    (root / ".booley" / "booley.toml").write_text(
        "[stealth]\nenabled = true\nignore_native_cores = true\n", encoding="utf-8"
    )
    registry = isolated_registry_root(root)
    assert registry_issues(root) == ("missing private registry",)
    assert not registry.exists()

    reconcile_isolated_registry(root)
    assert registry_issues(root) == ()

    core.write_text(_CORE.replace("demo:0", "demo:1"), encoding="utf-8")
    owned = next(registry.glob("*.core"))
    (registry / "booley-isolated-old.core").write_text(
        owned.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (registry / "foreign.core").write_text("user data\n", encoding="utf-8")
    (registry / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
    assert registry_issues(root) == (
        "FUSESOC_IGNORE blocks the private registry",
        "foreign foreign.core",
        f"stale {owned.name}",
        "stale booley-isolated-old.core",
    )
    owned.unlink()
    assert f"missing {owned.name}" in registry_issues(root)
    owned.write_text("user data\n", encoding="utf-8")
    assert f"foreign file blocks {owned.name}" in registry_issues(root)
