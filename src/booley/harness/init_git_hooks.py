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

import shutil
import subprocess
from pathlib import Path

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
        paths.append(path)
    return paths


def _count_crlf_worktree_files(project_root: Path) -> int | None:
    """Number of tracked files that will read as modified in the container."""
    paths = _crlf_worktree_files(project_root)
    return None if paths is None else len(paths)


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
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
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
        existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    except OSError:
        return False
    if existing and not existing.endswith("\n"):
        existing += "\n"
    try:
        with path.open("w", encoding="utf-8", newline="\n") as f:
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
    has real work in the tree. Untracked files are ignored because the repair
    deletes and restores only paths reported by ``git ls-files``. None = git
    could not answer.
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


def _restore_tracked_paths(
    project_root: Path, paths: list[str]
) -> subprocess.CompletedProcess[str]:
    """Restore exact tracked paths without hitting the Windows command-line limit."""
    return subprocess.run(
        [
            "git",
            "-C",
            str(project_root),
            "checkout",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
        ],
        input="\0".join(paths) + "\0",
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def _protected_index_paths(project_root: Path, paths: list[str]) -> list[str] | None:
    """Affected paths hidden from normal status by Git index flags."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "ls-files", "-v", "-z"],
            capture_output=True,
            text=True,
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


def _recheckout_as_lf(project_root: Path, paths: list[str]) -> str | None:
    """Delete affected tracked files, then restore them through the LF filters.

    `git checkout -- .` alone does NOT re-checkout: with the clean filter in
    place the worktree already matches the index, so git rewrites nothing and
    the CRLF survives (so does `git checkout-index -a -f`). The tracked files
    have to be gone first (F-3). Untracked and unaffected tracked files are
    left alone — only the paths known to have phantom CRLF diffs are restored.

    Returns an error string, or None on success.
    """
    try:
        protected = _protected_index_paths(project_root, paths)
        if protected is None:
            return "could not inspect Git index flags — refusing to re-check out files"
        if protected:
            names = ", ".join(protected[:3])
            suffix = " …" if len(protected) > 3 else ""
            return (
                f"refusing to re-check out Git-protected path(s): {names}{suffix} "
                "(skip-worktree or assume-unchanged may hide local edits)"
            )

        removed: list[str] = []
        for name in paths:
            try:
                (project_root / name).unlink()
                removed.append(name)
            except FileNotFoundError:
                pass  # already gone (deleted upstream, or a stale index entry)
            except OSError as exc:
                # Restore what we removed so far rather than leave a half tree.
                if removed:
                    _restore_tracked_paths(project_root, removed)
                return f"could not delete {name}: {exc}"
        restored = _restore_tracked_paths(project_root, paths)
        if restored.returncode != 0:
            return (
                f"git checkout failed after deleting the CRLF files: "
                f"{restored.stderr.strip()} — recover with "
                f"`git -C {project_root} checkout -- .`"
            )
    except (subprocess.SubprocessError, OSError) as exc:
        return f"re-checkout failed: {exc} — recover with `git -C {project_root} checkout -- .`"
    return None


def _step_line_endings(ctx: InitContext) -> None:
    """Keep the checkout from reading as dirty inside the container (F-15).

    Git for Windows defaults to ``core.autocrlf=true``, which checks text files
    out with CRLF. The Session Runtime bind-mounts that worktree into a Linux
    container whose git does no CRLF conversion, so every CRLF file shows as
    modified in-container — phantom diffs that trip the dirty-tree doctor
    warning, scope enforcement, and ticket worktrees.

    The fix is three acts of very different weight, so init treats them
    differently:

    - ``core.autocrlf=false`` — repo-local, reversible, touches no file. Done
      automatically; it is what stops CRLF coming back on the next checkout.
    - ``* text=auto eol=lf`` in ``.gitattributes`` — an added (never appended)
      line that normalizes detected text without forcing binary files through
      text conversion, only when the project has not stated its own policy.
      Left uncommitted: it is the user's tracked source, and only they should
      commit to it.
    - the re-checkout — deletes and restores tracked files automatically when
      the pre-mutation tree is clean, and is refused on a dirty tree; init will
      not be the thing that eats uncommitted work.
    """
    ctx.step_banner("line endings")

    probe = subprocess.run(
        ["git", "-C", str(ctx.project_root), "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if probe.returncode != 0:
        skip("project root is not a git repo — line-endings check skipped")
        ctx.record("line_endings", "skip", "not a git repo")
        return

    autocrlf_proc = subprocess.run(
        ["git", "-C", str(ctx.project_root), "config", "--get", "core.autocrlf"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    autocrlf = autocrlf_proc.stdout.strip().lower() if autocrlf_proc.returncode == 0 else ""

    crlf_paths = _crlf_worktree_files(ctx.project_root)
    crlf_count = None if crlf_paths is None else len(crlf_paths)
    if not crlf_count and autocrlf != "true":
        ok("working tree is container-safe (no CRLF checkouts, autocrlf off)")
        ctx.record("line_endings", "ok", "no CRLF")
        return

    # Sampled before the autocrlf flip below, which can itself expose CRLF
    # files as modified and make a genuinely clean tree look dirty.
    clean = _worktree_is_clean(ctx.project_root)

    if crlf_count:
        warn(
            f"{crlf_count} tracked file(s) are checked out with CRLF — the "
            "Session Runtime container will see every one as modified "
            "(phantom diffs break the dirty-tree check, scope enforcement, "
            "and ticket worktrees)"
        )
    if autocrlf == "true":
        warn(
            "core.autocrlf=true (Git for Windows' installer default) "
            "re-creates CRLF checkouts on every clone/checkout"
        )

    if ctx.check_only:
        _report_line_endings_plan(ctx, autocrlf, crlf_count, clean)
        ctx.record("line_endings", "warn", "CRLF working tree")
        return

    fixed: list[str] = []
    if autocrlf == "true" and _disable_autocrlf(ctx.project_root):
        fixed.append("autocrlf")

    # Disk first, .gitattributes second. The re-checkout restores every tracked
    # file from the index, and .gitattributes is normally tracked — writing our
    # rule before the re-checkout would hand it straight back to git to
    # overwrite, silently losing the one part of the fix the user's teammates
    # ever see.
    status, detail = (
        ("ok", "") if not crlf_count else _maybe_recheckout(ctx, crlf_paths or [], clean)
    )

    if _apply_gitattributes_rule(ctx.project_root):
        fixed.append("gitattributes")

    if status == "ok":
        detail = detail or "+".join(fixed)
        if not detail:
            status, detail = "warn", "no fix applied"
    ctx.record("line_endings", status, detail)


def _report_line_endings_plan(
    ctx: InitContext, autocrlf: str, crlf_count: int | None, clean: bool | None
) -> None:
    """Name every fix ``--check-only`` is holding back from (its contract)."""
    if autocrlf == "true":
        info("  would set core.autocrlf=false")
    if not _eol_policy_is_user_owned(ctx.project_root):
        info(f"  would add '{GITATTRIBUTES_RULE}' to .gitattributes")
    if crlf_count:
        if clean is True:
            info(f"  would re-check out {crlf_count} tracked file(s) with LF endings")
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


def _maybe_recheckout(
    ctx: InitContext, crlf_paths: list[str], clean: bool | None
) -> tuple[str, str]:
    """Re-check out a clean CRLF tree as LF.

    Returns the ``(status, detail)`` for the step record.
    """
    crlf_count = len(crlf_paths)
    if clean is None:
        warn("could not read `git status` — refusing to re-check out the tree")
        return "warn", "status unreadable"
    if not clean:
        warn(
            "working tree has uncommitted changes — refusing to re-check it out "
            "(the re-checkout replaces the affected tracked files). Commit or stash "
            "first, then re-run `booley init`."
        )
        return "warn", "dirty tree"

    error = _recheckout_as_lf(ctx.project_root, crlf_paths)
    if error:
        warn(error)
        return "warn", "re-checkout failed"

    remaining = _count_crlf_worktree_files(ctx.project_root)
    if remaining:
        warn(f"{remaining} file(s) still read as CRLF after the re-checkout")
        return "warn", "CRLF survived re-checkout"
    ok(f"re-checked out {crlf_count} file(s) with LF endings — tree is container-safe")
    return "ok", "re-checked out"


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
