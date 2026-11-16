"""Egress, advice, and image-kind helpers behind the Sandbox remedies (#4182)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from booley.runtime import sandbox_apply, session_admission, session_runtime


def _write_image(root: Path, image: str | None) -> None:
    booley = root / ".booley"
    booley.mkdir(exist_ok=True)
    text = f'[sandbox]\nimage = "{image}"\n' if image else ""
    (booley / "booley.toml").write_bytes(text.encode())


def _listed(root: Path, *, vscode: bool) -> session_admission.Sandbox:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return session_admission.Sandbox(
        "id", "box", str(root), None, now, now, True, vscode, egress="proxy"
    )


@pytest.mark.parametrize(
    ("vscode", "image", "expected"),
    [
        (
            True,
            None,
            "recreate it: on the host run `booley init --project {root}`, then {rebuild} in its VS Code window",
        ),
        (
            True,
            "own:1",
            "recreate it: on the host run `booley init --seed --project {root}`, then {rebuild} in its VS Code window",
        ),
        (False, None, "recreate it: run `booley session refresh --project {root}` on the host"),
        (
            False,
            "own:1",
            "recreate it: run `booley session down --project {root}`, "
            "`booley init --seed --project {root}`, and "
            "`booley session up --project {root}` on the host",
        ),
    ],
)
def test_egress_recreate_remedy_names_owner_workflow(
    tmp_path: Path, vscode: bool, image: str | None, expected: str
) -> None:
    _write_image(tmp_path, image)
    root = sandbox_apply.quote_arg(str(tmp_path))

    remedy = session_admission.recreate_remedy(_listed(tmp_path, vscode=vscode))

    assert remedy == expected.format(root=root, rebuild=sandbox_apply.REBUILD_CONTAINER)


def test_egress_remedy_for_a_deleted_headless_project_stops_it(tmp_path: Path) -> None:
    gone = tmp_path / "gone"

    remedy = session_admission.recreate_remedy(_listed(gone, vscode=False))

    assert (
        remedy
        == f"stop it: run `booley session down --project {sandbox_apply.quote_arg(str(gone))}`"
    )


def test_invalid_sandbox_image_setting_counts_as_managed(tmp_path: Path) -> None:
    booley = tmp_path / ".booley"
    booley.mkdir()
    (booley / "booley.toml").write_bytes(b"[sandbox]\nimage = 5\n")

    assert sandbox_apply.image_is_managed(tmp_path)


def test_stale_image_advice_for_headless_and_vscode_containers(tmp_path: Path) -> None:
    canonical = session_runtime.session_container_name(tmp_path)

    assert (
        session_runtime.stale_image_advice(tmp_path, canonical)
        == "run `booley session refresh` on the host"
    )
    assert (
        session_runtime.stale_image_advice(tmp_path, "vsc-editor")
        == "use Dev Containers: Rebuild Container"
    )


def _host_attachment(monkeypatch: pytest.MonkeyPatch, attachment: object) -> None:
    from booley.runtime import runtime_context

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr(session_runtime, "sandbox_attachment", lambda _root: attachment)


def test_sandbox_entry_remedy_keeps_the_capacity_advice(tmp_path: Path) -> None:
    error = session_runtime.SessionError("Sandbox start refused: host is at capacity")

    assert session_runtime.sandbox_entry_remedy(tmp_path, error) == (
        "free Sandbox capacity using the command in the refusal, then retry"
    )


@pytest.mark.parametrize(
    "error",
    [session_runtime.SessionError("bad issuance"), FileNotFoundError("docker"), OSError("boom")],
)
def test_sandbox_entry_remedy_names_the_owner_workflow_then_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    _host_attachment(monkeypatch, sandbox_apply.Attachment.VSCODE)

    remedy = session_runtime.sandbox_entry_remedy(tmp_path, error)

    assert remedy == (
        "on the host run `booley init`, then use Dev Containers: Rebuild Container, then retry"
    )


def test_sandbox_entry_remedy_survives_a_missing_docker_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.runtime import runtime_context

    def no_docker(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("docker")

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr(session_runtime, "_run", no_docker)

    remedy = session_runtime.sandbox_entry_remedy(tmp_path, session_runtime.SessionError("x"))

    assert remedy.endswith(", then retry")
