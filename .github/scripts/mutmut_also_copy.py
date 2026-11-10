#!/usr/bin/env python3
"""Write mutmut's ``also_copy`` list for the scheduled mutation campaign.

mutmut runs the selected tests inside ``mutants/``, which it seeds by default
with its ``source_paths``, ``tests/``, ``setup.cfg`` and ``pyproject.toml`` (and a
few other defaults that only duplicate entries here). The
harness tests also read the rest of the checkout (``VERSION``, docs, workflows,
Sandbox Image build inputs), so ``mutants/`` must be a faithful checkout. A
list kept by hand in ``setup.cfg`` broke the campaign whenever the tracked
layout changed (issue #3114); this script derives it from ``git ls-files``.

mutmut facts the list depends on (mutmut 3.8.1, ``copy_also_copy_files``):
entries are copied in order; a directory entry is copied with ``copytree``
(creating parents), a file entry with ``copy2`` (which does not); missing
paths are skipped silently. So a file entry needs an earlier directory entry
that creates its parent, and untracked content (Cargo ``target/`` build output
inside a crate) is excluded by splitting its folder into tracked children.

Run it at the checkout root before ``mutmut run``. It rewrites the working copy
of ``setup.cfg`` (comments are lost there, never in the committed file).
Locally, run it in a fresh ``git worktree`` and restore ``setup.cfg`` afterwards:
a developer checkout's ``__pycache__`` inside a folder that holds only files
leaves those files without a parent directory entry, which is an error here.
Stdlib only, so running it leaves no ``__pycache__`` in this folder.
"""

from __future__ import annotations

import configparser
import subprocess
import sys
from pathlib import Path

SETUP_CFG = Path("setup.cfg")
# mutmut always copies these beside its configured source paths.
MUTMUT_DEFAULT_COPIES = ("tests", "setup.cfg", "pyproject.toml")
GIT_TIMEOUT_SECONDS = 60


class AlsoCopyError(Exception):
    """The tracked layout cannot be expressed as a valid ``also_copy`` list."""


def _is_covered(path: str, entry: str) -> bool:
    """Return whether mutmut's copy of ``entry`` (a file or ``dir/``) includes ``path``."""
    if entry.endswith("/"):
        return path.startswith(entry)
    return path == entry


def also_copy_entries(tracked: list[str], untracked: list[str], skipped: list[str]) -> list[str]:
    """Return directory entries, then file entries, covering ``tracked`` minus ``skipped``.

    All paths are POSIX strings as ``git ls-files`` prints them; ``untracked``
    folders end in ``/``. A tracked folder holding no untracked path is one
    ``dir/`` entry; otherwise it is split into its tracked children.
    """
    skip = {entry.rstrip("/") for entry in skipped}
    tracked_files = set(tracked)
    directories: list[str] = []
    files: list[str] = []
    pending = [""]
    while pending:
        prefix = pending.pop()
        children = {
            path[len(prefix) :].partition("/")[0] for path in tracked if path.startswith(prefix)
        }
        for name in sorted(children):
            path = prefix + name
            if path in skip:
                continue
            if path not in tracked_files:  # a folder: it has tracked children below it
                folder = path + "/"
                if any(other.startswith(folder) for other in untracked):
                    pending.append(folder)
                else:
                    directories.append(folder)
            else:
                files.append(path)
    entries = sorted(directories) + sorted(files)
    _require_parent_directories(entries, untracked)
    return entries


def _require_parent_directories(entries: list[str], untracked: list[str]) -> None:
    """Raise when a file entry's parent folder is created by no directory entry."""
    directories = [entry for entry in entries if entry.endswith("/")]
    for entry in entries:
        parent = entry.rpartition("/")[0]
        if entry.endswith("/") or not parent:
            continue
        if not any(directory.startswith(parent + "/") for directory in directories):
            blocking = [path for path in untracked if path.startswith(parent + "/")]
            raise AlsoCopyError(
                f"cannot copy {entry}: mutmut copies files without creating "
                f"{parent}/, and untracked paths {blocking} keep the folder from "
                "being copied whole. Run this in a clean checkout (a fresh git worktree)."
            )


def write_also_copy(setup_cfg: Path, entries: list[str]) -> None:
    """Set ``[mutmut] also_copy`` in ``setup_cfg``, one entry per continuation line."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(setup_cfg, encoding="utf-8")
    parser.set("mutmut", "also_copy", "\n" + "\n".join(entries))
    with setup_cfg.open("w", encoding="utf-8", newline="\n") as handle:
        parser.write(handle)


def missing_after_write(setup_cfg: Path, tracked: list[str], skipped: list[str]) -> list[str]:
    """Return tracked paths mutmut would not copy, reading ``setup_cfg`` as mutmut does.

    mutmut reads ``setup.cfg`` with a plain ``ConfigParser`` and splits each
    value on newlines, dropping empty lines. A tracked name that starts with
    ``#`` or ``;`` or with a space does not survive that round trip.
    """
    parser = configparser.ConfigParser()
    parser.read(setup_cfg, encoding="utf-8")
    read_back = [line for line in parser.get("mutmut", "also_copy").split("\n") if line]
    copied = [*read_back]
    for entry in skipped:
        copied += [entry.rstrip("/"), entry.rstrip("/") + "/"]
    return [path for path in tracked if not any(_is_covered(path, entry) for entry in copied)]


def _git_paths(*arguments: str) -> list[str]:
    """Return the NUL-separated paths printed by ``git ls-files <arguments>``."""
    result = subprocess.run(
        ["git", "ls-files", "-z", *arguments],
        capture_output=True,
        check=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    return [path for path in result.stdout.decode("utf-8").split("\0") if path]


def main() -> int:
    """Generate the list for the checkout in the current directory."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(SETUP_CFG, encoding="utf-8")
    source_paths = [line for line in parser.get("mutmut", "source_paths").split("\n") if line]
    skipped = [*source_paths, *MUTMUT_DEFAULT_COPIES]
    tracked = _git_paths()
    try:
        # No --exclude-standard: ignored build output such as target/ counts as untracked.
        entries = also_copy_entries(tracked, _git_paths("--others", "--directory"), skipped)
        write_also_copy(SETUP_CFG, entries)
        missing = missing_after_write(SETUP_CFG, tracked, skipped)
        if missing:
            raise AlsoCopyError(f"generated also_copy misses: {missing}")
    except AlsoCopyError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"also_copy: {len(entries)} entries")
    for entry in entries:
        print(f"  {entry}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
