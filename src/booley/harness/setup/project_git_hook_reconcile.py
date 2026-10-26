"""Publish and reconcile the Project Git-hook bundle and its adapters."""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from booley.core.booley_dir_links import lexical_booley_path
from booley.core.booley_layout import BOOLEY_DIR_NAME
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
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.booley_dir import resolve_booley_dir
from booley.runtime.git_environment import inherited_git_environment

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
    booley_dir: Path
    bundle_path: Path
    hooks_dir: Path


@dataclass(frozen=True, slots=True)
class _Adapter:
    name: str
    owner_marker: str
    target: Path
    body: str
    outcome: WriteOutcome


@dataclass(frozen=True, slots=True)
class _Action:
    """One pending reconciliation change; the planner emits them in apply order."""

    kind: str  # publish, adapter, remove, rename, rmdir, or keep
    path: Path
    detail: str
    adapter: _Adapter | None = None


def _shell_bundle_resolution(project_root: Path, bundle_path: Path) -> str:
    """Resolve a checkout-local bundle, including a secondary worktree."""
    root = project_root.resolve()
    # A bundle reached through a symlinked Booley Directory entry stays $ROOT-relative.
    physical = lexical_booley_path(root / BOOLEY_DIR_NAME, bundle_path.resolve())
    try:
        relative = physical.relative_to(root).as_posix()
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
            env=inherited_git_environment(),
        )
    except subprocess.TimeoutExpired:
        skip("could not resolve Git hooks path within 10 seconds")
        ctx.record("project_git_hooks", "skip", "Git hooks path resolution timed out")
        return None
    if result.returncode != 0 or not result.stdout.strip():
        skip("Project Root is not a git repo — Project Git policy skipped")
        ctx.record("project_git_hooks", "skip", "not a git repo")
        return None
    booley_dir = resolve_booley_dir(ctx.project_root)
    managed_dir = booley_dir / ".managed"
    return _Locations(
        booley_dir,
        managed_dir / BUNDLE_NAME,
        (ctx.project_root / result.stdout.strip()).resolve(),
    )


def _adapters(ctx: InitContext, locations: _Locations) -> list[_Adapter]:
    """Describe adapter writes with dry-run ownership outcomes."""
    specs = (
        (
            "commit-msg",
            "commit_msg_hook.py",
            _build_commit_msg_hook_body(ctx.project_root, locations.bundle_path),
        ),
        (
            "pre-push",
            "pre_push_hook.py",
            _build_pre_push_hook_body(ctx.project_root, locations.bundle_path),
        ),
    )
    return [
        _Adapter(
            name,
            owner_marker,
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
        for name, owner_marker, body in specs
    ]


def _bundle_pending(path: Path, content: bytes) -> bool:
    """Return whether the destination differs from complete bundle bytes."""
    try:
        return path.read_bytes() != content
    except OSError:
        return True


def _publish_bundle(path: Path, content: bytes) -> None:
    """Publish a complete non-executable bundle atomically."""
    atomic_replace_bytes(path, content, mode=0o644)


def _canonical_source(name: str) -> bytes:
    """Return Booley's LF-normalized copy of one retired hook file."""
    return _normalized_source(_source_package_root() / dict(_SOURCE_INVENTORY)[name])


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


def _legacy_action(path: Path) -> _Action | None:
    """Decide what migration does with one retired hook file name."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    backup = path.with_name(f"{path.name}.pre-booley")
    if not stat.S_ISREG(mode):
        return _Action("keep", path, f"preserve unrecognized legacy path {path.name}")
    data = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if data == _canonical_source(path.name):
        return _Action("remove", path, f"remove legacy {path.name}")
    if not os.path.lexists(backup):
        return _Action("rename", path, f"rename divergent {path.name} to {backup.name}")
    return _Action("keep", path, f"keep divergent {path.name}: {backup.name} already exists")


def _legacy_actions(locations: _Locations) -> list[_Action]:
    """Return pending retired-hook migration changes: legacy files, bytecode, cache dir."""
    hooks = locations.booley_dir / "hooks"
    legacy = (_legacy_action(hooks / name) for name in _LEGACY_MANAGED_HOOKS)
    actions = [action for action in legacy if action is not None]
    cache = hooks / "__pycache__"
    bytecode = _managed_bytecode(cache)
    actions.extend(
        _Action("remove", path, f"remove managed bytecode {path.name}") for path in bytecode
    )
    if bytecode and set(cache.iterdir()) <= set(bytecode):
        actions.append(_Action("rmdir", cache, "remove empty hooks/__pycache__"))
    return actions


def _plan(ctx: InitContext, locations: _Locations, bundle: bytes) -> list[_Action]:
    """Return every pending change in apply order.

    Order: bundle, adapters, legacy files, then refused (symlinked) adapters. A
    refused adapter fails the step only after every other change is applied, so
    one bad link never blocks the other adapter or the migration.
    """
    actions = []
    if _bundle_pending(locations.bundle_path, bundle):
        actions.append(_Action("publish", locations.bundle_path, f"publish {BUNDLE_NAME}"))
    adapters = _adapters(ctx, locations)
    for adapter in adapters:
        if adapter.outcome is WriteOutcome.WRITTEN:
            actions.append(_Action("adapter", adapter.target, f"update {adapter.name}", adapter))
        elif adapter.outcome is WriteOutcome.BACKED_UP:
            detail = f"back up existing {adapter.name} adapter"
            actions.append(_Action("adapter", adapter.target, detail, adapter))
    actions.extend(_legacy_actions(locations))
    actions.extend(
        _Action("adapter", adapter.target, f"refuse symlinked {adapter.name} adapter", adapter)
        for adapter in adapters
        if adapter.outcome is WriteOutcome.REFUSED
    )
    return actions


def _write_adapter(adapter: _Adapter) -> None:
    """Install one adapter and reject unexpected ownership outcomes."""
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
        warn(f"backed up existing {adapter.name} adapter to {adapter.name}.pre-booley*")


def _apply(action: _Action, bundle: bytes) -> None:
    """Carry out one planned change; a rerun resumes after any failure."""
    if action.kind == "publish":
        _publish_bundle(action.path, bundle)
    elif action.kind == "adapter" and action.adapter is not None:
        _write_adapter(action.adapter)
    elif action.kind == "remove":
        action.path.unlink()
    elif action.kind == "rmdir":
        action.path.rmdir()
    elif action.kind == "rename":
        action.path.rename(action.path.with_name(f"{action.path.name}.pre-booley"))
        warn(f"renamed divergent legacy hook {action.path.name} to {action.path.name}.pre-booley")
    else:
        warn(action.detail)


def _tracking_git(directory: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Inspect the owning repository without ambient repository overrides."""
    env = inherited_git_environment()
    env["LC_ALL"] = "C"
    return subprocess.run(
        ["git", "-C", str(directory), "--literal-pathspecs", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env=env,
    )


def _bundle_tracking_detail(locations: _Locations) -> str | None:
    """Return manual remediation only when the literal bundle is in the index."""
    project = locations.booley_dir.resolve()
    ancestor = next(path for path in (project, *project.parents) if path.is_dir())
    owner_probe = _tracking_git(ancestor, "rev-parse", "--show-toplevel")
    if owner_probe.returncode:
        if (
            owner_probe.stderr.strip()
            == "fatal: not a git repository (or any of the parent directories): .git"
        ):
            return None
        raise RuntimeError(owner_probe.stderr.strip() or "Git owner discovery failed")
    if not owner_probe.stdout.strip():
        raise RuntimeError("Git owner discovery returned an empty repository root")
    owner = Path(owner_probe.stdout.strip()).resolve()
    relative = locations.bundle_path.resolve().relative_to(owner).as_posix()
    tracked = _tracking_git(owner, "ls-files", "--error-unmatch", "--", relative)
    if tracked.returncode == 1:
        return None
    if tracked.returncode:
        raise RuntimeError(tracked.stderr.strip() or "Git bundle index inspection failed")
    command = shlex.join(["git", "-C", str(owner), "rm", "--cached", "--", relative])
    return (
        f"Generated Project Git-hook bundle is already tracked; untrack it with: {command}. "
        "Commit the intentional removal and changed ignore policy yourself outside active Goal Mode; "
        "rerun init in other checkouts if removal deletes their local bundle."
    )


def _report_bundle_tracking(ctx: InitContext, locations: _Locations) -> None:
    """Keep a manual tracking advisory separate from mechanical pending work."""
    try:
        detail = _bundle_tracking_detail(locations)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        detail = f"Could not inspect Project Git-hook bundle tracking: {exc}; inspect Git tracking manually."
    if detail:
        warn(detail)
        ctx.record("project_git_hook_tracking", "skip", detail)


def step_project_git_hooks(ctx: InitContext) -> None:
    """Reconcile the bundle, adapters, and legacy managed files."""
    ctx.step_banner("project Git-hook bundle")
    locations = _locations(ctx)
    if locations is None:
        return
    _report_bundle_tracking(ctx, locations)
    try:
        bundle = build_project_git_hook_bundle().content
        actions = _plan(ctx, locations, bundle)
        if not actions:
            skip("project Git-hook bundle and adapters are current")
            ctx.record("project_git_hooks", "skip", "current")
            return
        if ctx.check_only:
            detail = "; ".join(action.detail for action in actions)
            warn(detail)
            ctx.record("project_git_hooks", "warn", detail)
            return
        for action in actions:
            _apply(action, bundle)
    except (OSError, RuntimeError, ValueError) as exc:
        err(f"Project Git-hook reconciliation failed: {exc}")
        ctx.record("project_git_hooks", "err", str(exc))
        return
    ok(
        f"Project Git policy installed in {BUNDLE_NAME}; Project-authored lifecycle hooks preserved"
    )
    ctx.record("project_git_hooks", "ok", "installed")
