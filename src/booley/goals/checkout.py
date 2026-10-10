"""The Git operations Goal entry performs on one worktree (ADR 0067 D8, D15).

Every operation runs ``git`` in the Goal worktree with a bounded timeout and
raises :class:`CheckoutError` naming the command when Git refuses, so entry
never mistakes a failed Git call for an answer. Branch deletion is a
compare-and-swap (``git update-ref -d <ref> <expected>``): a Goal Branch that
moved since entry created it is never deleted.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.git_environment import inherited_git_environment

# Generous for checkout in a large repository, still bounded.
GIT_TIMEOUT_S = 120
_BRANCH_REF_PREFIX = "refs/heads/"


class CheckoutError(RuntimeError):
    """A Git command in the Goal worktree failed."""


@dataclass(frozen=True)
class TreeEntry:
    """One ``git ls-tree`` row: mode, object id, and POSIX path."""

    mode: str
    object_id: str
    path: str


@dataclass(frozen=True)
class GoalCheckout:
    """Git access to one worktree root."""

    root: Path

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.root,
                env=inherited_git_environment(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CheckoutError(f"git {' '.join(args)} failed in {self.root}: {exc}") from exc
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
            raise CheckoutError(f"git {' '.join(args)} failed in {self.root}: {detail}")
        return result

    def containing_repository(self) -> tuple[Path, str] | None:
        """The innermost repository containing ``root`` and ``root``'s path inside it.

        Returns the repository's top level and the POSIX prefix of ``root``
        below it (empty at the top level), or ``None`` outside any repository.
        A nested repository (a paired Project checkout) wins over the one
        around it, as it does for Git itself.
        """
        result = self._git("rev-parse", "--show-toplevel", "--show-prefix", check=False)
        if result.returncode != 0:
            return None
        lines = result.stdout.splitlines()
        if not lines or not lines[0]:
            return None
        prefix = lines[1].rstrip("/") if len(lines) > 1 else ""
        return Path(lines[0]), prefix

    def head_sha(self) -> str:
        """The full commit id HEAD points at."""
        return self._git("rev-parse", "--verify", "HEAD^{commit}").stdout.strip()

    def head_ref(self) -> str | None:
        """The full branch ref HEAD is on (``refs/heads/...``), or ``None`` when detached."""
        result = self._git("symbolic-ref", "-q", "HEAD", check=False)
        if result.returncode == 0:
            return result.stdout.strip()
        if result.returncode == 1:
            return None  # detached HEAD
        detail = (result.stderr or result.stdout).strip()
        raise CheckoutError(f"git symbolic-ref HEAD failed in {self.root}: {detail}")

    def dirty_paths(self) -> list[str]:
        """Changed, staged, or untracked paths (ignored files excluded), as Git prints them."""
        output = self._git(
            "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"
        ).stdout
        return [entry[3:] for entry in output.split("\0") if len(entry) > 3]

    def _git_bytes(self, *args: str) -> bytes:
        """Run git and return raw stdout: path-bearing output is read as bytes."""
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.root,
                env=inherited_git_environment(),
                capture_output=True,
                timeout=GIT_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CheckoutError(f"git {' '.join(args)} failed in {self.root}: {exc}") from exc
        if result.returncode != 0:
            detail = (
                result.stderr.decode("utf-8", "replace").strip() or f"exit {result.returncode}"
            )
            raise CheckoutError(f"git {' '.join(args)} failed in {self.root}: {detail}")
        return result.stdout

    def head_tree(self, relative: str) -> list[TreeEntry]:
        """HEAD's entries at or under *relative* (a POSIX path inside the worktree)."""
        output = self._git_bytes("ls-tree", "-r", "-z", "HEAD", "--", relative)
        entries: list[TreeEntry] = []
        for record in output.split(b"\0"):
            if not record:
                continue
            meta, _, name = record.partition(b"\t")
            mode, _kind, object_id = meta.decode("ascii").split(" ")
            entries.append(TreeEntry(mode, object_id, name.decode("utf-8", "surrogateescape")))
        return entries

    def blob(self, object_id: str) -> bytes:
        """The content of one Git object."""
        return self._git_bytes("cat-file", "blob", object_id)

    def branch_tip(self, branch: str) -> str | None:
        """The commit *branch* points at, or ``None`` when it does not exist."""
        ref = branch_ref(branch)
        result = self._git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def require_valid_branch_name(self, branch: str) -> None:
        """Raise :class:`CheckoutError` unless *branch* is a valid branch name."""
        self._git("check-ref-format", "--branch", branch)

    def create_branch(self, branch: str, start: str) -> None:
        """Create *branch* at *start*; Git refuses an existing name."""
        self._git("branch", "--no-track", branch, start)

    def checkout_branch(self, branch: str) -> None:
        """Check out an existing local branch, never overwriting an ignored file.

        ``--no-overwrite-ignore`` makes Git refuse instead: a branch that
        moved between a caller's tip check and this checkout cannot destroy
        ignored local work.
        """
        self._git("checkout", "--quiet", "--no-overwrite-ignore", branch, "--")

    def checkout_ref(self, ref: str) -> None:
        """Check out a full branch ref by name, or a commit detached."""
        branch = branch_name(ref)
        if branch is not None:
            self.checkout_branch(branch)
        else:
            self._git("checkout", "--quiet", "--no-overwrite-ignore", "--detach", ref, "--")

    def delete_branch_if_at(self, branch: str, expected_tip: str) -> bool:
        """Delete *branch* only while it still points at *expected_tip*; return whether it did."""
        result = self._git("update-ref", "-d", branch_ref(branch), expected_tip, check=False)
        return result.returncode == 0


def branch_ref(branch: str) -> str:
    """The full ref of a local branch."""
    return f"{_BRANCH_REF_PREFIX}{branch}"


def branch_name(ref: str) -> str | None:
    """The branch a full ``refs/heads/...`` ref names, or ``None`` for a commit id."""
    return ref.removeprefix(_BRANCH_REF_PREFIX) if ref.startswith(_BRANCH_REF_PREFIX) else None
