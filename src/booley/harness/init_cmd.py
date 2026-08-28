"""booley init / booley doctor — project setup and health checks.

Replaces bootstrap.py with package-aware logic. Uses booley.runtime.paths for all
package data resolution and booley.runtime.project_dir for project directory discovery.

Idempotent — safe to re-run. Each step checks preconditions and skips if
already satisfied. A first init requires an explicit agent provider and auth
policy: a terminal prompts without defaults, while unattended callers pass
``--provider`` and ``--auth``.

Steps, in the order :func:`run_init` runs them. The number the user sees is
allocated at print time by :meth:`InitContext.step_banner`, so it is always
contiguous; identity lives in the ``record`` key, not the number.

    - Host bootstrap preflight (git, Docker, VS Code); unavailable Docker aborts
    - Scaffold a new IP from scratch (``--scaffold`` only)
    - Project directory (.booley_project/ with config skeletons)
    - Tickets directory tree (board states + logs)
    - Agent authentication setup
    - Skill deployment (system-level ~/.agents/ or ~/.claude/)
    - Pinned Nangate45 download into the per-user cache
    - Docker image build
    - Project sandbox image (bakes repo Python deps) — ADR 0018
    - Git hooks: repo hooks, project commit-msg, worktree prune guard,
      line endings, guidance links
    - Interactive Mode: untracked devcontainer + egress/reaper objects — ADR 0018
    - Post-setup advisories

Historical steps, since handled by ``pip install`` or eliminated: venv
creation, shell alias, agent directory (.agents/ scaffold, junctions — now
system-level).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from booley import __version__
from booley.config.guidance_links import ensure_guidance_links, plan_guidance_links
from booley.config.settings import InteractiveConfig, load_interactive_config
from booley.fusesoc.core_projection import (
    PROJECTED_CORE_GLOB,
    CoreProjectionError,
    authoritative_cores,
    projection_enabled,
    projection_issues,
    reconcile_projected_cores,
)
from booley.harness import devcontainer as dc

# --- Re-exported for backward compatibility (Single Responsibility split) ---
# These symbols were relocated into sibling init_* modules so this file is just
# the top-level `init` coordinator + a thin facade. They are re-exported here so
# existing importers (booley.harness.doctor, tests) and this module's own steps
# keep resolving them by their original ``init_cmd`` names. F401 is suppressed
# for this file (see pyproject) because a facade re-exports names it may not use.
from booley.harness import doctor_stamp, nangate_pdk
from booley.harness import interactive_docker as idk
from booley.harness.colors import accent, bold_chrome, green, red, yellow
from booley.harness.init_common import (
    InitContext,
    StepResult,
    WriteOutcome,
    banner,
    err,
    guarded_write,
    info,
    ok,
    skip,
    warn,
)
from booley.harness.init_docker_image import (
    DOCKER_IMAGE,
    FLAVOR_IMAGES,
    GHCR_IMAGE,
    LABEL_FINGERPRINT,
    _docker_build_image,
    _docker_build_wheel,
    _docker_check_only,
    _docker_image_exists,
    _docker_local_build,
    _image_build_fingerprint,
    _image_is_stale,
    _image_label,
    _iter_fingerprint_files,
    _read_version,
    _stamp_image_fingerprint,
    _step_docker_image,
    _try_pull_image,
    ensure_flavor_image,
    source_fingerprint_mismatch,
)
from booley.harness.init_git_hooks import (
    _PROJECT_HOOK_SCRIPTS,
    _build_commit_msg_hook_body,
    _step_git_hooks,
    _step_line_endings,
    _step_project_git_hooks,
    _step_worktree_prune_guard,
)
from booley.harness.init_plan import InitPlan, InitPreconditionError
from booley.harness.init_scaffold import step_scaffold
from booley.harness.init_skills import (
    _deploy_skills,
    _find_skill_targets,
    _is_booley_skill_link,
    _make_junction_or_symlink,
    _prune_stale_skill_links,
)
from booley.runtime import auth_token
from booley.runtime import project_image as pi
from booley.runtime.git import add_git_excludes
from booley.runtime.paths import docker_data_dir, skills_dir
from booley.runtime.platform_paths import IS_WINDOWS, docker_mount_path
from booley.runtime.project_dir import (
    PROJECT_DIR_NAME,
    reset_cache,
    resolve_checkout_project_dir,
    resolve_project_dir,
)
from booley.runtime.timefmt import detect_host_timezone
from booley.ticket_board.lifecycle import REQUIRED_BOARD_DIRS

# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------

# Ticket Board directories that `booley init` creates — derived from the canonical
# lifecycle (excludes archived, created on demand) so it can't drift from
# doctor's required-dirs check, which reads the same source.
BOARD_STATES = REQUIRED_BOARD_DIRS

MIN_PY = (3, 11)

# Output helpers (info/ok/skip/warn/err/banner), StepResult and InitContext now
# live in booley.harness.init_common and are re-exported at the top of this
# module. DOCKER_IMAGE / GHCR_IMAGE / LABEL_FINGERPRINT moved to
# booley.harness.init_docker_image (likewise re-exported).


# ---------------------------------------------------------------------------
# Config templates
# ---------------------------------------------------------------------------

BOOLEY_TOML_SKELETON = """\
# booley.toml - project-level config for Booley.
#
# Intentionally left empty by `booley init`.
# Populate this file with the booley-setup skill (Step 2, project config) after
# it has inspected the repository's docs, manifests, filelists, scripts, and CI.
#
# Does a test need a non-RTL build step before it can run (e.g. cross-compile
# the selected test's firmware)? Declare it as Pre-Run Commands (ADR 0039) —
# shell lines run inside the Session Runtime immediately before each sim run, under
# the BOOLEY_* env contract (BOOLEY_TEST_NAME, BOOLEY_TEST_NAMES,
# BOOLEY_RUN_CWD, BOOLEY_BUILD_ROOT, ...; see docs/CONFIG.md):
#
# [flows.sim]
# pre_run_commands = ["make -C tests build_case CASE=$BOOLEY_TEST_NAME"]
"""

# Comment-only, like BOOLEY_TOML_SKELETON. `booley init` used to scaffold the
# annotated TESTS_TEMPLATE.toml verbatim, which left the *example* test names
# (`reset`, `basic`, `stress`) under a `[sim]` key in a real project. Doctor's
# "tests.toml valid" check passes on that, so a half-finished setup read as
# healthier than it was, and a simulate run would pin a test that never existed
# (F-5). Scaffold nothing to declare instead; the template stays package data
# for the booley-setup skill to read.
TESTS_TOML_SKELETON = """\
# tests.toml - per-Target verification-intent test lists.
#
# Intentionally left empty by `booley init`.
# Populate this file with the booley-setup skill (Step 2, project config), which
# reads the annotated TESTS_TEMPLATE.toml shipped with the skill.
"""


def _ticket_defaults_skeleton() -> str:
    """Read the inactive Ticket Creation Defaults template shipped with the skill."""
    template = skills_dir() / "booley-ticket-create" / "TICKET_DEFAULTS_TEMPLATE.md"
    return template.read_text(encoding="utf-8")


# Inside ``.booley_project/`` we ignore transient state that should never be
# committed (tmp scratch, runtime logs, lockfiles).  ``.interactive_logs/`` is
# new in ADR 0012 — per-session transcripts written by the MCP server when an
# outer Claude Code / Codex tab calls Booley Flows and Specialists.
#
# Only *fixed-name*, Booley-owned transient dirs belong here — patterns that are
# correct for every project.  ``.runtime/`` (dotted) is the scratch/EDA build
# root (``resolve_project_dir()/".runtime"``, holds the multi-GB edalize tree);
# ``runtime/`` (no dot) is the container-lifetime bookkeeping dir — the doctor
# stamp (``runtime/doctor_stamp.json``), the developer probe, and the job-slot
# store all live there (F-6: it is a distinct dir from ``.runtime/``, not a
# typo — do not "dedupe" the two away).  ``worktrees/`` holds per-run git
# worktrees.  Project-configurable output dirs (``[flows.sim].output_dir``
# etc.) are deliberately NOT listed — they vary per project and often live
# outside ``.booley_project/``.
#
# ``__pycache__/`` + ``*.pyc``: init vendors the commit-msg/pre-push hook
# scripts into ``.booley_project/hooks/``, and running them writes bytecode
# right next to the sources — which the inner ``.booley_project`` repo then
# tracked (fpu F-29). Nothing under this dir is ever an intentional .pyc.
PROJECT_GITIGNORE_PATTERNS = (
    "tmp/",
    "tickets/logs/",
    "tickets/locks/",
    ".interactive_logs/",
    ".runtime/",
    "runtime/",
    "worktrees/",
    "__pycache__/",
    "*.pyc",
    "SETUP-REPORT.md",
    "FEEDBACK-REPORT.md",
)

PROJECT_GITIGNORE = "# Transient Booley state — do not commit.\n" + "".join(
    f"{pattern}\n" for pattern in PROJECT_GITIGNORE_PATTERNS
)


# ---------------------------------------------------------------------------
# Init step: project directory (record key: project_dir)
# ---------------------------------------------------------------------------


def _backfill_config_skeletons(project_dir: Path, ctx: InitContext) -> None:
    """Create missing config skeletons without guessing project-specific values."""
    # configs.toml is deliberately absent: the legacy registry was removed by
    # ADR 0022 (.core owns design-description) and doctor fails on an empty one.
    # tests.toml carries verification-intent; ticket_defaults.md is inactive
    # agent guidance. Both are scaffolded alongside booley.toml without guessing
    # Project-specific values.
    skeletons = {
        "booley.toml": BOOLEY_TOML_SKELETON,
        "tests.toml": TESTS_TOML_SKELETON,
        "ticket_defaults.md": _ticket_defaults_skeleton(),
    }
    added = [
        name
        for name, body in skeletons.items()
        if guarded_write(project_dir / name, body, dry_run=ctx.check_only) is WriteOutcome.WRITTEN
    ]
    if not added:
        return
    if ctx.check_only:
        warn(f"would add {len(added)} config skeleton file(s) under {project_dir}")
        return
    ok(f"added {len(added)} config skeleton file(s) under {project_dir}")


def _backfill_project_gitignore(project_dir: Path, ctx: InitContext) -> None:
    """Ensure ``.booley_project/.gitignore`` covers every required ignore pattern.

    Pre-existing projects predate later ignore lines (``.interactive_logs/``,
    ``.runtime/``, ``worktrees/``).  Rather than only creating the file when it's
    absent, we *self-heal*: any required pattern that isn't already present is
    appended.  This is non-destructive — existing (possibly user-customised)
    lines are never removed or reordered — but it means running ``booley init``
    on an old project repairs a stale ignore list instead of silently leaving a
    multi-GB ``.runtime/`` tree stage-able.
    """
    gitignore = project_dir / ".gitignore"

    if not gitignore.exists():
        if ctx.check_only:
            warn(f"would add {gitignore.name} (covers {', '.join(PROJECT_GITIGNORE_PATTERNS)})")
            return
        guarded_write(gitignore, PROJECT_GITIGNORE)
        ok(f"added {gitignore} (ignores {', '.join(PROJECT_GITIGNORE_PATTERNS)})")
        return

    # File present — append only the patterns it's missing.  Compare against
    # stripped lines so trailing whitespace or a missing final newline doesn't
    # cause a spurious duplicate.
    existing = gitignore.read_text(encoding="utf-8")
    present = {line.strip() for line in existing.splitlines()}
    missing = [p for p in PROJECT_GITIGNORE_PATTERNS if p not in present]
    if not missing:
        return

    if ctx.check_only:
        warn(
            f"would add {len(missing)} missing ignore pattern(s) to {gitignore.name}: {', '.join(missing)}"
        )
        return

    prefix = "" if existing.endswith("\n") or not existing else "\n"
    addition = (
        prefix
        + "\n# Added by booley init — transient Booley state.\n"
        + "".join(f"{p}\n" for p in missing)
    )
    gitignore.write_text(existing + addition, encoding="utf-8")
    ok(f"added {len(missing)} missing ignore pattern(s) to {gitignore}: {', '.join(missing)}")


FUSESOC_IGNORE_BODY = (
    "# Marker: FuseSoC's library scanner (and Booley's discover_cores) skip any\n"
    "# directory carrying this file. .booley_project/ holds transient git\n"
    "# worktrees whose .core copies share the project's VLNV — without this\n"
    "# marker FuseSoC's recursive --cores-root scan would let a stale worktree\n"
    "# core shadow the repo-root source and silently build the wrong RTL.\n"
    "#\n"
    "# One subtree is exempt BY DESIGN: cores/ (stealth authored cores, ADR\n"
    "# 0036). Booley scans it as a second root, so this marker never applies\n"
    "# to it — do not drop a FUSESOC_IGNORE inside cores/ unless you mean to\n"
    "# hide those cores from Booley too.\n"
)


def _backfill_fusesoc_ignore(project_dir: Path, ctx: InitContext) -> None:
    """Ensure ``.booley_project/FUSESOC_IGNORE`` exists.

    The load-bearing half of the worktree-shadowing fix: Booley's own
    :func:`~booley.fusesoc.fusesoc_registry.discover_cores` skips ``.booley_project/``
    structurally, but FuseSoC's ``--cores-root`` recursive scan is out of
    Booley's code control — only this on-disk marker reaches it. Idempotent and
    self-healing so re-running ``booley init`` repairs a project that predates
    the fix (or one whose marker a user deleted).
    """
    marker = project_dir / "FUSESOC_IGNORE"
    outcome = guarded_write(marker, FUSESOC_IGNORE_BODY, dry_run=ctx.check_only)
    if outcome is not WriteOutcome.WRITTEN:
        return
    if ctx.check_only:
        warn(f"would add {marker.name} (keeps worktree .core copies out of FuseSoC's scan)")
        return
    ok(f"added {marker} (worktree cores stay out of FuseSoC's --cores-root scan)")


def _step_project_dir(ctx: InitContext) -> None:
    ctx.step_banner("project directory")
    info(f"initializing project at {ctx.project_root}")

    # Init only ever creates/backfills .booley_project/ at the top-level repo dir
    # it was pointed at — a DIRECT check, never a walk-up. resolve_project_dir()
    # deliberately walks up to ancestors (so runtime lookups from inside worktrees
    # resolve), but that behavior is wrong for init's *creation* decision: a
    # sibling project's .booley_project/ one level up must not suppress scaffolding
    # for the repo the user actually ran init on.
    target = ctx.project_root / ".booley_project"

    if target.is_dir():
        if os.name != "nt" and stat.S_IMODE(target.stat().st_mode) != 0o700:
            if ctx.check_only:
                warn(f"would secure project directory permissions on {target} to 0700")
                ctx.record("project_dir", "warn", "permissions need 0700")
                return
            target.chmod(0o700)
            ok(f"secured project directory permissions on {target} to 0700")
        skip(f"project directory found at {target}")
        _backfill_config_skeletons(target, ctx)
        _backfill_project_gitignore(target, ctx)
        _backfill_fusesoc_ignore(target, ctx)
        _init_project_git_repo(target, ctx)  # self-heal an older stealth setup
        ctx.record("project_dir", "skip", "already present")
        return

    if ctx.check_only:
        warn(f"would create {target}/ with config skeletons")
        ctx.record("project_dir", "warn", "would create")
        return

    target.mkdir(parents=True, mode=0o700, exist_ok=True)
    if os.name != "nt":
        target.chmod(0o700)

    gitignore = target / ".gitignore"
    guarded_write(gitignore, PROJECT_GITIGNORE)

    # booley.toml + tests.toml skeletons (idempotent, never overwrites).
    _backfill_config_skeletons(target, ctx)
    _backfill_fusesoc_ignore(target, ctx)
    _init_project_git_repo(target, ctx)

    reset_cache()
    ok(f"project directory created at {target}")
    info(
        "  config skeletons created; run the booley-setup skill starting at "
        "Step 0 (planning). Its Step 2 fills these files."
    )
    ctx.record("project_dir", "ok", "created with config skeletons")


def _init_project_git_repo(target: Path, ctx: InitContext) -> None:
    """Give a stealth ``.booley_project/`` its own git repo (F-5, ADR 0036).

    A stealth project dir is excluded from the host repo via
    ``.git/info/exclude``, so ADR 0036's "versioned by the project dir's own
    git repo" only holds if that repo exists — but init never created it, so a
    fresh stealth setup was versioned *nowhere* until the user ran `git init` by
    hand. Do it for them, guarded:

    - only in stealth mode (the default; a ``--scaffold`` demo opts out with
      ``[stealth] enabled = false`` and is throwaway), and
    - only when ``target`` is not already its own git work tree (idempotent,
      never touches an existing repo).

    Staging/committing is left to the user or the booley-setup skill — an
    auto-commit would need the stealth author-identity machinery and is not
    what F-5 asked for.
    """
    from booley.dev_support.commit_msg_utils import stealth_enabled

    if not stealth_enabled(ctx.project_root):
        return
    if (target / ".git").exists():
        return  # already its own repo — leave it be
    if ctx.check_only:
        warn(f"would `git init` {target} (stealth persistence, ADR 0036)")
        return
    result = subprocess.run(
        ["git", "-C", str(target), "init", "-q"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        ok(f"initialized inner git repo at {target} (versions stealth config/cores)")
    else:
        warn(f"could not `git init` {target}: {result.stderr.strip()}")


def _step_core_projections(ctx: InitContext) -> None:
    """Reconcile stealth core projections at the repository root."""
    ctx.step_banner("stealth core projections")
    enabled = projection_enabled(ctx.project_root)
    cores = authoritative_cores(ctx.project_root)
    if ctx.check_only:
        try:
            issues = projection_issues(ctx.project_root)
        except (CoreProjectionError, OSError) as exc:
            err(f"could not inspect stealth core projections: {exc}")
            ctx.record("core_projections", "err", str(exc))
            return
        if issues:
            warn(f"would reconcile projected cores: {', '.join(issues)}")
            ctx.record("core_projections", "warn", f"{len(issues)} change(s)")
        else:
            skip("stealth core projections already reconciled")
            ctx.record("core_projections", "skip", "current")
        return

    add_git_excludes(ctx.project_root, [PROJECTED_CORE_GLOB])
    try:
        result = reconcile_projected_cores(ctx.project_root)
    except (CoreProjectionError, OSError) as exc:
        err(f"could not reconcile stealth core projections: {exc}")
        ctx.record("core_projections", "err", str(exc))
        return
    changed = len(result.written) + len(result.removed)
    if not enabled or not cores:
        note = "stealth disabled" if not enabled else "no authored stealth cores"
        skip(f"core projection inactive ({note})")
    else:
        ok(f"projected {len(cores)} stealth core(s) into the repository root")
    ctx.record("core_projections", "ok", f"{changed} change(s)")


# ---------------------------------------------------------------------------
# Init step: tickets tree (record key: tickets)
# ---------------------------------------------------------------------------


def _step_tickets(ctx: InitContext) -> None:
    ctx.step_banner("tickets tree")

    tickets_dir = resolve_project_dir(ctx.project_root) / "tickets"

    required = [tickets_dir / "board" / state for state in BOARD_STATES]
    required.append(tickets_dir / "logs")
    required.append(tickets_dir / "locks")

    missing = [p for p in required if not p.is_dir()]
    if not missing:
        skip(f"tickets tree already present ({len(required)} dirs)")
        ctx.record("tickets", "skip", "already present")
        return

    if ctx.check_only:
        warn(f"{len(missing)} missing directories (would create)")
        ctx.record("tickets", "warn", f"{len(missing)} dirs missing")
        return

    for p in required:
        p.mkdir(parents=True, exist_ok=True)
    ok(f"created tickets tree ({len(required)} dirs)")
    ctx.record("tickets", "ok", "created")


# ---------------------------------------------------------------------------
# Init step: agent authentication (record key: auth)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentSelection:
    """The explicit provider and billing policy initialization must persist."""

    provider: str
    auth: str
    write_provider: bool = False
    write_auth: bool = False


def _agent_config_path(project_root: Path) -> Path:
    """Return the resolved project config, or the future default on first init."""
    try:
        project_dir = resolve_checkout_project_dir(project_root)
    except FileNotFoundError:
        project_dir = project_root / PROJECT_DIR_NAME
    return project_dir / "booley.toml"


def _read_agent_selection(path: Path) -> tuple[str | None, str | None]:
    """Read declared selection fields, failing loud on malformed configuration."""
    if not path.is_file():
        return None, None
    with path.open("rb") as config_file:
        data = tomllib.load(config_file)
    agent = data.get("agent", {})
    if not isinstance(agent, dict):
        raise ValueError("booley.toml [agent] must be a table")
    from booley.config.agent import parse_auth, parse_provider

    return parse_provider(agent), parse_auth(agent)


def _prompt_agent_choice(label: str, choices: tuple[str, ...]) -> str | None:
    """Prompt for one required choice; an empty answer is never a default."""
    options = "/".join(choice.replace("_", "-") for choice in choices)
    for _attempt in range(3):
        try:
            value = (
                input(f"  Select agent {label} ({options}): ").strip().lower().replace("-", "_")
            )
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if value in choices:
            return value
        info(f"choose one of: {options.replace('/', ', ')} (there is no default)")
    err(f"agent {label} selection aborted after 3 invalid answers")
    return None


def _resolve_selection_value(
    ctx: InitContext,
    *,
    name: str,
    configured: str | None,
    requested: str | None,
    choices: tuple[str, ...],
) -> tuple[str | None, bool]:
    """Resolve one field and whether it must be added to booley.toml."""
    if configured is not None:
        if requested is not None and requested != configured:
            err(
                f"--{name}={requested} conflicts with booley.toml [agent] "
                f"{name}={configured!r}; edit the project config to change it"
            )
            return None, False
        return configured, False
    if requested is not None:
        return requested, True
    if ctx.interactive:
        return _prompt_agent_choice(name, choices), True
    err(
        f"agent {name} is not configured; unattended init requires "
        f"--{name} {{{','.join(choice.replace('_', '-') for choice in choices)}}}"
    )
    return None, False


def _resolve_agent_selection(
    ctx: InitContext, args: argparse.Namespace
) -> tuple[AgentSelection, Path] | None:
    """Resolve flags/config/prompts without guessing either agent setting."""
    config_path = _agent_config_path(ctx.project_root)
    try:
        provider, auth = _read_agent_selection(config_path)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        err(f"cannot select an agent from {config_path}: {exc}")
        ctx.record("agent_config", "err", "invalid booley.toml")
        return None
    provider, write_provider = _resolve_selection_value(
        ctx,
        name="provider",
        configured=provider,
        requested=getattr(args, "provider", None),
        choices=("claude", "codex"),
    )
    if provider is None:
        ctx.record("agent_config", "err", "explicit provider/auth required")
        return None
    auth, write_auth = _resolve_selection_value(
        ctx,
        name="auth",
        configured=auth,
        requested=getattr(args, "auth", None),
        choices=("auto", "subscription", "api_key"),
    )
    if auth is None:
        ctx.record("agent_config", "err", "explicit provider/auth required")
        return None
    return AgentSelection(provider, auth, write_provider, write_auth), config_path


def _insert_agent_fields(content: str, fields: list[str]) -> str:
    """Insert missing fields into an existing [agent] table, preserving the file."""
    lines = content.splitlines(keepends=True)
    header = next(
        (index for index, line in enumerate(lines) if line.split("#", 1)[0].strip() == "[agent]"),
        None,
    )
    if header is None:
        prefix = "" if not content or content.endswith("\n") else "\n"
        separator = "" if not content else "\n"
        return content + prefix + separator + "[agent]\n" + "".join(fields)
    end = next(
        (
            index
            for index in range(header + 1, len(lines))
            if lines[index].lstrip().startswith("[")
        ),
        len(lines),
    )
    insertion = "".join(fields)
    if end > 0 and not lines[end - 1].endswith(("\n", "\r")):
        insertion = "\n" + insertion
    lines[end:end] = [insertion]
    return "".join(lines)


def _step_agent_config(ctx: InitContext, selection: AgentSelection, path: Path) -> bool:
    """Persist a complete [agent] choice before auth checks and Session seeding."""
    ctx.step_banner("agent provider and authentication policy")
    fields = []
    if selection.write_provider:
        fields.append(f'provider = "{selection.provider}"\n')
    if selection.write_auth:
        fields.append(f'auth = "{selection.auth}"\n')
    if not fields:
        skip(f"using existing [agent] {selection.provider}/{selection.auth}")
        ctx.record("agent_config", "skip", f"{selection.provider}/{selection.auth}")
        return True
    if ctx.check_only:
        warn(f"would record [agent] {selection.provider}/{selection.auth} in {path}")
        ctx.record("agent_config", "warn", f"{selection.provider}/{selection.auth}")
        return True
    if not path.parent.is_dir():
        err(f"cannot persist agent selection: project directory is missing at {path.parent}")
        ctx.record("agent_config", "err", "project directory missing")
        return False
    content = path.read_text(encoding="utf-8") if path.is_file() else ""
    updated = _insert_agent_fields(content, fields)
    path.write_text(updated, encoding="utf-8")
    ok(f"recorded [agent] {selection.provider}/{selection.auth} in {path}")
    ctx.record("agent_config", "ok", f"{selection.provider}/{selection.auth}")
    return True


def _detect_auth_mode(provider: str, policy: str = "auto") -> str | None:
    """The auth mode *provider*'s agents would actually run (and bill) under.

    Resolved with the agent CLI's own precedence (env API key first, then the
    rotation-free token, then the refreshing login file — see
    :func:`auth_token.effective_credential`), NOT by mere presence of the login
    file: with both an exported API key and a subscription login, the key is
    what bills, and reporting "subscription" here would misstate that.
    """
    effective = auth_token.effective_credential(provider, policy)
    return effective.mode if effective else None


def _check_provider_creds(provider: str, auth_mode: str, policy: str = "auto") -> None:
    """Name the winning credential's source, and anything it silently outranks."""
    effective = auth_token.effective_credential(provider, policy)
    if effective is None or effective.mode != auth_mode:
        # The environment changed between detection and reporting — say nothing
        # rather than something stale.
        return
    ok(f"using {effective.source}")
    for loser in effective.overridden:
        info(f"{loser} is present but NOT used — {effective.source} takes precedence")


def _step_auth(ctx: InitContext, selection: AgentSelection) -> None:
    ctx.step_banner("agent authentication")
    info(f"configured agent: {selection.provider}/{selection.auth}")
    mode = _detect_auth_mode(selection.provider, selection.auth)
    if mode:
        ok(f"detected {mode} auth for {selection.provider}")
        _check_provider_creds(selection.provider, mode, selection.auth)
        ctx.record("auth", "ok", f"{selection.provider}/{mode}")
        return
    key_var = "ANTHROPIC_API_KEY" if selection.provider == "claude" else "OPENAI_API_KEY"
    login = "claude login" if selection.provider == "claude" else "codex login"
    warn(
        f"no {selection.auth} auth available for {selection.provider} — "
        f"set {key_var} or run '{login}' as appropriate"
    )
    ctx.record("auth", "warn", f"{selection.provider}/{selection.auth} unavailable")


# ---------------------------------------------------------------------------
# Init step: required host bootstrap tools (record key: eda_tools)
# ---------------------------------------------------------------------------


def _docker_daemon_error(executable: str) -> str | None:
    """Return a fatal Docker availability error, or ``None`` when ready."""
    try:
        result = subprocess.run(
            [executable, "info"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "docker daemon did not respond within 10 seconds"
    except (subprocess.SubprocessError, OSError) as exc:
        return f"could not contact docker daemon: {exc}"
    if result.returncode == 0:
        return None
    detail = (result.stderr or result.stdout).strip().splitlines()
    suffix = f": {detail[0][:200]}" if detail else ""
    return f"docker daemon is not running or not accessible{suffix}"


def _report_required_tool(name: str, version_arg: str, purpose: str) -> str | None:
    """Report one required executable and return its resolved path."""
    found = shutil.which(name)
    if not found:
        err(f"{name:<8} missing  (REQUIRED — {purpose})")
        return None
    try:
        out = subprocess.run(
            [found, version_arg],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        version = (out.stdout or out.stderr).strip().splitlines()[0][:60]
    except (subprocess.SubprocessError, IndexError):
        version = "(version probe failed)"
    ok(f"{name:<8} {version}")
    return found


def _report_vscode() -> bool:
    """Report VS Code availability and return whether a usable install exists."""
    state, detail = _detect_vscode()
    if state == "cli":
        ok(f"{'vscode':<8} {detail}  (Interactive Mode: Reopen in Container)")
        return True
    if state == "gui":
        warn(
            f"vscode found ({detail}) but the 'code' CLI is not on PATH — "
            "run 'Shell Command: Install code command in PATH' from VS Code"
        )
        return True
    err(
        "vscode  missing  (REQUIRED — Interactive Mode uses "
        "'Reopen in Container'; https://code.visualstudio.com)"
    )
    return False


def _step_eda_tool_detection(ctx: InitContext) -> bool:
    """Check host tools and report whether Docker is ready for initialization."""
    ctx.step_banner("host bootstrap tool detection")
    git = _report_required_tool("git", "--version", "version control")
    docker = _report_required_tool("docker", "--version", "container runtime for all EDA tools")
    daemon_error = _docker_daemon_error(docker) if docker else None
    if daemon_error:
        err(daemon_error)
    docker_ready = docker is not None and daemon_error is None
    vscode_ready = _report_vscode()
    any_missing = git is None or not docker_ready or not vscode_ready
    detail = "docker unavailable" if not docker_ready else ""
    ctx.record("eda_tools", "err" if any_missing else "ok", detail)
    return docker_ready


# ---------------------------------------------------------------------------
# Init step: project sandbox image (bakes the repo's Python deps) — ADR 0018
# (record key: project_image)
# ---------------------------------------------------------------------------


def _load_sandbox_config(toml_path: Path) -> dict:
    """Read the ``[sandbox]`` table from booley.toml, or {} if unreadable."""
    if toml_path.is_file():
        try:
            with toml_path.open("rb") as f:
                return tomllib.load(f).get("sandbox", {}) or {}
        except (OSError, tomllib.TOMLDecodeError):
            return {}
    return {}


def _resolve_baked_requirements(ctx, sandbox: dict) -> tuple[str, list] | None:
    """Resolve project requirements to bake into the sandbox image.

    Returns ``(dockerfile_body, kept_requirements)`` or ``None`` when there is
    nothing to bake (having already emitted the appropriate skip/record).
    """
    pip_req = sandbox.get("pip_requirements")
    pip_req_list = [str(x) for x in pip_req] if isinstance(pip_req, list) else None
    req_files, missing = pi.resolve_requirements(ctx.project_root, pip_req_list)
    for m in missing:
        warn(f"[sandbox].pip_requirements: {m} not found — skipping")
    if not req_files:
        skip("no [sandbox].pip_requirements to bake — using base sandbox image")
        ctx.record("project_image", "skip", "no requirements")
        return None

    rels = ", ".join(p.relative_to(ctx.project_root).as_posix() for p in req_files)
    info(f"project Python deps from: {rels}")

    body, kept, skipped, dropped_managed = pi.consolidated_requirements(
        ctx.project_root, req_files
    )
    for s in skipped:
        warn(f"skipping non-bakeable requirement (install at runtime): {s}")
    for d in dropped_managed:
        warn(f"dropping pin on Booley-managed package (image version wins): {d}")
    if not kept:
        skip("no PyPI-installable requirements to bake — using base sandbox image")
        ctx.record("project_image", "skip", "only local/editable deps")
        return None
    _report_curated_overrides(kept)
    return body, kept


def _report_curated_overrides(kept: list[str]) -> None:
    """Name the project pins that re-version a package the base image curates.

    Projects own their sim stack, so these pins are honored — that is the
    design. They were also completely silent: a ``cocotb 2.0.1 -> 1.5.1``
    downgrade got baked with no output, and the base Dockerfile's cocotb/VPI
    validation layer does not re-run on the project layer, so the first sign is
    a simulator behaving unlike the documented one (F-13). One info line per
    overridden package, no verdict attached.

    Advisory by contract: if the base image can't be queried (not built yet,
    docker unavailable) there is simply nothing to say.
    """
    base_versions = pi.base_image_packages()
    if not base_versions:
        return
    for req, name, base_version in pi.curated_overrides(kept, base_versions):
        info(f"pin '{req}' overrides the base image's {name}=={base_version} (project wins)")


def _selected_image_handled(ctx: InitContext, sandbox: dict, generated: str) -> bool:
    """Dispatch on an explicit ``[sandbox].image``; True when the step is done here.

    Three kinds of explicit selection:

    - a Booley-SHIPPED flavor (:data:`FLAVOR_IMAGES`) — Booley's own image, so
      init builds and refreshes it here just like the base. It used to land in
      the user-managed branch purely because its name isn't the generated one,
      which meant init rebuilt the base for 20 minutes and left the image the
      project actually runs stranded on the base's previous layers.
    - a genuinely user-managed image — hands off, as before.
    - the generated or base name — not handled here; the caller builds it.
    """
    configured = sandbox.get("image")
    if not (isinstance(configured, str) and configured.strip()):
        return False
    selected = configured.strip()
    if selected in FLAVOR_IMAGES:
        if ensure_flavor_image(ctx, selected):
            _warn_on_live_session_on_old_image(ctx, selected)
        return True
    if selected not in (generated, pi.BASE_IMAGE, DOCKER_IMAGE):
        skip(f"[sandbox].image={selected!r} is user-managed — project image skipped")
        ctx.record("project_image", "skip", "user-managed image")
        return True
    return False


def _project_image_setup_gate(  # noqa: PLR0911 — each early return is a distinct image-ownership/build gate
    ctx: InitContext,
) -> tuple[Path, Path, dict, str, str | None, bool] | None:
    """Run the early docker/project-dir/user-managed-image skip checks.

    Returns ``(project_dir, toml_path, sandbox, generated, configured,
    hand_authored)`` when the build should proceed, or ``None`` after the step
    is already accounted for: no docker, no project dir, a Booley-shipped
    sandbox flavor (handled here in full), a genuinely user-managed image, or
    hand-edited docker files whose image is already built (SETUP-6).
    ``hand_authored`` is True when the docker files are user-owned but their
    image is absent, so the step must build from them verbatim (F-5) instead
    of regenerating.
    """
    if not shutil.which("docker"):
        skip("docker not on PATH — project image skipped")
        ctx.record("project_image", "skip", "docker missing")
        return None
    try:
        project_dir = resolve_project_dir(ctx.project_root)
    except FileNotFoundError:
        skip("no .booley_project — project image skipped")
        ctx.record("project_image", "skip", "no project dir")
        return None

    toml_path = project_dir / "booley.toml"
    sandbox = _load_sandbox_config(toml_path)

    generated = pi.project_image_name(ctx.project_root)
    configured = sandbox.get("image")
    if _selected_image_handled(ctx, sandbox, generated):
        return None

    # Never clobber hand-edited docker files (SETUP-6): re-running init used to
    # silently overwrite a customized Dockerfile/requirements.txt and print
    # [OK]. If either is user-owned, leave both alone — but only skip the build
    # when the image they describe actually exists. Skipping with NO image
    # dead-ended the natural "drop a requirements.txt, re-run init" path (F-5):
    # the user had to `docker build` and set [sandbox].image by hand.
    docker_dir = project_dir / "docker"
    dockerfile = docker_dir / "Dockerfile"
    parent = pi.dockerfile_parent_image(dockerfile)
    parent_refreshed = False
    if parent in FLAVOR_IMAGES:
        result_count = len(ctx.results)
        parent_refreshed = ensure_flavor_image(ctx, parent)
        if parent_refreshed:
            _warn_on_live_session_on_old_image(ctx, parent)
        parent_result = ctx.results[-1] if len(ctx.results) > result_count else None
        if parent_result is not None and parent_result.status in {"warn", "err"}:
            return None

    user_owned = [
        p.name
        for p in (docker_dir / "Dockerfile", docker_dir / "requirements.txt")
        if not pi.is_managed_generated_file(p)
    ]
    if user_owned:
        owned = f"docker/{{{', '.join(user_owned)}}}"
        if idk.image_exists(generated):
            image_stale = source_fingerprint_mismatch(generated) is True
            if not parent_refreshed and not image_stale:
                warn(
                    f"manual edits detected in {owned} — leaving "
                    "them and the existing image untouched. Delete them (or set "
                    "[sandbox].image to your own image) to resume managed regeneration; "
                    "run `docker build` yourself to rebuild from your edits."
                )
                ctx.record("project_image", "skip", "manual edits preserved")
                return None
            reason = (
                f"its Booley-managed parent '{parent}' was refreshed"
                if parent_refreshed
                else "its inherited Booley provenance is stale"
            )
            info(
                f"manual edits detected in {owned}; rebuilding '{generated}' "
                f"from those files because {reason}"
            )
            return project_dir, toml_path, sandbox, generated, configured, True
        info(
            f"manual edits detected in {owned} and image '{generated}' is not "
            "built — using your files as the build input (leaving them untouched)"
        )
        return project_dir, toml_path, sandbox, generated, configured, True

    return project_dir, toml_path, sandbox, generated, configured, False


def _warn_on_live_session_on_old_image(ctx: InitContext, image: str) -> None:
    """Warn when a live Session Runtime still serves the pre-rebuild image (F-9).

    Rebuilding only moves the tag: a container created from the previous image
    keeps running it, so the change just baked in is absent inside the session
    the user is actually working in. Because the tag is unchanged, nothing looks
    wrong — the fix simply "doesn't work", and the hunt goes to the wrong layer
    (the devcontainer-image-drift trap, one door over). Advisory only: killing a
    live session out from under the user is not init's call.
    """
    # Deferred import: session_runtime is the container-lifecycle module and it
    # imports back into init_cmd (project_sandbox_image), so keep it lazy.
    from booley.harness import session_runtime as sr

    for name in sr.sessions_on_stale_image(ctx.project_root, image):
        warn(
            f"the running session container '{name}' was created from the previous "
            f"{image} image and keeps serving it — this rebuild is invisible inside "
            "it. Run `booley session down && booley session up` to restart the "
            "session on the image just built (in VS Code: Reopen in Container / "
            "Rebuild Container)."
        )


def _build_and_configure_image(
    ctx: InitContext,
    docker_dir: Path,
    generated: str,
) -> None:
    """Build the derived project image; its name is resolved automatically."""
    if not pi.build_project_image(generated, docker_dir, verbose=ctx.verbose):
        err(f"failed to build {generated} — re-run with -v for full output")
        ctx.record("project_image", "err", "build failed")
        return
    ok(f"built {generated}")
    _warn_on_live_session_on_old_image(ctx, generated)

    ctx.record("project_image", "ok", generated)


def _build_hand_authored_image(
    ctx: InitContext,
    project_dir: Path,
    generated: str,
) -> None:
    """Build the project image from user-owned docker/ files, verbatim (F-5).

    Reached when ``docker/{Dockerfile,requirements.txt}`` carry manual edits
    but the image they describe was never built (fresh clone/machine, or the
    files were hand-authored before any build). The user's files are the build
    input as-is; only a *missing* Dockerfile is backfilled with the managed one
    so a lone hand-authored requirements.txt is still buildable. Once the image
    exists, re-running init lands on the usual manual-edits skip path — so the
    step stays idempotent.
    """
    docker_dir = project_dir / "docker"
    if ctx.check_only:
        warn(f"would build {generated} from the hand-edited docker/ files")
        ctx.record("project_image", "warn", "would build")
        return
    if not (docker_dir / "Dockerfile").is_file():
        pi.write_managed_dockerfile(docker_dir)
        info("generated the managed docker/Dockerfile around your requirements.txt")
    _build_and_configure_image(ctx, docker_dir, generated)


def _step_project_image(ctx: InitContext) -> None:
    """Build a project image baking the repo's Python deps into the sandbox.

    The base image is project-agnostic and runtime egress is locked down, so a
    project's ``requirements.txt`` deps must be baked at build time. Bakes the
    files listed in ``[sandbox].pip_requirements`` (nothing is auto-discovered),
    builds ``<slug>-booley-sandbox``; that generated name is selected
    automatically. A
    user-set custom ``[sandbox].image`` disables this; hand-edited docker files
    are never regenerated, but are built from as-is when their image is absent
    (F-5).
    """
    ctx.step_banner("project sandbox image")

    gate = _project_image_setup_gate(ctx)
    if gate is None:
        return
    project_dir, _toml_path, sandbox, generated, _configured, hand_authored = gate

    if hand_authored:
        _build_hand_authored_image(ctx, project_dir, generated)
        return

    baked = _resolve_baked_requirements(ctx, sandbox)
    if baked is None:
        return
    body, kept = baked

    if ctx.check_only:
        warn(f"would build {generated} with {len(kept)} requirement(s)")
        ctx.record("project_image", "warn", "would build")
        return

    docker_dir = project_dir / "docker"
    pi.write_project_image_files(docker_dir, body)
    _build_and_configure_image(ctx, docker_dir, generated)


def _step_sandbox_images(ctx: InitContext) -> None:
    """Prepare only the runtime image chain selected by this project."""
    selected = project_sandbox_image(ctx.project_root)
    if selected not in FLAVOR_IMAGES:
        _step_docker_image(ctx, selected)
        _step_project_image(ctx)
        return

    ctx.step_banner("project sandbox image")
    changed = ensure_flavor_image(
        ctx,
        selected,
        ensure_base=lambda: _step_docker_image(ctx, selected),
    )
    if changed:
        _warn_on_live_session_on_old_image(ctx, selected)


# ---------------------------------------------------------------------------
# Sandbox image resolution for the Interactive Mode devcontainer.
# ---------------------------------------------------------------------------


def project_sandbox_image(project_root: Path) -> str:
    """Return the project-selected sandbox image, or the base image.

    Public: this is what the generated devcontainer spec's ``image`` is derived
    from, so ``booley session up`` compares against it to detect spec-vs-toml
    image drift (F-6).
    """
    try:
        project_dir = resolve_project_dir(project_root)
    except FileNotFoundError:
        return DOCKER_IMAGE
    toml_path = project_dir / "booley.toml"
    if not toml_path.is_file():
        return DOCKER_IMAGE
    try:
        with toml_path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return DOCKER_IMAGE
    raw = data.get("sandbox", {}).get("image", "")
    if isinstance(raw, str) and raw.strip():
        return raw
    dockerfile = project_dir / "docker" / "Dockerfile"
    if dockerfile.is_file():
        return pi.project_image_name(project_root)
    return DOCKER_IMAGE


def refresh_session_image(project_root: Path, *, verbose: bool = False) -> str:
    """Rebuild the configured Session Runtime image from current Booley sources.

    This is the implementation behind ``booley session refresh``. It reuses
    init's image builders with ``force=True`` but never rewrites booley.toml or
    the devcontainer spec. Booley-owned base/flavor images and the generated
    project image are reproducible here; an arbitrary explicit image remains
    user-managed and is rejected with an actionable error.
    """
    selected = project_sandbox_image(project_root)
    generated = pi.project_image_name(project_root)
    managed = {DOCKER_IMAGE, pi.BASE_IMAGE, generated, *FLAVOR_IMAGES}
    if selected not in managed:
        raise RuntimeError(
            f"[sandbox].image={selected!r} is user-managed, so Booley has no "
            "build recipe to refresh. Rebuild that image yourself, then run "
            "`booley session up --rebuild`."
        )

    ctx = InitContext(project_root=project_root, force=True, verbose=verbose)
    _step_docker_image(ctx, selected)
    if any(result.status == "err" for result in ctx.results):
        raise RuntimeError("base sandbox image refresh failed")

    # A generated image with hand-authored docker/ files is still rebuildable;
    # unlike init's conservative rerun path, refresh is an explicit request to
    # build those files verbatim. It must not regenerate or overwrite them.
    if selected == generated:
        project_dir = resolve_project_dir(project_root)
        docker_dir = project_dir / "docker"
        user_owned = any(
            path.is_file() and not pi.is_managed_generated_file(path)
            for path in (docker_dir / "Dockerfile", docker_dir / "requirements.txt")
        )
        if user_owned:
            ctx.step_banner("project sandbox image")
            if not (docker_dir / "Dockerfile").is_file():
                raise RuntimeError(
                    f"cannot refresh {selected}: {docker_dir / 'Dockerfile'} is missing"
                )
            if not pi.build_project_image(selected, docker_dir, verbose=verbose):
                raise RuntimeError(f"failed to rebuild {selected}")
            ok(f"rebuilt {selected} from the hand-authored docker/ recipe")
            ctx.record("project_image", "ok", selected)
        else:
            _step_project_image(ctx)
    elif selected in FLAVOR_IMAGES:
        # Build the selected shipped flavor after its refreshed base.
        _step_project_image(ctx)

    if any(result.status == "err" for result in ctx.results):
        raise RuntimeError(f"sandbox image refresh failed for {selected}")
    return selected


def reissue_session_spec(project_root: Path, *, verbose: bool = False) -> None:
    """Regenerate, pin, and stamp the Session spec after an image refresh."""
    ctx = InitContext(project_root=project_root, force=False, verbose=verbose)
    pdk_root = _step_nangate_pdk(ctx)
    _step_interactive(ctx, nangate_pdk_root=pdk_root)
    failures = [result.detail for result in ctx.results if result.status == "err"]
    if failures:
        raise RuntimeError("Session Runtime spec reissuance failed: " + "; ".join(failures))


def _project_sandbox_memory(project_root: Path) -> str:
    """Return the project's single container memory limit (ADR 0028), or ''.

    Parsed through ``_parse_sandbox_config`` so legacy tier tables /
    ``memory_tiers`` keys get their retirement warning here too.
    """
    toml_path = project_root / ".booley_project" / "booley.toml"
    if not toml_path.is_file():
        return ""
    try:
        with toml_path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    from booley.config.agent import _parse_sandbox_config

    return _parse_sandbox_config(data).memory


def _project_mask_paths(project_root: Path) -> list[str]:
    """Validated ``[sandbox].mask_paths`` — workspace subtrees to hide.

    Workspace-root-relative POSIX paths the Session Runtime must NOT see
    (oracle artifacts, competing lanes, private notes): each becomes a
    read-only bind of an always-empty host dir over the path's container view
    (both views, for a ``.booley_project/`` subtree — see
    :func:`booley.harness.devcontainer._mask_mounts`). An invalid knob is
    reported and ignored AS A WHOLE. A partially applied mask list would leave
    the user believing a path is
    hidden when it is not, which is worse than masking nothing loudly.
    """
    toml_path = project_root / ".booley_project" / "booley.toml"
    raw = _load_sandbox_config(toml_path).get("mask_paths")
    error = dc.mask_paths_error(raw)
    if error:
        err(f"{error} — no paths will be masked from the container")
        return []
    return list(raw or [])


def _mask_source_dir() -> Path:
    """Host path of the always-empty dir ``mask_paths`` binds over targets.

    Lives under Booley's per-user config dir (beside the ``booley auth``
    store) so it is host-global, never inside a workspace, and can never
    accumulate content of its own. Pure path math — ``_step_interactive``
    mkdirs it right before writing the spec, because docker's ``--mount``
    (unlike ``-v``) refuses a missing bind source at container create.
    """
    return auth_token.config_dir() / "empty-mask"


def _detect_claude_code() -> bool:
    """Heuristic: Claude Code is installed if its config dir or CLI exists."""
    if (Path.home() / ".claude").is_dir():
        return True
    return shutil.which("claude") is not None


def _detect_codex() -> bool:
    """Heuristic: Codex is installed if its config dir or CLI exists."""
    if (Path.home() / ".codex").is_dir():
        return True
    return shutil.which("codex") is not None


# Per-platform user config dir names that indicate a GUI install even when the
# `code` CLI shim was never added to PATH (a common macOS/Windows situation).
_VSCODE_CONFIG_NAMES = ("Code", "Code - Insiders", "VSCodium", "Cursor", "Windsurf")


def _vscode_config_dirs() -> list[Path]:
    """Return candidate VS Code(-family) user config dirs for this platform."""
    home = Path.home()
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = home / ".config"
    return [base / name for name in _VSCODE_CONFIG_NAMES]


def _detect_vscode() -> tuple[str, str]:
    """Detect a VS Code(-family) editor for Interactive Mode.

    Returns ``(state, detail)`` where *state* is one of:
      - ``"cli"``     — a ``code``-style CLI is on PATH (best); *detail* is its
                        ``--version`` line.
      - ``"gui"``     — only a GUI config dir was found; the CLI shim isn't on
                        PATH. *detail* is the discovered dir name.
      - ``"missing"`` — no VS Code(-family) install found; *detail* is ``""``.
    """
    from booley.config.editor import resolve_editor_command

    found = resolve_editor_command()
    if found:
        try:
            out = subprocess.run(
                [found, "--version"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            version = (out.stdout or out.stderr).strip().splitlines()[0][:60]
        except (subprocess.SubprocessError, IndexError):
            version = "(version probe failed)"
        return "cli", f"{Path(found).name} {version}"
    for cfg in _vscode_config_dirs():
        if cfg.is_dir():
            return "gui", cfg.name
    return "missing", ""


def _select_interactive_app(project_root: Path | None = None) -> str:
    """Return the project's declared agent app, never one inferred from the host."""
    if project_root is not None:
        from booley.config.agent import _VALID_PROVIDERS, _parse_provider
        from booley.core.config_paths import resolve_booley_toml

        config_path = resolve_booley_toml(project_root)
        if config_path.is_file():
            try:
                import tomllib

                with config_path.open("rb") as config_file:
                    config = tomllib.load(config_file)
                agent = config.get("agent", {})
                provider = _parse_provider(agent if isinstance(agent, dict) else {})
            except (OSError, ValueError):
                provider = None
            if provider in _VALID_PROVIDERS:
                return provider
    return dc.APP_NONE


def _resolve_auth_token_source(app: str) -> Path | None:
    """Return the host path to *app*'s subscription auth token, if it exists.

    Mounted read-only into the container (ADR 0018). Returns ``None`` for
    API-key auth or ``none`` (nothing to mount).
    """
    creds = {
        dc.APP_CLAUDE: Path.home() / ".claude" / ".credentials.json",
        dc.APP_CODEX: Path.home() / ".codex" / "auth.json",
    }.get(app)
    return creds if creds and creds.is_file() else None


def _resolve_token_seed_source(app: str) -> Path | None:
    """Return the host path of *app*'s stored rotation-free credential, if any.

    The file ``booley auth`` writes (``~/.config/booley/...``, mode 0600).
    Mounted read-only at the app's token-seed sidecar so
    ``incontainer_register`` can apply it on every container start — the only
    route that reaches VS Code's "Reopen in Container", which resolves the
    spec's ``${localEnv:...}`` against VS Code's own process env where the
    stored file is invisible. Gated on the file actually existing: a bind
    mount whose source is missing fails container creation outright.
    """
    if app not in auth_token.CREDENTIALS:
        return None
    return auth_token.token_path(app) if auth_token.read_stored_token(app) else None


def _resolve_config_seed_source(app: str) -> Path | None:
    """Return the host ``~/.claude.json`` to seed the container's user config.

    Claude Code caches statsig feature-gate grants (staged-rollout / promo model
    access such as Fable), the install stableID, and onboarding state in
    ``~/.claude.json`` — a file that sits *outside* the persisted ``~/.claude/``
    volume, so the container is born without it and gated models drop off the
    in-container picker. Seeding the container's copy from the host carries the
    grants across (mounted read-only + copied; the host file is never written).
    Only Claude keeps this cache; returns ``None`` for other apps or when the
    host config is absent (e.g. a machine that never ran Claude Code).
    """
    if app != dc.APP_CLAUDE:
        return None
    cfg = Path.home() / ".claude.json"
    return cfg if cfg.is_file() else None


def _report_seeded_mounts(
    app: str,
    seeding_auth: bool,
    seeding_config: bool,
    host_skills: list[tuple[str, str]],
    mask_paths: list[str],
) -> None:
    """Print the ``ok`` lines for what the seeded devcontainer spec mounts."""
    if seeding_auth:
        ok(f"mounting {app} auth token read-only")
    if seeding_config:
        ok("seeding ~/.claude.json (carries feature-gate grants, e.g. Fable)")
    if host_skills:
        ok(
            f"mounting {len(host_skills)} host skill(s) read-only "
            f"({', '.join(name for name, _ in host_skills)})"
        )
    if mask_paths:
        ok(
            f"masking {len(mask_paths)} path(s) from the container: "
            f"{', '.join(mask_paths)} (read-only empty binds over /work — and "
            "over /booley-project for .booley_project subtrees)"
        )


def _resolve_host_skills_sources(project_root: Path) -> list[tuple[str, str]]:
    """Return ``(name, docker_source)`` pairs for the user's HOST agent skills.

    Gated on ``[sandbox] mount_host_skills``. Scans the two host skill dirs an
    agent app reads — ``~/.claude/skills`` (Claude) and ``~/.agents/skills``
    (Codex) — and, for every entry that resolves to a real skill directory
    (has a ``SKILL.md``), returns its REAL path for a read-only bind. Symlinks
    are resolved on the host here precisely because the container never mounts
    their targets: binding ``~/.claude/skills`` (all symlinks in a typical
    install) verbatim would dangle every entry.

    Booley's own built-ins are excluded two ways — by name and by resolved
    path under the packaged skills dir — so the in-image copy is never shadowed
    by a stale host symlink, and the container's built-ins always win. Entries
    are de-duplicated by skill name (first dir scanned wins) and by real path.
    """
    raw = _load_sandbox_config(project_root / ".booley_project" / "booley.toml")
    if not bool(raw.get("mount_host_skills", False)):
        return []

    home = Path.home()
    builtin_dir = skills_dir().resolve() if skills_dir().is_dir() else None
    builtin_names = {d.name for d in skills_dir().iterdir()} if builtin_dir else set()

    pairs: list[tuple[str, str]] = []
    seen_names: set[str] = set(builtin_names)
    seen_paths: set[Path] = set()
    # Claude's dir first, then Codex's — a name in both resolves to Claude's copy.
    for skills_dir_host in (home / ".claude" / "skills", home / ".agents" / "skills"):
        if not skills_dir_host.is_dir():
            continue
        for entry in sorted(skills_dir_host.iterdir()):
            if entry.name in seen_names:
                continue
            try:
                real = entry.resolve(strict=True)  # follows symlinks; skips dangling
            except (OSError, RuntimeError):
                continue
            if not real.is_dir() or not (real / "SKILL.md").is_file():
                continue
            # A host link into the packaged skills dir is a built-in already baked
            # into the image — never re-mount it from the host.
            if builtin_dir and (real == builtin_dir or builtin_dir in real.parents):
                continue
            if real in seen_paths:
                continue
            seen_names.add(entry.name)
            seen_paths.add(real)
            pairs.append((entry.name, docker_mount_path(real)))
    return pairs


def _devcontainer_is_tracked(project_root: Path) -> bool:
    """True if ``.devcontainer/`` is tracked by git (must not be clobbered)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "ls-files", "--", ".devcontainer"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def _booley_repo_root() -> Path | None:
    """Locate sidecar build assets in a checkout or installed package."""
    docker_data = docker_data_dir()
    repo_root = docker_data.parent.parent.parent.parent
    if (repo_root / "pyproject.toml").is_file():
        return repo_root

    package_root = docker_data.parent.parent
    if (package_root / "docker").is_dir():
        return package_root
    return None


def _ensure_sidecar_image(
    booley_root: Path | None,
    ensure: Callable[..., bool],
    *,
    image: str,
    force: bool,
) -> bool:
    """Run a sidecar image build while keeping its diagnostic user-visible."""
    if booley_root is None:
        if idk.image_exists(image):
            return True
        warn(f"could not locate packaged sidecar build assets for {image}")
        return False
    try:
        return ensure(booley_root, force=force)
    except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
        warn(str(exc))
        return False


def _report_sidecar_unavailable(image: str, impact: str) -> None:
    warn(f"{image} unavailable — {impact}")
    info("  fix the build error above, then retry: booley init --seed")


def _ensure_egress_sidecar(
    ctx: InitContext, booley_root: Path | None, cfg: InteractiveConfig, notes: list[str]
) -> None:
    ready = _ensure_sidecar_image(
        booley_root,
        idk.ensure_egress_proxy_image,
        image=idk.PROXY_IMAGE,
        force=ctx.force,
    )
    if not ready:
        _report_sidecar_unavailable(
            "egress-proxy image", "Session Runtime has no model-service egress"
        )
        notes.append("proxy:skipped")
        return
    status = idk.ensure_egress_proxy(allowlist=cfg.egress_allowlist or None)
    ok(f"booley-proxy {status}")
    notes.append(f"proxy:{status}")


def _ensure_reaper_sidecar(
    ctx: InitContext, booley_root: Path | None, cfg: InteractiveConfig, notes: list[str]
) -> None:
    ready = _ensure_sidecar_image(
        booley_root,
        idk.ensure_reaper_image,
        image=idk.REAPER_IMAGE,
        force=ctx.force,
    )
    if not ready:
        _report_sidecar_unavailable(
            "reaper image", "idle timeout and maximum-session enforcement are unavailable"
        )
        notes.append("reaper:skipped")
        return
    status = idk.ensure_reaper(cfg, force=ctx.force)
    ok(f"booley-reaper {status} (idle={cfg.idle_timeout_seconds}s, max={cfg.max_sessions})")
    notes.append(f"reaper:{status}")


def _ensure_interactive_docker(ctx: InitContext, *, license_required: bool = False) -> list[str]:
    """Create the long-lived egress + reaper Docker objects. Returns status notes."""
    notes: list[str] = []
    booley_root = _booley_repo_root()
    cfg = load_interactive_config(ctx.project_root)

    if license_required:
        from booley.eda.flexnet_docker import ensure_relay_image

        if ensure_relay_image(force=ctx.force):
            ok("built booley-flexnet-relay image")
            notes.append("license-relay-image:built")
        else:
            skip("booley-flexnet-relay image already present")
            notes.append("license-relay-image:present")

    # Egress network + dual-homed proxy.
    if idk.ensure_egress_network():
        ok(f"created {idk.EGRESS_NETWORK} network (--internal, host-isolated)")
        notes.append("network:created")
    else:
        skip(f"{idk.EGRESS_NETWORK} network already present")
    _ensure_egress_sidecar(ctx, booley_root, cfg, notes)
    _ensure_reaper_sidecar(ctx, booley_root, cfg, notes)

    return notes


def _cleanup_unlicensed_relay(project_root: Path) -> bool:
    """Remove deterministic relay leftovers when a reseed no longer has a profile."""
    from booley.eda.flexnet_docker import remove_relay, resources_for_session

    resources = resources_for_session(str(project_root.resolve()))
    exists = (
        idk.container_exists(resources.relay_container)
        or idk.network_exists(resources.private_network)
        or idk.network_exists(resources.outbound_network)
    )
    if not exists:
        return False
    remove_relay(resources)
    return True


_NANGATE_PDK_NOT_REQUESTED = object()


def _step_nangate_pdk(ctx: InitContext) -> Path | None:
    """Prepare the pinned, user-owned Nangate45 cache used by synthesis."""
    ctx.step_banner("Nangate45 synthesis library")
    root = nangate_pdk.cache_root()
    issues = nangate_pdk.validation_errors(root)
    if not issues:
        skip(f"verified Nangate45 cache at {root}")
        ctx.record("nangate_pdk", "skip", str(root))
        return root

    info("Nangate45 is an optional upstream download; Booley does not redistribute it.")
    info("  License: non-commercial use; comparison with other libraries is restricted.")
    info(f"  Terms:   {nangate_pdk.LICENSE_ID} (included beside the downloaded files)")
    if ctx.check_only:
        warn(f"would download and verify {len(nangate_pdk.FILES)} pinned files into {root}")
        ctx.record("nangate_pdk", "warn", "; ".join(issues))
        return root

    try:
        nangate_pdk.fetch(root)
    except nangate_pdk.NangatePdkError as exc:
        err(f"could not prepare Nangate45 synthesis library: {exc}")
        ctx.record("nangate_pdk", "err", str(exc))
        return None

    ok(f"downloaded and verified Nangate45 at {root}")
    ctx.record("nangate_pdk", "ok", str(root))
    return root


def _step_interactive(  # noqa: PLR0911,PLR0912 - ordered setup boundary
    ctx: InitContext,
    *,
    nangate_pdk_root: Path | object | None = _NANGATE_PDK_NOT_REQUESTED,
    agent_app: str | None = None,
) -> None:
    """Seed the untracked devcontainer spec + long-lived Docker objects (ADR 0018)."""
    ctx.step_banner("Interactive Mode (Reopen in Container)")

    if (
        ctx.check_only
        and "BOOLEY_PROJECT_DIR" not in os.environ
        and not (ctx.project_root / ".booley_project").is_dir()
    ):
        warn("would seed the Session Runtime after creating the private project directory")
        ctx.record("interactive", "warn", "project directory would be created first")
        return

    if _devcontainer_is_tracked(ctx.project_root):
        err(".devcontainer/ is tracked by git — refusing to clobber it")
        info("  Interactive Mode is unavailable for this repo until it is removed")
        ctx.record("interactive", "err", "tracked .devcontainer")
        return

    if nangate_pdk_root is None:
        err("Session Runtime not seeded because the Nangate45 setup download failed")
        ctx.record("interactive", "err", "Nangate45 cache unavailable")
        return

    app = agent_app or _select_interactive_app(ctx.project_root)
    from booley.eda import authority as eda_authority
    from booley.eda import runtime_spec as eda_runtime_spec
    from booley.eda.config import EdaConfigError
    from booley.eda.flexnet_docker import RelayDockerError
    from booley.eda.vivado import CONTAINER_TARGET

    try:
        project_data_source = eda_runtime_spec.authorized_project_data_source(ctx.project_root)
        _, installation = eda_runtime_spec.requested_host_installation(ctx.project_root)
        license_profile = eda_runtime_spec.requested_license(ctx.project_root)
        if (
            license_profile is None
            and not ctx.check_only
            and shutil.which("docker")
            and _cleanup_unlicensed_relay(ctx.project_root)
        ):
            ok("removed orphaned license relay from unlicensed Project")
    except (
        EdaConfigError,
        eda_authority.AuthorityError,
        eda_runtime_spec.RuntimeSpecError,
        RelayDockerError,
    ) as exc:
        err(f"commercial EDA authorization failed closed: {exc}")
        ctx.record("interactive", "err", str(exc))
        return
    trusted_eda_mounts = (
        [(installation.source, CONTAINER_TARGET)] if installation is not None else []
    )
    if isinstance(nangate_pdk_root, Path):
        trusted_eda_mounts.append(
            (docker_mount_path(nangate_pdk_root), nangate_pdk.CONTAINER_ROOT)
        )
    fixed_container_env = (
        {"XILINXD_LICENSE_FILE": (f"{license_profile.lmgrd_port}@booley-license-xilinx")}
        if license_profile is not None
        else None
    )
    auth_source = _resolve_auth_token_source(app)
    token_seed = _resolve_token_seed_source(app)
    config_seed = _resolve_config_seed_source(app)
    host_skills = _resolve_host_skills_sources(ctx.project_root)
    mask_paths = _project_mask_paths(ctx.project_root)
    if mask_paths and not ctx.check_only:
        # The mask binds' empty source dir must exist before `docker run`:
        # --mount (unlike -v) hard-fails on a missing bind source. Created
        # here, not in the spec builder, so build_devcontainer_spec stays pure.
        _mask_source_dir().mkdir(parents=True, exist_ok=True)
    spec = dc.build_devcontainer_spec(
        app,
        image=project_sandbox_image(ctx.project_root),
        project_dir_source=docker_mount_path(project_data_source),
        project_id=dc.canonical_project_id(ctx.project_root),
        # docker_mount_path keeps every mount source in ONE path style — the
        # project-dir source above already goes through it, and a Windows spec
        # mixing /c/... with C:\... styles reads as accidental (F-8).
        auth_token_source=docker_mount_path(auth_source) if auth_source else None,
        config_seed_source=docker_mount_path(config_seed) if config_seed else None,
        mcp_start_command=dc.mcp_post_start_command(),
        memory=_project_sandbox_memory(ctx.project_root),
        # Only reference the app's credential when one actually resolves now, so
        # an empty ${localEnv:...} can't shadow the mounted subscription creds. A
        # credential stored by `booley auth` counts: resolve_token checks the env
        # var first, then Booley's store. Per-app: Claude's setup-token, Codex's
        # API key.
        forward_oauth_token=bool(auth_token.resolve_token(app)),
        # The stored credential additionally rides in as a read-only sidecar
        # mount: the ${localEnv:...} route above never reaches VS Code's
        # "Reopen in Container" (VS Code resolves localEnv against its own
        # process env), so incontainer_register applies the mounted copy on
        # every container start.
        token_seed_source=docker_mount_path(token_seed) if token_seed else None,
        # [sandbox].mount_host_skills: the user's HOST agent skills, resolved to
        # their real dirs and mounted read-only for use alongside the built-ins.
        host_skills=host_skills,
        trusted_eda_mounts=trusted_eda_mounts,
        protected_devcontainer_source=docker_mount_path(
            ctx.project_root.resolve() / ".devcontainer"
        ),
        fixed_container_env=fixed_container_env,
        # [sandbox].mask_paths: workspace subtrees hidden from the Session
        # Runtime via read-only empty binds over both container views.
        mask_paths=mask_paths,
        mask_source=docker_mount_path(_mask_source_dir()) if mask_paths else "",
        local_timezone=detect_host_timezone(),
    )

    try:
        eda_runtime_spec.pin_image(spec)
        eda_runtime_spec.seal(ctx.project_root, spec)
    except eda_runtime_spec.RuntimeSpecError as exc:
        err(f"could not pin Session Runtime image: {exc}")
        ctx.record("interactive", "err", str(exc))
        return

    if ctx.check_only:
        warn(
            "would write .devcontainer/devcontainer.json + exclude Booley files "
            "and run outputs (build/, util/)"
        )
        warn(f"would create {idk.EGRESS_NETWORK} network, booley-proxy, booley-reaper")
        if license_profile is not None:
            warn("would build the pinned booley-flexnet-relay image if absent")
        if host_skills:
            warn(f"would mount {len(host_skills)} host skill(s) read-only into the sandbox")
        if mask_paths:
            warn(
                f"would mask {len(mask_paths)} path(s) from the container: {', '.join(mask_paths)}"
            )
        ctx.record("interactive", "warn", f"app={app} (check-only)")
        return

    relay_image_built = False
    if license_profile is not None:
        from booley.eda.flexnet_docker import ensure_relay_image

        try:
            relay_image_built = ensure_relay_image(force=ctx.force)
        except RelayDockerError as exc:
            err(f"could not prepare immutable FlexNet relay image: {exc}")
            ctx.record("interactive", "err", str(exc))
            return

    # Write the untracked spec and hide it (+ .booley_project, .claude) from git history.
    path = dc.write_devcontainer(ctx.project_root, spec)
    try:
        eda_runtime_spec.issue(ctx.project_root, spec, path)
    except eda_runtime_spec.RuntimeSpecError as exc:
        path.unlink(missing_ok=True)
        err(f"could not issue Session Runtime specification: {exc}")
        ctx.record("interactive", "err", str(exc))
        return
    ok(f"wrote {path.relative_to(ctx.project_root)} (app={app})")
    _report_seeded_mounts(app, bool(auth_source), bool(config_seed), host_skills, mask_paths)
    # Only Booley-owned top-level artifacts. A bare `build`/`util` entry would
    # match ANY dir of that name at any depth (git exclude semantics), silently
    # swallowing files in repos that own those names (Ibex owns util/*.py) —
    # SETUP-20. Booley's own Flow and Specialist outputs live under .booley_project/.runtime/
    # (covered by the .booley_project entry; asic_synthesize is redirected there
    # by SETUP-27), so we no longer need to hide root build/ or util/.
    add_git_excludes(
        ctx.project_root,
        [".devcontainer", ".booley_project", ".claude"],
    )
    ok("excluded .devcontainer/, .booley_project/, .claude/ from git (info/exclude)")

    notes = [f"app={app}"]
    if not shutil.which("docker"):
        warn("docker not on PATH — Reopen in Container will not work until installed")
        ctx.record("interactive", "warn", ", ".join(notes) + ", docker missing")
        return
    try:
        notes += _ensure_interactive_docker(
            ctx,
            license_required=False,
        )
        if license_profile is not None:
            state = "built" if relay_image_built else "present"
            notes.append(f"license-relay-image:{state}")
    except RuntimeError as exc:
        err(f"failed to create Docker objects: {exc}")
        ctx.record("interactive", "err", ", ".join(notes) + f", {exc}")
        return

    status = "warn" if any(":skipped" in n for n in notes) else "ok"
    ctx.record("interactive", status, ", ".join(notes))


# ---------------------------------------------------------------------------
# Advisories
# ---------------------------------------------------------------------------


#: Builtin flows with no ``[flows.<flow>]`` wiring of their own, so the advisory
#: below must not nag about them: elaborate follows ``[flows.sim]``'s
#: selection and has no menu of its own (see doctor's
#: ``_EXECUTION_VALIDATING_TOOLS``).
_FLOWS_WITHOUT_OWN_WIRING = frozenset({"elab"})

#: Builtin flows booley-setup triages and wires, in display order.
SETUP_WIRED_FLOWS = ("sim", "lint", "synth", "fpga")


def _build_setup_step_lines() -> tuple[tuple[str, str], ...]:
    """(step key, advisory line) pairs, ordered by ascending booley-setup step.

    The Flow and Specialist wirings are all part of Step 2 (project config), so they precede
    Step 3 (F-1). Column widths are computed, not typed, so adding a flow can't
    silently misalign the list.
    """
    what = {
        "project": "records project name, sources, configs",
        "agents": "writes the project's AGENTS.md guide",
    }
    heads = {
        "project": "Step 2 (project config)",
        "agents": "Step 3 (AGENTS.md)",
    }
    for flow in SETUP_WIRED_FLOWS:
        heads[flow] = f"Step 2 ({flow.replace('_', ' ')})"
        what[flow] = f"configure [flows.{flow}] and its .core Target metadata"
    order = ("project", *SETUP_WIRED_FLOWS, "agents")
    width = max(len(heads[key]) for key in order)
    return tuple((key, f"{heads[key]:<{width}} - {what[key]}") for key in order)


#: The booley-setup steps init can *probe*, paired with the line it prints while
#: one is outstanding.
_SETUP_STEP_LINES = _build_setup_step_lines()


def _setup_step_done(key: str, project_root: Path, data: dict) -> bool:
    """Is booley-setup step *key* already done, judged from what's on disk?

    Evidence per step, rather than one "was this a fresh init?" flag: a flag
    would print the whole list or none of it, and the half-finished project is
    exactly the one that needs the list. For a Flow or Specialist line, an **absent**
    ``[flows.<name>]`` section is outstanding but an explicit
    ``enabled = false`` is not — doctor reads that as a deliberate "Flow or Specialist
    disabled" (ADR 0039), not an unset knob, so nagging about it would tell
    the user to undo their own choice.
    """
    if key == "project":
        project = data.get("project")
        name = project.get("name", "") if isinstance(project, dict) else ""
        return bool(str(name).strip())
    if key == "agents":
        return (project_root / ".booley_project" / "AGENTS.md").is_file()
    flows = data.get("flows") if isinstance(data.get("flows"), dict) else {}
    from booley.targets.flow_names import LEGACY_TO_CANONICAL, config_section

    section = config_section(flows, key)
    section_present = key in flows or any(
        old in flows and new == key for old, new in LEGACY_TO_CANONICAL.items()
    )
    if not section_present:
        return False
    if section.get("enabled") is False:
        return True
    from booley.fusesoc import fusesoc_registry

    if key != "fpga":
        return bool(fusesoc_registry.doctor_target_selectors(project_root, key))
    from booley.targets.target_surface import flow_can_drive

    return any(
        flow_can_drive("fpga", ref)
        for ref in fusesoc_registry.enumerate_targets(project_root).values()
    )


def _outstanding_setup_steps(project_root: Path) -> list[str]:
    """The booley-setup step lines this project has not satisfied yet."""
    from booley.config.settings import _load_booley_toml

    data = _load_booley_toml(project_root)
    return [
        line for key, line in _SETUP_STEP_LINES if not _setup_step_done(key, project_root, data)
    ]


def _print_configured_advisory(ctx: InitContext) -> None:
    """Send-off for a project whose .booley_project/ is already fully set up.

    The common case for a demo/clone: every setup step's evidence is on disk, so
    listing the booley-setup steps contradicts the user's own repo. All that is
    left is verification — and the doctor stamp already knows whether that has
    happened recently, so say which.
    """
    info("This project is already configured — booley.toml is populated, AGENTS.md is")
    info(
        "written, and every enabled Flow and Specialist is wired. No booley-setup steps are outstanding."
    )
    print()
    try:
        nag = doctor_stamp.check_stamp(resolve_project_dir(ctx.project_root), ctx.project_root)
    except (FileNotFoundError, OSError):
        nag = None  # advisory by contract — never let the stamp break init
    if nag:
        info(f"  * {nag}")
    else:
        info("  * `booley doctor` last ran clean against this config — you're good to go")
    info("  * Ensure git working tree is clean (no in-progress rebase/merge/cherry-pick)")


_DEMO_PROJECT_ORIGIN = "github.com/boldaxolotl/booley-prj-picorv32"


def _normalize_demo_origin(origin: str) -> str:
    """Normalize common GitHub remote URL forms for an exact repository comparison."""
    normalized = origin.strip().lower().replace("\\", "/").rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    for prefix in ("https://", "http://", "ssh://", "git://"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    return normalized.removeprefix("git@").replace("github.com:", "github.com/", 1)


def _is_demo_project(project_root: Path) -> bool:
    """Whether the project state is the published PicoRV32 demo checkout."""
    try:
        project_dir = resolve_project_dir(project_root)
    except FileNotFoundError:
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        warn(f"could not inspect PicoRV32 demo origin at {project_dir}: git timed out after 5s")
        return False
    except (FileNotFoundError, OSError) as exc:
        warn(f"could not inspect PicoRV32 demo origin at {project_dir}: {exc}")
        return False
    if result.returncode != 0:
        if error := result.stderr.strip():
            warn(
                f"could not inspect PicoRV32 demo origin at {project_dir}: "
                f"git exited {result.returncode}: {error}"
            )
        return False
    return _normalize_demo_origin(result.stdout) == _DEMO_PROJECT_ORIGIN


def _print_demo_advisory() -> None:
    """Send the preconfigured demo straight to its documented runtime steps."""
    info("This is the preconfigured PicoRV32 demo — the booley-setup skill does not apply.")
    print()
    info('  * Open the PicoRV32 folder in VS Code and choose "Reopen in Container"')
    info("  * In the container, run `bash .booley_project/hooks/post-setup.sh`")
    info("  * Then run `booley doctor --deep`; use booley-heal if it reports warnings")


def _failed_step_names(ctx: InitContext) -> list[str]:
    """Names of the steps that recorded an error so far, in run order."""
    return [r.name for r in ctx.results if r.status == "err"]


def _print_incomplete_advisory(failed: list[str]) -> None:
    """Send-off for a run where a required step failed (fpu F-2).

    After the mandatory Docker preflight, later steps deliberately keep running
    after a failure — every init step is idempotent, so finishing the pass
    leaves the project as far along as it can get and a re-run picks up the
    rest. What must NOT happen is the normal
    "now run booley-setup" send-off: the project is not ready, and the summary
    above it is mostly green, so the only signal was the final one-line rc=2.
    """
    info(f"init did not complete: {len(failed)} step(s) failed — {', '.join(failed)}.")
    info("The remaining steps still ran (every step is idempotent), but this project")
    info("is NOT ready for the booley-setup skill yet.")
    print()
    info("  * Fix the [XX] step(s) reported in the summary below")
    info("  * Re-run `booley init` — completed steps are no-ops on a second pass")
    info("  * Only then start the booley-setup skill (Step 0, the plan phase)")


def _print_success_advisory(ctx: InitContext, *, demo: bool, scaffolded: bool) -> str:
    """Print the successful-run send-off and return its summary detail."""
    if demo:
        _print_demo_advisory()
        return "demo"
    if scaffolded:
        info("Scaffolded starter project: booley.toml/tests.toml are populated and")
        info(
            "every enabled Flow and Specialist is already wired — most booley-setup steps don't apply:"
        )
        print()
        info("  * Step 3 (AGENTS.md) - writes the project's AGENTS.md guide (recommended)")
        info("  * Step 4 (doctor)    - `booley doctor --deep` should be green as scaffolded")
        info("  * Commit the scaffolded files and keep the working tree clean")
        return "scaffold"
    outstanding = _outstanding_setup_steps(ctx.project_root)
    if not outstanding:
        _print_configured_advisory(ctx)
        return "configured"
    info("Before running the harness, finish project setup with the booley-setup skill")
    info("(it starts with Step 0, the plan phase, here on the host):")
    print()
    for line in outstanding:
        info(f"  * {line}")
    info("  * Step 4 (doctor)          - final doctor + deep doctor audit")
    info("  * Ensure git working tree is clean (no in-progress rebase/merge/cherry-pick)")
    return ""


def _step_advisories(ctx: InitContext) -> None:
    ctx.step_banner("post-setup advisories")
    # A failed required step outranks every other send-off: routing the user to
    # booley-setup after an image build died reads as "ready" (fpu F-2).
    failed = _failed_step_names(ctx)
    if failed:
        _print_incomplete_advisory(failed)
        print()
        info("Optional:")
        info("  * Notifications: set [notifications] ntfy_topic in booley.toml")
        ctx.record("advisories", "ok", "incomplete")
        return
    demo = _is_demo_project(ctx.project_root)
    # A scaffolded project needs a different send-off: --scaffold already wrote
    # a populated booley.toml with every enabled Flow and Specialist already wired up, so
    # pointing the user at Steps 4-6 ("enable the disabled Flows and Specialists") contradicts
    # what just happened on their disk (SETUP.md: such a project "typically
    # only wants Step 3").
    scaffolded = any(r.name == "scaffold" and r.status in ("ok", "warn") for r in ctx.results)
    detail = _print_success_advisory(ctx, demo=demo, scaffolded=scaffolded)
    print()
    info("Optional:")
    info("  * Notifications: set [notifications] ntfy_topic in booley.toml")
    ctx.record("advisories", "ok", detail)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _print_summary(ctx: InitContext) -> int:
    banner("Summary")
    exit_code = 0
    _STATUS_FN = {"ok": green, "skip": accent, "warn": yellow, "err": red}
    for r in ctx.results:
        glyph = {"ok": "[OK]", "skip": "[--]", "warn": "[!!]", "err": "[XX]"}[r.status]
        suffix = f" — {r.detail}" if r.detail else ""
        print(f"  {_STATUS_FN[r.status](glyph)} {r.name}{suffix}")
        if r.status == "err":
            exit_code = 2

    print()
    if exit_code != 0:
        print(red("Setup incomplete — fix the errors above and re-run."))
        return exit_code

    # The send-off has to match what the advisories step just printed. Telling a fully
    # configured project (a demo repo cloned with its .booley_project/ intact)
    # to "finish the setup skills above" contradicts the empty step list right
    # above it, and reads as if init left work undone.
    advisory = next((r for r in ctx.results if r.name == "advisories"), None)
    if advisory is None:
        # --seed: no advisories step ran, so there is nothing "above" to finish.
        print(green("Booley base setup complete."))
    elif advisory.detail == "demo":
        print(green("Booley demo setup complete."))
        print(green('Next: open the PicoRV32 folder in VS Code and choose "Reopen in Container".'))
    elif advisory.detail == "configured":
        print(green("Booley setup complete — this project is ready."))
        print(green('Next: open this repo in VS Code and choose "Reopen in Container".'))
    else:
        # Setup starts with the skill's plan phase, which runs on the HOST; the
        # skill itself says when to move into the container (only its execution
        # steps need the Session Runtime toolchain).
        print(green("Booley base setup complete. Run the booley-setup skill from your agent"))
        print(green("chat here on the host — it plans setup with you first, then tells you"))
        print(green('when to "Reopen in Container" for the remaining steps.'))
    return exit_code


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

BOOLEY_MASCOT = r"""
    ____              _
   | __ )  ___   ___ | | ___ _   _
   |  _ \ / _ \ / _ \| |/ _ \ | | |
   | |_) | (_) | (_) | |  __/ |_| |
   |____/ \___/ \___/|_|\___|\__, |
                             |___/
"""


def _step_guidance_links(ctx: InitContext, planned: InitPlan | None = None) -> None:
    """Create/repair the root AGENTS.md & CLAUDE.md links, host-side (F-13).

    The project dir lives at ``/booley-project`` inside the Session Runtime, so
    a link written relative to that mount dangles on the host. A host-side
    editor or agent then reads no guidance at all. ``ensure_guidance_links``
    targets a repo-local path valid on both host and runtime and falls back to a
    hardlink/copy where Windows forbids symlinks.

    Skipped before the guidance file exists (booley-setup Step 3 authors it).
    """
    ctx.step_banner("guidance links")

    project_dir = ctx.project_root / ".booley_project"
    if not (project_dir / "AGENTS.md").is_file():
        skip("no AGENTS.md yet — run the booley-setup skill (Step 3, guidance)")
        ctx.record("guidance_links", "skip", "no guidance file")
        return

    try:
        plan = planned or plan_guidance_links(ctx.project_root, project_dir)
    except (OSError, FileNotFoundError) as exc:
        warn(f"could not inspect guidance links: {exc}")
        ctx.record("guidance_links", "warn", "inspection failed")
        return

    if plan.blockers:
        detail = "; ".join(f"{action.path.name}: {action.blocker}" for action in plan.blockers)
        warn(f"guidance links blocked: {detail}")
        ctx.record("guidance_links", "warn", detail)
        return

    pending = [action for action in plan.actions if action.mutation.value != "none"]
    if ctx.check_only:
        if pending:
            warn("would create/repair root AGENTS.md and CLAUDE.md links")
            ctx.record("guidance_links", "warn", "would link")
        else:
            ok("root guidance links are current")
            ctx.record("guidance_links", "ok", "current")
        return

    try:
        links = ensure_guidance_links(ctx.project_root, project_dir, plan=plan)
    except InitPreconditionError as exc:
        err(f"guidance filesystem changed before apply: {exc}")
        ctx.record("guidance_links", "err", "filesystem precondition changed")
        return
    except (OSError, FileNotFoundError) as exc:
        warn(f"could not create guidance links: {exc}")
        ctx.record("guidance_links", "warn", "link failed")
        return

    ok(f"root guidance links point at {project_dir / 'AGENTS.md'}")
    for link in links:
        info(f"  {link.name}")
    ctx.record("guidance_links", "ok", f"{len(links)} link(s)")


def _run_seed(ctx: InitContext, selection: AgentSelection) -> int:
    """Seed only the Interactive Mode devcontainer for this folder/worktree.

    Used per user-created worktree and per Ticket Mode worktree: the long-lived
    Docker objects are global (re-ensured idempotently here), but each session
    folder needs its own untracked ``.devcontainer/`` and exclude entry, since a
    folder without the seeded config will not offer "Reopen in Container".
    """
    if not ctx.check_only and sys.stdout.isatty():
        print(bold_chrome(f"  Seeding Interactive Mode for {ctx.project_root}"))
        print()
    if (
        "BOOLEY_PROJECT_DIR" not in os.environ
        and not (ctx.project_root / ".booley_project").is_dir()
    ):
        _step_interactive(ctx, agent_app=selection.provider)
        return _print_summary(ctx)
    pdk_root = _step_nangate_pdk(ctx)
    _step_interactive(ctx, nangate_pdk_root=pdk_root, agent_app=selection.provider)
    return _print_summary(ctx)


def _configure_progress_output() -> None:
    """Make redirected initialization progress visible without delay."""
    # Line-buffer stdout so progress (esp. the multi-minute docker build) streams
    # when piped/redirected. Python block-buffers a non-TTY stdout, which made
    # init look hung for minutes with no output (SETUP-2).
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]


def _print_init_banner(ctx: InitContext) -> None:
    """Print the interactive initialization heading."""
    if ctx.check_only or not sys.stdout.isatty():
        return
    print(BOOLEY_MASCOT)
    print(bold_chrome("  Booley project setup"))
    print()
    info(f"project root  : {ctx.project_root}")
    info(f"version       : {__version__}")
    info(f"platform      : {'Windows' if IS_WINDOWS else 'POSIX'}")


def run_init(  # noqa: PLR0911 - each return is a fail-fast initialization boundary
    args: argparse.Namespace, project_root: Path
) -> int:
    """Run the project initialization wizard."""
    _configure_progress_output()
    ctx = InitContext(
        project_root=project_root,
        check_only=getattr(args, "check_only", False),
        force=getattr(args, "force", False),
        verbose=getattr(args, "verbose", False),
        fix_line_endings=getattr(args, "fix_line_endings", False),
    )
    _print_init_banner(ctx)

    resolved_selection = _resolve_agent_selection(ctx, args)
    if resolved_selection is None:
        return _print_summary(ctx)
    selection, agent_config_path = resolved_selection

    # Docker is the execution substrate for every supported EDA flow. Fail
    # before creating or changing project files when its daemon is unavailable;
    # a half-initialized tree cannot be used and obscures the real prerequisite.
    if not _step_eda_tool_detection(ctx):
        return _print_summary(ctx)

    guidance_plan = None
    canon = ctx.project_root / ".booley_project" / "AGENTS.md"
    if canon.is_file():
        try:
            guidance_plan = plan_guidance_links(
                ctx.project_root,
                ctx.project_root / ".booley_project",
            )
        except (OSError, FileNotFoundError, ValueError) as exc:
            err(f"initialization filesystem inspection failed: {exc}")
            ctx.record("filesystem_plan", "err", "inspection failed")
            return _print_summary(ctx)
        if guidance_plan.blockers:
            for action in guidance_plan.blockers:
                err(f"initialization blocked by {action.path}: {action.blocker}")
            ctx.record("filesystem_plan", "err", "guidance ownership conflict")
            return _print_summary(ctx)

    if getattr(args, "seed", False):
        if not _step_agent_config(ctx, selection, agent_config_path):
            return _print_summary(ctx)
        return _run_seed(ctx, selection)

    # --scaffold: emit a runnable starter IP (RTL + TB + .core + populated
    # config) before the regular steps, which then backfill around it. A
    # refusal (existing design files) aborts the whole run — the user asked
    # for a fresh scaffold and must decide, not get a half-initialized mix.
    if getattr(args, "scaffold", None) and not step_scaffold(ctx, args):
        return _print_summary(ctx)

    _step_project_dir(ctx)
    if not _step_agent_config(ctx, selection, agent_config_path):
        return _print_summary(ctx)
    _step_core_projections(ctx)
    _step_tickets(ctx)
    _step_auth(ctx, selection)
    _deploy_skills(ctx)
    pdk_root = _step_nangate_pdk(ctx)
    _step_sandbox_images(ctx)
    _step_git_hooks(ctx)
    _step_project_git_hooks(ctx)
    _step_worktree_prune_guard(ctx)
    _step_line_endings(ctx)
    _step_guidance_links(ctx, guidance_plan)
    _step_interactive(ctx, nangate_pdk_root=pdk_root, agent_app=selection.provider)
    _step_advisories(ctx)

    return _print_summary(ctx)
