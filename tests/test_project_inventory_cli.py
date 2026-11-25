"""Public ``booley projects`` command contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from booley.config.sandbox import project_image_name, project_image_name_for_identity
from booley.core.host_shell import quote_arg
from booley.eda.provisioning import authority
from booley.eda.provisioning.policies.vivado import Inspection
from booley.harness import projects_view
from booley.presentation.terminal_table import render_table
from booley.projects import cli as project_inventory_cli
from booley.projects import inventory as project_inventory
from booley.runtime import image_keepers as runtime_image_keepers
from booley.runtime import (
    image_lifecycle,
    issuance_invalidation,
    session_admission,
    session_issuance,
)
from booley.runtime.lifecycle_lock import LifecycleLockError
from booley.runtime.session_issuance import keeper_image
from tests import eda_grant_support
from tests.docker_image_cli_fake import DockerImageCli, serve_docker_image_cli

_A, _B, _C = ("sha256:" + digit * 64 for digit in "abc")
_CONTAINER = "c" * 64


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> DockerImageCli:
    return serve_docker_image_cli(monkeypatch)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    project_inventory_cli.add_subparser(parser.add_subparsers(dest="command"))
    return parser


def _cli(*argv: str) -> int:
    return project_inventory_cli.run(
        _parser().parse_args(argv),
        keeper_operations=runtime_image_keepers,
        leftovers_view=projects_view.render_leftovers,
    )


def _run_without_keepers(*argv: str) -> int:
    return project_inventory_cli.run(
        _parser().parse_args(argv), leftovers_view=projects_view.render_leftovers
    )


def _remember(tmp_path: Path, name: str = "project") -> Path:
    project = tmp_path / name
    (project / ".booley").mkdir(parents=True)
    project_inventory.remember_project(project)
    return project


def _grant(root: Path) -> None:
    """A real license-only Project Grant, which needs no installation inspection."""
    eda_grant_support.register_license()
    eda_grant_support.add_grant(root, license_profile="registered")


def _prune(capsys: pytest.CaptureFixture[str], confirm: str | None = None) -> tuple[int, Any]:
    argv = ["projects", "prune-keepers", "--json"]
    if confirm is not None:
        argv.extend(["--confirm", confirm])
    status = _cli(*argv)
    return status, json.loads(capsys.readouterr().out)


def _write_session_files(project_root: str) -> None:
    """Give a Project root both a Sandbox issuance stamp and an admission claim."""
    document = json.dumps({"project_root": project_root})
    session_issuance.stamp_store().write(project_root, document)
    session_admission.claim_store().write(project_root, document)


def _session_files_remain(project_root: str) -> bool:
    return (
        session_issuance.stamp_store().locate(project_root) is not None
        or session_admission.claim_store().locate(project_root) is not None
    )


def test_discover_and_list_need_no_keeper_operations(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str]
) -> None:
    search_root = tmp_path / "workplace"
    project = search_root / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".booley").mkdir()
    root = str(project.resolve())

    assert _run_without_keepers("projects", "discover", str(search_root)) == 0
    assert capsys.readouterr().out == f"Remembered 1 Project Root(s):\n  {root}\n"
    assert _run_without_keepers("projects", "discover", str(search_root), "--json") == 0
    assert json.loads(capsys.readouterr().out) == {"schema": 1, "discovered": [root]}
    assert _run_without_keepers("projects", "--json") == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema": 1,
        "projects": [
            {"project_root": root, "status": "present", "remembered": True, "grants": []}
        ],
    }
    assert docker.calls == []


@pytest.mark.parametrize("output", ["json", "human"])
def test_forget_releases_only_the_own_unused_keeper(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str], output: str
) -> None:
    project = _remember(tmp_path)
    own, other = keeper_image(project), keeper_image(tmp_path / "other")
    docker.images.update({own: _A, other: _B, "unrelated:latest": _A})

    assert _cli("projects", "forget", str(project), *(["--json"] if output == "json" else [])) == 0

    printed = capsys.readouterr().out
    if output == "json":
        assert json.loads(printed) == {
            "schema": 1,
            "forgotten": str(project.resolve()),
            "keeper": {"status": "released", "tag": own, "image_id": _A},
            "purge": None,
            "leftovers": [],
        }
    else:
        assert printed.splitlines() == [
            f"Forgot Project path: {project.resolve()}",
            f"Keeper released: {own} ({_A})",
            "No Docker resources left behind for this Project.",
        ]
    assert own not in docker.images
    assert other in docker.images and "unrelated:latest" in docker.images
    # A release is one exact, non-force, no-prune tag removal.
    assert docker.removals() == [["docker", "image", "rm", "--no-prune", own]]
    assert project_inventory.project_inventory() == ()


def test_projects_human_output_lists_status_and_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli("projects") == 0
    assert "No remembered Project paths or Project Grants" in capsys.readouterr().out
    present = _remember(tmp_path)
    deleted = tmp_path / "deleted"
    source = tmp_path / "Xilinx" / "2025.2"
    (deleted / ".git").mkdir(parents=True)
    source.mkdir(parents=True)
    # Inspection runs the Vivado launcher's -version probe on Linux x86-64 only, and no shared
    # fake installation exists, so this one test stubs it to check the installation value.
    monkeypatch.setattr(
        authority,
        "inspect_installation",
        lambda source, project_root=None: Inspection(source.resolve(), "2025.2", "linux-x86_64"),
    )
    authority.register_installation("vivado_2025_2", "vivado", source)
    eda_grant_support.register_license()
    eda_grant_support.add_grant(
        deleted, installation="vivado_2025_2", license_profile="registered"
    )
    shutil.rmtree(deleted)

    assert _cli("projects") == 0

    output = capsys.readouterr().out
    assert output.splitlines() == [
        "Project Inventory",
        f"  {deleted.resolve()} [missing; grant only]",
        "    vivado",
        "      Installation: vivado_2025_2",
        "      License Profile: registered",
        f"  {present.resolve()} [present]",
        "    Grants: none",
    ]


@pytest.mark.parametrize("state", ["inventory", "grants"])
def test_projects_reports_unreadable_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    if state == "inventory":
        _remember(tmp_path)
        project_inventory.state_path().write_text("{", encoding="utf-8")
        expected = "ERROR: Project Inventory is unreadable"
    else:
        eda_grant_support.register_license()
        authority.state_path().write_text("{", encoding="utf-8")
        expected = "ERROR: cannot read Project Grants"

    assert _cli("projects") == 2

    assert capsys.readouterr().err.startswith(expected)


_UNRECORDED_LINE = (
    "unrecorded: kept because Booley cannot tell which Project it belongs to; "
    "remove it with `docker image rm <id>` once no other Project uses it"
)


@pytest.mark.parametrize("output", ["human", "json"])
def test_forget_explains_unrecorded_leftovers(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str], output: str
) -> None:
    project = _remember(tmp_path)
    # A digest pull can leave the Project's repository on an untagged image no record names.
    image = "sha256:" + "7" * 64
    docker.untagged_rows.append((project_image_name(project), "<none>", image))

    assert _cli("projects", "forget", str(project), *(["--json"] if output == "json" else [])) == 0

    printed = capsys.readouterr().out
    if output == "human":
        table = render_table(
            ["Kind", "Name", "Size", "Reason", "Tags"],
            [["image", image, "1GB", "unrecorded", ""]],
        )
        assert "\n".join(table) + "\n" + _UNRECORDED_LINE + "\n" in printed
    else:
        assert "unrecorded:" not in printed.replace('"reason": "unrecorded"', "")
        assert json.loads(printed)["leftovers"] == [
            {"kind": "image", "name": image, "reason": "unrecorded", "size": "1GB", "tags": []}
        ]


def test_forget_lists_leftovers_with_sizes_and_tags(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _remember(tmp_path)
    docker.images.update({f"{project_image_name(project)}:latest": _A, "mirror:x": _A})

    assert _cli("projects", "forget", str(project)) == 0

    table = render_table(
        ["Kind", "Name", "Size", "Reason", "Tags"],
        [["image", _A, "1GB", "shared", f"mirror:x, {project_image_name(project)}:latest"]],
    )
    assert "Left behind for this Project:\n" + "\n".join(table) + "\n" in capsys.readouterr().out


def test_forget_purge_removes_unused_own_tags_and_names_the_rest(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _remember(tmp_path, "it's a project")
    family = project_image_name(project)
    docker.images.update({f"{family}:latest": _A, f"{family}-wheel:latest": _B, "mirror:x": _B})
    assert _cli("projects", "forget", str(project)) == 0
    first = capsys.readouterr().out
    assert f"booley projects forget --purge {quote_arg(str(project))}" in first
    assert "unrecorded:" not in first

    assert _cli("projects", "forget", "--purge", str(project)) == 0

    output = capsys.readouterr().out
    assert f"Removed: {family}-wheel:latest\nRemoved: {family}:latest\n" in output
    assert "No Docker resources left behind" in output and "mirror:x" in docker.images
    assert "in-use:" not in output
    docker.images[f"{family}-busy:latest"] = _C
    docker.containers[_CONTAINER] = _C
    assert _cli("projects", "forget", "--purge", str(project)) == 0
    assert (
        f"in-use: kept because a container still uses it; rerun `booley projects forget --purge {quote_arg(str(project))}`"
        in capsys.readouterr().out
    )
    # Every removal is the exact release of one of the Project's own tags.
    assert {tuple(call) for call in docker.removals()} == {
        ("docker", "image", "rm", "--no-prune", f"{family}:latest"),
        ("docker", "image", "rm", "--no-prune", f"{family}-wheel:latest"),
    }


@pytest.mark.parametrize("grant", [False, True])
def test_purge_accepts_a_forgotten_root_unless_a_grant_holds_it(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str], grant: bool
) -> None:
    base = tmp_path.resolve()
    deleted = base / "deleted"
    (deleted / ".git").mkdir(parents=True)
    if grant:
        _grant(deleted)
    shutil.rmtree(deleted)
    own = f"{project_image_name_for_identity(str(deleted))}:latest"
    docker.images[own] = _A
    # The old path now points at another Project; purge must not follow it there.
    other = _remember(base / "other-root")  # a remembered Project with its own images
    docker.images[f"{project_image_name(other)}:latest"] = _C
    deleted.symlink_to(other, target_is_directory=True)
    _write_session_files(str(deleted))

    assert _cli("projects", "forget", "--purge", str(deleted), "--json") == (2 if grant else 0)

    if grant:
        assert "revoke" in capsys.readouterr().err
        assert len(docker.images) == 2 and docker.calls == []
    else:
        assert json.loads(capsys.readouterr().out)["purge"]["removed"] == [own]
    assert f"{project_image_name(other)}:latest" in docker.images
    assert str(other) in [entry.project_root for entry in project_inventory.project_inventory()]
    # A refused purge withdraws nothing; an accepted one clears the stamp and claim.
    assert _session_files_remain(str(deleted)) is grant


def test_forget_reports_leftover_listing_failure_after_forgetting(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _remember(tmp_path)
    docker.answer("image", "ls", "--all", code=1, stderr="daemon went away")

    assert _cli("projects", "forget", str(project)) == 2

    error = capsys.readouterr().err
    assert "could not list its leftover Docker resources" in error
    assert "daemon went away" in error
    assert f"(keeper absent: {keeper_image(project)})" in error
    assert project_inventory.project_inventory() == ()


@pytest.mark.parametrize(
    "flags", [["projects", "--json", "prune-keepers"], ["projects", "prune-keepers", "--json"]]
)
def test_public_prune_preview_then_digest_confirmation(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    project = _remember(tmp_path)
    own, orphan = keeper_image(project), keeper_image(tmp_path / "legacy")
    docker.images.update({own: _A, orphan: _B})
    assert _cli(*flags) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["released"] == [] and orphan in docker.images

    assert _cli(*flags, "--confirm", preview["digest"]) == 0

    assert orphan not in docker.images and own in docker.images
    assert docker.removals() == [["docker", "image", "rm", "--no-prune", orphan]]


@pytest.mark.parametrize("state", ["running", "stopped"])
def test_forget_retains_an_image_any_container_uses(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    """A stopped container still pins the keeper, and the Session authority goes either way."""
    project = _remember(tmp_path)
    tag = keeper_image(project)
    docker.images[tag] = _A
    docker.containers[_CONTAINER] = _A  # .Image stays immutable whatever tag created it
    if state == "stopped":
        docker.stopped.add(_CONTAINER)
    _write_session_files(str(project.resolve()))

    assert _cli("projects", "forget", str(project)) == 0

    assert f"Keeper retained-in-use: {tag} ({_A})" in capsys.readouterr().out
    assert tag in docker.images and docker.removals() == []
    assert project_inventory.project_inventory() == ()
    assert not _session_files_remain(str(project.resolve()))
    project_inventory.remember_project(project)
    assert _cli("projects", "forget", str(project), "--json") == 0
    assert json.loads(capsys.readouterr().out)["keeper"]["status"] == "retained-in-use"


def test_forget_removal_failure_preserves_inventory(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _remember(tmp_path)
    tag = keeper_image(project)
    docker.images[tag] = _A
    docker.answer("image", "rm", tag, code=1, stderr="removal denied")

    assert _cli("projects", "forget", str(project)) == 2

    assert "removal denied" in capsys.readouterr().err
    assert project_inventory.project_inventory()[0].remembered


@pytest.mark.parametrize("change", ["container", "remember"])
def test_prune_confirmation_goes_stale_when_protection_changes(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str], change: str
) -> None:
    orphan = keeper_image(tmp_path / ("orphan" if change == "container" else "project"))
    docker.images[orphan] = _A
    status, preview = _prune(capsys)
    assert status == 0
    if change == "container":
        docker.containers[_CONTAINER] = _A
    else:
        _remember(tmp_path)

    status, result = _prune(capsys, preview["digest"])

    assert status == 2 and result["released"] == [] and orphan in docker.images


def test_prune_digest_ignores_unrelated_observations(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str]
) -> None:
    docker.images[keeper_image(tmp_path / "orphan")] = _A
    _, first = _prune(capsys)
    docker.images["unrelated:latest"] = _B
    docker.containers[_CONTAINER] = _B
    _, second = _prune(capsys)
    assert first["digest"] == second["digest"]


def test_prune_with_nothing_eligible_offers_no_confirmation_and_refuses_one(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two keepers, both protected: one inventoried, one orphan used by a container (#1576)."""
    project = _remember(tmp_path)
    in_use = keeper_image(tmp_path / "orphan")
    docker.images.update({keeper_image(project): _A, in_use: _B})
    docker.containers[_CONTAINER] = _B

    assert _cli("projects", "prune-keepers") == 0

    human = capsys.readouterr().out
    assert "Nothing to release." in human
    assert "Preview digest" not in human and "--confirm" not in human
    for tag in (keeper_image(project), in_use):
        assert f"Keeper tag retained or unresolved: {tag}" in human
    status, preview = _prune(capsys)
    assert status == 0
    assert preview["digest"] is None and preview["schema"] == 1
    assert preview["candidates"] and not any(item["eligible"] for item in preview["candidates"])
    assert set(preview) == {"digest", "candidates", "released", "retained", "errors", "schema"}
    images_before = dict(docker.images)
    # Deliberately mirrors the digest algorithm in runtime.image_keepers: it is the digest an
    # older preview would have printed here.
    copied = hashlib.sha256(
        json.dumps(preview["candidates"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    for confirm in (copied, "0" * 64):
        status, result = _prune(capsys, confirm)
        assert status == 2, confirm
        assert any("nothing to release" in error for error in result["errors"])
        assert result["released"] == []
    assert docker.images == images_before and docker.removals() == []


def test_prune_partial_failure_accounts_for_prior_releases(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str]
) -> None:
    tags = sorted([keeper_image(tmp_path / "one"), keeper_image(tmp_path / "two")])
    docker.images.update(dict.fromkeys(tags, _A))
    docker.answer("image", "rm", tags[1], code=1, stderr="removal denied")
    _, preview = _prune(capsys)
    status, result = _prune(capsys, preview["digest"])
    assert status == 2 and result["released"] == [tags[0]]
    assert result["retained"] == [tags[1]] and result["errors"]
    docker.replies.clear()
    _, preview = _prune(capsys)
    assert _prune(capsys, preview["digest"])[0] == 0


@pytest.mark.parametrize("stamp", ["runtime-issuance.json", "session-spec.json"])
def test_prune_inventory_absence_authorizes_historical_stamps(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str], stamp: str
) -> None:
    project = tmp_path / "old"
    (project / ".booley").mkdir(parents=True)
    (project / ".booley" / stamp).write_text(json.dumps({"project_root": str(project)}))
    tag = keeper_image(project)
    docker.images[tag] = _A
    _, preview = _prune(capsys)
    assert preview["candidates"][0]["reason"] == "inventory-absent"
    assert _prune(capsys, preview["digest"])[0] == 0 and tag not in docker.images


@pytest.mark.parametrize("change", ["missing", "uninitialized", "replacement-symlink"])
def test_forget_uses_exact_stored_identity(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str], change: str
) -> None:
    project = _remember(tmp_path)
    own = keeper_image(project)
    docker.images[own] = _A
    if change == "uninitialized":
        shutil.rmtree(project / ".booley")
    else:
        shutil.rmtree(project)
        if change == "replacement-symlink":
            target = tmp_path / "replacement"
            target.mkdir()
            project.symlink_to(target, target_is_directory=True)

    assert _cli("projects", "forget", str(project), "--json") == 0

    assert json.loads(capsys.readouterr().out)["keeper"]["tag"] == own
    assert own not in docker.images
    assert project_inventory.project_inventory() == ()


def test_prune_protects_missing_uninitialized_and_grant_only_roots(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str]
) -> None:
    base = tmp_path.resolve()
    project = _remember(base)
    missing = base / "missing"
    (missing / ".booley").mkdir(parents=True)
    project_inventory.remember_project(missing)
    shutil.rmtree(missing)
    shutil.rmtree(project / ".booley")
    grant_only = base / "grant-only"
    (grant_only / ".git").mkdir(parents=True)
    _grant(grant_only)
    tags = [keeper_image(project), keeper_image(missing), keeper_image(grant_only)]
    docker.images.update(dict.fromkeys(tags, _A))
    docker.images["booley-issued-BAD:session"] = _A

    _, preview = _prune(capsys)

    assert all(item["reason"] == "inventoried" for item in preview["candidates"])
    assert preview["digest"] is None and _prune(capsys, "0" * 64)[0] == 2
    assert len(docker.images) == 4


def test_lifecycle_contention_becomes_cli_error(
    tmp_path: Path,
    docker: DockerImageCli,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _remember(tmp_path)

    # The real lock waits DEFAULT_WAIT_TIMEOUT_SECONDS (120 s) under contention, the CLI exposes
    # no timeout and the default is bound at definition time: no public entry point reaches the
    # refusal quickly, so the lock is replaced by one that refuses at once.
    @contextmanager
    def busy(_operation: Any) -> Iterator[None]:
        raise LifecycleLockError("lifecycle busy")
        yield

    monkeypatch.setattr(runtime_image_keepers, "host_lifecycle_lock", busy)
    for operation in ("forget", "prune-keepers", "prune-stamps"):
        argv = (
            ["projects", operation, str(project)]
            if operation != "prune-keepers"
            else ["projects", operation, "--confirm", "digest"]
        )
        assert _cli(*argv) == 2
        assert "lifecycle busy" in capsys.readouterr().err and docker.calls == []


@pytest.mark.parametrize("mutation", ["retag", "container", "grant"])
def test_apply_rechecks_before_deletion(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str], mutation: str
) -> None:
    orphan = tmp_path.resolve() / "orphan"
    tag = keeper_image(orphan)
    docker.images[tag] = _A
    _, preview = _prune(capsys)
    changed: list[str] = []

    def mutate(argv: list[str]) -> None:
        if argv[1:3] != ["image", "inspect"] or changed:
            return
        changed.append(mutation)  # once, after the first inspect of the confirming run
        if mutation == "retag":
            docker.images[tag] = _B
        elif mutation == "container":
            docker.containers[_CONTAINER] = _A
        else:
            (orphan / ".git").mkdir(parents=True)
            _grant(orphan)

    docker.after_call = mutate

    status, result = _prune(capsys, preview["digest"])

    assert status == 0 and result["released"] == [] and result["retained"] == [tag]
    assert tag in docker.images and docker.removals() == []


def test_forget_withdraws_issuance_stamp_and_admission_claim(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str]
) -> None:
    kept = _remember(tmp_path / "kept")
    purged = _remember(tmp_path / "purged")
    bystander = _remember(tmp_path / "bystander")
    for project in (kept, purged, bystander):
        _write_session_files(str(project.resolve()))
    purged_root = str(purged.resolve())
    shutil.rmtree(purged)  # a purge also accepts a deleted remembered root

    for flags in ([str(kept)], ["--purge", purged_root]):
        assert _cli("projects", "forget", *flags, "--json") == 0
        capsys.readouterr()

    assert not _session_files_remain(str(kept.resolve()))
    assert not _session_files_remain(purged_root)
    # Forget withdraws only its own Project's Session authority.
    assert _session_files_remain(str(bystander.resolve()))
    remembered = [entry.project_root for entry in project_inventory.project_inventory()]
    assert remembered == [str(bystander.resolve())]


@pytest.mark.parametrize("failure", ["invalid-image", "invalid-container", "daemon"])
def test_forget_incomplete_observation_preserves_inventory(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    project = _remember(tmp_path)
    _write_session_files(str(project.resolve()))
    tag = keeper_image(project)
    docker.images[tag] = "not-an-image" if failure == "invalid-image" else _A
    if failure == "invalid-container":
        docker.containers["bad-container"] = _A
    if failure == "daemon":
        docker.answer(code=1, stderr="daemon unavailable")  # every command fails

    assert _cli("projects", "forget", str(project)) == 2

    assert capsys.readouterr().err and project_inventory.project_inventory()[0].remembered
    assert tag in docker.images
    # A release that did not complete leaves the Project's Session authority alone.
    assert _session_files_remain(str(project.resolve()))


def test_prune_stamps_withdraws_only_orphan_session_files(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only a deleted, unremembered root loses its stamp and claim; no Docker command runs."""
    orphan = str(tmp_path / "orphan")
    remembered_gone = _remember(tmp_path, "remembered-gone")
    existing = tmp_path / "existing"
    existing.mkdir()
    keeper = keeper_image(Path(orphan))
    docker.images[keeper] = _A
    roots = [orphan, str(remembered_gone.resolve()), str(existing)]
    for root in roots:
        _write_session_files(root)
    shutil.rmtree(remembered_gone)
    inventory_before = project_inventory.project_inventory()

    assert _cli("projects", "prune-stamps", "--json") == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["candidates"] == [orphan]
    assert preview["withdrawn"] == [] and preview["kept"] == []
    assert all(_session_files_remain(root) for root in roots)

    named = ["projects", "prune-stamps", orphan, str(remembered_gone.resolve()), "--json"]
    assert _cli(*named) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["withdrawn"] == [orphan]
    assert result["kept"] == [str(remembered_gone.resolve())]
    assert not _session_files_remain(orphan)
    assert _session_files_remain(str(remembered_gone.resolve()))
    assert _session_files_remain(str(existing))
    # No Docker command ran, so no image or state volume was removed.
    assert docker.calls == [] and keeper in docker.images
    assert project_inventory.project_inventory() == inventory_before


def test_prune_stamps_human_output_names_the_exact_command(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str]
) -> None:
    orphan = str(tmp_path / "orphan root")
    _write_session_files(orphan)
    assert _cli("projects", "prune-stamps") == 0
    preview = capsys.readouterr().out
    assert f"Removable: {orphan}" in preview
    assert f"booley projects prune-stamps {quote_arg(orphan)}" in preview
    assert _cli("projects", "prune-stamps", orphan) == 0
    removed = capsys.readouterr().out
    assert f"Removed Sandbox issuance stamp: {orphan}" in removed
    assert "booley projects forget --purge" in removed
    assert _cli("projects", "prune-stamps") == 0
    assert "No Sandbox issuance stamps" in capsys.readouterr().out
    existing = tmp_path / "existing"
    existing.mkdir()
    assert _cli("projects", "prune-stamps", str(existing)) == 2
    kept = capsys.readouterr().out
    assert f"Kept (not a deleted, forgotten Project): {existing}" in kept
    assert "Removed Sandbox" not in kept and "forget --purge" not in kept
    assert docker.calls == []


@pytest.mark.parametrize("operation", ["forget", "prune-keepers", "prune-stamps"])
@pytest.mark.parametrize("journal_kind", ["invalidation", "malformed-invalidation"])
def test_real_pending_journal_preserves_keeper_and_inventory(
    tmp_path: Path,
    docker: DockerImageCli,
    capsys: pytest.CaptureFixture[str],
    operation: str,
    journal_kind: str,
) -> None:
    project = _remember(tmp_path)
    tag = keeper_image(project)
    docker.images[tag] = _A
    if operation == "prune-keepers":
        project_inventory.forget_project(project)
        _, preview = _prune(capsys)
    issuance_invalidation.prepare(str(project))
    journal = issuance_invalidation.journal_store().path(str(project))
    if journal_kind == "malformed-invalidation":
        journal.write_text("{invalid")
    before_journal = journal.read_bytes()
    before_inventory = project_inventory.state_path().read_bytes()
    docker.calls.clear()
    argv = {
        "forget": ["projects", "forget", str(project)],
        "prune-stamps": ["projects", "prune-stamps", str(project)],
    }.get(operation) or ["projects", "prune-keepers", "--confirm", preview["digest"]]

    assert _cli(*argv) == 2

    captured = capsys.readouterr()
    assert captured.err and captured.out == ""
    assert docker.calls == [] and tag in docker.images
    assert journal.read_bytes() == before_journal
    assert project_inventory.state_path().read_bytes() == before_inventory


@pytest.mark.parametrize("action", ["forget", "preview", "confirm"])
def test_missing_keeper_adapter_refuses_before_observation(
    tmp_path: Path, docker: DockerImageCli, capsys: pytest.CaptureFixture[str], action: str
) -> None:
    project = _remember(tmp_path)
    tag = keeper_image(project)
    docker.images[tag] = _A
    issuance_invalidation.prepare(str(project))
    journal = issuance_invalidation.journal_store().path(str(project))
    before = (project_inventory.state_path().read_bytes(), journal.read_bytes())
    argv = (
        ["projects", "forget", str(project)]
        if action == "forget"
        else ["projects", "prune-keepers"]
    )
    if action == "confirm":
        argv.extend(["--confirm", "digest"])

    assert _run_without_keepers(*argv) == 2

    assert capsys.readouterr().err.strip() == (
        "ERROR: Project keeper access requires host lifecycle coordination"
    )
    assert docker.calls == [] and tag in docker.images
    assert (project_inventory.state_path().read_bytes(), journal.read_bytes()) == before


@pytest.mark.parametrize("command", ["purge", "refused-prune"])
def test_purge_and_refused_prune_sweep_records_of_absent_images(
    tmp_path: Path, docker: DockerImageCli, command: str
) -> None:
    gone = "sha256:" + "d" * 64
    project = _remember(tmp_path)
    docker.images.update({keeper_image(project): _A, "present:latest": _A})
    registered = image_lifecycle.OwnedImages(tmp_path / "project")
    registered.record({}, (gone,))
    stale = image_lifecycle.OwnedImages(str(tmp_path / "unregistered"))
    stale.record({"booley-lifecycle-x:candidate": gone}, (gone, _A))

    if command == "purge":
        assert _cli("projects", "forget", "--purge", str(tmp_path / "unrelated")) == 0
    else:
        assert _cli("projects", "prune-keepers", "--confirm", "0" * 64) == 2

    assert image_lifecycle.OwnedImages(tmp_path / "project").ids == [gone]
    swept = image_lifecycle.OwnedImages(str(tmp_path / "unregistered"))
    assert (swept.ids, swept.refs) == ([_A], {})
    assert docker.removals() == []
