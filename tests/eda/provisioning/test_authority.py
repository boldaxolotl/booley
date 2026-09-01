"""Private host EDA authority security and integrity tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from booley.eda.provisioning import authority
from booley.eda.provisioning.policies.vivado import Inspection


@pytest.fixture
def private_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(
        authority,
        "inspect_installation",
        lambda source, project_root=None: Inspection(source.resolve(), "2025.2", "linux-x86_64"),
    )
    return config


def _registered(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    source = tmp_path / "Xilinx" / "2025.2"
    project.mkdir()
    (project / ".git").mkdir()
    source.mkdir(parents=True)
    authority.register_installation("vivado_2025_2", "vivado", source)
    return project, source


def test_authority_lock_contention_has_controlled_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        authority, "lock_fd", lambda _lock: (_ for _ in ()).throw(BlockingIOError())
    )
    times = iter((0.0, 11.0))
    monkeypatch.setattr(authority.time, "monotonic", lambda: next(times))

    with pytest.raises(authority.AuthorityError, match="busy with another operation"):
        authority._wait_for_lock(object())


def test_registration_grant_resolution_and_referential_integrity(
    tmp_path: Path, private_state: Path
) -> None:
    project, source = _registered(tmp_path)
    profile = authority.register_license(
        "site_a",
        server_ipv4="10.20.30.40",
        server_hostid="licenses.example.internal",
        lmgrd_port=2100,
        vendor_port=2101,
    )
    grant = authority.add_grant(
        project, "vivado", installation="vivado_2025_2", license_profile=profile.name
    )

    assert authority.resolve_installation(project).source == str(source.resolve())
    assert authority.resolve_license(project) == profile
    assert grant.project_root == str(project.resolve())
    with pytest.raises(authority.AuthorityError, match="live grant"):
        authority.remove_installation("vivado_2025_2")
    with pytest.raises(authority.AuthorityError, match="live grant"):
        authority.remove_license("site_a")


def test_issuance_resolves_the_installation_selected_only_by_the_grant(
    tmp_path: Path, private_state: Path
) -> None:
    project, source = _registered(tmp_path)
    authority.add_grant(project, "vivado", installation="vivado_2025_2")

    with authority.resolve_for_issuance(project, True) as (installation, profile):
        assert installation is not None
        assert installation.name == "vivado_2025_2"
        assert installation.source == str(source.resolve())
        assert profile is None


def test_issuance_rejects_host_provisioning_without_an_installation_grant(
    tmp_path: Path, private_state: Path
) -> None:
    project, _ = _registered(tmp_path)
    authority.register_license(
        "site_a",
        server_ipv4="10.20.30.40",
        server_hostid="licenses.example.internal",
        lmgrd_port=2100,
        vendor_port=2101,
    )
    authority.add_grant(project, "vivado", license_profile="site_a")

    with (
        pytest.raises(authority.AuthorityError, match="grant has no installation"),
        authority.resolve_for_issuance(project, True),
    ):
        pass


def test_issuance_does_not_mount_a_stale_installation_grant(
    tmp_path: Path, private_state: Path
) -> None:
    project, _ = _registered(tmp_path)
    authority.add_grant(project, "vivado", installation="vivado_2025_2")

    with (
        pytest.raises(authority.AuthorityError, match="does not request host provisioning"),
        authority.resolve_for_issuance(project, False),
    ):
        pass


def test_exact_project_identity_does_not_authorize_copy(
    tmp_path: Path, private_state: Path
) -> None:
    project, _ = _registered(tmp_path)
    copied = tmp_path / "copied"
    copied.mkdir()
    (copied / ".git").mkdir()
    authority.add_grant(project, "vivado", installation="vivado_2025_2")

    with pytest.raises(authority.AuthorityError, match="no exact"):
        authority.resolve_grant(copied, "vivado")


def test_grant_canonicalizes_a_marked_project_symlink(tmp_path: Path, private_state: Path) -> None:
    project, _ = _registered(tmp_path)
    alias = tmp_path / "project-alias"
    alias.symlink_to(project, target_is_directory=True)

    grant = authority.add_grant(alias, "vivado", installation="vivado_2025_2")

    assert grant.project_root == str(project.resolve())
    assert authority.resolve_grant(project, "vivado") == grant


def test_grant_rejects_non_project_directory(tmp_path: Path, private_state: Path) -> None:
    _registered(tmp_path)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()

    with pytest.raises(authority.AuthorityError, match=r"no canonical \.git or \.booley_project"):
        authority.add_grant(unrelated, "vivado", installation="vivado_2025_2")


@pytest.mark.parametrize("root", [Path("/"), Path.home(), Path("/etc"), Path("/tmp")])
def test_grant_rejects_broad_or_system_root(
    tmp_path: Path, private_state: Path, root: Path
) -> None:
    _registered(tmp_path)
    if not root.exists():
        pytest.skip(f"system root absent on this platform: {root}")

    with pytest.raises(authority.AuthorityError, match="unsafe Project root"):
        authority.add_grant(root, "vivado", installation="vivado_2025_2")


def test_grant_rejects_private_authority_overlap(tmp_path: Path, private_state: Path) -> None:
    _registered(tmp_path)
    private_state.mkdir(exist_ok=True)
    (private_state / ".git").mkdir()

    with pytest.raises(authority.AuthorityError, match="private EDA authority"):
        authority.add_grant(private_state, "vivado", installation="vivado_2025_2")


def test_grant_rejects_registered_installation_overlap(
    tmp_path: Path, private_state: Path
) -> None:
    project = tmp_path / "project"
    source = project / "vendor" / "Xilinx" / "2025.2"
    (project / ".git").mkdir(parents=True)
    source.mkdir(parents=True)
    authority.register_installation("nested", "vivado", source)

    with pytest.raises(authority.AuthorityError, match=r"overlaps.*installation"):
        authority.add_grant(project, "vivado", installation="nested")


def test_revoke_removes_authority_before_invalidation_failure(
    tmp_path: Path, private_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = _registered(tmp_path)
    authority.add_grant(project, "vivado", installation="vivado_2025_2")
    monkeypatch.setattr(
        authority, "invalidate_project_specs", lambda _root: (_ for _ in ()).throw(OSError("boom"))
    )

    with pytest.raises(OSError, match="boom"):
        authority.revoke_grant(project, "vivado")
    with pytest.raises(authority.AuthorityError, match="no exact"):
        authority.resolve_grant(project, "vivado")


def test_revoke_cleans_exact_runtime_after_authority_removal(
    tmp_path: Path, private_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = _registered(tmp_path)
    authority.add_grant(project, "vivado", installation="vivado_2025_2")
    cleaned: list[Path] = []
    monkeypatch.setattr(authority, "_cleanup_revoked_runtime", cleaned.append)

    authority.revoke_grant(project, "vivado")

    assert cleaned == [project.resolve()]
    with pytest.raises(authority.AuthorityError, match="no exact"):
        authority.resolve_grant(project, "vivado")


def test_revoke_reports_cleanup_failure_but_keeps_authority_revoked(
    tmp_path: Path, private_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = _registered(tmp_path)
    authority.add_grant(project, "vivado", installation="vivado_2025_2")
    monkeypatch.setattr(
        authority,
        "_cleanup_revoked_runtime",
        lambda _project: (_ for _ in ()).throw(authority.AuthorityError("residual relay")),
    )

    with pytest.raises(authority.AuthorityError, match="residual relay"):
        authority.revoke_grant(project, "vivado")
    with pytest.raises(authority.AuthorityError, match="no exact"):
        authority.resolve_grant(project, "vivado")


@pytest.mark.parametrize(
    "address,hostid,first,second",
    [
        ("license.example", "server", 2100, 2101),
        ("::1", "server", 2100, 2101),
        ("127.0.0.1", "server", 2100, 2101),
        ("240.0.0.1", "server", 2100, 2101),
        ("192.0.2.10", "192.0.2.1", 2100, 2101),
        ("192.0.2.10", "server", 2100, 2100),
        ("192.0.2.10", "server", True, 2101),
    ],
)
def test_license_profile_rejects_unsupported_topologies(
    tmp_path: Path,
    private_state: Path,
    address: str,
    hostid: str,
    first: int,
    second: int,
) -> None:
    with pytest.raises(authority.AuthorityError):
        authority.register_license(
            "site", server_ipv4=address, server_hostid=hostid, lmgrd_port=first, vendor_port=second
        )


def test_insecure_registry_mode_and_symlink_fail_closed(
    tmp_path: Path, private_state: Path
) -> None:
    _registered(tmp_path)
    path = authority.state_path()
    if os.name != "nt":
        path.chmod(0o644)
        with pytest.raises(authority.AuthorityError, match="mode 600"):
            authority.load_state()
        path.chmod(0o600)

    content = path.read_text(encoding="utf-8")
    path.unlink()
    target = tmp_path / "redirected.json"
    target.write_text(content, encoding="utf-8")
    target.chmod(0o600)
    path.symlink_to(target)
    with pytest.raises(authority.AuthorityError, match="symlink"):
        authority.load_state()


def test_corrupt_and_duplicate_grants_fail_closed(tmp_path: Path, private_state: Path) -> None:
    project, _ = _registered(tmp_path)
    authority.add_grant(project, "vivado", installation="vivado_2025_2")
    path = authority.state_path()
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["grants"].append(dict(raw["grants"][0]))
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(authority.AuthorityError, match="duplicate"):
        authority.load_state()


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("installations", "version", 2025.2),
        ("installations", "architecture", None),
        ("installations", "policy_revision", True),
        ("licenses", "server_hostid", 1234),
        ("licenses", "lmgrd_port", True),
    ],
)
def test_corrupt_record_field_types_fail_closed(
    tmp_path: Path,
    private_state: Path,
    section: str,
    field: str,
    value: object,
) -> None:
    _registered(tmp_path)
    authority.register_license(
        "site",
        server_ipv4="10.20.30.40",
        server_hostid="server",
        lmgrd_port=2100,
        vendor_port=2101,
    )
    path = authority.state_path()
    raw = json.loads(path.read_text(encoding="utf-8"))
    record = next(iter(raw[section].values()))
    record[field] = value
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(authority.AuthorityError):
        authority.load_state()


def test_symlinked_authority_ancestor_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = tmp_path / "actual-config"
    actual.mkdir(mode=0o700)
    redirected = tmp_path / "redirected-config"
    redirected.symlink_to(actual, target_is_directory=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(redirected))

    with pytest.raises(authority.AuthorityError, match=r"ancestor.*symlink"):
        authority.ensure_state_dir()
