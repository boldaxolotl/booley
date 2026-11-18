"""Diagnostic owner for the Git-hygiene rules of authored ``.core`` inputs.

Takes a typed request and returns a :class:`DiagnosticReport`; the caller (Doctor)
only renders it. The owner knows nothing of Doctor, CLI orchestration or
rendering. It decides three things a fresh clone or CI checkout would get wrong:
a ``.core``-referenced file that exists but is untracked, a ``$readmemh`` or
``$readmemb`` literal target that is untracked, and a committed build artifact
that sits next to its in-repo source.

Permitted effects: reads ``.core`` files and referenced HDL; runs bounded
read-only ``git ls-files`` queries (15 seconds each) in the Project Root and in a
Versioned Booley Directory; writes nothing.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from booley.audit.diagnostic_results import DiagnosticReport, Findings
from booley.core.booley_dir import in_checkout_booley_dir
from booley.fusesoc import fusesoc_registry
from booley.runtime import platform_paths

__all__ = ["CoreRepositoryHygieneRequest", "inspect_core_repository_hygiene"]

# HDL source suffixes the readmem scan reads. Design sizing owns its own source
# classification in :mod:`booley.audit.design_size`.
_HDL_SUFFIXES = frozenset({".v", ".sv", ".vh", ".svh"})

# A committed compiled artifact (firmware image, memory init) whose *source*
# lives in the same directory is a "ship the toolchain, not the artifact"
# violation (booley-setup SKILL.md): it should be rebuilt on demand inside the
# sandbox, not frozen into git. Suffixes that read as build outputs, and the
# sibling-source signals that mark them as buildable-in-repo.
_BUILD_ARTIFACT_SUFFIXES = frozenset({".hex", ".mem", ".bin", ".elf"})
_BUILD_SOURCE_SUFFIXES = frozenset({".c", ".s", ".cc", ".cpp", ".cxx", ".asm", ".rs"})
_BUILD_SOURCE_NAMES = frozenset({"makefile", "cmakelists.txt"})

# The $readmemh/$readmemb memory-load call pattern (HDL suffixes: _HDL_SUFFIXES).
_READMEM_RE = re.compile(r"\$readmem[hb]\s*\(\s*\"([^\"]+)\"")


@dataclass(frozen=True, slots=True)
class CoreRepositoryHygieneRequest:
    """Inputs of the Git-hygiene audit of one Project's authored ``.core`` inputs."""

    project_root: Path  # the Project Root whose .core-referenced files are judged


def inspect_core_repository_hygiene(request: CoreRepositoryHygieneRequest) -> DiagnosticReport:
    """Check that authored core inputs are represented safely in Git."""
    findings = Findings()
    _check_core_files_tracked(request.project_root, findings)
    _check_readmemh_targets_tracked(request.project_root, findings)
    _check_committed_build_artifacts(request.project_root, findings)
    return findings.report()


def _looks_built_from_sibling_source(path: Path) -> bool:
    """True when ``path``'s directory also holds compiler/assembler source.

    Case-folded so ``start.S`` (assembly) and a ``Makefile`` both count. A
    firmware ``.hex`` next to ``hello.c`` / ``start.S`` / a ``Makefile`` is a
    build output of in-repo source, not an opaque vendored blob.
    """
    parent = path.parent
    if not parent.is_dir():
        return False
    for entry in parent.iterdir():
        if not entry.is_file():
            continue
        name = entry.name.lower()
        if name in _BUILD_SOURCE_NAMES or entry.suffix.lower() in _BUILD_SOURCE_SUFFIXES:
            return True
    return False


def _check_committed_build_artifacts(root: Path, findings: Findings) -> None:
    """Warn when a compiled artifact built from in-repo source is committed.

    A vendored ``firmware.hex`` sitting next to its ``*.c`` / ``*.S`` / Makefile
    is a frozen build output: it hides the real dependency (the cross-compiler)
    and rots on the first source change. The supported fix is to bake the
    toolchain into the project sandbox image and rebuild on demand from a
    post-setup hook (booley-setup SKILL.md: "ship the toolchain, not the
    artifact"). Warn, not fail — the committed blob still simulates locally.
    An opaque vendored blob with no sibling source is left alone (may be the
    only option); the untracked-data trap is handled by
    :func:`_check_core_files_tracked`. A file the ``.core`` explicitly tags
    ``tags: [vendored]`` is exempt too — that is the maintainer asserting the
    blob is upstream-shipped with no rebuildable in-repo source, which the
    sibling-source heuristic cannot otherwise tell from a frozen local build.
    (``tags`` is the CAPI2-valid marker; a bare ``vendored: true`` key breaks
    real fusesoc and is rejected by the shallow ``.core`` check — see QA-3.)
    """
    referenced = fusesoc_registry.all_referenced_files(root)
    vendored = fusesoc_registry.vendored_files(root)
    suspects = [
        rel
        for rel in referenced
        if Path(rel).suffix.lower() in _BUILD_ARTIFACT_SUFFIXES
        and rel not in vendored
        and (root / rel).is_file()
        and _looks_built_from_sibling_source(root / rel)
    ]
    if not suspects:
        return  # nothing that looks built-from-source; stay quiet (no pass noise)
    try:
        result = platform_paths.run_captured_bounded(
            ["git", "ls-files", "--", *suspects], timeout=15, cwd=root, errors="strict"
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return  # not a git repo / git missing — tracking is unknowable, stay quiet
    if result.returncode != 0:
        return
    tracked = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    committed = [rel for rel in suspects if rel.replace("\\", "/") in tracked]
    if committed:
        shown = ", ".join(committed[:5]) + (", …" if len(committed) > 5 else "")
        findings.warn(
            f"{len(committed)} committed artifact(s) look built from in-repo "
            f"source ({shown}) — ship the toolchain in the sandbox image and "
            "rebuild on demand (project Dockerfile + post-setup hook) instead of "
            "committing a frozen blob (booley-setup SKILL.md)",
            check_id="project.committed-build-artifact",
            subject=",".join(sorted(committed)),
        )


def _check_core_files_tracked(root: Path, findings: Findings) -> None:
    """Warn when a ``.core`` references a file that exists on disk but is untracked.

    The silent vendored-data trap: a data file (firmware ``.hex``, memory-init
    image) matching the upstream ``.gitignore`` gets ``git add``-ed to no effect,
    so it stays untracked. On disk it is present (doctor sees it), but a fresh
    clone / CI checkout would lack it and the testbench would fail opaquely. A
    warn with the ``git add -f`` remedy, not a fail: the local run still works.

    Files in a Versioned Booley Directory (ADR 0036 stealth cores) are judged
    against its own Git repository — ``.booley/`` is invisible to the host repo by
    design, so asking the host is guaranteed noise. No repo there → skip them
    (nothing knowable). Tracking is symlink-aware in both zones: a path whose
    ancestor is a tracked symlink (a stealth core's reach-through like
    ``cores/dhrystone -> ../../dhrystone``) arrives with a fresh clone, so it
    counts as tracked. It is submodule-aware for the same reason — see
    ``_tracked_set``.
    """
    referenced = fusesoc_registry.all_referenced_files(root)
    on_disk = [rel for rel in referenced if (root / rel).is_file()]
    if not on_disk:
        return
    state_dir = in_checkout_booley_dir(root)
    state_prefix = state_dir.relative_to(root).as_posix()  # ".booley" or the relocated dir
    host_files = [
        rel for rel in on_disk if not rel.replace("\\", "/").startswith(state_prefix + "/")
    ]
    state_files = [rel for rel in on_disk if rel.replace("\\", "/").startswith(state_prefix + "/")]

    def _tracked_set(cwd: Path) -> set[str] | None:
        """Tracked paths under *cwd*, submodule contents included.

        Plain ``git ls-files`` stops at a submodule boundary and lists only the
        gitlink, so every file *inside* a vendored submodule read as untracked —
        a false alarm, since the submodule's own repo tracks them and
        `git submodule update` restores them on a fresh clone.
        ``--recurse-submodules`` descends and reports each file at its
        superproject-relative path, which is the vocabulary the caller compares
        against. A git too old for the flag retries plain rather than go silent.
        """
        for cmd in (["git", "ls-files", "--recurse-submodules"], ["git", "ls-files"]):
            try:
                result = platform_paths.run_captured_bounded(
                    cmd, timeout=15, cwd=cwd, errors="strict"
                )
            except (FileNotFoundError, subprocess.SubprocessError):
                return None  # git missing — tracking is unknowable, stay quiet
            if result.returncode == 0:
                return {line.strip() for line in result.stdout.splitlines() if line.strip()}
        return None  # not a git repo

    def _is_tracked(rel: str, tracked: set[str]) -> bool:
        # git ls-files reports repo-relative forward-slash paths. An ancestor
        # entry in the tracked set can only be a tracked symlink (git never
        # lists directories) — reach-through content still ships with a clone.
        norm = rel.replace("\\", "/")
        if norm in tracked:
            return True
        parts = norm.split("/")
        return any("/".join(parts[:i]) in tracked for i in range(1, len(parts)))

    untracked: list[str] = []
    host_tracked = _tracked_set(root)
    if host_tracked is not None:
        untracked += [rel for rel in host_files if not _is_tracked(rel, host_tracked)]
    if state_files and (state_dir / ".git").exists():
        state_tracked = _tracked_set(state_dir)
        if state_tracked is not None:
            untracked += [
                rel
                for rel in state_files
                if not _is_tracked(
                    PurePosixPath(rel.replace("\\", "/")).relative_to(state_prefix).as_posix(),
                    state_tracked,
                )
            ]
    if untracked:
        shown = ", ".join(untracked[:5]) + (", …" if len(untracked) > 5 else "")
        findings.warn(
            f"{len(untracked)} .core-referenced file(s) exist on disk but are "
            f"untracked by git ({shown}) — if gitignored (vendored firmware / "
            "build artifact), force-add with `git add -f <file>` and verify with "
            "`git ls-files <file>`; a fresh clone or CI checkout will lack them",
            check_id="project.core-file-untracked",
            subject=",".join(sorted(untracked)),
        )
    else:
        findings.pass_(f"all {len(on_disk)} .core-referenced on-disk file(s) tracked by git")


def _readmem_literal_targets(root: Path) -> list[str]:
    """Project-relative memory images named by a *literal* $readmemh/$readmemb.

    Scans the ``.core``-referenced HDL for ``$readmem[hb]("path")`` calls and
    resolves each literal against the Project Root and the calling file's own
    directory. Only plain literals are considered; a path built from a plusarg,
    a format specifier, or concatenation can't be resolved statically and is
    skipped. Used to catch memory images the testbench hardcodes but that no
    ``.core`` fileset lists — the gap :func:`_check_core_files_tracked` misses.
    """
    targets: set[str] = set()
    root_res = root.resolve()
    for rel in fusesoc_registry.all_referenced_files(root):
        if Path(rel).suffix.lower() not in _HDL_SUFFIXES:
            continue
        src = root / rel
        if not src.is_file():
            continue
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _READMEM_RE.finditer(text):
            literal = match.group(1)
            if any(ch in literal for ch in "%{}+*?"):
                continue  # constructed / format / plusarg / glob — not a static path
            for base in (root, src.parent):
                cand = base / literal
                if cand.is_file():
                    with contextlib.suppress(ValueError):
                        targets.add(cand.resolve().relative_to(root_res).as_posix())
                    break
    return sorted(targets)


def _check_readmemh_targets_tracked(root: Path, findings: Findings) -> None:
    """Warn when a hardcoded $readmemh memory image exists on disk but is untracked.

    A testbench that boots from ``$readmemh("firmware.vmem", mem)`` depends on a
    file no ``.core`` fileset lists, so :func:`_check_core_files_tracked` can't
    see it. If that image matches a blanket ``.gitignore`` (vendored firmware,
    build output), ``git add`` no-ops and a fresh clone / CI checkout lacks it —
    the sim then spins on uninitialized RAM (SETUP-12, and the static companion
    to the runtime readmemh-missing guard). Warn with the ``git add -f`` remedy.
    """
    targets = _readmem_literal_targets(root)
    if not targets:
        return
    try:
        result = platform_paths.run_captured_bounded(
            ["git", "ls-files", "--", *targets], timeout=15, cwd=root, errors="strict"
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return  # not a git repo / git missing — tracking is unknowable, stay quiet
    if result.returncode != 0:
        return
    tracked = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    untracked = [rel for rel in targets if rel.replace("\\", "/") not in tracked]
    if untracked:
        shown = ", ".join(untracked[:5]) + (", …" if len(untracked) > 5 else "")
        findings.warn(
            f"{len(untracked)} $readmemh/$readmemb memory image(s) exist on disk "
            f"but are untracked by git ({shown}) — if gitignored (vendored "
            "firmware / build output), force-add with `git add -f <file>`; a "
            "fresh clone or CI checkout will lack them and the sim boots "
            "uninitialized RAM",
            check_id="project.memory-image-untracked",
            subject=",".join(sorted(untracked)),
        )
    else:
        findings.pass_(f"all {len(targets)} $readmemh-referenced image(s) tracked by git")
