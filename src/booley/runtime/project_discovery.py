"""Infer a checkout without mistaking its Booley Directory for design sources."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from itertools import chain, islice
from pathlib import Path

from booley.core.booley_dir import BOOLEY_DIR_NAME, resolve_authoritative_booley_dir
from booley.core.checkout_role import SourceCheckoutProjectError, require_project_checkout
from booley.runtime.booley_dir import contains
from booley.runtime.sandbox_layout import BOOLEY_DIR_TARGET, WORK_DIR


class ProjectRootDiscoveryError(RuntimeError):
    """A Booley Directory cannot prove its owning checkout."""


def has_git_worktree_marker(path: Path) -> bool:
    """Recognize a checkout, excluding empty mounted Git sentinels."""
    marker = path / ".git"
    try:
        return (
            (marker / "HEAD").is_file()
            if marker.is_dir()
            else marker.is_file()
            and marker.read_text(encoding="utf-8", errors="replace").startswith("gitdir:")
        )
    except OSError:
        return False


def _data_root(
    start: Path, checkouts: list[Path], checkout_data: Callable[[Path], Path | None]
) -> Path | None:
    for parent in (start, *start.parents):
        if parent.name == BOOLEY_DIR_NAME and (
            parent != Path.home() / BOOLEY_DIR_NAME or has_git_worktree_marker(parent.parent)
        ):
            return parent
        if parent in checkouts:
            break
    configured = [os.environ.get("BOOLEY_DIR")]
    if os.environ.get("BOOLEY_CONTAINER") == "1":
        configured.append(BOOLEY_DIR_TARGET)
    directories = chain(
        (Path(value).resolve() for value in configured if value),
        (
            data.resolve()
            for checkout in checkouts
            if (data := checkout_data(checkout)) is not None
        ),
    )
    for data in directories:
        if contains(start, booley_dir=data) is not None:
            if any(
                (canonical := contains(p, booley_dir=data)) is not None
                and canonical != data.resolve()
                for p in checkouts
            ):
                continue
            return data
    return None


def _checkout_candidates(start: Path | None) -> tuple[Path, list[Path]]:
    logical = Path(start or Path.cwd()).absolute()
    current = logical.resolve()
    if start is None and os.environ.get("PWD"):
        candidate = Path(os.environ["PWD"])
        try:
            if candidate.samefile(current):
                logical = candidate.absolute()
        except OSError:
            pass
    paths = (current, *current.parents, logical, *logical.parents)
    checkouts = list(
        dict.fromkeys(
            p.resolve()
            for p in paths
            if (p.name != BOOLEY_DIR_NAME or not has_git_worktree_marker(p.parent))
            and has_git_worktree_marker(p)
        )
    )
    return current, checkouts


def _owning_checkouts(
    candidates: list[Path], data: Path, checkout_data: Callable[[Path], Path | None]
) -> set[Path]:
    owners = set()
    for candidate in candidates:
        if contains(candidate, booley_dir=data) is None and has_git_worktree_marker(candidate):
            try:
                root = candidate.resolve()
                selected = checkout_data(root)
                if selected is not None and selected.samefile(data):
                    owners.add(root)
            except OSError:
                pass
    return owners


def discover_project_root(
    start: Path | None = None, *, required: bool = False, explicit_selection: bool = False
) -> Path | None:
    """Resolve explicit selection or an independently proven owning checkout."""

    @cache
    def checkout_data(root):
        try:
            return resolve_authoritative_booley_dir(require_project_checkout(root))
        except SourceCheckoutProjectError:
            return None

    current, checkouts = _checkout_candidates(start)
    data = _data_root(current, checkouts, checkout_data)
    env = None if explicit_selection else os.environ.get("RTL_PROJECT_ROOT")
    explicit = None
    if env:
        explicit, explicit_checkouts = _checkout_candidates(Path(env))
        explicit_data = _data_root(explicit, explicit_checkouts, checkout_data)
        if explicit_data is None:
            return explicit
        data = data or explicit_data
    if data is not None:
        candidates = checkouts[:]
        if os.environ.get("BOOLEY_CONTAINER") == "1":
            candidates.append(Path(WORK_DIR))
        owners = _owning_checkouts(candidates, data, checkout_data)
        if not owners and start is not None:
            # A data-path lookup can originate in its owning checkout.
            try:
                _current, cwd_checkouts = _checkout_candidates(None)
            except OSError:
                cwd_checkouts = []
            owners = _owning_checkouts(cwd_checkouts, data, checkout_data)
        if len(owners) == 1 and explicit is None:
            return owners.pop()
        raise ProjectRootDiscoveryError(
            "Cannot determine Project Root from Booley Directory; "
            "run from the Project Root or set RTL_PROJECT_ROOT to it."
        )
    if checkouts:
        return checkouts[0]
    return None if required else current


@dataclass(frozen=True)
class SessionContext:
    """Owner authority, selected design checkout, and optional existing-mount cwd."""

    project_root: Path
    checkout: Path
    booley_dir: Path
    cwd: str | None


def _session_error() -> ProjectRootDiscoveryError:
    return ProjectRootDiscoveryError("Cannot determine owning Project; run from the Project Root.")


def _registered_checkouts(root: Path) -> list[Path]:
    from booley.runtime.project_repositories import run_git

    result = run_git(root, "worktree", "list", "--porcelain", "-z")
    if result.returncode:
        return []
    return [
        Path(item[9:]).resolve()
        for item in result.stdout.split("\0")
        if item.startswith("worktree ")
    ]


def _owner_candidates(checkout: Path, common_dir: Path, registered: list[Path]) -> list[Path]:
    from booley.runtime.project_repositories import run_git

    configured = run_git(checkout, "config", "--path", "--get", "core.worktree")
    if configured.returncode == 0 and configured.stdout.strip():
        candidate = Path(configured.stdout.strip())
        registered.append(
            (common_dir / candidate).resolve()
            if not candidate.is_absolute()
            else candidate.resolve()
        )
    return registered


def _linked_git_owner(checkout: Path) -> Path | None:
    from booley.runtime.project_repositories import GitDirectoryInspectionError, git_directories

    try:
        directories = git_directories(checkout)
    except GitDirectoryInspectionError:
        return None
    if directories.git_dir == directories.common_dir:
        return checkout
    registered = _registered_checkouts(checkout)
    if checkout not in registered:
        raise _session_error()
    # Git's registration includes the back-pointer; a copied .git file is not ownership.
    try:
        back_pointer = (directories.git_dir / "gitdir").read_text().strip()
        if Path(back_pointer).resolve() != checkout / ".git":
            raise _session_error()
    except (OSError, UnicodeError) as exc:
        raise _session_error() from exc
    registered = _owner_candidates(checkout, directories.common_dir, registered)
    for candidate in registered:
        try:
            if (
                git_directories(candidate).git_dir == directories.common_dir
                and resolve_authoritative_booley_dir(candidate).is_dir()
            ):
                return candidate
        except GitDirectoryInspectionError:
            continue
    from booley.runtime.project_repositories import run_git

    # Query the shared administration: linked checkouts themselves are never bare.
    bare = run_git(directories.common_dir, "rev-parse", "--is-bare-repository")
    if bare.returncode or bare.stdout.strip() != "true":
        raise _session_error()
    owners = [
        candidate
        for candidate in registered
        if candidate != checkout and resolve_authoritative_booley_dir(candidate).is_dir()
    ]
    if len(owners) == 1:
        return owners[0]
    raise _session_error()


def _sandbox_git_pointer(checkout: Path, owner: Path) -> bool:
    from booley.runtime.project_repositories import GitDirectoryInspectionError, git_directories

    try:
        common = git_directories(owner).common_dir
        entries = list(islice((common / "worktrees").iterdir(), 4097))
        if len(entries) > 4096:
            return False
        pointer = (checkout / ".git").read_text().strip().removeprefix("gitdir:").strip()
        for admin in entries:
            if Path((admin / "gitdir").read_text().strip()).resolve() != checkout / ".git":
                continue
            for source, target in (
                (owner, WORK_DIR),
                (resolve_authoritative_booley_dir(owner).resolve(), BOOLEY_DIR_TARGET),
            ):
                if admin.is_relative_to(source) and pointer == str(
                    Path(target) / admin.relative_to(source)
                ):
                    return True
    except (OSError, UnicodeError, GitDirectoryInspectionError):
        return False
    return False


def _creator_owner(checkout: Path) -> Path | None:
    for candidate in checkout.parents:
        namespace = candidate / BOOLEY_DIR_NAME / "worktrees"
        if checkout.parent != namespace.resolve() or not has_git_worktree_marker(candidate):
            continue
        require_project_checkout(candidate)
        if not resolve_authoritative_booley_dir(candidate).is_dir():
            continue
        if checkout not in _registered_checkouts(candidate):
            raise _session_error()
        return candidate
    return None


def _session_checkout(selected: Path) -> Path:
    current, checkouts = _checkout_candidates(selected)
    if checkouts:
        return checkouts[0]
    # Initialized non-Git Projects retain ordinary directory discovery.
    for parent in (current, *current.parents):
        if parent != Path.home() and resolve_authoritative_booley_dir(parent).is_dir():
            return parent
    raise _session_error()


def discover_session_context(
    selected: Path, *, invocation_cwd: Path | None = None
) -> SessionContext:
    """Resolve host session ownership without changing general design discovery."""
    try:
        checkout = _session_checkout(selected)
        require_project_checkout(checkout)
        linked = (checkout / ".git").is_file()
        creator = _creator_owner(checkout) if linked else None
        git_owner = _linked_git_owner(checkout) if linked else None
        if creator is not None and git_owner is not None and creator != git_owner:
            raise _session_error()
        if (
            creator is not None
            and git_owner is None
            and not _sandbox_git_pointer(checkout, creator)
        ):
            raise _session_error()
        owner = creator or git_owner or checkout
        require_project_checkout(owner)
        if linked and creator is None and git_owner is None:
            raise _session_error()
        data = resolve_authoritative_booley_dir(owner)
        if not data.is_dir():
            raise _session_error()
        cwd = None
        if owner != checkout:
            location = checkout
            if invocation_cwd is not None and invocation_cwd.resolve().is_relative_to(checkout):
                location = invocation_cwd.resolve()
            for source, target in ((owner, WORK_DIR), (data.resolve(), BOOLEY_DIR_TARGET)):
                if location.is_relative_to(source):
                    cwd = str(Path(target) / location.relative_to(source))
                    break
        return SessionContext(owner, checkout, data, cwd)
    except UnicodeError as exc:
        raise _session_error() from exc


def linked_auth_error(selected: Path) -> str | None:
    """Refuse host credential mutation only for a proven linked Project checkout."""
    if os.environ.get("BOOLEY_CONTAINER") == "1":
        return None
    _current, checkouts = _checkout_candidates(selected)
    if not checkouts or not (checkouts[0] / ".git").is_file():
        return None
    context = discover_session_context(selected)
    if context.project_root != context.checkout:
        return "Auth mutation from a linked worktree is unsupported; run from the Project Root."
    return None
