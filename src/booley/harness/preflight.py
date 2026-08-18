"""Preflight checks -- fast-fail before any ticket work begins.

Runs at the very top of run_ticket(), before ticket intake.
These are environment/repo sanity checks that don't need a ticket context.
"""

from __future__ import annotations

import ast
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from booley import runtime_context

logger = logging.getLogger(__name__)


class PreflightError(Exception):
    """Raised when preflight checks fail -- execution should not start."""

    def __init__(self, failures: list[str]) -> None:
        self.failures = failures
        msg = "Preflight failed:\n  " + "\n  ".join(failures)
        super().__init__(msg)


def run_preflight(project_root: Path) -> None:
    """Run all preflight checks. Raises PreflightError on failure.

    Checks (in order):
      0. Running inside the Session Runtime (Ticket Mode is container-only)
      1. .tickets/ directory exists
      2. Git is available and we're in a repo
      3. Dirty working tree warning (non-blocking)
      4. No in-progress git operations (merge/rebase/cherry-pick)
      5. ticket_board package is importable
      6. FuseSoC core-tree setup hazards
      7. Custom MCP endpoints & criteria validation
      8. Agent backend health (warning only)
    """
    failures: list[str] = []

    # 0. Ticket Mode is container-only (ADR 0028): every ticket runs inside
    # the Session Runtime alongside the interactive session. Fail loud here
    # with the fix rather than later with a confusing path/Flow error.
    _check_inside_container()

    # 1. Tickets directory
    from booley.ticket_board.helpers import tickets_dir_from_project_root

    tickets_dir = tickets_dir_from_project_root(project_root)
    if not tickets_dir.is_dir():
        failures.append(f"tickets directory not found at {tickets_dir}")

    # 2-4. Git checks
    failures.extend(_check_git(project_root))

    # 5. ticket_board reachable
    failures.extend(_check_ticket_board(project_root))

    # 6. Conditions that otherwise hang or force network access during setup.
    failures.extend(_check_core_setup_hazards(project_root))

    if failures:
        raise PreflightError(failures)

    # 7. Custom MCP endpoints & criteria validation
    _validate_custom_endpoints_and_criteria(project_root)

    # 8. Active agent backend health (warning only)
    _check_agent_backend()

    logger.info("Preflight OK")


def _check_inside_container() -> None:
    """Refuse to start a ticket run anywhere but the Session Runtime.

    Booley is container-only (ADR 0028): tickets execute inside the same
    devcontainer as the interactive session — one runtime, one filesystem, one
    slot store. A host-side `booley run` would race the container over the
    same `.booley_project/` state through different path roots.
    """
    error = runtime_context.container_only_error("booley run")
    if error is not None:
        raise PreflightError([error])


def _check_git(project_root: Path) -> list[str]:
    """Check git availability, dirty tree, and in-progress operations."""
    git_cwd = str(project_root)

    # Git available?
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=git_cwd,
            check=False,
        )
        if result.returncode != 0:
            return ["Not inside a git work tree"]
    except FileNotFoundError:
        return ["git not found on PATH"]
    except subprocess.TimeoutExpired:
        return ["git timed out -- possible filesystem issue"]

    _warn_dirty_tree(git_cwd)
    return _check_in_progress_ops(git_cwd, project_root)


def _warn_dirty_tree(git_cwd: str) -> None:
    """Warn (non-blocking) if the working tree has uncommitted changes."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--ignore-submodules"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=git_cwd,
            check=False,
        )
        if result.returncode == 0:
            dirty = [ln for ln in result.stdout.strip().split("\n") if ln.strip()]
            if dirty:
                preview = dirty[:5]
                suffix = f" (and {len(dirty) - 5} more)" if len(dirty) > 5 else ""
                file_list = ", ".join(ln.strip() for ln in preview) + suffix
                logger.warning(
                    "Dirty working tree (%d modified files): %s  "
                    "-- proceeding anyway (worktree will use committed state)",
                    len(dirty),
                    file_list,
                )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def _check_in_progress_ops(git_cwd: str, project_root: Path) -> list[str]:
    """Return errors if a merge/rebase/cherry-pick is in progress."""
    errors: list[str] = []
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=git_cwd,
            check=False,
        )
        if result.returncode == 0:
            git_dir = Path(result.stdout.strip())
            if not git_dir.is_absolute():
                git_dir = project_root / git_dir
            conflict_markers = {
                "merge": git_dir / "MERGE_HEAD",
                "rebase": git_dir / "rebase-merge",
                "rebase (apply)": git_dir / "rebase-apply",
                "cherry-pick": git_dir / "CHERRY_PICK_HEAD",
            }
            for state, marker in conflict_markers.items():
                if marker.exists():
                    errors.append(f"Git {state} in progress -- resolve before running Booley")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return errors


def _check_agent_backend() -> None:
    """Probe the active agent backend. Log warning if degraded."""
    try:
        from .config import get_backend_config

        cfg = get_backend_config()
        warning = cfg.active_backend.health_check()
        if warning:
            logger.warning("Agent backend (%s): %s", cfg.active_backend.name, warning)
        else:
            logger.debug("Agent backend (%s): OK", cfg.active_backend.name)
    except (ImportError, AttributeError, RuntimeError, OSError) as e:
        logger.warning("Agent backend health check failed: %s", e)


def _check_ticket_board(project_root: Path) -> list[str]:
    """Verify ticket_board package is importable."""
    errors: list[str] = []
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import booley.ticket_board"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(project_root),
            check=False,
        )
        if result.returncode != 0:
            errors.append(f"ticket_board package not importable: {result.stderr.strip()}")
    except (subprocess.TimeoutExpired, FileNotFoundError, NotADirectoryError):
        errors.append("Could not verify ticket_board package")
    return errors


def _configured_core_files(project_root: Path) -> set[Path]:
    """Core files in configured ``[flows.*].default_target`` dependency closures."""
    from booley import fusesoc_registry
    from booley.flow_names import DEFAULT_TARGET_KEY
    from booley.harness.config import _load_booley_toml

    flows = _load_booley_toml(project_root).get("flows", {})
    if not isinstance(flows, dict):
        return set()
    seeds: list[str] = []
    for section in flows.values():
        if not isinstance(section, dict):
            continue
        raw = section.get(DEFAULT_TARGET_KEY)
        if not isinstance(raw, str):
            continue
        for raw_token in raw.split(","):
            token = raw_token.strip()
            if token and token not in seeds:
                seeds.append(token)
    closure = fusesoc_registry.selectable_core_closure(project_root, seeds)
    return set(closure or ())


def _check_core_setup_hazards(project_root: Path) -> list[str]:
    """Reject recursive links and provider-backed cores selected by the project."""
    from booley import fusesoc_registry

    selected = _configured_core_files(project_root)
    state_cores = fusesoc_registry.state_cores_dir(project_root)
    failures: list[str] = []
    for hazard in fusesoc_registry.core_setup_hazards(project_root):
        rel = hazard.path.relative_to(project_root)
        if hazard.kind == "recursive-symlink":
            failures.append(
                f"FuseSoC recursive symlink {rel}: {hazard.detail}; add a "
                "FUSESOC_IGNORE marker to the containing subtree or remove the link"
            )
            continue
        owned = hazard.path in selected or state_cores in hazard.path.parents
        if owned:
            failures.append(
                f"FuseSoC core {rel} has a provider block that requests network access; "
                "remove provider: from the in-tree core"
            )
        else:
            logger.warning(
                "FuseSoC core %s has a provider block, but no configured Flow selects it",
                rel,
            )
    return failures


# ---------------------------------------------------------------------------
# Custom MCP endpoints & criteria validation
# ---------------------------------------------------------------------------


def _validate_custom_endpoints_and_criteria(project_root: Path) -> None:
    """Validate custom MCP endpoints and project criteria.

    Load order: criteria TOML first, then endpoint scan, then cross-validation.

    Per-endpoint errors (checks 1-5, 7, 9) → skip endpoint + warn.
    Structural errors (checks 6, 8) → hard-fail.
    """
    try:
        # Probe importability up front: if any of these are unavailable, skip
        # the whole validation (structural checks below re-import the same,
        # now-cached, names themselves rather than taking them as params).
        from booley.dev_support.criteria import (  # noqa: F401
            load_base_criteria,
            load_project_criteria,
            merge_criteria_defs,
        )
        from booley.mcp_tools.registry import (  # noqa: F401
            discover_mcp_tools,
            extract_mcp_tool_info,
        )
    except ImportError:
        logger.debug("Custom MCP endpoint validation skipped (imports unavailable)")
        return

    # --- Load criteria and validate structural integrity ---
    all_criteria_names = _validate_criteria_structure(project_root)

    # --- Load endpoint config and check structural endpoint errors ---
    mcp_tool_config, flow_config = _load_endpoint_config(project_root)
    custom_mcp_tools_dir = project_root / ".booley_project" / "mcp_tools"
    # --- Per-endpoint validation (warn + skip on error) ---
    if not custom_mcp_tools_dir.is_dir():
        return

    builtin_names = {t.name for t in discover_mcp_tools()}

    for py_file in sorted(custom_mcp_tools_dir.glob("*.py")):
        if py_file.stem.startswith("_"):
            continue
        _validate_single_endpoint(
            py_file, mcp_tool_config, flow_config, builtin_names, all_criteria_names
        )

    logger.debug("Custom MCP endpoint validation complete")


def _validate_criteria_structure(project_root: Path) -> set[str]:
    """Load and cross-validate criteria definitions. Returns all criteria names.

    Raises PreflightError on criteria conflicts (check 8).
    """
    from booley.dev_support.criteria import (
        load_base_criteria,
        load_project_criteria,
        merge_criteria_defs,
    )

    base_criteria = load_base_criteria()
    base_criteria_names = {c.name for c in base_criteria}

    project_criteria_path = project_root / ".booley_project" / "criteria.toml"
    project_criteria = load_project_criteria(project_criteria_path)

    _merged, merge_errors = merge_criteria_defs(base_criteria, project_criteria)
    structural_errors = [f"CRITERIA CONFLICT: {e}" for e in merge_errors]
    if structural_errors:
        raise PreflightError(structural_errors)

    return base_criteria_names | {c.name for c in project_criteria}


def _check_satisfies_refs(
    py_file: Path,
    info: Any,
    all_criteria_names: set[str],
) -> None:
    """Warn when satisfies references unknown criteria names."""
    if not info.satisfies:
        return
    for crit_name in info.satisfies:
        if crit_name not in all_criteria_names:
            logger.warning(
                "CUSTOM MCP ENDPOINT WARNING: %s — satisfies references unknown "
                "criterion '%s'. Define it in .booley_project/criteria.toml "
                "or check for typos.",
                py_file.name,
                crit_name,
            )


def _warn_retired_sandbox_attr(
    py_file: Path,
    tree: ast.Module,
) -> None:
    """Warn when a custom MCP endpoint retains retired ``sandbox`` metadata."""
    sandbox_val = _extract_sandbox_attr(tree)
    if sandbox_val is None:
        return
    logger.warning(
        "CUSTOM MCP ENDPOINT WARNING: %s — class attribute sandbox=%r is retired and ignored; "
        "delete the retired sandbox metadata; endpoints run in the Session Runtime",
        py_file.name,
        sandbox_val,
    )


def _validate_single_endpoint(
    py_file: Path,
    mcp_tool_config: dict[str, Any],
    flow_config: dict[str, Any],
    builtin_names: set[str],
    all_criteria_names: set[str],
) -> None:
    """Validate one custom MCP endpoint file; warn and skip on local errors."""
    from booley.mcp_tools.registry import extract_mcp_tool_info

    # Check 1: Parse errors
    try:
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
    except SyntaxError as e:
        logger.warning(
            "CUSTOM MCP ENDPOINT SKIPPED: %s — Python syntax error: %s", py_file.name, e
        )
        return

    # Check 3: No recognized MCP endpoint subclass
    info = extract_mcp_tool_info(py_file, builtin=False)
    if info is None:
        logger.warning(
            "CUSTOM MCP TOOL SKIPPED: %s — no McpTool/BooleyFlow/Specialist "
            "subclass found, or missing name/description",
            py_file.name,
        )
        return

    namespace = flow_config if info.kind == "flow" else mcp_tool_config
    entry = namespace.get(info.name)
    if isinstance(entry, dict) and entry.get("enabled") is False:
        return

    # Check 4: Name collision with builtin
    if info.name in builtin_names:
        logger.warning(
            "CUSTOM MCP ENDPOINT SKIPPED: %s — name '%s' conflicts with a built-in endpoint. "
            "Rename it in the class definition.",
            py_file.name,
            info.name,
        )
        return

    _check_satisfies_refs(py_file, info, all_criteria_names)
    _warn_retired_sandbox_attr(py_file, tree)

    # Check 9: Empty satisfies warning for enabled endpoints.
    if not info.satisfies:
        logger.warning(
            "CUSTOM MCP ENDPOINT WARNING: %s — enabled endpoint '%s' has "
            "satisfies=[] (no criteria declared). This may be an AST extraction "
            "limitation if satisfies uses computed values.",
            py_file.name,
            info.name,
        )


def _load_endpoint_config(project_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the ``[mcp_tools]`` and ``[flows]`` sections from booley.toml."""
    toml_path = project_root / ".booley_project" / "booley.toml"
    if not toml_path.exists():
        return {}, {}
    try:
        import tomllib

        with toml_path.open("rb") as f:
            data = tomllib.load(f)
        mcp_tools = data.get("mcp_tools", {})
        flows = data.get("flows", {})
        return (
            mcp_tools if isinstance(mcp_tools, dict) else {},
            flows if isinstance(flows, dict) else {},
        )
    except Exception as e:  # noqa: BLE001 — malformed/unreadable toml degrades to empty config so preflight continues
        logger.warning("Failed to load booley.toml: %s", e)
        return {}, {}


def _extract_sandbox_attr(tree: ast.Module) -> str | None:
    """Extract sandbox class attribute value from an AST tree."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            attr_name = None
            val = None
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                attr_name = item.target.id
                val = item.value
            elif isinstance(item, ast.Assign) and len(item.targets) == 1:
                if isinstance(item.targets[0], ast.Name):
                    attr_name = item.targets[0].id
                    val = item.value
            if (
                attr_name == "sandbox"
                and isinstance(val, ast.Constant)
                and isinstance(val.value, str)
            ):
                return val.value
    return None
