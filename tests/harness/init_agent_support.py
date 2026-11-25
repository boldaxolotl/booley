"""Shared setup for the agent selection and auth tests of ``booley init``.

It fakes terminal input, Host Bootstrap and the Sandbox Image chain.
"""

from __future__ import annotations

import argparse
import builtins
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from booley.harness import init_cmd
from booley.runtime import auth_token
from booley.runtime.image_lifecycle import ImageLifecycleError, LifecycleResult

_CLAUDE_LOGIN = {
    "claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "expiresAt": 4102444800000}
}
_CODEX_LOGIN = {"tokens": {"access_token": "a", "refresh_token": "r"}}


def empty_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty host: no credential in the environment or on disk, no terminal to prompt."""
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    for name in (
        *auth_token.CREDENTIAL_ENV_KEYS,
        "CODEX_HOME",
        "BOOLEY_DIR",
        "BOOLEY_PRIMARY_PROVIDER",
        "BOOLEY_PRIMARY_AUTH",
        "BOOLEY_AGENT_APP",
    ):
        monkeypatch.delenv(name, raising=False)

    def no_terminal(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr(builtins, "input", no_terminal)
    init_cmd.reset_cache()
    return home


def login(home: Path, cli: str) -> None:
    """Leave *cli*'s refreshing login file on the host."""
    path, content = {
        "claude": (home / ".claude" / ".credentials.json", _CLAUDE_LOGIN),
        "codex": (home / ".codex" / "auth.json", _CODEX_LOGIN),
    }[cli]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(content).encode())


def api_key(monkeypatch: pytest.MonkeyPatch, cli: str) -> None:
    monkeypatch.setenv(auth_token.API_KEY_ENV[cli], "sk-test-key")


def project_config(root: Path, body: bytes | None = None) -> Path:
    """Create *root*'s Booley Directory, write *body* when given, return ``booley.toml``."""
    booley_dir = root / ".booley"
    booley_dir.mkdir(parents=True, exist_ok=True)
    config = booley_dir / "booley.toml"
    if body is not None:
        config.write_bytes(body)
    return config


def stub_host_images(
    monkeypatch: pytest.MonkeyPatch, image: LifecycleResult | None = None
) -> list[Path]:
    """Stand in for Host Bootstrap and the Sandbox Image chain, which run Docker.

    Returns the Project Root of every image reconciliation that was reached, so a
    test can tell whether a run got past selection and persistence. Without
    *image* the reconciliation fails, as on a host with no Sandbox Image.
    """
    reached: list[Path] = []

    def reconcile(scope: Any, *_args: object, **_kwargs: object) -> LifecycleResult:
        reached.append(scope.project_root)
        if image is None:
            raise ImageLifecycleError("no Sandbox Image in tests")
        return image

    # No public entry point runs `booley init` without Docker-backed Host Bootstrap.
    monkeypatch.setattr(
        init_cmd,
        "reconcile_bootstrap",
        lambda intent, **_kwargs: init_cmd.BootstrapResult(intent, ()),
    )
    # The Sandbox Image chain plans, pulls and builds images through Docker.
    monkeypatch.setattr(init_cmd.image_lifecycle, "reconcile_planned", reconcile)
    return reached


def seed_init(root: Path, **flags: Any) -> int:
    """Run ``booley init --seed`` for *root* with the given argument overrides."""
    init_cmd.reset_cache()
    values: dict[str, Any] = {"seed": True, "provider": None, "auth": None, **flags}
    return init_cmd.run_init(argparse.Namespace(**values), root)


_watched: list[list[str]] = []


def _record_open(event: str, args: tuple[Any, ...]) -> None:
    if event == "open" and _watched:
        _watched[-1].append(str(args[0]))


# Audit hooks cannot be removed: register once and record only inside a block.
sys.addaudithook(_record_open)


@contextmanager
def credential_reads(home: Path) -> Iterator[list[str]]:
    """Collect the path of every file opened under *home* while the block runs."""
    opened: list[str] = []
    _watched.append(opened)
    try:
        yield opened
    finally:
        _watched.pop()
        opened[:] = [path for path in opened if Path(path).is_relative_to(home)]
