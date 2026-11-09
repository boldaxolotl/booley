"""Contracts for ``.github/scripts/renovate_artifacts.py`` (#2999).

The script runs as a Renovate post-upgrade command in a checkout of an update branch:
it refreshes the agent CLI lockfile after a manifest bump and the CI constraints file
after a ``pyproject.toml`` bump. Nothing here runs npm or uv: ``subprocess.run`` is
replaced by a recorder that checks the argument list and the directory it was run in,
and plays the tool's result.
"""

from __future__ import annotations

import functools
import importlib
import json
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / ".github/scripts"))

_MANIFEST = "src/booley/data/docker/agent-clis-package.json"
_LOCK = "src/booley/data/docker/agent-clis-package-lock.json"
_CONSTRAINTS = ".github/ci/constraints.txt"
_DATA_FILE_ENV = "RENOVATE_POST_UPGRADE_COMMAND_DATA_FILE"
_OLD_LOCK = (
    '{"lockfileVersion": 3, "old": true, "packages": {"": {}, "node_modules/a": '
    '{"resolved": "https://registry.npmjs.org/a/-/a-1.2.3.tgz"}}}\n'
)
# Exact registry versions and an alias: what the real agent CLI manifest holds.
_MANIFEST_TEXT = '{"dependencies": {"a": "1.2.3", "b": "npm:@scope/b@0.1.0-linux-x64"}}\n'
_NEW_LOCK = '{"lockfileVersion": 3, "old": false}\n'
_NPM_ARGV = [
    "npm",
    "install",
    "--package-lock-only",
    "--ignore-scripts",
    "--no-audit",
    "--no-fund",
]


@functools.cache
def _script() -> ModuleType:
    """Load the script on first use so a missing script fails the test body (red evidence)."""
    return importlib.import_module("renovate_artifacts")


class _Tool:
    """Stand-in for ``subprocess.run``: records each call and plays a result.

    ``effect`` runs inside the call, with the working directory the tool was given, so
    it can both check what the tool would see and write what it would produce.
    """

    def __init__(
        self,
        effect: Callable[[Path], None] | None = None,
        *,
        returncode: int = 0,
        stderr: str = "",
        raises: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self._effect = effect
        self._returncode = returncode
        self._stderr = stderr
        self._raises = raises

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        if self._effect is not None:
            self._effect(Path(kwargs["cwd"]))
        if self._raises is not None:
            raise self._raises
        return subprocess.CompletedProcess(argv, self._returncode, "", self._stderr)


def _repo(tmp_path: Path) -> Path:
    """A checkout holding the real constraints file and project, and a small CLI lock pair."""
    root = tmp_path / "clone"
    for relative in ("pyproject.toml", _CONSTRAINTS):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((_ROOT / relative).read_bytes())
    for relative, text in ((_MANIFEST, _MANIFEST_TEXT), (_LOCK, _OLD_LOCK)):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode())
    return root


def _run(
    root: Path,
    task: str,
    packages: list[str],
    tool: _Tool,
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    """Run ``task`` in ``root`` with a data file upgrading ``packages`` (their package files)."""
    entries = [{"depName": "dep", "packageFile": file, "newValue": "1"} for file in packages]
    data_file = root.parent / "upgrades.json"
    data_file.write_text(json.dumps({"upgrades": entries}), encoding="utf-8")
    monkeypatch.setenv(_DATA_FILE_ENV, str(data_file))
    monkeypatch.chdir(root)
    monkeypatch.setattr(_script().subprocess, "run", tool)
    return _script().main([task])


def _half_written(cwd: Path) -> None:
    """npm may rewrite its copy of the lock and then fail."""
    (cwd / "package-lock.json").write_bytes(b"half written")


def _npm_session(seen: dict[str, Any]) -> Callable[[Path], None]:
    """Check what npm is given, then write the lock it would produce."""

    def effect(cwd: Path) -> None:
        seen["files"] = sorted(path.name for path in cwd.iterdir())
        seen["manifest"] = (cwd / "package.json").read_bytes()
        seen["lock"] = (cwd / "package-lock.json").read_bytes()
        seen["cwd"] = cwd
        (cwd / "package-lock.json").write_bytes(_NEW_LOCK.encode())

    return effect


def test_agent_cli_lock_is_refreshed_from_npm_named_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    seen: dict[str, Any] = {}
    tool = _Tool(_npm_session(seen))

    assert _run(root, "agent-cli-lock", [_MANIFEST], tool, monkeypatch) == 0

    [(argv, kwargs)] = tool.calls
    assert argv == _NPM_ARGV  # no package script runs, nothing is audited or fetched for display
    assert kwargs["timeout"] > 0
    assert not kwargs.get("shell")
    # npm sees only the two copies, under the names it knows: no branch .npmrc, no repository.
    assert seen["files"] == ["package-lock.json", "package.json"]
    assert seen["manifest"] == _MANIFEST_TEXT.encode()
    assert seen["lock"] == _OLD_LOCK.encode()
    assert seen["cwd"].resolve() != root.resolve()
    assert not seen["cwd"].exists()
    # The repository gets the new lock; the manifest stays as Renovate wrote it.
    assert (root / _LOCK).read_bytes() == _NEW_LOCK.encode()
    assert (root / _MANIFEST).read_bytes() == _MANIFEST_TEXT.encode()
    assert sorted(path.name for path in (root / _LOCK).parent.iterdir()) == [
        "agent-clis-package-lock.json",
        "agent-clis-package.json",
    ]


def test_agent_cli_lock_skips_branches_without_a_manifest_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    tool = _Tool()
    # Only the manifest triggers the task: not Cargo.lock maintenance, a Dockerfile pin, the
    # lock itself or the constraints file.
    others = ["Cargo.lock", "src/booley/data/docker/Dockerfile.base", _LOCK, _CONSTRAINTS]

    assert _run(root, "agent-cli-lock", others, tool, monkeypatch) == 0

    assert tool.calls == []
    assert (root / _LOCK).read_bytes() == _OLD_LOCK.encode()
    assert "nothing to do" in capsys.readouterr().err


def test_failed_npm_run_leaves_the_lock_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    timeout = subprocess.TimeoutExpired(_NPM_ARGV, 1)
    failures = [
        (_Tool(_half_written, returncode=1, stderr="npm ERR! boom"), "npm ERR! boom"),
        (_Tool(_half_written, raises=timeout), "timed out"),
        (_Tool(_half_written, raises=FileNotFoundError("npm")), "npm"),
    ]
    for tool, message in failures:
        # npm may have rewritten its copy before failing; the repository must not see it.
        assert _run(root, "agent-cli-lock", [_MANIFEST], tool, monkeypatch) == 1

        assert (root / _LOCK).read_bytes() == _OLD_LOCK.encode()
        assert message in capsys.readouterr().err


_FOREIGN_SOURCES = [
    ('{"dependencies": {"x": "github:attacker/x"}}', _OLD_LOCK, "exact registry"),
    ('{"dependencies": {"x": "^1.2.3"}}', _OLD_LOCK, "exact registry"),
    ('{"overrides": {"x": "1.0.0"}}', _OLD_LOCK, "exact registry"),
    ("[]", _OLD_LOCK, "refusing"),
    (
        '{"dependencies": {"x": "1.0.0"}}',
        '{"packages": {"node_modules/x": {"resolved": "git+https://example.test/x.git"}}}',
        "node_modules/x",
    ),
]


@pytest.mark.parametrize(("manifest", "lock", "message"), _FOREIGN_SOURCES)
def test_agent_cli_inputs_naming_foreign_sources_never_reach_npm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    manifest: str,
    lock: str,
    message: str,
) -> None:
    """A forged branch cannot make npm clone git or URL dependencies (AC4)."""
    root = _repo(tmp_path)
    (root / _MANIFEST).write_bytes(manifest.encode())
    (root / _LOCK).write_bytes(lock.encode())
    tool = _Tool()

    assert _run(root, "agent-cli-lock", [_MANIFEST], tool, monkeypatch) == 1

    assert tool.calls == []
    assert (root / _LOCK).read_bytes() == lock.encode()
    assert message in capsys.readouterr().err


def test_agent_cli_lock_needs_both_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    (root / _LOCK).unlink()
    tool = _Tool()

    assert _run(root, "agent-cli-lock", [_MANIFEST], tool, monkeypatch) == 1

    assert tool.calls == []
    assert "cannot copy" in capsys.readouterr().err


def _checked_command() -> str:
    """The command ``ci_constraints`` requires in the header, for the real project."""
    import ci_constraints

    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return ci_constraints.regenerate_command(project)


def test_constraints_regenerate_with_the_checked_command_and_no_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    command = _checked_command()
    tool = _Tool()

    assert _run(root, "constraints", ["pyproject.toml"], tool, monkeypatch) == 0

    [(argv, kwargs)] = tool.calls
    # The header uv writes stays the checked command although two flags are added.
    assert argv == [
        *shlex.split(command),
        "--no-build",  # no build backend of the branch runs
        "--no-config",  # no [tool.uv] or uv.toml of the branch is read
        "--no-sources",  # no [tool.uv.sources] git, path or URL redirect is followed
        "--custom-compile-command",
        command,
    ]
    assert Path(kwargs["cwd"]).resolve() == root.resolve()
    assert kwargs["timeout"] > 0
    assert not kwargs.get("shell")


def test_constraints_skip_branches_without_a_pyproject_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    tool = _Tool()

    assert _run(root, "constraints", [_MANIFEST, "Cargo.lock"], tool, monkeypatch) == 0

    assert tool.calls == []


_BAD_HEADERS = [
    "uv pip compile pyproject.toml --all-extras --universal --python-version 3.11 -o x.txt",
    "uv pip compile pyproject.toml --all-extras --universal --python-version 3.11 "
    "-o .github/ci/constraints.txt && curl evil.example",
]


def test_unexpected_constraints_header_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    good = (root / _CONSTRAINTS).read_text(encoding="utf-8").splitlines(keepends=True)
    tool = _Tool()
    for header in _BAD_HEADERS:
        lines = list(good)
        lines[1] = f"#    {header}\n"
        (root / _CONSTRAINTS).write_bytes("".join(lines).encode())

        assert _run(root, "constraints", ["pyproject.toml"], tool, monkeypatch) == 1

        assert _CONSTRAINTS in capsys.readouterr().err
    assert tool.calls == []


@pytest.mark.parametrize(
    "tool",
    [_Tool(returncode=1, stderr="no solution"), _Tool(raises=subprocess.TimeoutExpired("uv", 1))],
    ids=["exit", "timeout"],
)
def test_failed_uv_run_fails(
    tool: _Tool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _repo(tmp_path)

    assert _run(root, "constraints", ["pyproject.toml"], tool, monkeypatch) == 1

    assert capsys.readouterr().err.startswith("error: uv")


@pytest.mark.parametrize(
    "contents", [None, "{", '{"upgrades": []}'], ids=["unset", "bad", "empty"]
)
def test_unusable_data_file_is_a_usage_error(
    contents: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _repo(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.delenv(_DATA_FILE_ENV, raising=False)
    if contents is not None:
        data_file = tmp_path / "upgrades.json"
        data_file.write_text(contents, encoding="utf-8")
        monkeypatch.setenv(_DATA_FILE_ENV, str(data_file))
    tool = _Tool()
    monkeypatch.setattr(_script().subprocess, "run", tool)

    assert _script().main(["constraints"]) == 2

    assert tool.calls == []
    assert capsys.readouterr().err.startswith("error:")
