"""Select one physical Booley Directory namespace for linked worktrees."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from booley.core.booley_dir import resolve_booley_dir, resolve_checkout_booley_dir
from booley.core.boundary import as_dict, as_str_list, is_str_list


def worktree_state_dir(project_root: Path, *, booley_dir: Path | None = None) -> Path:
    """Use a checkout-local spelling only when it names the selected state."""
    root = project_root.resolve()
    selected = booley_dir.resolve() if booley_dir is not None else resolve_booley_dir(root)
    local = resolve_checkout_booley_dir(root)
    try:
        local.relative_to(root)
        if local.samefile(selected):
            return local
    except (OSError, ValueError):
        pass
    return selected


def relative_worktree_paths(project_root: Path) -> bool:
    """Whether this layout can carry relative links between host and Sandbox."""
    root = project_root.resolve()
    if not _git_supports_relative_paths() or (root / ".git").is_file():
        return False
    try:
        worktree_state_dir(root).relative_to(root)
    except ValueError:
        return False
    if root == Path("/work"):
        alias = Path("/booley-project")
        return (
            alias.is_symlink()
            and alias.readlink() == Path("/work/.booley")
            and alias.lstat().st_uid == 0
            and Path("/").stat().st_uid == 0
            and not Path("/").stat().st_mode & 0o022
        )
    return True


def worktree_creation_config(project_root: Path) -> tuple[str, ...]:
    """Override only this creation when the selected topology is not portable."""
    return (
        () if relative_worktree_paths(project_root) else ("-c", "worktree.useRelativePaths=false")
    )


def _git_supports_relative_paths() -> bool:
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    match = re.match(r"git version (\d+)\.(\d+)", result.stdout)
    return (
        result.returncode == 0 and match is not None and tuple(map(int, match.groups())) >= (2, 48)
    )


class HostRemovalError(ValueError):
    """A new worktree cannot be removed using a proven host namespace."""


@dataclass(frozen=True)
class HostRemovalContext:
    """Validated host paths and workspace kind for a removal recipe."""

    root: PurePosixPath
    source: PurePosixPath
    destination: PurePosixPath
    paired: bool


def _removal_refusal(detail: str) -> HostRemovalError:
    return HostRemovalError(
        f"cannot provide host removal instructions: {detail}. "
        "Use host initialization/session reissuance for a supported primary-root, "
        "local-state canonical-alias Sandbox; enable worktree.useRelativePaths "
        "with Git >=2.48 through host setup/repair before re-entering the Sandbox"
    )


def _host_mounts(spec: Path) -> tuple[PurePosixPath, PurePosixPath]:
    try:
        value = json.loads(spec.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _removal_refusal(f"cannot read protected Sandbox definition: {exc}") from exc
    mounts = (as_dict(value) or {}).get("mounts")
    if not is_str_list(mounts):
        raise _removal_refusal("protected Sandbox mounts are missing")
    parsed = [_parse_host_mount(raw) for raw in as_str_list(mounts)]
    if any(
        m.get("target", "").startswith(("/work/.booley/", "/work/.devcontainer/")) for m in parsed
    ):
        raise _removal_refusal("conflicting subtree mounts are unsupported")
    definition = _one_host_bind(parsed, "/work/.devcontainer", readonly=True)
    state = _one_host_bind(parsed, "/work/.booley", readonly=False)
    if any(m.get("target", "").startswith("/booley-project") for m in parsed):
        raise _removal_refusal("separate Booley Directory binds are unsupported")
    if definition.name != ".devcontainer" or state != definition.parent / ".booley":
        raise _removal_refusal(
            "host definition and local-state binds do not share relative geometry"
        )
    return definition.parent, state


def _parse_host_mount(raw: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in raw.split(","):
        key, _, value = part.partition("=")
        if key in fields:
            raise _removal_refusal("duplicate mount fields")
        fields[key] = value
    return fields


def _one_host_bind(mounts: list[dict[str, str]], target: str, *, readonly: bool) -> PurePosixPath:
    matches = [m for m in mounts if m.get("target") == target]
    if len(matches) != 1:
        raise _removal_refusal(f"{target} requires exactly one host source")
    mount = matches[0]
    source = mount.get("source", "")
    path = PurePosixPath(source)
    if (
        set(mount)
        != ({"source", "target", "type", "readonly"} if readonly else {"source", "target", "type"})
        or mount.get("readonly", "") != ""
        or mount.get("type") != "bind"
        or ("readonly" in mount) != readonly
        or not source.startswith("/")
        or source.startswith("//")
        or ".." in path.parts
        or source.startswith(("/run/desktop/", "/host_mnt/"))
        or re.match(r"/mnt/[a-z]/", source) is not None
    ):
        raise _removal_refusal(f"{target} has no supported absolute host bind source")
    return path


def host_removal_context(
    project_root: Path, worktree: Path, *, paired: bool
) -> HostRemovalContext:
    """Prove removal paths without changing Git configuration or registrations."""
    from booley.runtime import runtime_context
    from booley.runtime.project_repositories import run_git

    root = project_root.resolve()
    spec = root / ".devcontainer/devcontainer.json"
    if not spec.exists():
        if runtime_context.inside_session_runtime():
            raise _removal_refusal("protected Sandbox definition is unavailable")
        host = PurePosixPath(root.as_posix())
        return HostRemovalContext(
            host, host / ".booley", PurePosixPath(worktree.resolve().as_posix()), paired
        )
    host, source = _host_mounts(spec)
    try:
        destination = worktree.resolve().relative_to(root)
        if worktree_state_dir(root) != (root / ".booley").resolve():
            raise ValueError("selected Booley Directory is external")
    except ValueError as exc:
        raise _removal_refusal("worktree or Booley Directory escapes the primary root") from exc
    if (root / ".booley").is_symlink() or not relative_worktree_paths(root):
        raise _removal_refusal("layout or Git version cannot use relative registrations")
    for repository in (root, root / ".booley") if paired else (root,):
        if (repository / ".git").is_symlink():
            raise _removal_refusal("Git metadata is outside the proven primary-root geometry")
        config = run_git(repository, "config", "--bool", "--get", "worktree.useRelativePaths")
        if config.returncode or config.stdout.strip() != "true":
            raise _removal_refusal("effective worktree.useRelativePaths is not true")
    return HostRemovalContext(host, source, host / destination.as_posix(), paired)
