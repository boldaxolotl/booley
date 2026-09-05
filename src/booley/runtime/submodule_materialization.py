"""Materialize Git submodules from local Project repositories without network access."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from booley.config.settings import SubmoduleConfigError, load_submodule_config
from booley.runtime.filesystem_utils import safe_rmtree

_TIMEOUT_S = 300
_GIT_CONFIG = ("-c", "protocol.allow=never", "-c", "submodule.recurse=false")


class SubmoduleMaterializationError(RuntimeError):
    """A destination submodule could not be reconstructed from local objects."""


def materialize_submodules(
    source_root: Path,
    destination_root: Path,
    *,
    excluded_top_level: frozenset[str] = frozenset(),
) -> None:
    """Populate destination gitlinks from initialized same-path source repositories.

    ``excluded_top_level`` names checkouts already owned by a higher-level workspace
    transaction; their own nested gitlinks are materialized by that owner separately.
    """
    source_root = source_root.resolve()
    destination_root = destination_root.resolve()
    created: list[tuple[Path, bool]] = []
    try:
        selected = _selected_top_level_paths(source_root, destination_root)
        selected = [path for path in selected if path.as_posix() not in excluded_top_level]
        _materialize_tree(
            source_root,
            destination_root,
            destination_root,
            Path(),
            created,
            selected,
        )
    except SubmoduleMaterializationError:
        _rollback(destination_root, created)
        raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        _rollback(destination_root, created)
        raise SubmoduleMaterializationError(f"local submodule operation failed: {exc}") from exc
    except BaseException:
        _rollback(destination_root, created)
        raise


def materialize_ticket_submodules(source_root: Path, destination_root: Path) -> None:
    """Populate every repository in a composite Ticket checkout offline."""
    from booley.runtime.project_dir import checkout_project_dir_relative_to
    from booley.runtime.ticket_repositories import (
        TicketWorkspaceError,
        paired_project_repository,
        resolve_inner_project_repo,
    )

    source_root = source_root.resolve()
    destination_root = destination_root.resolve()
    try:
        paired_source = paired_project_repository(source_root)
    except TicketWorkspaceError as exc:
        raise SubmoduleMaterializationError(str(exc)) from exc
    project_source = (
        paired_source.worktree
        if paired_source is not None
        else resolve_inner_project_repo(source_root)
    )
    if project_source is None:
        materialize_submodules(source_root, destination_root)
        return
    try:
        project_relative = checkout_project_dir_relative_to(source_root)
    except (FileNotFoundError, ValueError) as exc:
        raise SubmoduleMaterializationError(f"paired project path is unavailable: {exc}") from exc
    project_destination = destination_root / project_relative
    if not (project_destination / ".git").exists():
        raise SubmoduleMaterializationError(
            f"paired project checkout is unavailable: {project_destination}"
        )
    materialize_submodules(
        source_root,
        destination_root,
        excluded_top_level=frozenset({project_relative.as_posix()}),
    )
    materialize_submodules(project_source, project_destination)


def _materialize_tree(
    source_root: Path,
    destination_repo: Path,
    destination_root: Path,
    prefix: Path,
    created: list[tuple[Path, bool]],
    selected: list[Path] | None = None,
) -> None:
    for relative in selected if selected is not None else _submodule_paths(destination_repo):
        full_relative = prefix / relative
        source = source_root / full_relative
        destination = destination_root / full_relative
        commit = _gitlink_commit(destination_repo, relative)
        _assert_link_free_path(destination, destination_root, "destination")
        _validate_source(source, source_root, full_relative)
        if _accept_existing(destination, commit):
            _materialize_tree(source_root, destination, destination_root, full_relative, created)
            continue
        had_placeholder = _remove_empty_placeholder(destination, destination_root)
        created.append((destination, had_placeholder))
        _create_repository(source, destination, commit, full_relative)
        _materialize_tree(source_root, destination, destination_root, full_relative, created)


def _selected_top_level_paths(source_root: Path, destination_root: Path) -> list[Path]:
    discovered = _submodule_paths(destination_root)
    try:
        configured_paths = load_submodule_config(source_root).paths
    except SubmoduleConfigError as exc:
        raise SubmoduleMaterializationError(str(exc)) from exc
    if configured_paths is None:
        return discovered
    configured = [Path(path) for path in configured_paths]
    for path in configured:
        _validate_relative_path(path)
    configured_values = {path.as_posix() for path in configured}
    return [path for path in discovered if path.as_posix() in configured_values]


def _submodule_paths(repository: Path) -> list[Path]:
    result = _run_git(repository, "ls-files", "--stage", "-z")
    records = [record for record in result.stdout.split("\0") if record]
    paths: list[Path] = []
    for record in records:
        if not record.startswith("160000 "):
            continue
        metadata, separator, raw_path = record.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise SubmoduleMaterializationError("Git index contains an invalid entry")
        mode, _object_id, stage = fields
        if mode != "160000":
            continue
        if stage != "0":
            raise SubmoduleMaterializationError(f"Git index has a conflicted gitlink: {raw_path}")
        paths.append(Path(raw_path))
    if len(paths) != len(set(paths)):
        raise SubmoduleMaterializationError(".gitmodules contains duplicate submodule paths")
    for path in paths:
        _validate_relative_path(path)
    return paths


def _validate_relative_path(path: Path) -> None:
    value = path.as_posix()
    parts = PurePosixPath(value).parts
    if (
        not value
        or value != value.strip()
        or not value.isprintable()
        or value.startswith(("-", "/"))
        or "\\" in value
        or PureWindowsPath(value).drive
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise SubmoduleMaterializationError(
            f"unsafe submodule path {value!r}; expected a repository-relative POSIX path"
        )


def _gitlink_commit(repository: Path, relative: Path) -> str:
    pathspec = f":(literal){relative.as_posix()}"
    result = _run_git(repository, "ls-files", "--stage", "-z", "--", pathspec)
    records = [record for record in result.stdout.split("\0") if record]
    if len(records) != 1 or not records[0].startswith("160000 "):
        raise SubmoduleMaterializationError(
            f"configured submodule path is not an exact Git submodule: {relative.as_posix()}"
        )
    return records[0].split(maxsplit=2)[1]


def _accept_existing(destination: Path, commit: str) -> bool:
    if destination.is_symlink() or _is_junction(destination):
        raise SubmoduleMaterializationError(f"submodule destination is a link: {destination}")
    if not destination.exists():
        return False
    if destination.is_dir() and not any(destination.iterdir()):
        return False
    git_dir = destination / ".git"
    if git_dir.is_symlink() or _is_junction(git_dir) or not git_dir.is_dir():
        raise SubmoduleMaterializationError(
            f"submodule destination is not a standalone repository: {destination}"
        )
    if _repository_is_dirty(destination):
        raise SubmoduleMaterializationError(f"submodule {destination} is dirty")
    actual = _run_git(destination, "rev-parse", "HEAD").stdout.strip()
    if actual != commit:
        raise SubmoduleMaterializationError(
            f"submodule {destination} is at {actual[:12]}, expected {commit[:12]}"
        )
    return True


def _validate_source(source: Path, source_root: Path, relative: Path) -> None:
    _assert_link_free_path(source, source_root, "source")
    if not source.is_dir() or not (source / ".git").exists():
        raise SubmoduleMaterializationError(
            f"submodule {relative.as_posix()} not found in source Project; initialize it first"
        )
    shallow = _run_git(source, "rev-parse", "--is-shallow-repository").stdout.strip()
    if shallow == "true":
        raise SubmoduleMaterializationError(
            f"submodule {relative.as_posix()} is shallow; fetch its complete history first"
        )
    if _repository_is_dirty(source):
        raise SubmoduleMaterializationError(f"submodule {relative.as_posix()} is dirty")


def _repository_is_dirty(repository: Path) -> bool:
    staged = _run_git(repository, "diff-index", "--cached", "--quiet", "HEAD", "--", check=False)
    unstaged = _run_git(
        repository,
        "diff-files",
        "--quiet",
        "--ignore-cr-at-eol",
        "--ignore-submodules=dirty",
        "--",
        check=False,
    )
    for action, result in (
        ("checking staged files", staged),
        ("checking worktree files", unstaged),
    ):
        if result.returncode not in (0, 1):
            _raise_git(action, result)
    untracked = _run_git(repository, "ls-files", "--others", "--exclude-standard", "-z")
    return staged.returncode == 1 or unstaged.returncode == 1 or bool(untracked.stdout)


def _remove_empty_placeholder(destination: Path, root: Path) -> bool:
    _assert_link_free_path(destination, root, "destination")
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        return False
    if not destination.is_dir() or any(destination.iterdir()):
        raise SubmoduleMaterializationError(f"submodule destination is not empty: {destination}")
    destination.rmdir()
    return True


def _create_repository(source: Path, destination: Path, commit: str, relative: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="booley-git-template-") as template:
        _run_git(destination.parent, "init", "-q", f"--template={template}", str(destination))
    _transfer_objects(source, destination, commit, relative)
    check = _run_git(
        destination,
        "fsck",
        "--connectivity-only",
        "--no-dangling",
        commit,
        check=False,
    )
    if check.returncode != 0:
        _raise_git(f"verifying local objects for {relative.as_posix()}", check)
    checkout = _run_git(destination, "checkout", "--detach", "--force", commit, check=False)
    if checkout.returncode != 0:
        _raise_git(f"checking out submodule {relative.as_posix()}", checkout)


def _transfer_objects(source: Path, destination: Path, commit: str, relative: Path) -> None:
    pack_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="booley-submodule-", suffix=".pack", delete=False
        ) as pack:
            pack_path = Path(pack.name)
            packed = subprocess.run(
                _git_command(source, "pack-objects", "--stdout", "--revs"),
                input=f"{commit}\n".encode(),
                stdout=pack,
                stderr=subprocess.PIPE,
                env=_offline_env(),
                timeout=_TIMEOUT_S,
                check=False,
            )
        if packed.returncode != 0:
            detail = packed.stderr.decode(errors="replace").strip()
            raise SubmoduleMaterializationError(
                f"local objects for submodule {relative.as_posix()} are incomplete: {detail}"
            )
        with pack_path.open("rb") as pack_input:
            indexed = subprocess.run(
                _git_command(destination, "index-pack", "--stdin", "--fix-thin"),
                stdin=pack_input,
                capture_output=True,
                text=True,
                env=_offline_env(),
                timeout=_TIMEOUT_S,
                check=False,
            )
        if indexed.returncode != 0:
            _raise_git(f"installing local objects for {relative.as_posix()}", indexed)
    finally:
        if pack_path is not None:
            pack_path.unlink(missing_ok=True)


def _rollback(root: Path, created: list[tuple[Path, bool]]) -> None:
    for destination, restore_placeholder in reversed(created):
        _assert_contained(destination, root)
        if destination.exists():
            safe_rmtree(destination, protect_git_root=False)
        if restore_placeholder:
            destination.mkdir(parents=True, exist_ok=True)


def _assert_contained(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SubmoduleMaterializationError(f"submodule path escapes destination: {path}") from exc


def _assert_link_free_path(path: Path, root: Path, role: str) -> None:
    _assert_contained(path, root)
    current = path
    while True:
        if current.is_symlink() or _is_junction(current):
            suffix = "is a link" if current == path else "crosses a link"
            raise SubmoduleMaterializationError(f"submodule {role} {suffix}: {current}")
        if current == root:
            return
        current = current.parent


def _is_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    return bool(checker and checker())


def _run_git(
    cwd: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        _git_command(cwd, *args),
        capture_output=True,
        text=True,
        env=_offline_env(),
        timeout=_TIMEOUT_S,
        check=False,
    )
    if check and result.returncode != 0:
        _raise_git("running local Git command", result)
    return result


def _git_command(cwd: Path, *args: str) -> list[str]:
    return ["git", *_GIT_CONFIG, "-C", str(cwd), *args]


def _offline_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _raise_git(action: str, result: subprocess.CompletedProcess[str]) -> None:
    detail = (result.stderr or result.stdout or str(result.returncode)).strip()
    raise SubmoduleMaterializationError(f"{action} failed: {detail}")
