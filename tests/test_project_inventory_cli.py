"""Public ``booley projects`` command contracts."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from booley.harness import projects_view
from booley.harness.terminal_table import render_table
from booley.projects import cli as project_inventory_cli
from booley.projects import inventory as project_inventory
from booley.runtime import image_keepers as runtime_image_keepers
from booley.runtime.session_issuance import keeper_image


def _run(args: argparse.Namespace) -> int:
    return project_inventory_cli.run(
        args,
        keeper_operations=runtime_image_keepers,
        leftovers_view=projects_view.render_leftovers,
    )


def _run_without_keepers(argv: list[str]) -> int:
    return project_inventory_cli.run(
        _parser().parse_args(argv), leftovers_view=projects_view.render_leftovers
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    project_inventory_cli.add_subparser(parser.add_subparsers(dest="command"))
    return parser


def test_projects_json_has_a_versioned_nested_contract(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".booley").mkdir()
    project_inventory.remember_project(project)

    args = _parser().parse_args(["projects", "--json"])

    assert _run(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema": 1,
        "projects": [
            {
                "project_root": str(project.resolve()),
                "status": "present",
                "remembered": True,
                "grants": [],
            }
        ],
    }


def test_projects_discover_accepts_json_after_the_subcommand(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    search_root = tmp_path / "workplace"
    project = search_root / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".booley").mkdir()

    args = _parser().parse_args(["projects", "discover", str(search_root), "--json"])

    assert _run(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema": 1,
        "discovered": [str(project.resolve())],
    }


def test_projects_forget_reports_the_exact_root(
    tmp_path: Path, monkeypatch, capsys, keeper_host
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    project = tmp_path / "project"
    (project / ".booley").mkdir(parents=True)
    project_inventory.remember_project(project)

    args = _parser().parse_args(["projects", "forget", str(project), "--json"])

    assert _run(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema": 1,
        "forgotten": str(project.resolve()),
        "keeper": {
            "status": "absent",
            "tag": keeper_image(project),
            "image_id": None,
        },
        "purge": None,
        "leftovers": [],
    }


def test_projects_human_output_groups_status_and_grants(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        project_inventory,
        "project_inventory",
        lambda: (
            project_inventory.ProjectInventoryEntry(
                "/deleted/project",
                project_inventory.ProjectStatus.MISSING,
                False,
                (project_inventory.ProjectGrantSummary("vivado", "vivado_2025_2", "site"),),
            ),
        ),
    )

    assert _run(_parser().parse_args(["projects"])) == 0

    output = capsys.readouterr().out
    assert "/deleted/project [missing; grant only]" in output
    assert "vivado_2025_2" in output
    assert "site" in output


def test_projects_discover_human_output_lists_remembered_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    monkeypatch.setattr(project_inventory, "discover_projects", lambda _roots: (project,))

    args = _parser().parse_args(["projects", "discover", str(tmp_path)])

    assert _run(args) == 0
    assert f"Remembered 1 Project Root(s):\n  {project}" in capsys.readouterr().out


def test_projects_forget_human_output_names_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    from booley.projects import image_keepers

    monkeypatch.setattr(
        runtime_image_keepers,
        "forget_project",
        lambda _project, purge: (project, image_keepers.KeeperRelease("absent", "keeper"), None),
    )
    monkeypatch.setattr(runtime_image_keepers, "project_leftovers", lambda _project: ())

    args = _parser().parse_args(["projects", "forget", str(project)])

    assert _run(args) == 0
    output = capsys.readouterr().out
    assert f"Forgot Project path: {project}" in output
    assert "No Docker resources left behind for this Project." in output


def test_forget_lists_leftovers_with_sizes_and_tags(
    tmp_path: Path, keeper_host: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.config.sandbox import project_image_name

    project = _remember(tmp_path)
    image = "sha256:" + "a" * 64
    keeper_host.tags.update({f"{project_image_name(project)}:latest": image, "mirror:x": image})
    assert _run(_parser().parse_args(["projects", "forget", str(project)])) == 0
    table = render_table(
        ["Kind", "Name", "Size", "Reason", "Tags"],
        [["image", image, "1GB", "shared", f"mirror:x, {project_image_name(project)}:latest"]],
    )
    assert "Left behind for this Project:\n" + "\n".join(table) + "\n" in capsys.readouterr().out


def test_forget_purge_removes_unused_own_tags_and_names_the_rest(
    tmp_path: Path, keeper_host: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.config.sandbox import project_image_name

    project = _remember(tmp_path)
    family = project_image_name(project)
    unused, shared = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    keeper_host.tags.update({f"{family}:latest": unused, f"{family}-wheel:latest": shared})
    keeper_host.tags["mirror:x"] = shared
    assert _run(_parser().parse_args(["projects", "forget", str(project)])) == 0
    assert f"booley projects forget --purge {project}" in capsys.readouterr().out
    assert _run(_parser().parse_args(["projects", "forget", "--purge", str(project)])) == 0
    output = capsys.readouterr().out
    assert f"Removed: {family}-wheel:latest\nRemoved: {family}:latest\n" in output
    assert "No Docker resources left behind" in output and "mirror:x" in keeper_host.tags


@pytest.mark.parametrize("grant", [False, True])
def test_purge_accepts_a_forgotten_root_unless_a_grant_holds_it(
    tmp_path: Path,
    keeper_host: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    grant: bool,
) -> None:
    from types import SimpleNamespace

    from booley.config.sandbox import project_image_name, project_image_name_for_identity

    deleted = tmp_path / "deleted"
    keeper_host.tags[f"{project_image_name_for_identity(str(deleted))}:latest"] = (
        "sha256:" + "a" * 64
    )
    # The old path now points at another Project; purge must not follow it there.
    other = _remember(tmp_path / "other-root")  # a remembered Project with its own images
    keeper_host.tags[f"{project_image_name(other)}:latest"] = "sha256:" + "c" * 64
    deleted.symlink_to(other, target_is_directory=True)
    grants = (
        (
            SimpleNamespace(
                project_root=str(deleted), kind="vivado", installation=None, license_profile=None
            ),
        )
        if grant
        else ()
    )
    monkeypatch.setattr(project_inventory, "_authority_grants", lambda: grants)
    never = tmp_path / "never-remembered"
    assert _run(_parser().parse_args(["projects", "forget", str(never)])) == 2
    assert "not remembered" in capsys.readouterr().err
    flags = ["projects", "forget", "--purge", str(deleted), "--json"]
    assert _run(_parser().parse_args(flags)) == (2 if grant else 0)
    if grant:
        assert "revoke" in capsys.readouterr().err and len(keeper_host.tags) == 2
    else:
        assert json.loads(capsys.readouterr().out)["purge"]["removed"] == [
            f"{project_image_name_for_identity(str(deleted))}:latest"
        ]
    assert f"{project_image_name(other)}:latest" in keeper_host.tags
    assert str(other) in [entry.project_root for entry in project_inventory.project_inventory()]


def test_forget_reports_leftover_listing_failure_after_forgetting(
    tmp_path: Path,
    keeper_host: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _remember(tmp_path)

    def fail(_identity: str) -> tuple[()]:
        raise RuntimeError("daemon went away")

    monkeypatch.setattr(runtime_image_keepers.project_resources, "leftovers", fail)
    assert _run(_parser().parse_args(["projects", "forget", str(project)])) == 2
    error = capsys.readouterr().err
    assert "could not list its leftover Docker resources: daemon went away" in error
    assert f"(keeper absent: {keeper_image(project)})" in error
    assert project_inventory.project_inventory() == ()


def test_projects_human_output_explains_empty_inventory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(project_inventory, "project_inventory", lambda: ())

    assert _run(_parser().parse_args(["projects"])) == 0

    assert "No remembered Project paths or Project Grants" in capsys.readouterr().out


def test_projects_human_output_explains_root_without_grants(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        project_inventory,
        "project_inventory",
        lambda: (
            project_inventory.ProjectInventoryEntry(
                "/project", project_inventory.ProjectStatus.PRESENT, True, ()
            ),
        ),
    )

    assert _run(_parser().parse_args(["projects"])) == 0

    assert "Grants: none" in capsys.readouterr().out


def test_projects_reports_inventory_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> tuple[project_inventory.ProjectInventoryEntry, ...]:
        raise project_inventory.ProjectInventoryError("corrupt inventory")

    monkeypatch.setattr(project_inventory, "project_inventory", fail)

    assert _run(_parser().parse_args(["projects"])) == 2

    assert "ERROR: corrupt inventory" in capsys.readouterr().err


class FakeDocker:
    """Stateful daemon boundary; every mutation is an exact non-force tag release."""

    def __init__(self) -> None:
        self.tags: dict[str, str] = {}
        self.containers: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.fail_remove: str | None = None

    def __call__(self, args: Any, **kwargs: Any) -> Any:  # noqa: PLR0911 -- one branch per Docker command
        import subprocess

        self.calls.append(args)
        if args[:2] == ["image", "ls"]:
            # The leftover report asks for ID, reference and size rows.
            rows = (
                "".join(
                    f"{image}\t{tag}\t1GB\n"
                    for tag, image in self.tags.items()
                    if not tag.startswith("sha256:")
                )
                if "--all" in args
                else "\n".join(self.tags)
            )
            return subprocess.CompletedProcess(args, 0, rows, "")
        if args[:2] in (["volume", "ls"], ["network", "ls"]) or args[:4] == [
            "container",
            "ls",
            "--all",
            "--filter",
        ]:
            # No state volumes, networks, or Project-labelled containers on this host.
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["container", "ls"]:
            return subprocess.CompletedProcess(args, 0, "\n".join(self.containers), "")
        if args[:2] == ["container", "inspect"]:
            return subprocess.CompletedProcess(args, 0, self.containers[args[2]], "")
        if args[:2] == ["image", "inspect"]:
            # Purge orders tags by layer count; every fake image has one layer.
            value = "1" if "{{len .RootFS.Layers}}" in args else self.tags.get(args[2])
            return subprocess.CompletedProcess(
                args, 0 if value else 1, value or "", "" if value else "No such image"
            )
        if args[:2] == ["image", "rm"]:
            # Only the Project's own keeper or managed image tags are ever released.
            assert args[2] == "--no-prune" and len(args) == 4
            assert args[3].startswith("booley-issued-") or "-booley-sandbox-" in args[3]
            if args[3] == self.fail_remove:
                return subprocess.CompletedProcess(args, 1, "", "removal denied")
            self.tags.pop(args[3], None)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)


@pytest.fixture
def keeper_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    from booley.runtime import interactive_docker

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    docker = FakeDocker()
    monkeypatch.setattr(interactive_docker, "_run_docker", docker)
    return docker


def test_public_forget_releases_only_own_unused_keeper(
    tmp_path: Path, keeper_host: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.runtime.session_issuance import keeper_image

    project = tmp_path / "project"
    (project / ".booley").mkdir(parents=True)
    project_inventory.remember_project(project)
    own = keeper_image(project)
    other = keeper_image(tmp_path / "other")
    keeper_host.tags.update(
        {
            own: "sha256:" + "a" * 64,
            other: "sha256:" + "b" * 64,
            "unrelated:latest": "sha256:" + "a" * 64,
        }
    )
    assert _run(_parser().parse_args(["projects", "forget", str(project), "--json"])) == 0
    assert own not in keeper_host.tags
    assert other in keeper_host.tags and "unrelated:latest" in keeper_host.tags
    assert json.loads(capsys.readouterr().out)["keeper"]["status"] == "released"


@pytest.mark.parametrize(
    "flags", [["projects", "--json", "prune-keepers"], ["projects", "prune-keepers", "--json"]]
)
def test_public_prune_preview_then_digest_confirmation(
    tmp_path: Path, keeper_host: FakeDocker, capsys: pytest.CaptureFixture[str], flags: Any
) -> None:
    from booley.runtime.session_issuance import keeper_image

    project = tmp_path / "project"
    (project / ".booley").mkdir(parents=True)
    project_inventory.remember_project(project)
    own, orphan = keeper_image(project), keeper_image(tmp_path / "legacy")
    keeper_host.tags.update({own: "sha256:" + "a" * 64, orphan: "sha256:" + "b" * 64})
    assert _run(_parser().parse_args(flags)) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["released"] == [] and orphan in keeper_host.tags
    assert _run(_parser().parse_args([*flags, "--confirm", preview["digest"]])) == 0
    assert orphan not in keeper_host.tags and own in keeper_host.tags


def _remember(tmp_path):
    project = tmp_path / "project"
    (project / ".booley").mkdir(parents=True)
    project_inventory.remember_project(project)
    return project


def _prune(capsys, confirm=None):
    flags = ["projects", "prune-keepers", "--json"]
    if confirm is not None:
        flags.extend(["--confirm", confirm])
    status = _run(_parser().parse_args(flags))
    return status, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("kind", ["foreign-running", "vscode-stopped", "created"])
def test_forget_retains_image_used_by_any_container(
    tmp_path: Path, keeper_host: FakeDocker, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    from booley.runtime.session_issuance import keeper_image

    project = _remember(tmp_path)
    tag = keeper_image(project)
    keeper_host.tags[tag] = "sha256:" + "a" * 64
    # .Image remains immutable even when Config.Image was a different tag.
    keeper_host.containers["c" * 64] = keeper_host.tags[tag]
    assert _run(_parser().parse_args(["projects", "forget", str(project), "--json"])) == 0
    assert json.loads(capsys.readouterr().out)["keeper"]["status"] == "retained-in-use"
    assert tag in keeper_host.tags and project_inventory.project_inventory() == ()


def test_forget_removal_failure_preserves_inventory(
    tmp_path: Path, keeper_host: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.runtime.session_issuance import keeper_image

    project = _remember(tmp_path)
    tag = keeper_image(project)
    keeper_host.tags[tag] = "sha256:" + "a" * 64
    keeper_host.fail_remove = tag
    assert _run(_parser().parse_args(["projects", "forget", str(project)])) == 2
    assert "removal denied" in capsys.readouterr().err
    assert project_inventory.project_inventory()[0].remembered


def test_prune_stale_confirmation_and_shared_container_protection(
    tmp_path: Path, keeper_host: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.runtime.session_issuance import keeper_image

    orphan = keeper_image(tmp_path / "orphan")
    keeper_host.tags[orphan] = "sha256:" + "a" * 64
    status, preview = _prune(capsys)
    assert status == 0
    keeper_host.containers["c" * 64] = keeper_host.tags[orphan]
    status, result = _prune(capsys, preview["digest"])
    assert status == 2 and result["released"] == [] and orphan in keeper_host.tags


def test_prune_inventory_remember_invalidates_confirmation(
    tmp_path: Path, keeper_host: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.runtime.session_issuance import keeper_image

    project = tmp_path / "project"
    keeper_host.tags[keeper_image(project)] = "sha256:" + "a" * 64
    _, preview = _prune(capsys)
    _remember(tmp_path)
    status, result = _prune(capsys, preview["digest"])
    assert status == 2 and result["released"] == []


def test_prune_digest_ignores_unrelated_observations(
    tmp_path: Path, keeper_host: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.runtime.session_issuance import keeper_image

    keeper_host.tags[keeper_image(tmp_path / "orphan")] = "sha256:" + "a" * 64
    _, first = _prune(capsys)
    keeper_host.tags["unrelated:latest"] = "sha256:" + "b" * 64
    keeper_host.containers["c" * 64] = "sha256:" + "b" * 64
    _, second = _prune(capsys)
    assert first["digest"] == second["digest"]


def _nothing_eligible_host(tmp_path: Path, keeper_host: FakeDocker) -> None:
    """Two keepers, both protected: one inventoried, one orphan used by a container."""
    project = _remember(tmp_path)
    in_use = keeper_image(tmp_path / "orphan")
    keeper_host.tags[keeper_image(project)] = "sha256:" + "a" * 64
    keeper_host.tags[in_use] = "sha256:" + "b" * 64
    keeper_host.containers["c" * 64] = keeper_host.tags[in_use]


def test_prune_with_nothing_eligible_offers_no_confirmation(
    tmp_path: Path, keeper_host: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    _nothing_eligible_host(tmp_path, keeper_host)
    assert _run(_parser().parse_args(["projects", "prune-keepers"])) == 0
    human = capsys.readouterr().out
    assert "Nothing to release." in human
    assert "Preview digest" not in human and "--confirm" not in human
    status, preview = _prune(capsys)
    assert status == 0
    assert preview["digest"] is None and preview["schema"] == 1
    assert preview["candidates"] and not any(item["eligible"] for item in preview["candidates"])
    assert set(preview) == {"digest", "candidates", "released", "retained", "errors", "schema"}


def test_prune_confirm_with_nothing_eligible_is_refused(
    tmp_path: Path,
    keeper_host: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import hashlib

    from booley.runtime import image_lifecycle

    cleanups: list[str] = []
    monkeypatch.setattr(
        image_lifecycle, "cleanup_owned_images", lambda *a, **k: cleanups.append("called") or []
    )
    _nothing_eligible_host(tmp_path, keeper_host)
    tags_before = dict(keeper_host.tags)
    _, preview = _prune(capsys)
    # Deliberately mirrors the digest algorithm in runtime.image_keepers._preview (unchanged
    # for eligible snapshots): it is the digest an older preview would have printed here.
    copied = hashlib.sha256(
        json.dumps(preview["candidates"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    for confirm in (copied, "0" * 64):
        status, result = _prune(capsys, confirm)
        assert status == 2, confirm
        assert any("nothing to release" in error for error in result["errors"])
        assert result["released"] == []
    assert keeper_host.tags == tags_before
    assert not any(call[:2] == ["image", "rm"] for call in keeper_host.calls)
    assert cleanups == []


def test_prune_partial_failure_accounts_for_prior_releases(
    tmp_path: Path, keeper_host: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.runtime.session_issuance import keeper_image

    tags = sorted([keeper_image(tmp_path / "one"), keeper_image(tmp_path / "two")])
    keeper_host.tags.update(dict.fromkeys(tags, "sha256:" + "a" * 64))
    keeper_host.fail_remove = tags[1]
    _, preview = _prune(capsys)
    status, result = _prune(capsys, preview["digest"])
    assert status == 2 and result["released"] == [tags[0]]
    assert result["retained"] == [tags[1]] and result["errors"]
    keeper_host.fail_remove = None
    _, preview = _prune(capsys)
    assert _prune(capsys, preview["digest"])[0] == 0


@pytest.mark.parametrize("stamp", ["runtime-issuance.json", "session-spec.json"])
def test_prune_inventory_absence_authorizes_historical_stamps(
    tmp_path: Path, keeper_host: FakeDocker, capsys: pytest.CaptureFixture[str], stamp: str
) -> None:
    from booley.runtime.session_issuance import keeper_image

    project = tmp_path / "old"
    (project / ".booley").mkdir(parents=True)
    (project / ".booley" / stamp).write_text(json.dumps({"project_root": str(project)}))
    tag = keeper_image(project)
    keeper_host.tags[tag] = "sha256:" + "a" * 64
    _, preview = _prune(capsys)
    assert preview["candidates"][0]["reason"] == "inventory-absent"
    assert _prune(capsys, preview["digest"])[0] == 0 and tag not in keeper_host.tags


def test_forget_grant_guard_performs_no_docker_work(
    tmp_path: Path,
    keeper_host: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from types import SimpleNamespace

    project = _remember(tmp_path)
    monkeypatch.setattr(
        project_inventory,
        "_authority_grants",
        lambda: (SimpleNamespace(project_root=str(project)),),
    )
    assert _run(_parser().parse_args(["projects", "forget", str(project)])) == 2
    assert "revoke" in capsys.readouterr().err and keeper_host.calls == []


@pytest.mark.parametrize("change", ["missing", "uninitialized", "replacement-symlink"])
def test_forget_uses_exact_stored_identity(
    tmp_path: Path, keeper_host: FakeDocker, capsys: pytest.CaptureFixture[str], change: str
) -> None:
    import shutil

    project = _remember(tmp_path)
    own = keeper_image(project)
    keeper_host.tags[own] = "sha256:" + "a" * 64
    if change == "uninitialized":
        shutil.rmtree(project / ".booley")
    else:
        shutil.rmtree(project)
        if change == "replacement-symlink":
            target = tmp_path / "replacement"
            target.mkdir()
            project.symlink_to(target, target_is_directory=True)
    assert _run(_parser().parse_args(["projects", "forget", str(project), "--json"])) == 0
    assert json.loads(capsys.readouterr().out)["keeper"]["tag"] == own
    assert own not in keeper_host.tags


def test_prune_protects_missing_uninitialized_and_grant_only_roots(
    tmp_path: Path,
    keeper_host: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import shutil
    from types import SimpleNamespace

    project = _remember(tmp_path)
    missing = tmp_path / "missing"
    (missing / ".booley").mkdir(parents=True)
    project_inventory.remember_project(missing)
    shutil.rmtree(missing)
    shutil.rmtree(project / ".booley")
    grant_root = str(tmp_path / "grant-only")
    monkeypatch.setattr(
        project_inventory,
        "_authority_grants",
        lambda: (
            SimpleNamespace(
                project_root=grant_root, kind="vivado", installation=None, license_profile=None
            ),
        ),
    )
    tags = [keeper_image(project), keeper_image(missing), keeper_image(Path(grant_root))]
    keeper_host.tags.update(dict.fromkeys(tags, "sha256:" + "a" * 64))
    keeper_host.tags["booley-issued-BAD:session"] = "sha256:" + "a" * 64
    _, preview = _prune(capsys)
    assert all(item["reason"] == "inventoried" for item in preview["candidates"])
    assert preview["digest"] is None and _prune(capsys, "0" * 64)[0] == 2
    assert len(keeper_host.tags) == 4


@pytest.mark.parametrize("operation", ["forget", "prune-keepers"])
def test_lifecycle_contention_becomes_cli_error(
    tmp_path: Path,
    keeper_host: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operation: str,
) -> None:
    from contextlib import contextmanager

    from booley.runtime.lifecycle_lock import LifecycleLockError

    project = _remember(tmp_path)

    @contextmanager
    def busy(_operation: Any) -> Iterator[None]:
        raise LifecycleLockError("lifecycle busy")
        yield

    monkeypatch.setattr(runtime_image_keepers, "host_lifecycle_lock", busy)
    flags = (
        ["projects", operation, str(project)]
        if operation == "forget"
        else ["projects", operation, "--confirm", "digest"]
    )
    assert _run(_parser().parse_args(flags)) == 2
    assert "lifecycle busy" in capsys.readouterr().err and keeper_host.calls == []


@pytest.mark.parametrize("mutation", ["retag", "container", "grant"])
def test_apply_rechecks_before_deletion(
    tmp_path: Path,
    keeper_host: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mutation: str,
) -> None:
    from types import SimpleNamespace

    tag = keeper_image(tmp_path / "orphan")
    keeper_host.tags[tag] = "sha256:" + "a" * 64
    _, preview = _prune(capsys)
    original = keeper_host.__call__
    count = 0

    def mutate(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal count
        result = original(args, **kwargs)
        if args[:2] == ["image", "inspect"]:
            count += 1
            if count == 1:
                if mutation == "retag":
                    keeper_host.tags[tag] = "sha256:" + "b" * 64
                elif mutation == "container":
                    keeper_host.containers["c" * 64] = "sha256:" + "a" * 64
                else:
                    monkeypatch.setattr(
                        project_inventory,
                        "_authority_grants",
                        lambda: (SimpleNamespace(project_root=str(tmp_path / "orphan")),),
                    )
        return result

    from booley.runtime import interactive_docker

    monkeypatch.setattr(interactive_docker, "_run_docker", mutate)
    status, result = _prune(capsys, preview["digest"])
    assert status == 0 and result["released"] == [] and result["retained"] == [tag]
    assert tag in keeper_host.tags


def test_forget_release_requires_reissuance_before_admission(
    tmp_path: Path,
    keeper_host: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from booley.runtime import devcontainer as dc
    from booley.runtime import interactive_docker
    from booley.runtime import session_issuance as issuance
    from tests.runtime.test_session_issuance import _install_trusted_validator

    validator = _install_trusted_validator(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", str(validator.parent))
    project = _remember(tmp_path)
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    image = "sha256:" + "a" * 64
    keeper_host.tags[spec["image"]] = image
    keeper_host.tags[image] = image
    monkeypatch.setattr(
        issuance,
        "_resolve_image_id",
        lambda ref: (
            keeper_host.tags[ref]
            if ref in keeper_host.tags
            else (_ for _ in ()).throw(issuance.RuntimeSpecError("missing image"))
        ),
    )
    monkeypatch.setattr(issuance, "_project_data_alias_capable", lambda _image: False)
    issuance.pin_image(spec)
    issuance.seal(project, spec)
    path = dc.write_devcontainer(project, spec)
    monkeypatch.setattr(
        interactive_docker,
        "tag_image",
        lambda source, target: keeper_host.tags.update({target: source}),
    )
    stamp = issuance.issue(project, spec, path)
    assert _run(_parser().parse_args(["projects", "forget", str(project)])) == 0
    capsys.readouterr()
    assert issuance.stamp_path(project).exists()
    with pytest.raises(issuance.RuntimeSpecError, match="keeper is missing"):
        issuance.validate(project, spec, path)
    issuance.issue(project, spec, path)
    assert issuance.validate(project, spec, path).image_id == stamp.image_id


def test_actual_vscode_down_then_forget_retains_stopped_container(
    tmp_path: Path,
    keeper_host: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from types import SimpleNamespace

    from booley.runtime import interactive_docker, session_admission, session_runtime

    project = _remember(tmp_path)
    tag = keeper_image(project)
    image = "sha256:" + "a" * 64
    container = "c" * 64
    keeper_host.tags[tag] = image
    keeper_host.containers[container] = image
    item = SimpleNamespace(container_id=container, name="editor", running=True, vscode=True)
    monkeypatch.setattr(
        session_admission,
        "running_project_sandboxes",
        lambda *args, **kwargs: (item,) if item.running else (),
    )
    monkeypatch.setattr(session_admission, "clear_vscode_claim", lambda _root: False)
    monkeypatch.setattr(interactive_docker, "container_exists", lambda _name: False)
    monkeypatch.setattr(session_runtime, "_relay_objects_exist", lambda _relay: False)
    monkeypatch.setattr(session_runtime, "_recover_before_lifecycle", lambda *args: None)
    commands = []

    def run(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        pytest.fail(f"CLI must not mutate the editor Sandbox: {args}")

    monkeypatch.setattr(session_runtime, "_run", run)
    with pytest.raises(session_runtime.SessionError, match="VS Code owns"):
        session_runtime.down(project)
    assert item.running and commands == [] and container in keeper_host.containers
    # Dev Containers owns stopping its Sandbox; the stopped object still pins its image.
    item.running = False
    assert _run(_parser().parse_args(["projects", "forget", str(project), "--json"])) == 0
    assert json.loads(capsys.readouterr().out)["keeper"]["status"] == "retained-in-use"
    assert tag in keeper_host.tags


@pytest.mark.parametrize("failure", ["invalid-image", "invalid-container", "daemon"])
def test_forget_incomplete_observation_preserves_inventory(
    tmp_path: Path,
    keeper_host: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Any,
) -> None:
    import subprocess

    from booley.runtime import interactive_docker

    project = _remember(tmp_path)
    tag = keeper_image(project)
    keeper_host.tags[tag] = "not-an-image" if failure == "invalid-image" else "sha256:" + "a" * 64
    if failure == "invalid-container":
        keeper_host.containers["bad-container"] = keeper_host.tags[tag]
    if failure == "daemon":
        monkeypatch.setattr(
            interactive_docker,
            "_run_docker",
            lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", "daemon unavailable"),
        )
    assert _run(_parser().parse_args(["projects", "forget", str(project)])) == 2
    assert capsys.readouterr().err and project_inventory.project_inventory()[0].remembered
    assert tag in keeper_host.tags


@pytest.mark.parametrize("operation", ["forget", "prune-keepers"])
def test_keeper_release_refuses_pending_recovery(
    tmp_path: Path,
    keeper_host: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operation: str,
) -> None:
    project = _remember(tmp_path)
    tag = keeper_image(project)
    keeper_host.tags[tag] = "sha256:" + "a" * 64
    monkeypatch.setattr(
        runtime_image_keepers, "shared_recovery_blocks_command", lambda **kwargs: True
    )
    flags = (
        ["projects", "forget", str(project)]
        if operation == "forget"
        else ["projects", "prune-keepers", "--confirm", "digest"]
    )
    assert _run(_parser().parse_args(flags)) == 2
    assert "requires recovery" in capsys.readouterr().err
    assert keeper_host.calls == [] and tag in keeper_host.tags
    assert project_inventory.project_inventory()[0].remembered


def test_human_keeper_outcomes_identify_image_and_retained_tags(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from booley.projects import image_keepers

    image = "sha256:" + "a" * 64
    release = image_keepers.KeeperRelease("retained-in-use", "keeper", image)
    assert (
        project_inventory_cli._render_forgotten(
            Path("/project"),
            release,
            None,
            (),
            json_output=False,
            leftovers_view=projects_view.render_leftovers,
        )
        == 0
    )
    assert image in capsys.readouterr().out
    result = image_keepers.PruneResult("digest", (), retained=["changed", "unresolved"])
    assert project_inventory_cli._render_prune(result, json_output=False) == 0
    output = capsys.readouterr().out
    assert "Keeper tag retained or unresolved: changed" in output
    assert "Keeper tag retained or unresolved: unresolved" in output


@pytest.mark.parametrize("operation", ["forget", "prune-keepers"])
@pytest.mark.parametrize("journal_kind", ["refresh", "invalidation", "malformed-refresh"])
def test_real_pending_journal_preserves_keeper_and_inventory(
    tmp_path: Path,
    keeper_host: FakeDocker,
    capsys: pytest.CaptureFixture[str],
    operation: str,
    journal_kind: str,
) -> None:
    from booley.runtime import issuance_invalidation
    from tests.harness.test_session_refresh_recovery import _write_restore_journal

    project = _remember(tmp_path)
    tag = keeper_image(project)
    keeper_host.tags[tag] = "sha256:" + "a" * 64
    if operation == "prune-keepers":
        project_inventory.forget_project(project)
        _, preview = _prune(capsys)
    if journal_kind == "invalidation":
        issuance_invalidation.prepare(str(project))
        journal = issuance_invalidation._path(str(project))
    else:
        journal = _write_restore_journal(tmp_path / "config", project)
        if journal_kind == "malformed-refresh":
            journal.write_text("{invalid")
    before_journal = journal.read_bytes()
    before_inventory = project_inventory.state_path().read_bytes()
    keeper_host.calls.clear()
    flags = (
        ["projects", "forget", str(project)]
        if operation == "forget"
        else ["projects", "prune-keepers", "--confirm", preview["digest"]]
    )
    assert _run(_parser().parse_args(flags)) == 2
    assert capsys.readouterr().err
    assert keeper_host.calls == [] and tag in keeper_host.tags
    assert journal.read_bytes() == before_journal
    assert project_inventory.state_path().read_bytes() == before_inventory


@pytest.mark.parametrize("action", ["forget", "preview", "confirm"])
def test_missing_keeper_adapter_refuses_before_observation(
    tmp_path: Path, keeper_host: FakeDocker, capsys: pytest.CaptureFixture[str], action: str
) -> None:
    from tests.harness.test_session_refresh_recovery import _write_restore_journal

    project = _remember(tmp_path)
    tag = keeper_image(project)
    keeper_host.tags[tag] = "sha256:" + "a" * 64
    journal = _write_restore_journal(tmp_path / "config", project)
    before = (project_inventory.state_path().read_bytes(), journal.read_bytes())
    flags = (
        ["projects", "forget", str(project)]
        if action == "forget"
        else ["projects", "prune-keepers"]
    )
    if action == "confirm":
        flags.extend(["--confirm", "digest"])
    assert _run_without_keepers(flags) == 2
    assert capsys.readouterr().err.strip() == (
        "ERROR: Project keeper access requires host lifecycle coordination"
    )
    assert keeper_host.calls == [] and tag in keeper_host.tags
    assert (project_inventory.state_path().read_bytes(), journal.read_bytes()) == before


@pytest.fixture
def public_projects_host(
    keeper_host: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> Callable[[list[str]], int]:
    from booley.harness import booley
    from booley.runtime import host_install, runtime_context
    from booley.runtime.paths import skills_dir

    monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
    observations: list[str] = []

    def inside() -> bool:
        observations.append("venue")
        return False

    def installed(directory: Path) -> None:
        assert directory == skills_dir()
        observations.append("installation")

    monkeypatch.setattr(runtime_context, "inside_session_runtime", inside)
    monkeypatch.setattr(host_install, "host_install_error", installed)

    def invoke(flags: list[str]) -> int:
        before = len(observations)
        monkeypatch.setattr(sys, "argv", ["booley", *flags])
        result = booley.main()
        assert observations[before:] == ["venue", "installation"]
        return result

    return invoke


def _public_success(
    invoke: Callable[[list[str]], int],
    flags: list[str],
    docker: FakeDocker,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_bytes = project_inventory.state_path().read_bytes()
    tags = dict(docker.tags)
    calls = len(docker.calls)
    result = invoke(flags)
    if result != 0:
        assert capsys.readouterr().err.strip() == (
            "ERROR: Project keeper access requires host lifecycle coordination"
        )
        assert project_inventory.state_path().read_bytes() == inventory_bytes
        assert docker.tags == tags and len(docker.calls) == calls
    assert result == 0


def test_real_public_forget_uses_keeper_service(
    tmp_path: Path,
    keeper_host: FakeDocker,
    public_projects_host: Callable[[list[str]], int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _remember(tmp_path)
    own, other = keeper_image(project), keeper_image(tmp_path / "other")
    keeper_host.tags.update({own: "sha256:" + "a" * 64, other: "sha256:" + "b" * 64})
    _public_success(
        public_projects_host, ["projects", "forget", str(project), "--json"], keeper_host, capsys
    )
    assert json.loads(capsys.readouterr().out)["keeper"]["status"] == "released"
    assert own not in keeper_host.tags and other in keeper_host.tags
    assert project_inventory.project_inventory() == ()


@pytest.mark.parametrize(
    "flags", [["projects", "--json", "prune-keepers"], ["projects", "prune-keepers", "--json"]]
)
def test_real_public_prune_uses_keeper_service(
    tmp_path: Path,
    keeper_host: FakeDocker,
    public_projects_host: Callable[[list[str]], int],
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
) -> None:
    project = _remember(tmp_path)
    own, orphan = keeper_image(project), keeper_image(tmp_path / "orphan")
    keeper_host.tags.update({own: "sha256:" + "a" * 64, orphan: "sha256:" + "b" * 64})
    _public_success(public_projects_host, flags, keeper_host, capsys)
    preview = json.loads(capsys.readouterr().out)
    assert preview["released"] == [] and orphan in keeper_host.tags
    _public_success(
        public_projects_host, [*flags, "--confirm", preview["digest"]], keeper_host, capsys
    )
    assert json.loads(capsys.readouterr().out)["released"] == [orphan]
    assert orphan not in keeper_host.tags and own in keeper_host.tags


def test_real_module_operations_support_keywords_and_preview_default(
    tmp_path: Path, keeper_host: FakeDocker
) -> None:
    project = _remember(tmp_path)
    own = keeper_image(project)
    keeper_host.tags[own] = "sha256:" + "a" * 64
    preview = runtime_image_keepers.prune_keepers()
    assert preview.released == [] and not preview.candidates[0].eligible
    root, release, _removed = runtime_image_keepers.forget_project(project=project)
    assert root == project and release.status == "released" and own not in keeper_host.tags
    orphan = keeper_image(tmp_path / "orphan")
    keeper_host.tags[orphan] = "sha256:" + "b" * 64
    preview = runtime_image_keepers.prune_keepers()
    assert preview.released == [] and orphan in keeper_host.tags
    result = runtime_image_keepers.prune_keepers(confirm=preview.digest)
    assert result.released == [orphan] and orphan not in keeper_host.tags


def test_inventory_commands_without_keeper_adapter_still_work(
    tmp_path: Path, keeper_host: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "discover"
    (project / ".booley").mkdir(parents=True)
    (project / ".git").mkdir()
    assert _run_without_keepers(["projects", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["projects"] == []
    assert _run_without_keepers(["projects", "discover", str(project), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["discovered"] == [str(project)]
    assert keeper_host.calls == []
