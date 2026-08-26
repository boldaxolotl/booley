"""Git commit-msg hook generation and installation for ``booley init``.

Extracted from ``init_cmd.py`` (Single Responsibility): the ``git_hooks`` step
installs the leak guard into the project-agnostic ``.booley/`` repo, and the
``project_git_hooks`` step vendors the commit-message sanitizer scripts into
``.booley_project/hooks/`` and installs a repo-relative ``commit-msg``
delegator into the project's own ``.git/hooks/``. Steps are named by their
record key, never by a display number — the banner numbers are allocated at
print time from the steps that actually run (see :meth:`InitContext.step_banner`),
so a hardcoded display number in a comment drifts the moment a step is
added or skipped (fpu F-12).

Depends only on ``init_common`` for console output and :class:`InitContext`;
it never imports back from ``init_cmd``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO

from booley.harness.init_common import (
    InitContext,
    WriteOutcome,
    guarded_write,
    info,
    ok,
    skip,
    warn,
)
from booley.runtime.paths import dev_support_dir
from booley.runtime.project_dir import resolve_project_dir


def _step_git_hooks(ctx: InitContext) -> None:
    ctx.step_banner("git hooks")

    project_dir = resolve_project_dir(ctx.project_root)
    guard_sh = project_dir / "hooks" / "booley_commit_guard.sh"
    if not guard_sh.is_file():
        # Only Booley's own dev repo ships this repo-level guard; a normal
        # project repo has none, so there is nothing to install — expected, not
        # a failure (SETUP-4). The project commit-msg hook is a separate step.
        skip(
            "no repo-level commit guard (booley_commit_guard.sh) to install — "
            "expected for a project repo; the project commit-msg hook installs "
            "separately below"
        )
        ctx.record("git_hooks", "skip", "no repo-level guard (expected)")
        return

    # Find the .booley/.git/hooks/ target
    # For pip-installed booley, the git repo is wherever the package is installed from
    # Try common locations
    booley_git_hooks: Path | None = None
    for candidate in [
        ctx.project_root / ".booley" / ".git" / "hooks",
    ]:
        if candidate.parent.is_dir():
            booley_git_hooks = candidate
            break

    if booley_git_hooks is None:
        skip("no .booley/.git/ found — git hooks skipped")
        ctx.record("git_hooks", "skip", "no .booley git dir")
        return

    hook_target = booley_git_hooks / "commit-msg"

    guard_body = guard_sh.read_text(encoding="utf-8")
    # The content sniff that marks an installed hook as ours. A guard script
    # that never names itself degrades to create-only: installed once, never
    # refreshed, and an existing hook is left alone.
    marker = "booley_commit_guard" if "booley_commit_guard" in guard_body else None

    outcome = guarded_write(
        hook_target,
        guard_body,
        owner_marker=marker,
        dry_run=ctx.check_only,
        newline="\n",
        executable=True,
    )
    if outcome in (WriteOutcome.UNCHANGED, WriteOutcome.SKIPPED):
        skip("commit-msg hook already installed")
        ctx.record("git_hooks", "skip", "already installed")
        return
    if outcome is WriteOutcome.REFUSED:
        warn("existing .booley/.git/hooks/commit-msg is not booley's — leaving it untouched")
        ctx.record("git_hooks", "warn", "foreign hook preserved")
        return
    if ctx.check_only:
        warn("would install commit-msg hook")
        ctx.record("git_hooks", "warn", "would install")
        return

    ok("commit-msg hook installed in .booley/.git/hooks/")
    ctx.record("git_hooks", "ok", "installed")


# Utility scripts that make up the self-contained project git hooks.
# commit_msg_hook.py and pre_push_hook.py import the utils by bare name,
# resolving them via their own directory — so all must sit side-by-side in
# the project repo.
# (run_command.py is vendored alongside too — see _step_project_git_hooks — but it
# lives in core/, not dev_support/, so it isn't listed here.)
_PROJECT_HOOK_SCRIPTS = (
    "commit_msg_hook.py",
    "commit_msg_utils.py",
    "validate_commit_msg.py",
    "pre_push_hook.py",
)


def _build_hook_delegator_body(
    project_root: Path,
    hooks_dst: Path,
    script_name: str,
    purpose: str,
    *,
    fail_open: bool,
) -> str:
    """Build a delegator shell script for a vendored Python hook.

    References the vendored script repo-relatively so the hook works on any
    clone regardless of where the project lives. Falls back to an absolute
    path if the project data dir is somehow outside the repo.

    The script may legitimately be absent from ``$ROOT``: a user-made secondary
    worktree (``git worktree add``) has its own toplevel, and
    ``.booley_project/`` is untracked/git-ignored, so it exists only in the main
    worktree. ``exec``ing the missing path made EVERY commit in such a worktree
    fail with ENOENT (fpu F-42). The body therefore always resolves through the
    shared git dir first, so the hook genuinely RUNS in a secondary worktree.

    Only the last-resort fallback differs per hook, and *fail_open* picks it:

    - ``True`` (commit-msg): skip with one explanatory line. The sanitizer is a
      convenience; a missing script must never wedge local committing (F-42).
    - ``False`` (pre-push): refuse the push. That hook exists for one reason —
      to block — so treating "I could not check" as "nothing to report" is the
      one answer it must never give. ``.booley_project/`` is git-ignored, so a
      ``git clean -xdf`` deletes the vendored script while ``.git/hooks/``
      survives; fail-open there would wave every subsequent push past the
      ``[stealth]`` banned-term scan and the ``allowed_authors`` allowlist.
    """
    try:
        script_rel = f"{(hooks_dst.relative_to(project_root)).as_posix()}/{script_name}"
        resolve = (
            f'SCRIPT="$ROOT/{script_rel}"\n'
            'if [ ! -f "$SCRIPT" ]; then\n'
            "    # Secondary worktree (F-42): .booley_project/ is untracked, so it\n"
            "    # exists only in the MAIN worktree — reach it via the shared git dir.\n"
            '    COMMON=$(cd "$ROOT" && git rev-parse --path-format=absolute '
            "--git-common-dir 2>/dev/null) || COMMON=\n"
            '    case "$COMMON" in\n'
            "        '') ;;\n"
            f'        /*) SCRIPT="$(dirname "$COMMON")/{script_rel}" ;;\n'
            f'        [A-Za-z]:/*) SCRIPT="$(dirname "$COMMON")/{script_rel}" ;;\n'
            f'        *) SCRIPT="$(dirname "$ROOT/$COMMON")/{script_rel}" ;;\n'
            "    esac\n"
            "fi\n"
        )
    except ValueError:
        resolve = f'SCRIPT="{(hooks_dst / script_name).as_posix()}"\n'
    if fail_open:
        # A hook whose script is genuinely unavailable must SKIP, not ENOENT-fail
        # every commit in the worktree (F-42).
        missing = (
            f"    echo 'booley {script_name}: vendored hook script not found"
            " — skipping (run `booley init` to reinstall)' >&2\n"
            "    exit 0\n"
        )
    else:
        # ENOENT would also have blocked, but only by accident and with a
        # baffling message. Block on purpose, and say how to get unblocked.
        missing = (
            f"    echo 'booley {script_name}: vendored hook script not found at'"
            ' "$SCRIPT" >&2\n'
            "    echo 'This is the leak guard — it cannot pass what it could not"
            " check, so the push is REFUSED.' >&2\n"
            "    echo 'Restore it with `booley init` (it is git-ignored, so"
            " `git clean -xdf` removes it).' >&2\n"
            "    exit 1\n"
        )
    resolve += 'if [ ! -f "$SCRIPT" ]; then\n' + missing + "fi\n"
    script_ref = '"$SCRIPT"'

    return (
        "#!/bin/sh\n"
        f"# Booley {purpose}. Self-contained: delegates to\n"
        "# the copy vendored under the project's hooks dir, located repo-\n"
        "# relatively so it needs no Booley source checkout.\n"
        # Same fail-open/fail-closed split as the missing-script branch below:
        # a blocking guard that cannot even locate the repo must not report "ok".
        f"ROOT=$(git rev-parse --show-toplevel) || exit {0 if fail_open else 1}\n"
        + resolve
        + "# Interpreter ladder (F-7): stock Windows has no python3.exe — only the\n"
        "# Microsoft Store alias, which prints a Store nag and exits non-zero,\n"
        "# failing EVERY commit. `command -v` alone can't tell the alias from a\n"
        "# real interpreter, so each candidate must actually run `-c ''`.\n"
        "PY=\n"
        "for cand in python3 python; do\n"
        "    if \"$cand\" -c '' >/dev/null 2>&1; then PY=$cand; break; fi\n"
        "done\n"
        "if [ -z \"$PY\" ] && py -3 -c '' >/dev/null 2>&1; then PY='py -3'; fi\n"
        'if [ -z "$PY" ]; then\n'
        f"    echo 'booley {script_name}: no usable Python found (tried python3, python, py -3)' >&2\n"
        "    exit 1\n"
        "fi\n"
        f'exec $PY {script_ref} "$@"\n'
    )


def _build_commit_msg_hook_body(project_root: Path, hooks_dst: Path) -> str:
    """Build the commit-msg delegator shell script for the project repo."""
    return _build_hook_delegator_body(
        project_root,
        hooks_dst,
        "commit_msg_hook.py",
        "commit-msg hook (sanitize + validate) — strips AI/tooling\n"
        "# attribution (Co-Authored-By, claude, generated, ...) and project-\n"
        "# internal terms from commit messages",
        # A sanitizer that cannot run is an inconvenience; a commit that cannot
        # be made is a wedge. Skip (F-42).
        fail_open=True,
    )


def _build_pre_push_hook_body(project_root: Path, hooks_dst: Path) -> str:
    """Build the pre-push delegator shell script for the project repo.

    The pre-push leak guard closes the F-17 hole: ``git revert`` and
    ``git commit --no-verify`` bypass the commit-msg sanitizer entirely, so
    banned terms are re-checked on every outgoing commit at push time. It also
    enforces the ``[stealth] allowed_authors`` identity allowlist, which
    commit-msg structurally cannot: git hands that hook only the message file,
    so ``git commit --author=...`` never reaches it.
    """
    return _build_hook_delegator_body(
        project_root,
        hooks_dst,
        "pre_push_hook.py",
        "pre-push hook (leak guard, F-17) — blocks pushes whose\n"
        "# outgoing commits carry banned terms, or an author/committer\n"
        "# identity outside [stealth] allowed_authors (git revert,\n"
        "# --author and --no-verify all bypass the commit-msg sanitizer)",
        # The guard's whole job is blocking, so an unrunnable guard must block.
        # Fail-open here silently re-opens F-17 the first time `git clean -xdf`
        # takes the git-ignored .booley_project/ with it.
        fail_open=False,
    )


def _step_project_git_hooks(ctx: InitContext) -> None:
    """Install the commit-msg sanitizer + pre-push leak guard.

    The ``git_hooks`` step installs the leak guard into the project-agnostic
    .booley/ repo.
    This step covers the repo the user actually commits from: it vendors the
    sanitizer scripts into .booley_project/hooks/ and installs repo-relative
    commit-msg and pre-push delegators into the project's own .git/hooks/.
    The pre-push guard re-checks outgoing commits because ``git revert`` and
    ``--no-verify`` bypass commit-msg entirely (F-17).

    Both the scripts and the hook are local to this machine (git hooks never
    travel with a clone, and .booley_project/ is commonly git-ignored) — they
    are reproduced on every machine by `booley init`. Source-independence
    comes from copying out of the installed booley package (dev_support_dir()), not
    from any Booley source checkout, so end users need only the pip package.
    """
    ctx.step_banner("project commit-msg hook")

    project_dir = resolve_project_dir(ctx.project_root)
    hooks_dst = project_dir / "hooks"
    src_dir = dev_support_dir()
    # The stdlib-only subprocess runner the sanitizer imports (for its staged
    # `git diff` scan). Vendored flat beside the hook scripts so
    # validate_commit_msg resolves it by bare name with no core/ package present
    # — the fix for the SETUP-9 regression that crashed every host commit.
    runner_src = src_dir.parent / "core" / "run_command.py"

    missing = [s for s in _PROJECT_HOOK_SCRIPTS if not (src_dir / s).is_file()]
    if not runner_src.is_file():
        missing.append("run_command.py")
    if missing:
        skip(f"sanitizer scripts not found in developer-support dir: {', '.join(missing)}")
        ctx.record("project_git_hooks", "skip", "sanitizer scripts missing")
        return

    # Locate the project repo's hooks dir (handles worktrees, where .git is a
    # file pointing elsewhere) via git itself.
    proc = subprocess.run(
        ["git", "-C", str(ctx.project_root), "rev-parse", "--git-path", "hooks"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        skip("project root is not a git repo — project commit-msg hook skipped")
        ctx.record("project_git_hooks", "skip", "not a git repo")
        return
    hooks_dir = (ctx.project_root / proc.stdout.strip()).resolve()

    # (hook filename, delegated vendored script, delegator body)
    hook_installs = [
        (
            "commit-msg",
            "commit_msg_hook.py",
            _build_commit_msg_hook_body(ctx.project_root, hooks_dst),
        ),
        ("pre-push", "pre_push_hook.py", _build_pre_push_hook_body(ctx.project_root, hooks_dst)),
    ]

    if ctx.check_only:
        warn("would vendor sanitizer scripts and install project commit-msg and pre-push hooks")
        ctx.record("project_git_hooks", "warn", "would install")
        return

    hooks_dst.mkdir(parents=True, exist_ok=True)
    for name in _PROJECT_HOOK_SCRIPTS:
        shutil.copy2(str(src_dir / name), str(hooks_dst / name))
    shutil.copy2(str(runner_src), str(hooks_dst / "run_command.py"))

    hooks_dir.mkdir(parents=True, exist_ok=True)
    for hook_name, script_name, hook_body in hook_installs:
        hook_target = hooks_dir / hook_name

        # A hook referencing the vendored script is ours (refreshed in place);
        # a pre-existing non-Booley hook is backed up, never silently clobbered.
        # newline="\n" is load-bearing: the default (newline=None) applies OS line
        # translation, so on a Windows host `#!/bin/sh\n` lands on disk as
        # `#!/bin/sh\r\n`. The Session Runtime container then tries to exec an
        # interpreter literally named "/bin/sh\r", which does not exist — an ENOENT
        # that fails EVERY in-container commit and misleadingly names the hook, not
        # the interpreter (QA_REPORT D0a). A shell script must always be LF.
        outcome = guarded_write(
            hook_target,
            hook_body,
            owner_marker=script_name,
            backup_suffix=".pre-booley",
            newline="\n",
            executable=True,
        )
        if outcome is WriteOutcome.BACKED_UP:
            warn(f"backed up existing {hook_name} hook to {hook_name}.pre-booley")

    ok("project commit-msg + pre-push hooks installed (vendored into .booley_project/hooks/)")
    ctx.record("project_git_hooks", "ok", "installed")


# ---------------------------------------------------------------------------
# Line-endings advisory (record key: line_endings) — F-15
# ---------------------------------------------------------------------------


def _crlf_worktree_files(project_root: Path) -> list[str] | None:
    """Tracked paths that will read as modified in the container.

    ``git ls-files --eol`` prints one ``i/<index-eol> w/<worktree-eol>
    attr/<text-attr>`` line per tracked file. A CRLF worktree file is only a
    *phantom diff* when its line endings differ from the blob in the index —
    that mismatch is what a Linux container's git (which does no CRLF
    conversion) reports as a modification.

    A file whose ``.gitattributes`` marks it ``-text`` (or which git detected as
    binary) is stored CRLF and checked out CRLF: ``i/crlf w/crlf``. It matches
    the index byte for byte, in the container as much as on the host, so it
    cannot produce a phantom diff and must not be counted. Upstream repos
    legitimately do this for CRLF-native payloads carried as text — Windows
    ``.bat`` scripts, vendor register dumps — and counting them made the check
    fire on projects with nothing to fix (8 files on taxi, every one exempt).

    Comparing the two eol fields (rather than sniffing ``attr/-text``) tests the
    condition directly, so it also catches the inverse case ``-text`` never
    covers: an ``i/lf w/crlf`` file, which IS a phantom diff.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "ls-files", "--eol", "-z"],
            capture_output=True,
            text=True,
            errors="surrogateescape",
            check=False,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    paths: list[str] = []
    for record in proc.stdout.split("\0"):
        metadata, separator, path = record.partition("\t")
        fields = metadata.split()
        if not separator or not path:
            continue
        if len(fields) < 2:
            continue
        index_eol, worktree_eol = fields[0], fields[1]
        if worktree_eol not in ("w/crlf", "w/mixed"):
            continue
        if index_eol[2:] == worktree_eol[2:]:
            continue  # index already holds CRLF — no diff for git to see
        if any(field.lower() == "eol=crlf" for field in fields[2:]):
            continue  # an explicit checkout policy the Session Runtime also honors
        paths.append(path)
    return paths


def _count_crlf_worktree_files(project_root: Path) -> int | None:
    """Number of tracked files that will read as modified in the container."""
    paths = _crlf_worktree_files(project_root)
    return None if paths is None else len(paths)


def read_autocrlf_enabled(project_root: Path) -> bool | None:
    """Return Git's normalized ``core.autocrlf`` Boolean; None means unreadable."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "config", "--bool", "--get", "core.autocrlf"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode == 1 and not proc.stdout.strip():
        return False  # unset: Git's non-Windows checkout default
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip().lower()
    return value == "true" if value in {"true", "false"} else None


GITATTRIBUTES_RULE = "* text=auto eol=lf"


def _eol_policy_is_user_owned(project_root: Path) -> bool:
    """Does the root ``.gitattributes`` already set an all-files eol policy?

    A ``*``-pattern line carrying ``text``/``-text``/``eol=`` means the project
    has stated its own whole-tree policy. Whatever it says, it is a deliberate
    choice and init must not second-guess it — we neither add our rule nor
    argue with theirs.
    """
    path = project_root / ".gitattributes"
    try:
        lines = path.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
    except OSError:
        return False
    for line in lines:
        fields = line.split("#", 1)[0].split()
        if len(fields) < 2 or fields[0] != "*":
            continue
        if any(a in {"text", "-text"} or a.startswith(("text=", "eol=")) for a in fields[1:]):
            return True
    return False


def _write_gitattributes_rule(project_root: Path) -> bool:
    """Put ``* text=auto eol=lf`` at the TOP of root ``.gitattributes``.

    ``text=auto`` keeps Git's binary detection intact while applying LF to
    detected text. Prepended, never appended: git resolves attributes
    last-match-wins, so the first line lets every specific rule below it —
    including ``*.bat -text`` / vendor-dump exemptions — override this default
    (see :func:`_count_crlf_worktree_files`).

    Returns True if the file was created or changed.
    """
    path = project_root / ".gitattributes"
    try:
        existing = (
            path.read_text(encoding="utf-8", errors="surrogateescape") if path.exists() else ""
        )
    except OSError:
        return False
    if existing and not existing.endswith("\n"):
        existing += "\n"
    try:
        with path.open("w", encoding="utf-8", errors="surrogateescape", newline="\n") as f:
            f.write(f"{GITATTRIBUTES_RULE}\n{existing}")
    except OSError:
        return False
    return True


def _worktree_is_clean(project_root: Path) -> bool | None:
    """Is the host-side working tree free of tracked uncommitted changes?

    Sampled *before* init touches anything, because the CRLF fix itself moves
    this answer: with ``core.autocrlf=true`` the clean filter hides the CRLF
    from ``git status`` on the host, and flipping the knob can expose those
    same files as modified. Only the pre-fix reading tells us whether the user
    has real work in the tree. Untracked files are ignored because normalization
    rewrites only exact paths reported by ``git ls-files``. None = git could not
    answer.
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "status",
                "--porcelain",
                "--untracked-files=no",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return not proc.stdout.strip()


def _tracked_phantom_paths(project_root: Path) -> list[str] | None:
    """Tracked paths reported dirty even though staged and unstaged diffs are empty."""
    commands = (
        ("status", "--porcelain", "-z", "--untracked-files=no"),
        ("diff", "--quiet", "--ignore-submodules=none"),
        ("diff", "--cached", "--quiet", "--ignore-submodules=none"),
    )
    results: list[subprocess.CompletedProcess[bytes]] = []
    try:
        for command in commands:
            results.append(
                subprocess.run(
                    ["git", "-C", str(project_root), *command],
                    capture_output=True,
                    check=False,
                    timeout=60,
                )
            )
    except (subprocess.SubprocessError, OSError):
        return None

    status, unstaged, staged = results
    if (
        status.returncode != 0
        or unstaged.returncode not in (0, 1)
        or staged.returncode not in (0, 1)
    ):
        return None
    if not status.stdout.strip() or unstaged.returncode != 0 or staged.returncode != 0:
        return []

    paths: list[str] = []
    for record in status.stdout.split(b"\0"):
        if not record:
            continue
        if len(record) < 4 or record[:3] not in (b" M ", b"M  "):
            return None
        paths.append(os.fsdecode(record[3:]))
    return paths


def _tracked_status_is_phantom(project_root: Path) -> bool | None:
    paths = _tracked_phantom_paths(project_root)
    return None if paths is None else bool(paths)


def _protected_index_paths(project_root: Path, paths: list[str]) -> list[str] | None:
    """Affected paths hidden from normal status by Git index flags."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "ls-files", "-v", "-z"],
            capture_output=True,
            text=True,
            errors="surrogateescape",
            check=False,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None

    affected = set(paths)
    protected: list[str] = []
    for record in proc.stdout.split("\0"):
        tag, separator, path = record.partition(" ")
        if not separator or path not in affected:
            continue
        if tag == "S" or tag.islower():
            protected.append(path)
    return protected


def _file_digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def _candidate_digest(project_root: Path, name: str) -> tuple[bytes | None, str | None]:
    """Snapshot one candidate without following links or disturbing metadata."""
    path = project_root / name
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            return None, f"refusing to normalize non-regular tracked path {name!r}"
        if metadata.st_nlink > 1:
            return None, f"refusing to normalize hard-linked tracked path {name!r}"
        return _file_digest(path), None
    except OSError as exc:
        return None, f"could not inspect tracked path {name!r}: {exc}"


def _snapshot_candidates(
    project_root: Path, paths: list[str]
) -> tuple[dict[str, bytes], str | None]:
    snapshots: dict[str, bytes] = {}
    for name in paths:
        digest, error = _candidate_digest(project_root, name)
        if error:
            return {}, error
        assert digest is not None
        snapshots[name] = digest
    return snapshots, None


def _staged_paths(project_root: Path, output: bytes) -> dict[str, Path]:
    staged: dict[str, Path] = {}
    for record in output.split(b"\0"):
        temporary, separator, original = record.partition(b"\t")
        if separator and temporary and original:
            staged[os.fsdecode(original)] = project_root / os.fsdecode(temporary)
    return staged


def _cleanup_staged_files(paths: dict[str, Path]) -> None:
    for path in paths.values():
        with suppress(FileNotFoundError):
            path.unlink()


def _stage_lf_files(project_root: Path, paths: list[str]) -> tuple[dict[str, Path], str | None]:
    """Materialize checkout-filtered LF replacements without touching the worktree."""
    encoded_paths = b"\0".join(os.fsencode(name) for name in paths) + b"\0"
    try:
        proc = subprocess.run(
            [
                "git",
                "--literal-pathspecs",
                "-c",
                "core.autocrlf=false",
                "-C",
                str(project_root),
                "checkout-index",
                "--temp",
                "-z",
                "--stdin",
            ],
            input=encoded_paths,
            capture_output=True,
            check=False,
            timeout=300,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return {}, f"could not stage LF replacements: {exc}"
    staged = _staged_paths(project_root, proc.stdout)
    if proc.returncode != 0 or set(staged) != set(paths):
        _cleanup_staged_files(staged)
        detail = proc.stderr.decode(errors="replace").strip() or "incomplete Git output"
        return {}, f"could not stage LF replacements: {detail}"
    return staged, None


def _restore_after_write_failure(
    target: BinaryIO, original: bytes, path: Path, cause: OSError
) -> str:
    try:
        target.seek(0)
        target.write(original)
        target.truncate()
        target.flush()
    except OSError as restore_exc:
        return (
            f"could not normalize {path.name!r}: {cause}; restoring the original "
            f"content also failed: {restore_exc}"
        )
    return f"could not normalize {path.name!r}: {cause}; original content restored"


def _rewrite_from_stage(path: Path, replacement: Path, expected: bytes) -> str | None:
    """Update file content in place, preserving its inode and non-Git metadata."""
    digest, error = _candidate_digest(path.parent, path.name)
    if error:
        return error
    if digest != expected:
        return f"tracked path changed during line-ending repair: {path.name!r}"
    try:
        replacement_bytes = replacement.read_bytes()
        with path.open("r+b") as target:
            original = target.read()
            if hashlib.sha256(original).digest() != expected:
                return f"tracked path changed during line-ending repair: {path.name!r}"
            try:
                target.seek(0)
                target.write(replacement_bytes)
                target.truncate()
                target.flush()
            except OSError as exc:
                return _restore_after_write_failure(target, original, path, exc)
    except OSError as exc:
        return f"could not normalize {path.name!r}: {exc}"
    return None


def _refresh_normalized_index(project_root: Path, paths: list[str]) -> str | None:
    """Refresh Git's stat cache for paths whose normalized content is unchanged."""
    encoded_paths = b"\0".join(os.fsencode(name) for name in paths) + b"\0"
    try:
        proc = subprocess.run(
            [
                "git",
                "--literal-pathspecs",
                "-C",
                str(project_root),
                "add",
                "-u",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
            ],
            input=encoded_paths,
            capture_output=True,
            check=False,
            timeout=300,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return f"could not refresh normalized Git index entries: {exc}"
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip() or "Git add failed"
        return f"could not refresh normalized Git index entries: {detail}"
    return _verify_refreshed_index(project_root, encoded_paths)


def _verify_refreshed_index(project_root: Path, encoded_paths: bytes) -> str | None:
    """Confirm the content-aware refresh changed metadata only."""
    try:
        staged = subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "diff",
                "--cached",
                "--quiet",
                "--ignore-submodules=none",
            ],
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return f"could not verify the normalized Git index entries: {exc}"
    if staged.returncode not in (0, 1):
        detail = staged.stderr.decode(errors="replace").strip() or "Git diff failed"
        return f"could not verify the normalized Git index entries: {detail}"
    if staged.returncode == 1:
        return _restore_unexpected_index_changes(project_root, encoded_paths)
    if _worktree_is_clean(project_root) is not True:
        return "tracked files changed while Git index metadata was being refreshed"
    return None


def _restore_unexpected_index_changes(project_root: Path, encoded_paths: bytes) -> str:
    """Restore affected index entries after a refresh produced staged content."""
    try:
        restored = subprocess.run(
            [
                "git",
                "--literal-pathspecs",
                "-C",
                str(project_root),
                "reset",
                "-q",
                "HEAD",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
            ],
            input=encoded_paths,
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return (
            "normalization unexpectedly changed staged content and could not restore "
            f"the affected index entries: {exc}"
        )
    if restored.returncode == 0:
        return (
            "normalization unexpectedly changed staged content; "
            "restored the affected index entries"
        )
    detail = restored.stderr.decode(errors="replace").strip() or "Git reset failed"
    return (
        "normalization unexpectedly changed staged content and could not restore "
        f"the affected index entries: {detail}"
    )


def _normalize_as_lf(
    project_root: Path, paths: list[str], snapshots: dict[str, bytes]
) -> str | None:
    """Stage every replacement, then rewrite only unchanged, unprotected paths."""
    protected = _protected_index_paths(project_root, paths)
    if protected is None:
        return "could not inspect Git index flags — refusing to normalize files"
    if protected:
        names = ", ".join(repr(name) for name in protected[:3])
        suffix = " …" if len(protected) > 3 else ""
        return (
            f"refusing to normalize Git-protected path(s): {names}{suffix} "
            "(skip-worktree or assume-unchanged may hide local edits)"
        )

    staged, error = _stage_lf_files(project_root, paths)
    if error:
        return error
    try:
        for name in paths:
            error = _rewrite_from_stage(project_root / name, staged[name], snapshots[name])
            if error:
                return error
    finally:
        _cleanup_staged_files(staged)
    return _refresh_normalized_index(project_root, paths)


def _finish_safe_line_endings_step(ctx: InitContext) -> None:
    """Heal legacy stat-only dirtiness or report an already-safe LF checkout."""
    phantom_paths = _tracked_phantom_paths(ctx.project_root)
    if phantom_paths is None:
        warn("could not compare tracked status with Git diffs — no files changed")
        ctx.record("line_endings", "warn", "status comparison unreadable")
        return
    if not phantom_paths:
        ok("working tree is container-safe (no CRLF checkouts, autocrlf off)")
        ctx.record("line_endings", "ok", "no CRLF")
        return
    if ctx.check_only:
        warn(f"would refresh stale Git index metadata for {len(phantom_paths)} tracked file(s)")
        ctx.record("line_endings", "warn", "stale index metadata")
        return

    error = _refresh_normalized_index(ctx.project_root, phantom_paths)
    if error:
        warn(error)
        ctx.record("line_endings", "warn", "index refresh failed")
        return
    ok(f"refreshed stale Git index metadata for {len(phantom_paths)} tracked file(s)")
    ctx.record("line_endings", "ok", "index refreshed")


def _step_line_endings(ctx: InitContext) -> None:
    """Keep the checkout from reading as dirty inside the container (F-15).

    Disable future auto-conversion, normalize unchanged candidates in place,
    then install the project policy only after file content is safe.
    """
    ctx.step_banner("line endings")
    if not _is_git_repository(ctx.project_root):
        skip("project root is not a git repo — line-endings check skipped")
        ctx.record("line_endings", "skip", "not a git repo")
        return

    autocrlf = read_autocrlf_enabled(ctx.project_root)
    if autocrlf is None:
        warn("could not read core.autocrlf as a Git Boolean — no files changed")
        ctx.record("line_endings", "warn", "autocrlf unreadable")
        return
    crlf_paths = _crlf_worktree_files(ctx.project_root)
    if crlf_paths is None:
        warn("could not read `git ls-files --eol` — no files changed")
        ctx.record("line_endings", "warn", "EOL scan unreadable")
        return
    if not crlf_paths and not autocrlf:
        _finish_safe_line_endings_step(ctx)
        return

    snapshots, safety_error = _snapshot_candidates(ctx.project_root, crlf_paths)
    clean = _worktree_is_clean(ctx.project_root)
    _report_line_ending_findings(len(crlf_paths), autocrlf)
    if ctx.check_only:
        _report_line_endings_plan(ctx, autocrlf, len(crlf_paths), clean, safety_error)
        ctx.record("line_endings", "warn", "CRLF working tree")
        return
    _apply_line_ending_repairs(ctx, autocrlf, crlf_paths, snapshots, clean, safety_error)


def _is_git_repository(project_root: Path) -> bool:
    try:
        probe = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--git-dir"],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return probe.returncode == 0


def _report_line_ending_findings(crlf_count: int, autocrlf: bool) -> None:
    if crlf_count:
        warn(
            f"{crlf_count} tracked file(s) are checked out with CRLF — the "
            "Session Runtime container will see every one as modified "
            "(phantom diffs break the dirty-tree check, scope enforcement, "
            "and ticket worktrees)"
        )
    if autocrlf:
        warn(
            "core.autocrlf=true (Git for Windows' installer default) "
            "re-creates CRLF checkouts on every clone/checkout"
        )


def _apply_line_ending_repairs(
    ctx: InitContext,
    autocrlf: bool,
    crlf_paths: list[str],
    snapshots: dict[str, bytes],
    clean: bool | None,
    safety_error: str | None,
) -> None:
    fixed: list[str] = []
    if autocrlf and _disable_autocrlf(ctx.project_root):
        fixed.append("autocrlf")
    status, detail = (
        ("ok", "")
        if not crlf_paths
        else _maybe_normalize(ctx, crlf_paths, snapshots, clean, safety_error)
    )
    if _apply_gitattributes_rule(ctx.project_root):
        fixed.append("gitattributes")
    if status == "ok":
        detail = detail or "+".join(fixed)
        if not detail:
            status, detail = "warn", "no fix applied"
    ctx.record("line_endings", status, detail)


def _report_line_endings_plan(
    ctx: InitContext,
    autocrlf: bool,
    crlf_count: int,
    clean: bool | None,
    safety_error: str | None,
) -> None:
    """Name every fix ``--check-only`` is holding back from (its contract)."""
    if autocrlf:
        info("  would set core.autocrlf=false")
    if not _eol_policy_is_user_owned(ctx.project_root):
        info(f"  would add '{GITATTRIBUTES_RULE}' to .gitattributes")
    if crlf_count:
        if safety_error:
            info(f"  would leave tracked files untouched: {safety_error}")
        elif clean is True:
            info(f"  would normalize {crlf_count} tracked file(s) to LF in place")
        elif clean is False:
            info("  would leave tracked files untouched until changes are committed or stashed")
        else:
            info("  would leave tracked files untouched because `git status` was unreadable")


def _disable_autocrlf(project_root: Path) -> bool:
    """Turn the CRLF checkout filter off for this repo. Returns True if set."""
    proc = subprocess.run(
        ["git", "-C", str(project_root), "config", "core.autocrlf", "false"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if proc.returncode != 0:
        warn(f"could not set core.autocrlf: {proc.stderr.strip()}")
        return False
    ok("core.autocrlf=false (repo-local; CRLF will not come back on checkout)")
    return True


def _apply_gitattributes_rule(project_root: Path) -> bool:
    """Add the LF rule unless the project already has an eol policy of its own."""
    if _eol_policy_is_user_owned(project_root):
        skip(".gitattributes already states a whole-tree eol policy — left as-is")
        return False
    if not _write_gitattributes_rule(project_root):
        warn("could not write .gitattributes")
        return False
    ok(f"added '{GITATTRIBUTES_RULE}' as the first line of .gitattributes")
    info("  commit it — the rule only travels to your team through git")
    return True


def _normalization_refusal(clean: bool | None, safety_error: str | None) -> tuple[str, str] | None:
    if safety_error:
        return safety_error, "candidate unsafe"
    if clean is None:
        return (
            "could not read `git status` — refusing to normalize tracked files",
            "status unreadable",
        )
    if not clean:
        return (
            "working tree has uncommitted changes — refusing to normalize it "
            "(normalization rewrites the affected tracked files). Commit or stash "
            "first, then re-run `booley init`.",
            "dirty tree",
        )
    return None


def _maybe_normalize(
    ctx: InitContext,
    crlf_paths: list[str],
    snapshots: dict[str, bytes],
    clean: bool | None,
    safety_error: str | None,
) -> tuple[str, str]:
    """Normalize a clean CRLF tree without deleting worktree files."""
    crlf_count = len(crlf_paths)
    refusal = _normalization_refusal(clean, safety_error)
    if refusal:
        message, detail = refusal
        warn(message)
        return "warn", detail

    error = _normalize_as_lf(ctx.project_root, crlf_paths, snapshots)
    if error:
        warn(error)
        return "warn", "normalization failed"

    remaining = _count_crlf_worktree_files(ctx.project_root)
    if remaining is None:
        warn("could not verify line endings after normalization")
        return "warn", "EOL verification unreadable"
    if remaining:
        warn(f"{remaining} file(s) still read as CRLF after normalization")
        return "warn", "CRLF survived normalization"
    ok(f"normalized {crlf_count} file(s) to LF in place — tree is container-safe")
    return "ok", "normalized"


# The exact git config knob/value the guard sets; doctor checks the same pair.
WORKTREE_PRUNE_KEY = "gc.worktreePruneExpire"
WORKTREE_PRUNE_VALUE = "never"


def read_worktree_prune_expire(project_root: Path) -> str | None:
    """Return the repo's ``gc.worktreePruneExpire`` value, or None if unset.

    Also returns None when *project_root* is not a git repo or git itself is
    unavailable — callers distinguish those cases themselves if they care.
    """
    proc = subprocess.run(
        ["git", "-C", str(project_root), "config", "--get", WORKTREE_PRUNE_KEY],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def _step_worktree_prune_guard(ctx: InitContext) -> None:
    """Set ``gc.worktreePruneExpire=never`` on the project repo.

    Ticket Mode worktrees are created in-container (ADR 0028 Decision 10), so
    their git worktree metadata records container paths. A host-side
    ``git gc`` cannot see those paths and would prune the registrations —
    "never" makes worktree pruning explicit-only (``git worktree prune``).
    """
    ctx.step_banner("worktree prune guard")

    # Confirm the project root is a git repo before touching its config.
    probe = subprocess.run(
        ["git", "-C", str(ctx.project_root), "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if probe.returncode != 0:
        skip("project root is not a git repo — worktree prune guard skipped")
        ctx.record("worktree_prune_guard", "skip", "not a git repo")
        return

    if read_worktree_prune_expire(ctx.project_root) == WORKTREE_PRUNE_VALUE:
        skip(f"{WORKTREE_PRUNE_KEY} already '{WORKTREE_PRUNE_VALUE}'")
        ctx.record("worktree_prune_guard", "skip", "already set")
        return

    if ctx.check_only:
        warn(f"would set {WORKTREE_PRUNE_KEY}={WORKTREE_PRUNE_VALUE}")
        ctx.record("worktree_prune_guard", "warn", "would set")
        return

    proc = subprocess.run(
        ["git", "-C", str(ctx.project_root), "config", WORKTREE_PRUNE_KEY, WORKTREE_PRUNE_VALUE],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if proc.returncode != 0:
        warn(f"could not set {WORKTREE_PRUNE_KEY}: {proc.stderr.strip()}")
        ctx.record("worktree_prune_guard", "warn", "git config failed")
        return

    ok(
        f"{WORKTREE_PRUNE_KEY}={WORKTREE_PRUNE_VALUE} (host `git gc` can no "
        "longer prune in-container worktrees)"
    )
    ctx.record("worktree_prune_guard", "ok", "set")
