"""Local mirror of CI's "Enforce 90% changed-line coverage" step.

The readiness ``pytest.broad`` command writes Cobertura XML outside the
checkout. After a green run, ``tests/conftest.py`` writes a stamp next to it
naming the source that run measured (:func:`begin_stamp`, :func:`finish_stamp`).
The ``coverage.changed-lines`` handoff command (:func:`main`) refuses a missing
or stale stamp, then runs diff-cover the way the CI coverage job does: the same
threshold and the same terminology-only exclusions, against the newest merge
base with ``main``.

This module depends only on the standard library and on diff-cover being
installed beside the running interpreter (the shared tools environment).
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

# CI's threshold in .github/workflows/test.yml ("Enforce 90% changed-line coverage").
FAIL_UNDER = 90
# Base refs a branch may have been cut from; the newest merge base wins.
_BASE_REFS = ("refs/remotes/origin/main", "refs/heads/main")
_GIT_TIMEOUT_S = 60
# diff-cover parses the whole XML and runs `git diff`; seconds in practice.
_DIFF_COVER_TIMEOUT_S = 600
_EXIT_UNUSABLE = 2


class CoverageGateError(RuntimeError):
    """The gate cannot be evaluated: Git state or the coverage run is unusable."""


def stamp_path(xml: Path) -> Path:
    """Return the stamp file that vouches for ``xml``."""
    return xml.with_name(xml.name + ".source")


def source_fingerprint(root: Path) -> str:
    """Identify the measured source: HEAD's tree plus any uncommitted tracked change.

    diff-cover counts staged and unstaged edits, so they belong to the
    identity; untracked files are outside both diff-cover's diff and this one.
    """
    tree = _git(root, "rev-parse", "HEAD^{tree}").decode().strip()
    changes = hashlib.sha256(_git(root, "diff", "--binary", "HEAD")).hexdigest()
    return f"{tree}+{changes[:16]}"


def begin_stamp(root: Path, xml: Path) -> str:
    """Invalidate the previous run's stamp and return this run's fingerprint."""
    stamp_path(xml).unlink(missing_ok=True)
    return source_fingerprint(root)


def finish_stamp(root: Path, xml: Path, fingerprint: str) -> bool:
    """Stamp ``xml`` with ``fingerprint`` when the source did not change mid-run."""
    if not xml.is_file() or source_fingerprint(root) != fingerprint:
        return False
    stamp = stamp_path(xml)
    partial = stamp.with_name(stamp.name + ".tmp")
    partial.write_text(fingerprint + "\n", encoding="utf-8")
    partial.replace(stamp)
    return True


def stale_reason(root: Path, xml: Path) -> str | None:
    """Return why ``xml`` cannot vouch for the current source, or None."""
    try:
        stamped = stamp_path(xml).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return (
            f"no stamped coverage report at {xml} (the last pytest.broad run failed or never ran)"
        )
    if not xml.is_file():
        return f"coverage report {xml} is missing"
    if stamped != source_fingerprint(root):
        return "the coverage report measured a different source tree"
    return None


def merge_base(root: Path) -> str:
    """Return the newest merge base of HEAD with ``origin/main`` or ``main``.

    A stale ``origin/main`` must not pull main's own newer commits into the
    diff, and a stale local ``main`` must not either, so the descendant wins.
    """
    bases = [
        _git(root, "merge-base", "HEAD", ref, check=False).decode().strip() for ref in _BASE_REFS
    ]
    bases = [base for base in bases if base]
    if not bases:
        raise CoverageGateError("HEAD shares no history with origin/main or main")
    newest = bases[0]
    for base in bases[1:]:
        if _is_ancestor(root, newest, base):
            newest = base
    return newest


def terminology_exclusions(root: Path, base: str, python: str) -> list[str]:
    """Run CI's terminology-only exclusion script exactly as the coverage job does."""
    result = subprocess.run(
        (python, ".github/scripts/terminology_diff_exclusions.py", "--base", base),
        cwd=root,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )
    if result.returncode != 0:
        raise CoverageGateError(f"terminology exclusion script failed: {result.stderr.strip()}")
    return result.stdout.splitlines()


def diff_cover_argv(python: str, xml: Path, base: str, exclusions: Sequence[str]) -> list[str]:
    """Return CI's diff-cover invocation, minus its HTML/Markdown artifacts."""
    argv = [
        python,
        "-m",
        "diff_cover.diff_cover_tool",
        str(xml),
        f"--compare-branch={base}",
        f"--fail-under={FAIL_UNDER}",
    ]
    if exclusions:
        argv += ["--exclude", *exclusions]
    return argv


def check(root: Path, xml: Path, python: str) -> int:
    """Run the gate for the checkout at ``root``; return a process exit code."""
    reason = stale_reason(root, xml)
    if reason is not None:
        print(
            f"changed-line coverage: {reason}. Run the readiness pytest.broad command "
            "on this tree first and let it pass.",
            file=sys.stderr,
        )
        return _EXIT_UNUSABLE
    base = merge_base(root)
    exclusions = terminology_exclusions(root, base, python)
    print(
        f"Changed-line coverage against {base[:12]}; terminology-only files: {len(exclusions)}",
        flush=True,
    )
    argv = diff_cover_argv(python, xml, base, exclusions)
    return subprocess.run(argv, cwd=root, timeout=_DIFF_COVER_TIMEOUT_S, check=False).returncode


def main(argv: Sequence[str] | None = None) -> int:
    """Gate the checkout containing the current directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage-xml", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        root = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
        return check(root, args.coverage_xml, sys.executable)
    except CoverageGateError as error:
        print(f"changed-line coverage: {error}", file=sys.stderr)
        return _EXIT_UNUSABLE


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ("git", "merge-base", "--is-ancestor", ancestor, descendant),
        cwd=root,
        capture_output=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )
    return result.returncode == 0


def _git(root: Path, *args: str, check: bool = True) -> bytes:
    result = subprocess.run(
        ("git", *args),
        cwd=root,
        capture_output=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise CoverageGateError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout if result.returncode == 0 else b""
