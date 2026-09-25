"""Publish and reconcile the Project Git-hook bundle and its adapters."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import subprocess
import tempfile
import zipfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from booley.harness.setup.common import (
    InitContext,
    WriteOutcome,
    err,
    guarded_write,
    ok,
    skip,
    warn,
)
from booley.harness.setup.project_git_hook_bundle import (
    _SOURCE_INVENTORY,
    BUNDLE_NAME,
    _normalized_source,
    _source_package_root,
    build_project_git_hook_bundle,
)
from booley.runtime.project_dir import resolve_project_dir

_LEGACY_MANAGED_HOOKS = (
    "boundary.py",
    "checkout_role.py",
    "run_command.py",
    "commit_msg_utils.py",
    "validate_commit_msg.py",
    "commit_msg_hook.py",
    "pre_push_hook.py",
)


@dataclass(frozen=True, slots=True)
class _Locations:
    project_dir: Path
    bundle_path: Path
    hooks_dir: Path


@dataclass(frozen=True, slots=True)
class _Adapter:
    name: str
    owner_marker: str
    command: str
    target: Path
    body: str
    outcome: WriteOutcome


@dataclass(frozen=True, slots=True)
class _PathState:
    """Recoverable state for one path touched by reconciliation."""

    kind: str
    data: bytes | None = None
    target: Path | str | None = None
    mode: int | None = None


class _ReconciliationTransaction:
    """Restore the pre-init filesystem state if reconciliation is interrupted."""

    def __init__(self) -> None:
        self._states: dict[Path, _PathState] = {}

    def watch(self, path: Path) -> None:
        """Record *path* once before a mutation can touch it."""
        if path in self._states:
            return
        try:
            info = path.lstat()
        except FileNotFoundError:
            self._states[path] = _PathState("missing")
            return
        if stat.S_ISLNK(info.st_mode):
            self._states[path] = _PathState("symlink", target=path.readlink())
        elif stat.S_ISREG(info.st_mode):
            self._states[path] = _PathState("file", data=path.read_bytes(), mode=info.st_mode)
        elif stat.S_ISDIR(info.st_mode):
            self._states[path] = _PathState("directory", mode=info.st_mode)
        else:
            self._states[path] = _PathState("other", mode=info.st_mode)

    def rollback(self) -> None:
        """Restore watched paths deepest-first, retaining no partial success."""
        for path, state in sorted(
            self._states.items(), key=lambda item: len(item[0].parts), reverse=True
        ):
            _restore_path(path, state)


def _remove_path(path: Path) -> None:
    """Remove one path if it is now present, without following symlinks."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        path.rmdir()
    else:
        path.unlink()


def _restore_path(path: Path, state: _PathState) -> None:
    """Restore one path state recorded by :class:`_ReconciliationTransaction`."""
    if state.kind == "missing":
        with suppress(FileNotFoundError, OSError):
            _remove_path(path)
        return
    if state.kind == "other":
        return
    _remove_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if state.kind == "directory":
        path.mkdir()
    elif state.kind == "symlink":
        path.symlink_to(state.target)
    elif state.kind == "file":
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=f".{path.name}.", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(state.data or b"")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(stat.S_IMODE(state.mode or 0o644))
            temporary.replace(path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _shell_bundle_resolution(project_root: Path, bundle_path: Path) -> str:
    """Resolve a checkout-local bundle, including a secondary worktree."""
    try:
        relative = bundle_path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return f"BUNDLE={shlex.quote(str(bundle_path.resolve()))}\n"
    return (
        f'BUNDLE="$ROOT/{relative}"\n'
        'if [ ! -f "$BUNDLE" ]; then\n'
        '    COMMON=$(cd "$ROOT" && git rev-parse --path-format=absolute '
        "--git-common-dir 2>/dev/null) || COMMON=\n"
        '    case "$COMMON" in\n'
        "        '') ;;\n"
        f'        /*) BUNDLE="$(dirname "$COMMON")/{relative}" ;;\n'
        f'        [A-Za-z]:/*) BUNDLE="$(dirname "$COMMON")/{relative}" ;;\n'
        f'        *) BUNDLE="$(dirname "$ROOT/$COMMON")/{relative}" ;;\n'
        "    esac\n"
        "fi\n"
    )


def _build_hook_delegator_body(
    project_root: Path,
    bundle_path: Path,
    bundle_command: str,
    purpose: str,
    *,
    fail_open: bool,
) -> str:
    """Build one isolated shell adapter for the managed bundle."""
    owner_marker = "commit_msg_hook.py" if bundle_command == "commit-msg" else "pre_push_hook.py"
    missing_exit = 0 if fail_open else 1
    missing = (
        f"    echo 'booley {BUNDLE_NAME} {bundle_command}: bundle not found at' \"$BUNDLE\" >&2\n"
    )
    if fail_open:
        missing += (
            "    echo 'Skipping this local commit; run `booley init` to restore the bundle.' >&2\n"
        )
    else:
        missing += "    echo 'This guard cannot approve a push it could not check; the push is REFUSED.' >&2\n"
        missing += "    echo 'Restore it with `booley init`.' >&2\n"
    missing += f"    exit {missing_exit}\n"
    return (
        "#!/bin/sh\n"
        f"# Booley {purpose}.\n"
        f"# Managed bundle: {BUNDLE_NAME}; command: {bundle_command}; owner: {owner_marker}\n"
        f"ROOT=$(git rev-parse --show-toplevel) || {{ echo 'booley {BUNDLE_NAME} "
        f"{bundle_command}: repository root not found' >&2; exit {missing_exit}; }}\n"
        + _shell_bundle_resolution(project_root, bundle_path)
        + 'if [ ! -f "$BUNDLE" ]; then\n'
        + missing
        + "fi\n"
        + "# Probe the same isolated interpreter mode used for the real invocation.\n"
        + "PY=\n"
        + "for cand in python3 python; do\n"
        + "    if \"$cand\" -I -S -c '' >/dev/null 2>&1; then PY=$cand; break; fi\n"
        + "done\n"
        + "if [ -z \"$PY\" ] && py -3 -I -S -c '' >/dev/null 2>&1; then PY='py -3'; fi\n"
        + 'if [ -z "$PY" ]; then\n'
        + f"    echo 'booley {BUNDLE_NAME} {bundle_command}: no usable isolated Python found "
        + "(tried python3, python, py -3)' >&2\n"
        + "    exit 1\n"
        + "fi\n"
        + f'exec $PY -I -S "$BUNDLE" {bundle_command} "$@"\n'
    )


def _build_commit_msg_hook_body(project_root: Path, bundle_path: Path) -> str:
    """Build the fail-open commit-msg adapter."""
    return _build_hook_delegator_body(
        project_root,
        bundle_path,
        "commit-msg",
        "commit-msg policy adapter",
        fail_open=True,
    )


def _build_pre_push_hook_body(project_root: Path, bundle_path: Path) -> str:
    """Build the fail-closed pre-push adapter."""
    return _build_hook_delegator_body(
        project_root,
        bundle_path,
        "pre-push",
        "pre-push leak-guard adapter",
        fail_open=False,
    )


def _locations(ctx: InitContext) -> _Locations | None:
    """Resolve all Project and Git-hook destinations before mutation."""
    try:
        result = subprocess.run(
            ["git", "-C", str(ctx.project_root), "rev-parse", "--git-path", "hooks"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        skip("could not resolve Git hooks path within 10 seconds")
        ctx.record("project_git_hooks", "skip", "Git hooks path resolution timed out")
        return None
    if result.returncode != 0 or not result.stdout.strip():
        skip("project root is not a git repo — Project Git policy skipped")
        ctx.record("project_git_hooks", "skip", "not a git repo")
        return None
    project_dir = resolve_project_dir(ctx.project_root)
    managed_dir = project_dir / ".managed"
    return _Locations(
        project_dir,
        managed_dir / BUNDLE_NAME,
        (ctx.project_root / result.stdout.strip()).resolve(),
    )


def _adapters(ctx: InitContext, locations: _Locations) -> list[_Adapter]:
    """Describe adapter writes with dry-run ownership outcomes."""
    specs = (
        (
            "commit-msg",
            "commit_msg_hook.py",
            "commit-msg",
            _build_commit_msg_hook_body(ctx.project_root, locations.bundle_path),
        ),
        (
            "pre-push",
            "pre_push_hook.py",
            "pre-push",
            _build_pre_push_hook_body(ctx.project_root, locations.bundle_path),
        ),
    )
    return [
        _Adapter(
            name,
            owner_marker,
            command,
            locations.hooks_dir / name,
            body,
            guarded_write(
                locations.hooks_dir / name,
                body,
                owner_marker=owner_marker,
                backup_suffix=".pre-booley",
                dry_run=True,
                newline="\n",
                executable=True,
            ),
        )
        for name, owner_marker, command, body in specs
    ]


def _bundle_pending(path: Path, content: bytes) -> bool:
    """Return whether the destination differs from complete bundle bytes."""
    try:
        return path.read_bytes() != content
    except OSError:
        return True


def _publish_bundle(path: Path, content: bytes) -> None:
    """Publish a complete non-executable bundle atomically."""
    if not _bundle_pending(path, content):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{BUNDLE_NAME}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o644)
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _verify_adapters(adapters: list[_Adapter]) -> None:
    """Verify each installed adapter invokes its bundle command."""
    for adapter in adapters:
        body = adapter.target.read_text(encoding="utf-8")
        invocation = f'"$BUNDLE" {adapter.command} "$@"'
        if BUNDLE_NAME not in body or invocation not in body:
            raise RuntimeError(
                f"installed {adapter.name} adapter does not invoke {BUNDLE_NAME} {adapter.command}"
            )


def _current_source_bytes() -> dict[str, bytes]:
    """Read canonical sources for legacy-file recognition."""
    root = _source_package_root()
    return {name: _normalized_source(root / relative) for name, relative in _SOURCE_INVENTORY}


def _previous_bundle_hashes(path: Path) -> dict[str, str]:
    """Read source hashes from the previous bundle when available."""
    try:
        with zipfile.ZipFile(path) as archive:
            document = json.loads(archive.read("manifest.json"))
        if not isinstance(document, dict):
            return {}
        values = document.get("source_sha256", {})
        if not isinstance(values, dict):
            return {}
        return {name: value for name, value in values.items() if isinstance(value, str)}
    except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile):
        return {}


def _legacy_matches(
    path: Path, name: str, current: dict[str, bytes], previous: dict[str, str]
) -> bool:
    """Return whether a legacy source matches current or previous managed code."""
    data = path.read_bytes()
    if data == current[name]:
        return True
    normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if normalized == current[name]:
        return True
    return hashlib.sha256(normalized).hexdigest() == previous.get(name)


def _backup_candidate(path: Path, data: bytes) -> Path:
    """Allocate a digest-bearing backup without overwriting another file."""
    digest = hashlib.sha256(data).hexdigest()[:12]
    for index in range(1000):
        suffix = "" if index == 0 else f"-{index}"
        candidate = path.with_name(f"{path.name}.pre-booley.{digest}{suffix}")
        if not candidate.exists():
            return candidate
        if candidate.is_file() and candidate.read_bytes() == data:
            return candidate
    raise OSError(f"could not allocate a collision-safe backup for {path}")


def _publish_backup(path: Path, data: bytes, mode: int, *, target: Path | None = None) -> Path:
    """Publish one legacy backup atomically."""
    target = target or _backup_candidate(path, data)
    if target.is_file() and target.read_bytes() == data:
        return target
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{target.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(stat.S_IMODE(mode))
        temporary.replace(target)
        temporary = None
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _managed_bytecode(cache_dir: Path) -> list[Path]:
    """Return only bytecode for the seven retired source module names."""
    if not cache_dir.is_dir():
        return []
    stems = {Path(name).stem for name in _LEGACY_MANAGED_HOOKS}
    return sorted(
        path
        for path in cache_dir.iterdir()
        if path.is_file() and path.suffix == ".pyc" and path.name.split(".", 1)[0] in stems
    )


def _legacy_detail(
    legacy_dir: Path, current: dict[str, bytes], previous: dict[str, str]
) -> list[str]:
    """Describe legacy sources and bytecode that cleanup would remove."""
    details: list[str] = []
    for name in _LEGACY_MANAGED_HOOKS:
        path = legacy_dir / name
        if not path.exists():
            continue
        if not path.is_file():
            details.append(f"preserve unrecognized legacy path {name}")
        elif _legacy_matches(path, name, current, previous):
            details.append(f"remove legacy {name}")
        else:
            details.append(
                f"back up divergent {name} as {_backup_candidate(path, path.read_bytes()).name}, then remove it"
            )
    cache_dir = legacy_dir / "__pycache__"
    bytecode = _managed_bytecode(cache_dir)
    details.extend(f"remove managed bytecode {path.name}" for path in bytecode)
    if bytecode and all(path in bytecode for path in cache_dir.iterdir()):
        details.append("remove empty hooks/__pycache__")
    return details


def _cleanup_legacy(
    legacy_dir: Path,
    current: dict[str, bytes],
    previous: dict[str, str],
    transaction: _ReconciliationTransaction,
) -> list[str]:
    """Remove only known legacy files after adapters are verified."""
    errors: list[str] = []
    for name in _LEGACY_MANAGED_HOOKS:
        path = legacy_dir / name
        if not path.exists():
            continue
        if not path.is_file():
            warn(f"preserving unrecognized legacy path {path}")
            continue
        try:
            transaction.watch(path)
            data = path.read_bytes()
            if not _legacy_matches(path, name, current, previous):
                backup = _backup_candidate(path, data)
                transaction.watch(backup)
                _publish_backup(path, data, path.stat().st_mode, target=backup)
                warn(f"backed up divergent legacy hook {name} to {backup.name}")
            path.unlink()
        except OSError as exc:
            errors.append(f"could not remove legacy {name}: {exc}")
    cache_dir = legacy_dir / "__pycache__"
    transaction.watch(cache_dir)
    for path in _managed_bytecode(cache_dir):
        try:
            transaction.watch(path)
            path.unlink()
        except OSError as exc:
            errors.append(f"could not remove managed bytecode {path.name}: {exc}")
    if cache_dir.is_dir():
        with suppress(OSError):
            cache_dir.rmdir()
    return errors


def _apply_adapters(adapters: list[_Adapter], transaction: _ReconciliationTransaction) -> None:
    """Install adapters and reject unexpected ownership outcomes."""
    for adapter in adapters:
        transaction.watch(adapter.target)
        transaction.watch(adapter.target.with_name(adapter.target.name + ".pre-booley"))
        outcome = guarded_write(
            adapter.target,
            adapter.body,
            owner_marker=adapter.owner_marker,
            backup_suffix=".pre-booley",
            newline="\n",
            executable=True,
        )
        if outcome not in (WriteOutcome.WRITTEN, WriteOutcome.UNCHANGED, WriteOutcome.BACKED_UP):
            raise RuntimeError(f"could not install {adapter.name} adapter: {outcome.value}")
        if outcome is WriteOutcome.BACKED_UP:
            warn(f"backed up existing {adapter.name} adapter to {adapter.name}.pre-booley")


def _pending_detail(
    bundle_path: Path,
    bundle_content: bytes,
    adapters: list[_Adapter],
    legacy: list[str],
) -> list[str]:
    """Combine all planned mutations for check-only output."""
    details = [f"publish {BUNDLE_NAME}"] if _bundle_pending(bundle_path, bundle_content) else []
    updates = [adapter.name for adapter in adapters if adapter.outcome is WriteOutcome.WRITTEN]
    if updates:
        details.append("update " + ", ".join(updates))
    details.extend(
        f"back up existing {adapter.name} adapter"
        for adapter in adapters
        if adapter.outcome is WriteOutcome.BACKED_UP
    )
    details.extend(legacy)
    return details


def step_project_git_hooks(ctx: InitContext) -> None:
    """Reconcile the bundle, adapters, and legacy managed files."""
    ctx.step_banner("project Git-hook bundle")
    locations = _locations(ctx)
    if locations is None:
        return
    try:
        bundle = build_project_git_hook_bundle()
        current = _current_source_bytes()
        previous = _previous_bundle_hashes(locations.bundle_path)
        adapters = _adapters(ctx, locations)
        details = _pending_detail(
            locations.bundle_path,
            bundle.content,
            adapters,
            _legacy_detail(locations.project_dir / "hooks", current, previous),
        )
        if not details:
            skip("project Git-hook bundle and adapters are current")
            ctx.record("project_git_hooks", "skip", "current")
            return
        if ctx.check_only:
            detail = "; ".join(details)
            warn(detail)
            ctx.record("project_git_hooks", "warn", detail)
            return
        transaction = _ReconciliationTransaction()
        transaction.watch(locations.bundle_path)
        transaction.watch(locations.bundle_path.parent)
        _publish_bundle(locations.bundle_path, bundle.content)
        _apply_adapters(adapters, transaction)
        _verify_adapters(adapters)
        errors = _cleanup_legacy(locations.project_dir / "hooks", current, previous, transaction)
        if errors:
            raise RuntimeError("; ".join(errors))
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        if "transaction" in locals():
            with suppress(OSError):
                transaction.rollback()
        err(f"Project Git-hook reconciliation failed: {exc}")
        ctx.record("project_git_hooks", "err", str(exc))
        return
    ok(
        f"Project Git policy installed in {BUNDLE_NAME}; Project-authored lifecycle hooks preserved"
    )
    ctx.record("project_git_hooks", "ok", "installed")
