"""Edalize invocation layer for built-in Booley Flows (ADR 0019).

Booley stops hand-writing per-EDA-tool invocation (raw ``verilator``/``yosys``
command lines, ``.tcl``/``.ys`` templates) and instead resolves each run into
an **EDAM** (EDA Metadata) description that an ``edalize.flows.*`` backend
turns into the EDA-tool-specific command. Edalize owns *how an EDA tool is
invoked*;
Booley keeps everything around it:

  * EDAM generation — built here, from a resolved Booley config (0019 dec. 3).
    Phase 2 (ADR 0022) supersedes this with ``fusesoc run --setup``; the
    flow-invocation half (``configure`` + command builders) stays.
  * command execution — via ``BooleyFlow._execute`` inside the Session Runtime
    (0019 dec. 5).
  * result interpretation — sentinel scraping / metric extraction stays in each
    Flow (0019 dec. 4). This module is invocation only.

Flow-API model (edalize 0.6.8, empirically confirmed):
    edam = {
        "name": <design>,
        "files": [{"name": <abs path>, "file_type": <CAPI2 type>}, ...],
        "toplevel": <top module>,
        "parameters": {<name>: {"datatype", "paramtype", "default"}, ...},
        "flow_options": {"tool": <eda_tool>, "<eda_tool>_options": [...], ...},
    }
    flow = Sim(edam=edam, work_root=<dir>); flow.configure()
    # configure() writes <dir>/{Makefile, <name>.vc} — pure file generation,
    # in-process, inside the trust zone. Booley then runs `make` in <dir>.

Security (0019 dec. 6):
  * every file entry must resolve **under the workspace root** —
    ``EdamSecurityError`` otherwise;
  * free-form Edalize option fields (``verilator_options`` etc.) pass straight
    to the EDA tool, so only **whitelisted** ``flow_options`` keys are accepted.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

from booley.runtime.file_lock import (
    LockContentionError,
    acquire_file_lock,
    release_file_lock,
    wait_for_file_lock,
)
from booley.runtime.platform_paths import posix_relpath

logger = logging.getLogger(__name__)


class EdamSecurityError(ValueError):
    """An EDAM input violated the file-confinement or option whitelist."""


class WorkRootLeaseError(RuntimeError):
    """A mutable Edalize work root could not be leased safely."""

    def __init__(self, work_root: Path, lock_path: Path, cause: OSError) -> None:
        self.work_root = work_root
        self.lock_path = lock_path
        super().__init__(f"cannot lease {work_root} via {lock_path}: {cause}")


# ---------------------------------------------------------------------------
# CAPI2 file-type mapping
# ---------------------------------------------------------------------------

# Suffix → CAPI2 file_type. Mirrors the file kinds Booley's built-ins handle;
# unknown suffixes fall through to ``user`` (carried but untyped by Edalize).
_FILE_TYPE_BY_SUFFIX: dict[str, str] = {
    ".sv": "systemVerilogSource",
    ".svh": "systemVerilogSource",
    ".v": "verilogSource",
    ".vh": "verilogSource",
    ".vlt": "vlt",  # Verilator waiver/config file
    ".c": "cSource",
    ".cc": "cppSource",
    ".cpp": "cppSource",
    ".sdc": "SDC",
    ".xdc": "xdc",
    ".tcl": "tclSource",
}


def file_type_for(path: Path | str) -> str:
    """Return the CAPI2 ``file_type`` for *path* by suffix."""
    return _FILE_TYPE_BY_SUFFIX.get(Path(path).suffix.lower(), "user")


# ---------------------------------------------------------------------------
# flow_options whitelist (free-form pass-through is the watch-point)
# ---------------------------------------------------------------------------

# Only these keys may appear in a Target's ``flow_options``. The upstream
# literal ``tool`` selects the Edalize EDA tool; the ``*_options`` lists pass
# straight through to that EDA tool
# and so are the security-sensitive surface — anything not listed here is
# rejected rather than forwarded blindly.
_ALLOWED_FLOW_OPTIONS: dict[str, frozenset[str]] = {
    "sim": frozenset(
        {
            "tool",
            "verilator_options",
            "iverilog_options",
            "vlog_options",
            "vsim_options",
            "run_options",
            "make_options",
            "timescale",
        }
    ),
    "lint": frozenset({"tool", "verilator_options"}),
    "vivado": frozenset(
        {
            "tool",
            "part",
            "pnr",
            "synth",
            "jobs",
            "vivado-settings",
        }
    ),
    # Yosys ASIC-synth has no dedicated flow in edalize 0.6.8 → generic flow
    # with a custom node graph (Phase-0 residual). Kept permissive but bounded.
    "generic": frozenset(
        {
            "tool",
            "yosys_synth_options",
            "yosys_template",
            "yosys_as_subtool",
            "arch",
            "output_format",
            "make_options",
        }
    ),
}


def _validate_flow_options(flow: str, flow_options: dict[str, Any]) -> dict[str, Any]:
    """Reject ``flow_options`` keys outside the per-flow whitelist."""
    allowed = _ALLOWED_FLOW_OPTIONS.get(flow)
    if allowed is None:
        raise EdamSecurityError(f"unknown flow {flow!r} (no option whitelist)")
    bad = set(flow_options) - allowed
    if bad:
        raise EdamSecurityError(
            f"flow_options keys {sorted(bad)} not permitted for flow {flow!r}; "
            f"allowed: {sorted(allowed)}"
        )
    if "tool" not in flow_options:
        raise EdamSecurityError(
            f"flow_options for flow {flow!r} must name the upstream Edalize 'tool' field"
        )
    return dict(flow_options)


def _confined_path(
    path: Path,
    workspace_root: Path,
    relative_to: Path | None,
) -> str:
    """Confine *path* to *workspace_root* and return its Edalize ``name``.

    The path is always ``resolve()``-d and required to live under the
    workspace root (symlink escapes are caught because both sides are
    resolved) — this is the security check.

    When *relative_to* is given (the Edalize ``work_root``), the returned name
    is **relative** to it. That makes the generated work dir relocatable: the
    generated ``.vc``/Makefile remains relocatable within the Session Runtime
    workspace. Without it the absolute resolved path is returned.
    """
    resolved = Path(path).resolve()
    root = Path(workspace_root).resolve()
    if not resolved.is_relative_to(root):
        raise EdamSecurityError(f"file {resolved} is outside the workspace root {root}")
    if relative_to is not None:
        # This relative name lands in a .vc/Makefile consumed inside the Linux
        # Session Runtime, so it must use POSIX separators.
        return posix_relpath(resolved, Path(relative_to).resolve())
    return str(resolved)


# ---------------------------------------------------------------------------
# Parameter encoding (defines / vlogparams / plusargs → EDAM parameters)
# ---------------------------------------------------------------------------

_INT_RE = re.compile(r"^[+-]?\d+$")


def _infer_datatype(value: Any) -> tuple[str, Any]:
    """Infer an EDAM ``(datatype, value)`` pair from a Python/str value."""
    if isinstance(value, bool):
        return "bool", value
    if isinstance(value, int):
        return "int", value
    text = str(value)
    if _INT_RE.match(text):
        return "int", int(text)
    return "str", text


def _define_param(define: str) -> tuple[str, dict[str, Any]]:
    """Map a ``NAME`` or ``NAME=VALUE`` define to an EDAM vlogdefine param.

    A bare ``NAME`` becomes a boolean define (Edalize renders ``-DNAME=1``);
    ``NAME=VALUE`` carries the value text verbatim (``-DNAME=VALUE``).
    """
    if "=" in define:
        name, value = define.split("=", 1)
        datatype, val = _infer_datatype(value)
        return name.strip(), {
            "datatype": datatype,
            "paramtype": "vlogdefine",
            "default": val,
        }
    return define.strip(), {
        "datatype": "bool",
        "paramtype": "vlogdefine",
        "default": True,
    }


def _vlogparam(value: Any) -> dict[str, Any]:
    """Map a parameter value to an EDAM ``vlogparam`` (``-GNAME=VALUE``)."""
    datatype, val = _infer_datatype(value)
    return {"datatype": datatype, "paramtype": "vlogparam", "default": val}


def _plusarg(value: Any) -> dict[str, Any]:
    """Map a run-time plusarg to an EDAM ``plusarg`` parameter (``+NAME=v``).

    A value of ``None`` declares the plusarg without a default (the Target
    surface advertises it; the run stage supplies the value). Otherwise the
    value is baked in — re-``configure()`` per test is cheap (no recompile).
    """
    if value is None:
        return {"datatype": "str", "paramtype": "plusarg"}
    datatype, val = _infer_datatype(value)
    return {"datatype": datatype, "paramtype": "plusarg", "default": val}


_NAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_]+")


def _sanitize_name(name: str) -> str:
    """Sanitize an EDAM design name to a safe identifier."""
    cleaned = _NAME_SANITIZE_RE.sub("_", name).strip("_")
    return cleaned or "design"


# ---------------------------------------------------------------------------
# EDAM construction
# ---------------------------------------------------------------------------


def build_edam(
    *,
    name: str,
    files: list[Path | str],
    toplevel: str,
    eda_tool: str,
    workspace_root: Path | str,
    flow: str = "sim",
    include_dirs: list[Path | str] | None = None,
    defines: list[str] | None = None,
    vlogparams: dict[str, Any] | None = None,
    plusargs: dict[str, Any] | None = None,
    eda_tool_options: dict[str, Any] | None = None,
    relative_to: Path | str | None = None,
) -> dict[str, Any]:
    """Build a flow-API EDAM dict from resolved Booley config inputs.

    Args:
        name: Design/EDAM name (sanitized to an identifier).
        files: Source files (``.sv``/``.v``/...); each is confined to the
            workspace and typed by suffix.
        toplevel: Top module name.
        eda_tool: EDA tool selected through Edalize
            (``verilator``/``icarus``/``yosys``/``vivado``).
        workspace_root: Confinement root for every file entry.
        flow: Edalize flow selecting the option whitelist (``sim``/``lint``/
            ``vivado``/``generic``).
        include_dirs: Verilog include directories (emitted as include files).
        defines: ``NAME`` / ``NAME=VALUE`` vlogdefines.
        vlogparams: ``name -> value`` Verilog parameter overrides (``-G``).
        plusargs: ``name -> value`` run-time plusargs (``None`` value =
            declare-only).
        eda_tool_options: EDA-tool-specific Edalize options (e.g.
            ``{"verilator_options": [...]}``); merged into ``flow_options`` and
            validated against the whitelist.
        relative_to: When set (the Edalize ``work_root``), file ``name`` entries
            are emitted relative to it so the generated work dir is relocatable
            inside the Session Runtime workspace. See :func:`_confined_path`.

    Returns:
        An EDAM dict ready for :func:`configure`.

    Raises:
        EdamSecurityError: a file escapes the workspace, or a ``flow_options``
            key is not whitelisted.
    """
    root = Path(workspace_root)
    rel = Path(relative_to) if relative_to is not None else None

    file_entries: list[dict[str, Any]] = []
    for f in files:
        file_entries.append(
            {
                "name": _confined_path(Path(f), root, rel),
                "file_type": file_type_for(f),
            }
        )
    for inc in include_dirs or []:
        inc_name = _confined_path(Path(inc), root, rel)
        file_entries.append(
            {
                "name": inc_name,
                "file_type": "verilogSource",
                "is_include_file": True,
                # The entry *is* the include directory. Edalize's _add_include_dir
                # otherwise derives the incdir as os.path.dirname(name), which strips
                # the final component (e.g. rtl/include -> rtl) and breaks `include`
                # resolution in the generated vivado tcl. Pin include_path to the dir
                # itself so every backend uses it verbatim. (Sim/lint masked this by
                # also passing the header *files*, whose dirname is correct; fpga
                # passes only dirs, so the strip surfaced as a Vivado synth failure.)
                "include_path": inc_name,
            }
        )

    parameters: dict[str, dict[str, Any]] = {}
    for define in defines or []:
        if not define.strip():
            continue
        pname, pspec = _define_param(define)
        parameters[pname] = pspec
    for pname, pvalue in (vlogparams or {}).items():
        parameters[pname] = _vlogparam(pvalue)
    for pname, pvalue in (plusargs or {}).items():
        parameters[pname] = _plusarg(pvalue)

    flow_options: dict[str, Any] = {"tool": eda_tool, **(eda_tool_options or {})}
    flow_options = _validate_flow_options(flow, flow_options)

    return {
        "name": _sanitize_name(name),
        "toplevel": toplevel,
        "files": file_entries,
        "parameters": parameters,
        "flow_options": flow_options,
    }


# ---------------------------------------------------------------------------
# Flow invocation
# ---------------------------------------------------------------------------


def _flow_class(flow: str) -> type:
    """Return the ``edalize.flows`` class for *flow* (lazy import).

    Lazy so importing this module never hard-requires Edalize (it is only
    needed when a built-in actually invokes a flow, in-sandbox).
    """
    if flow == "sim":
        from edalize.flows.sim import Sim

        return Sim
    if flow == "lint":
        from edalize.flows.lint import Lint

        return Lint
    if flow == "vivado":
        from edalize.flows.vivado import Vivado

        return Vivado
    if flow == "generic":
        from edalize.flows.generic import Generic

        return Generic
    raise ValueError(f"unsupported edalize flow {flow!r}")


def configure(flow: str, edam: dict[str, Any], work_root: Path | str) -> Path:
    """Run the Edalize flow ``configure()`` for *edam* into *work_root*.

    This is pure file generation (Makefile + EDA-tool config files) executed
    in-process inside the trust zone — no EDA tool runs yet. Returns the
    ``work_root`` so callers can build the execution command against it.
    """
    cls = _flow_class(flow)
    root = Path(work_root)
    root.mkdir(parents=True, exist_ok=True)
    backend = cls(edam=edam, work_root=str(root))
    backend.configure()
    logger.debug("edalize %s flow configured in %s", flow, root)
    return root


# Edalize work dirs live here, under the worktree so they cross into the
# sandbox at /work and stay out of git (the .runtime/ tree is transient).
# Nested under .booley_project/ so it never pollutes the repo top-level; that
# dir is already git-excluded and present in both the real project root and the
# per-ticket worktree. The isolation scanner skips it via _ARTIFACT_ROOT_NAMES.
_EDALIZE_SUBDIR = Path(".booley_project") / ".runtime" / "edalize"


def work_root_for(
    work_dir: Path | str,
    flow: str,
    config: str,
    *,
    variant: str = "",
) -> Path:
    """Return the per-(Flow, config[, variant]) Edalize work dir.

    Distinct per variant so the trace overlay (``variant="trace"``) gets its
    own cached work dir keyed by ``(target, trace)`` (ADR 0022 dec. 20),
    composing as a separate directory from the untraced build.
    """
    from booley.runtime.checkout_role import require_project_checkout

    root = require_project_checkout(Path(work_dir))
    safe = _NAME_SANITIZE_RE.sub("_", config).strip("_") or "config"
    leaf = f"{safe}-{variant}" if variant else safe
    return root / _EDALIZE_SUBDIR / flow / leaf


def _work_root_lock_path(work_root: Path) -> Path:
    root = work_root.resolve()
    return root.parent / ".locks" / f"{root.name}.lock"


@contextmanager
def _work_root_lease_context(
    work_root: Path | str,
    acquire: Callable[[IO[Any]], bool],
) -> Iterator[Path | None]:
    """Apply one acquisition policy to a safely managed work-root lock."""
    root = Path(work_root).resolve()
    lock_path = _work_root_lock_path(root)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
    except OSError as exc:
        raise WorkRootLeaseError(root, lock_path, exc) from exc
    with handle:
        try:
            acquired = acquire(handle)
        except OSError as exc:
            raise WorkRootLeaseError(root, lock_path, exc) from exc
        if not acquired:
            yield None
            return
        try:
            yield root
        finally:
            try:
                release_file_lock(handle)
            except OSError as exc:
                raise WorkRootLeaseError(root, lock_path, exc) from exc


def _try_acquire_file_lock(handle: IO[Any]) -> bool:
    try:
        acquire_file_lock(handle)
    except LockContentionError:
        return False
    return True


@contextmanager
def try_work_root_lease(work_root: Path | str) -> Iterator[Path | None]:
    """Try to own one mutable Edalize work root without waiting."""
    with _work_root_lease_context(work_root, _try_acquire_file_lock) as root:
        yield root


@contextmanager
def work_root_lease(
    work_root: Path | str,
    *,
    timeout_s: float,
    on_wait: Callable[[], None] | None = None,
) -> Iterator[Path]:
    """Own one mutable Edalize work root for the protected operation."""

    def acquire(handle: IO[Any]) -> bool:
        wait_for_file_lock(handle, timeout_s=timeout_s, on_wait=on_wait)
        return True

    with _work_root_lease_context(work_root, acquire) as root:
        assert root is not None
        yield root


def relpath_for_make(work_root: Path | str, work_dir: Path | str) -> str:
    """Express *work_root* relative to *work_dir* for ``make -C``.

    The relative form keeps the generated command independent of the Runtime's
    absolute workspace path. The result is POSIX-separated because it is
    consumed inside the Linux Session Runtime.
    """
    return posix_relpath(Path(work_root).resolve(), Path(work_dir).resolve())


def make_command(
    work_root: Path | str,
    *,
    target: str | None = None,
    make_vars: dict[str, str] | None = None,
) -> list[str]:
    """Build the ``make`` command that drives an Edalize work dir.

    Edalize's ``configure()`` emits a Makefile whose default target builds and
    whose ``run`` target runs. Booley executes this command locally inside the
    Session Runtime.
    """
    cmd = ["make", "-C", str(work_root)]
    if target:
        cmd.append(target)
    for key, value in (make_vars or {}).items():
        cmd.append(f"{key}={value}")
    return cmd
