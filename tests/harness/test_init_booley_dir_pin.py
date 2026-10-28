"""``booley init`` pins a symlinked ``.booley`` entry and re-pins only on consent."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from booley.core import booley_dir_pin
from booley.harness import init_cmd
from booley.harness.setup.common import InitContext

pytestmark = pytest.mark.skipif(os.name == "nt", reason="directory symlink requires privilege")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
    root = tmp_path / "project"
    root.mkdir()
    (root / ".booley").symlink_to(_new_target(tmp_path, "ext-a"), target_is_directory=True)
    return root


def _new_target(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    target.mkdir()
    (target / "booley.toml").write_text("[project]\n", encoding="utf-8")
    return target


def _retarget(project: Path, target: Path) -> None:
    (project / ".booley").unlink()
    (project / ".booley").symlink_to(target, target_is_directory=True)


def _reconcile(
    project: Path, *, interactive: bool = False, **flags: bool
) -> tuple[bool, InitContext]:
    args = type("Args", (), flags)()
    ctx = InitContext(
        project_root=project, interactive=interactive, check_only=flags.get("check_only", False)
    )
    return init_cmd._reconcile_booley_dir_pin(ctx, args), ctx


def _pin_file(project: Path) -> Path:
    return booley_dir_pin._store().root / booley_dir_pin._pin_filename(project)


def test_init_pins_an_unpinned_linked_entry(project: Path) -> None:
    proceed, ctx = _reconcile(project)
    assert proceed
    assert ctx.results[-1].status == "ok"
    booley_dir_pin.require_pinned_entry(project)
    assert booley_dir_pin.read_pin(project).target == str((project / ".booley").resolve())


def test_init_repins_only_with_flag(project: Path, tmp_path: Path) -> None:
    _reconcile(project)
    new = _new_target(tmp_path, "ext-b")
    _retarget(project, new)

    proceed, _ctx = _reconcile(project, repin_booley_dir=True)

    assert proceed
    assert booley_dir_pin.read_pin(project).target == str(new.resolve())
    booley_dir_pin.require_pinned_entry(project)


def test_interactive_prompt_decides_the_repin(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reconcile(project)
    _retarget(project, _new_target(tmp_path, "ext-b"))
    before = _pin_file(project).read_bytes()

    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    proceed, _ctx = _reconcile(project, interactive=True)
    assert not proceed
    assert _pin_file(project).read_bytes() == before

    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    proceed, _ctx = _reconcile(project, interactive=True)
    assert proceed
    booley_dir_pin.require_pinned_entry(project)


def test_flag_replaces_an_unreadable_pin_and_prompt_decline_is_reported(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reconcile(project)
    _pin_file(project).write_text("{not json", encoding="utf-8")

    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    proceed, ctx = _reconcile(project, interactive=True)
    assert not proceed
    assert ctx.results[-1].status == "err"

    proceed, _ctx = _reconcile(project, repin_booley_dir=True)
    assert proceed
    booley_dir_pin.require_pinned_entry(project)


def test_noninteractive_init_without_flag_keeps_pin_and_fails(
    project: Path, tmp_path: Path
) -> None:
    _reconcile(project)
    before = _pin_file(project).read_bytes()
    _retarget(project, _new_target(tmp_path, "ext-b"))

    proceed, ctx = _reconcile(project)

    assert not proceed
    assert _pin_file(project).read_bytes() == before
    assert (ctx.results[-1].name, ctx.results[-1].status) == ("booley_dir_pin", "err")
    assert "--repin-booley-dir" in ctx.results[-1].detail


def test_check_only_and_seed_never_pin(project: Path, tmp_path: Path) -> None:
    for mode in ({"check_only": True}, {"seed": True}):
        proceed, ctx = _reconcile(project, **mode)
        assert not proceed
        assert ctx.results[-1].status == "err"
        assert not _pin_file(project).exists()

    _reconcile(project)
    _retarget(project, _new_target(tmp_path, "ext-b"))
    before = _pin_file(project).read_bytes()
    for mode in ({"check_only": True}, {"seed": True}):
        proceed, ctx = _reconcile(project, repin_booley_dir=True, **mode)
        assert not proceed
        assert ctx.results[-1].status == "err"
        assert _pin_file(project).read_bytes() == before


def test_full_init_pins_before_bootstrap_and_stops_on_a_retargeted_link(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_init`` writes the pin first; a mismatch ends init before any step follows the link."""
    reached_bootstrap: list[bool] = []

    def stop_at_bootstrap(_ctx, _args):
        reached_bootstrap.append(True)

    monkeypatch.setattr(init_cmd, "_project_checkout_ready", lambda _ctx: True)
    monkeypatch.setattr(init_cmd, "_reconcile_init_bootstrap", stop_at_bootstrap)
    monkeypatch.setattr("sys.stdin", type("NoTty", (), {"isatty": lambda self: False})())
    args = type("Args", (), {})()

    assert init_cmd._run_init_unlocked(args, project) == 0
    assert reached_bootstrap == [True]
    booley_dir_pin.require_pinned_entry(project)

    reached_bootstrap.clear()
    _retarget(project, _new_target(tmp_path, "ext-b"))
    assert init_cmd._run_init_unlocked(args, project) == 2
    assert reached_bootstrap == []
