"""ElaborateFlow — BooleyFlow for elaboration-only checks.

Runs compile + elaborate (no simulation) per config and reports any errors.
Intended to be called by code-modifying agents (tb_coder) to verify
that their RTL/TB changes compile and elaborate cleanly before submitting.

Unlike the historical version, this eda_tool does NOT spawn an inner fix-loop
agent — the caller is itself an LLM agent that will read the errors and
fix them, then call again. Keep it sharp and minimal.

Exit codes: 0 = elaborated clean, 1 = the compiler rejected the design,
2 = no verdict was reached (EDAM/configure failure). See docs/USAGE.md for
the shared Booley Flow exit-code taxonomy.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from booley.core.boundary import require_bool
from booley.flows.eda_parsers import extract_error_gist
from booley.fusesoc import fusesoc_registry
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS, McpToolResult
from booley.runtime import job_slots
from booley.runtime.platform_paths import posix_relpath
from booley.runtime.timefmt import utc_now_rfc3339
from booley.sim.sim_result import write_run_log
from booley.targets.parameter_integrity import validate_top_parameter_intent
from booley.yosys.syn_core import (
    DEFAULT_FRONTEND,
    SV2V_OUTPUT_NAME,
    build_elaborate_script,
    resolve_frontend,
    resolve_slang_options,
    sv2v_argv,
)

from .. import edam as edam_layer
from .. import execution, output_budget
from ..base import BooleyFlow, SubprocessResult
from ..flow_config import _load_flow_config, resolve_flow_default_target
from ..human_display import cap_target_items

logger = logging.getLogger(__name__)

# Max chars of error output retained in the report / displayed. This is the
# 12KB-MCP-budget default; the effective cap scales with a raised
# BOOLEY_MCP_MAX_STDOUT_BYTES (see output_budget.scaled). The full untruncated
# output is persisted as run.log in the elaborate work root either way.
_ERROR_TAIL_CHARS = 2000

_DEFAULT_TIMEOUT_MS = 300_000


def _elaborate_exit_code(all_passed: bool, eda_tool_failed: bool) -> int:
    """Grade the run: clean, design failure, or EDA-tool failure (F-29).

    Exit 2 is reserved for "no verdict was reached about the RTL" — an
    EDAM/configure failure, a missing eda_tool. A compiler that ran and rejected
    the design is exit 1, the same grading ``lint`` gives the identical
    source. An EDA-tool failure outranks a design failure, since an unusable
    toolchain makes the other Targets' verdicts untrustworthy.
    """
    if all_passed:
        return EXIT_SUCCESS
    return EXIT_ERROR if eda_tool_failed else EXIT_FAILURE


def _followed_selection(work_dir: Path | None = None) -> execution.ExecutionSelection:
    """Resolve elaboration enablement from its own Flow configuration."""
    return execution.resolve_execution("elab", work_dir)


# The compiler-error gist extractor lives in the shared parser module.
_extract_error_gist = extract_error_gist


def _standalone_entries(records: list[dict[str, str]]) -> list[dict[str, str]]:
    """Report-shaped rows for a list of standalone probe outcomes.

    The full compiler output stays in the sweep's run.log; the report keeps
    only the gist. Shared by the ``failures`` and ``unparsed`` lists so the two
    never drift into different shapes.
    """
    return [
        {
            "module": r["module"],
            "file": r["file"],
            "error_gist": _extract_error_gist(r["error"]),
        }
        for r in records
    ]


# ---------------------------------------------------------------------------
# Standalone-elaboration check (`elaborate_standalone` criterion, ADR 0042)
# ---------------------------------------------------------------------------

# Criterion key the standalone sweep satisfies. Not per-target: the check
# covers the union of the selected Targets' RTL source filesets.
_STANDALONE_CRITERION = "elaborate_standalone"

# Max failing modules echoed inline on the console; the full compiler output
# for every module is persisted in the standalone run.log either way.
_MAX_ECHOED_STANDALONE_FAILURES = 5

# Compiled (System)Verilog sources — what the per-module iverilog probe can
# parse. Everything else in an RTL fileset (cpp mains, .vmem data, SDC) is
# skipped.
_HDL_SUFFIXES = frozenset({".v", ".sv"})

# Comment stripper for declaration scanning: a `module old_impl` inside a
# block comment must not become a phantom standalone-compile (iverilog would
# report the module missing — a fabricated finding).
_COMMENT_RE = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)
# Module declarations — same shape lint._toplevel_declared scans for, plus
# name capture. `macromodule` and lifetime qualifiers included.
_MODULE_DECL_RE = re.compile(
    r"^\s*(?:macro)?module\s+(?:automatic\s+|static\s+)?([A-Za-z_]\w*)",
    re.MULTILINE,
)
# Shared-prerequisite markers: a file declaring a `package` or `interface` is
# included on EVERY per-module compile line, so a legitimate `import pkg::*`
# or interface port never scores as a cross-file dependency (false positive).
_SHARED_DECL_RE = re.compile(r"^\s*(?:package|interface)\s+", re.MULTILINE)

# Frontends the per-module standalone probe can drive, and the
# `[flows.elab].standalone_frontend` values that pin one.
#
# ADR 0042 fixed Icarus here ("the cheapest conforming SystemVerilog
# elaborator in the image"). The fpu port (F-25) showed that premise does not
# hold: `iverilog -g2012` rejects a legal named assignment pattern
# ('{fmadd: 0, ...}) that Verilator — the frontend those very Targets elaborate
# with — accepts, so every module in the scope "failed" a check the design
# actually passes. `auto` therefore probes with the frontend the design is
# already known to build under (verilator, when it is on PATH) and falls back
# to iverilog only when it is not.
_FRONTEND_AUTO = "auto"
_FRONTEND_IVERILOG = "iverilog"
_FRONTEND_VERILATOR = "verilator"
_STANDALONE_FRONTENDS = (_FRONTEND_AUTO, _FRONTEND_IVERILOG, _FRONTEND_VERILATOR)

# Edalize eda_tool name -> the probe frontend that is *the same compiler*.
# Only the two the probe can drive need an entry; every other eda_tool a Target
# may resolve to is by construction a
# different compiler from the probe. Edalize spells Icarus `icarus`; older
# Booley configs said `iverilog` (see sim_edam._normalize).
_EDA_TOOL_AS_FRONTEND = {
    "verilator": _FRONTEND_VERILATOR,
    "icarus": _FRONTEND_IVERILOG,
    "iverilog": _FRONTEND_IVERILOG,
}

# Compiler output that means "this frontend cannot parse the construct",
# as opposed to "your design is broken": Icarus emits a bare syntax error on
# SystemVerilog it never implemented, Verilator names it `Unsupported:`.
# A genuinely malformed source produces the identical text, so this pattern is
# only ever consulted when the claim "the *other* frontend accepted these very
# sources" can actually be made — see _parse_gap_is_credible.
_PARSE_GAP_RE = re.compile(r"syntax error|Unsupported:|sorry: ", re.IGNORECASE)


def _scan_hdl_declarations(text: str) -> tuple[list[str], bool]:
    """(declared module names, declares-a-package-or-interface) for one file.

    Purely lexical (comment-stripped regex scan) — the same fidelity level as
    lint's ``_toplevel_declared``. Preprocessor-conditional declarations are
    taken at face value: a module only declared under an ``ifdef`` still gets
    probed with defaults, which is the check's intent (default parameterization
    / default defines must elaborate).
    """
    stripped = _COMMENT_RE.sub("", text)
    modules = _MODULE_DECL_RE.findall(stripped)
    has_shared = bool(_SHARED_DECL_RE.search(stripped))
    return modules, has_shared


# Epilogue printed when the sv2v stage of an ASIC elaborate fails. The stderr
# above it comes from sv2v and reads nothing like a Yosys diagnostic — without
# this line the user is left guessing which eda_tool rejected the design (the same
# "downstream symptom never names the cause" family as F-30). Deliberately
# apostrophe-free: it is embedded in a shell command, and shlex quoting would
# shred it into unreadable '"'"' fragments.
_SV2V_STAGE_FAILED = (
    "ERROR: elab: the sv2v transpile FAILED. The errors above come from sv2v, "
    "not from Yosys, which never read the design. This is a real elaboration "
    "failure, not an EDA tool problem: the Target uses [flows.synth] "
    'frontend = "sv2v", so asic_synthesize runs the very same transpile first and '
    "will fail identically until the source is fixed. (A design whose SystemVerilog "
    'sv2v cannot handle can switch to frontend = "slang".)'
)


@dataclass(frozen=True)
class _AsicElabInputs:
    """RTL inputs for an ASIC Target's elaborate, relative to the worktree."""

    sources: list[Path]
    inc_dirs: list[Path]
    defines: list[str]
    params: dict[str, str]


@dataclass
class _StandaloneOutcome:
    """Result of one standalone-elaboration sweep, ready to merge into _run."""

    lines: list[str] = field(default_factory=list)
    passed: bool = False
    eda_tool_failed: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    display: str = ""


class ElaborateFlow(BooleyFlow):
    """Elaboration-only check (compile + elaborate, no simulation)."""

    name: str = "elab"
    description: str = (
        "Compile + elaborate RTL/TB for one or more Targets (no simulation). "
        "Use after editing RTL/TB to verify the change compiles and elaborates "
        "before committing. Pass a single Target or comma-separated Targets. "
        "With --standalone (or an elaborate_standalone criterion in the "
        "ticket) also verifies every RTL module elaborates from its declaring "
        "file alone."
    )
    code_modifying: bool = False
    satisfies: ClassVar[list[str]] = ["elab_pass", "elaborate_standalone"]
    satisfies_args: ClassVar[dict[str, str]] = {
        "elaborate_standalone": "--standalone",
    }

    def _resolve_job_class(self) -> str:
        """Elaboration is a heavy Session Runtime workload."""
        return job_slots.CLASS_HEAVY

    @property
    def _exec_selection(self) -> execution.ExecutionSelection:
        """The followed ``[flows.sim]`` selection, resolved once per run."""
        selection = getattr(self, "_followed", None)
        if selection is None:
            selection = _followed_selection(Path(self.args.work_dir))
            self._followed = selection
        return selection

    def _add_args(self, parser: argparse.ArgumentParser) -> None:
        # tb_top left the surface (ADR 0021): a sim Target's `toplevel` is its TB
        # top, sourced from the resolved Target. --no-dpi was unused by the
        # edalize elaborate path (the DPI option lives in the Target's
        # verilator_options when needed).
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print commands as JSON without executing",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=_DEFAULT_TIMEOUT_MS,
            help="Per-config timeout in milliseconds (default: 300000)",
        )
        parser.add_argument(
            "--standalone",
            action="store_true",
            help="Also verify every module in the Targets' RTL source scope "
            "elaborates standalone from its declaring file (package/interface "
            "files auto-included). The probe frontend follows "
            "[flows.elab].standalone_frontend — 'auto' (default) uses "
            "verilator when available, else iverilog -g2012. Runs "
            "automatically when the ticket declares the elaborate_standalone "
            "criterion.",
        )

    def _build_command(self) -> list[str]:
        """Unused — _run() drives the multi-config loop directly."""
        return []

    def _interpret_result(self, result: SubprocessResult) -> McpToolResult:
        """Unused — _run() handles interpretation directly."""
        return McpToolResult()

    def _get_timeout(self) -> int:
        """Per-config timeout in seconds (CLI arg is milliseconds)."""
        return max(1, self.args.timeout // 1000)

    def _prepare_elab_command(self, target: str) -> list[str]:
        """Resolve the sim Target through FuseSoC and return its build ``make``.

        ADR 0022 (decision 4): FuseSoC owns design-description. ``resolve_target``
        runs ``fusesoc run --setup`` (which runs Edalize ``configure()``) and
        leaves a ready-to-``make`` build dir — superseding the Booley-built sim
        EDAM of 0019. The default ``make`` target compiles+links the Verilator
        executable (no ``run`` target) — elaboration only.

        Everything the legacy path hand-assembled now lives in the ``.core`` sim
        Target: sources, ``toplevel`` (the TB top, decision 12), the
        ``--timing``/``--trace``/``-Wno-fatal`` option set
        (``flow_options.verilator_options``), and the custom ``--timing`` C++
        main + ``booley_vcd_dump.sv`` — which are compiled sources, so they are
        fileset members (a ``cppSource`` main wired through ``--exe``), not files
        Booley generates into the build dir. Defines are declared ``vlogdefine``
        params (decision 8). ``target`` is the FuseSoC Target name (decision 10).

        The resolved build dir is relocatable, so ``make -C <relpath>`` crosses
        the host/sandbox boundary unchanged. Raises on setup failure so the
        caller records it as a Flow error.

        One exception to the make-driving rule: a Target that synthesizes
        through the slang frontend is elaborated by Booley's own Yosys script
        instead (see :meth:`_slang_elab_command`, F-31).
        """
        build_root = edam_layer.work_root_for(self.args.work_dir, "elab", target)
        resolved = fusesoc_registry.resolve_target(
            target,
            project_root=self.args.work_dir,
            build_root=build_root,
        )
        validate_top_parameter_intent(resolved, flow="elab")
        self._ensure_warnings_nonfatal(resolved)
        self._record_eda_tool(target, getattr(resolved, "eda_tool", None))
        self._record_build_dir(target, getattr(resolved, "build_root", None))
        asic_cmd = self._asic_elab_command(target, resolved)
        if asic_cmd is not None:
            return asic_cmd
        rel = edam_layer.relpath_for_make(resolved.build_root, self.args.work_dir)
        command = edam_layer.make_command(rel)
        if getattr(resolved, "eda_tool", None) == "vivado":
            return [*command, "synth"]
        return command

    # ------------------------------------------------------------------
    # ASIC frontend parity (F-31)
    # ------------------------------------------------------------------

    def _asic_elab_command(self, target: str, resolved: Any) -> list[str] | None:
        """The elaborate command for an ASIC (Yosys) Target, or ``None``.

        ``None`` means "keep make-driving Edalize" — every Target that is not a
        Yosys Target with a toplevel.

        Why ASIC Targets need their own path (ravenoc F-31): Edalize's Yosys
        flow reads RTL with a generic ``read_verilog``, which cannot parse a
        package ``import`` and dies with ``syntax error, unexpected
        TOK_IMPORT``. That made *every* SystemVerilog ASIC Target
        un-elaboratable, whichever frontend the project had configured. Booley
        instead reads the design exactly the way ``asic_synthesize`` will:

        * ``slang`` — one ``yosys -p`` running ``read_slang`` with the Target's
          include dirs, defines, params and ``slang_options``;
        * ``sv2v`` (the default) — the sv2v transpile first, then ``yosys -p``
          over the transpiled file, the same two stages the synthesis Makefile
          chains (see :meth:`_sv2v_elab_command`).

        Both are composed from :mod:`booley.yosys.syn_core`'s shared builders,
        so elaborate's verdict cannot drift from the one synthesis reaches.
        """
        # No toplevel means no elaboration root for `--top`; leave such a Target
        # to the Edalize path, which reports the gap in its own vocabulary
        # rather than through a truncated Yosys command line.
        if getattr(resolved, "eda_tool", None) != "yosys" or not getattr(resolved, "toplevel", ""):
            return None
        recipe = dict(resolved.flow_options)
        frontend = (
            resolve_frontend(
                recipe,
                field=f"Target {target!r} flow_options.frontend",
            )
            or DEFAULT_FRONTEND
        )
        inputs = self._asic_frontend_inputs(resolved)
        self._asic_targets().add(target)
        if frontend == "slang":
            script = build_elaborate_script(
                inputs.sources,
                resolved.toplevel,
                frontend="slang",
                inc_dirs=inputs.inc_dirs,
                defines=inputs.defines,
                params=inputs.params,
                slang_options=resolve_slang_options(
                    recipe,
                    field=f"Target {target!r} flow_options.slang_options",
                ),
            )
            return ["yosys", "-p", script]
        return self._sv2v_elab_command(resolved, inputs)

    def _asic_frontend_inputs(self, resolved: Any) -> _AsicElabInputs:
        """The RTL inputs an ASIC Target hands its frontend, worktree-relative.

        The same slice ``asic_synthesize`` forwards to ``run_yosys_syn``: HDL
        sources (headers excluded — they arrive as ``-I`` dirs), the Target's
        ``vlogdefine`` params, and its ``vlogparam`` overrides. Paths are relative because the
        commands run with the work dir as cwd.
        """
        # Imported lazily: this is the same
        # FuseSoC-params -> Yosys-inputs mapping asic_synthesize forwards, and
        # elaborate must apply it identically or the two Flows read different
        # RTL configurations.
        from ..target_parameters import vlogdefine_args, vlogparam_args

        work_dir = Path(self.args.work_dir)
        return _AsicElabInputs(
            sources=[
                Path(posix_relpath(f.absolute(resolved.build_root), work_dir))
                for f in resolved.rtl_hdl_source_files
            ],
            inc_dirs=[Path(posix_relpath(inc, work_dir)) for inc in resolved.rtl_include_dirs],
            defines=vlogdefine_args(resolved.parameters),
            params=dict(a.split("=", 1) for a in vlogparam_args(resolved.parameters)),
        )

    def _sv2v_elab_command(self, resolved: Any, inputs: _AsicElabInputs) -> list[str]:
        """Transpile with sv2v, then elaborate the result — the sv2v frontend.

        Two stages, chained in one ``sh -c`` because a Target's elaborate run is
        a single command: ``sv2v -I… -D… <sources> -w sv2v_converted.v`` (argv
        from :func:`syn_core.sv2v_argv`, shared with the synthesis Makefile
        recipe) followed by ``yosys -p`` over that one transpiled file. Exactly
        the ``sv2v`` → ``yosys`` chain ``asic_synthesize``'s generated Makefile
        runs, minus tech-mapping.

        The transpiled file lands in the Target's resolved build dir, so it is
        removed with that dir on a clean run (F-33) and kept for triage on a
        failing one.

        A failing transpile aborts before Yosys ever reads the design, and its
        stderr looks nothing like a Yosys diagnostic — so the epilogue names
        sv2v as the stage that failed. It is a genuine elaboration failure and
        graded as one (exit 1, not a Flow error): the same transpile runs first
        in synthesis, so the Target does not elaborate *or* synthesize until it
        is fixed.
        """
        work_dir = Path(self.args.work_dir)
        build_dir = Path(posix_relpath(Path(resolved.build_root), work_dir))
        transpiled = build_dir / SV2V_OUTPUT_NAME
        sv2v = shlex.join(sv2v_argv(inputs.sources, inputs.inc_dirs, inputs.defines, transpiled))
        script = build_elaborate_script(
            [transpiled],
            resolved.toplevel,
            frontend="sv2v",
            params=inputs.params,
        )
        yosys = shlex.join(["yosys", "-p", script])
        return [
            "sh",
            "-c",
            f"{sv2v} || {{ rc=$?; echo {shlex.quote(_SV2V_STAGE_FAILED)}; exit $rc; }}\n{yosys}\n",
        ]

    def _asic_targets(self) -> set[str]:
        """Targets whose command is a local ASIC elaborate, not a boundary ``make``.

        ``yosys -p`` / the ``sh -c`` sv2v+yosys chain are not
        Boundary-Command-Contract commands (ADR 0037 §5 crossings are
        relocatable ``make`` argvs only), so — like the standalone sweep's
        ``iverilog`` probes — they run as local subprocesses in the Session
        Runtime and never route to the host.
        """
        if not hasattr(self, "_asic_elab_targets"):
            self._asic_elab_targets: set[str] = set()
        return self._asic_elab_targets

    def _record_eda_tool(self, target: str, eda_tool: str | None) -> None:
        """Remember *target*'s resolved EDA tool (e.g. ``"verilator"``).

        Feeds the per-target report/console line and the run-level ``eda_tool``
        report key (base.write_report) — ", "-joined unique names when a
        multi-target run resolves to different EDA tools.
        """
        if not eda_tool:
            return
        if not hasattr(self, "_target_eda_tools"):
            self._target_eda_tools: dict[str, str] = {}
        self._target_eda_tools[target] = eda_tool
        self._eda_tool = ", ".join(dict.fromkeys(self._target_eda_tools.values()))

    def _eda_tool_for(self, target: str) -> str | None:
        """The resolved EDA tool for *target*, or None before/without resolve."""
        return getattr(self, "_target_eda_tools", {}).get(target)

    # ------------------------------------------------------------------
    # Build-tree retention (F-33)
    # ------------------------------------------------------------------

    def _record_build_dir(self, target: str, build_dir: Path | None) -> None:
        """Remember the compiler build tree resolution generated for *target*."""
        if build_dir is None:
            return
        if not hasattr(self, "_target_build_dirs"):
            self._target_build_dirs: dict[str, Path] = {}
        self._target_build_dirs[target] = Path(build_dir)

    def _discard_build_dir(self, target: str) -> None:
        """Delete *target*'s build tree after it elaborated clean (F-33).

        A verilated build tree runs ~130 MB per Target, and elaborate is a
        compile-only check: an 11-Target sweep left 1.4 GB behind for verdicts
        that are already in the report. Only the *inner* resolved build dir is
        removed — ``run.log`` lives one level up in the work root, so failure
        triage keeps the full compiler output either way.

        Skipped entirely on failure (the tree is the evidence) and when
        ``[flows.elab] keep_build_dir = true`` asks for it back — the
        opt-out for anyone who wants ``make``'s incremental rebuild across
        repeated runs. Best-effort: a removal failure must never fail a run
        that already reached its verdict.
        """
        if self._keep_build_dir():
            return
        build_dir = getattr(self, "_target_build_dirs", {}).get(target)
        if build_dir is None:
            return
        try:
            shutil.rmtree(build_dir, ignore_errors=True)
        except OSError:  # pragma: no cover — ignore_errors already swallows these
            logger.debug("could not remove elaborate build dir %s", build_dir, exc_info=True)

    def _keep_build_dir(self) -> bool:
        """``[flows.elab].keep_build_dir`` — retain build trees after a PASS.

        Defaults to false (clean up). Resolved once per run and cached; a
        wrong-typed value is a loud config error rather than a silently
        disarmed knob.
        """
        cached = getattr(self, "_keep_build_dir_cached", None)
        if cached is None:
            cached = require_bool(
                _load_flow_config(self.name, Path(self.args.work_dir)),
                "keep_build_dir",
                default=False,
                field="[flows.elab] 'keep_build_dir'",
            )
            self._keep_build_dir_cached = cached
        return cached

    @staticmethod
    def _ensure_warnings_nonfatal(resolved: Any) -> None:
        """Make Verilator warnings non-fatal for this elaborate build (QA-5).

        Verilator exits non-zero on *any* warning by default
        (``%Error: Exiting due to N warning(s)``), so a benign style warning
        (e.g. ``%Warning-NORETURN``) that ``lint`` only WARNs on would abort
        elaboration and make ``elab_pass`` unreachable for an otherwise-clean
        core. ``lint`` reports warnings without dying; bring elaborate to the
        same severity by appending ``-Wno-fatal`` to the resolved Verilator
        command file (the ``.vc`` Edalize emits, read by ``verilator -f``).

        Genuine ``%Error`` (syntax errors, undeclared signals, and any rule the
        project explicitly promoted with ``-Werror-<CODE>``) is unaffected —
        ``-Wno-fatal`` only demotes plain warnings — so real elaboration errors
        still fail the build. Verilator-only: no other supported eda_tool has a
        fatal-on-warning default. Idempotent (a ``.vc`` that already carries
        ``-Wno-fatal`` is left untouched); best-effort (an unreadable/absent
        ``.vc`` is skipped — the build simply keeps Verilator's default).
        """
        if getattr(resolved, "eda_tool", None) != "verilator":
            return
        build_root = getattr(resolved, "build_root", None)
        if build_root is None:
            return
        for vc in sorted(Path(build_root).glob("*.vc")):
            try:
                text = vc.read_text(encoding="utf-8")
            except OSError:
                continue
            if "-Wno-fatal" in text:
                continue
            sep = "" if (not text or text.endswith("\n")) else "\n"
            try:
                vc.write_text(f"{text}{sep}-Wno-fatal\n", encoding="utf-8")
            except OSError:
                logger.debug("could not append -Wno-fatal to %s", vc, exc_info=True)

    def _dry_run_command(self, target: str) -> list[str]:
        """Build a side-effect-free ``--dry-run`` preview for one config.

        Mirrors :meth:`SimulateFlow._dry_run_command`: unlike
        :meth:`_prepare_elab_command` (which runs ``fusesoc run --setup`` to
        resolve the build dir), dry-run **does not resolve**. It shows the
        ``fusesoc run --setup`` command resolution *would* execute (via
        :func:`fusesoc_registry.setup_command`, a cheap ``.core`` YAML read — no
        subprocess, works on the host with no ``fusesoc`` on PATH) chained to the
        deterministic build-only ``make``. The previewed ``make -C`` names the
        outer build root; resolution nests the Makefile one level deeper, so the
        preview shows *what would run*, not a byte-exact runnable command. An
        unauthored Target yields a clean ``ERROR`` entry rather than raising.
        """
        build_root = edam_layer.work_root_for(self.args.work_dir, "elab", target)
        try:
            setup_cmd = fusesoc_registry.setup_command(
                target,
                project_root=self.args.work_dir,
                build_root=build_root,
            )
        except fusesoc_registry.TargetResolutionError as exc:
            return [f"ERROR: elab dry-run: {exc}"]
        rel = edam_layer.relpath_for_make(build_root, self.args.work_dir)
        script = f"{shlex.join(setup_cmd)} && {shlex.join(edam_layer.make_command(rel))}"
        return ["sh", "-c", script]

    def _persist_run_log(self, target: str, combined: str) -> str | None:
        """Persist *combined* as run.log in the elaborate work root (pass AND fail).

        simulate/lint parity: the report/console tail is capped, so without a
        durable copy the full compiler output is gone once the MCP layer
        truncates stdout. Written into the outer per-Target work root (the
        same dir lint uses), via the sim layer's atomic writer. Returns the
        project-relative pointer to cite, or None on a write failure —
        best-effort, a log-write failure must never fail the run.
        """
        try:
            log_dir = edam_layer.work_root_for(self.args.work_dir, "elab", target)
            log_dir.mkdir(parents=True, exist_ok=True)
            path = write_run_log(log_dir, combined)
        except OSError:
            logger.debug("could not persist elaborate run.log for %s", target, exc_info=True)
            return None
        return posix_relpath(path, self.args.work_dir)

    def _compile_command_str(self, target: str) -> str | None:
        """The composed setup+build command line for *target*, or None.

        The same ``sh -c`` script :meth:`_dry_run_command` previews — reused
        so the report shows exactly what a ``--dry-run`` would print. Cached
        per target; best-effort: any failure (including the clean ``ERROR:``
        entry an unauthored Target yields) returns None and the key is
        omitted rather than failing the EDA tool.
        """
        cache: dict[str, str | None] = getattr(self, "_compile_command_cache", {})
        if not hasattr(self, "_compile_command_cache"):
            self._compile_command_cache = cache
        if target in cache:
            return cache[target]
        command: str | None = None
        try:
            cmd = self._dry_run_command(target)
            if cmd[:2] == ["sh", "-c"]:
                command = cmd[2]
        except Exception:  # noqa: BLE001 — observability only; never fail the run over it
            logger.debug("could not compose compile command for %s", target, exc_info=True)
        cache[target] = command
        return command

    def _fileset_for_report(self, target: str) -> dict[str, list[str]] | None:
        """*target*'s declared source fileset, split rtl/tb, or None.

        A cheap ``.core`` read (``target_source_files``, dependency closure
        included — a layered repo's RTL arrives transitively, F-27), cached
        per target. Best-effort like :meth:`_compile_command_str`.
        """
        cache: dict[str, dict[str, list[str]] | None] = getattr(self, "_fileset_cache", {})
        if not hasattr(self, "_fileset_cache"):
            self._fileset_cache = cache
        if target in cache:
            return cache[target]
        fileset: dict[str, list[str]] | None = None
        try:
            sources = fusesoc_registry.target_source_files(
                self.args.work_dir,
                target,
                include_dependencies=True,
            )
            fileset = {
                "rtl": list(sources.rtl_source_files),
                "tb": list(sources.tb_files),
            }
        except Exception:  # noqa: BLE001 — observability only; never fail the run over it
            logger.debug("could not read fileset for %s", target, exc_info=True)
        cache[target] = fileset
        return fileset

    def _failure_context_lines(self, target: str, log_pointer: str | None) -> list[str]:
        """Compact failure-card lines naming the build config (best-effort).

        The invisible half of most compile failures (benchmark finding,
        47/57 cases): the composed compile command (e.g. a missing language
        flag) and the fileset size (a fileset missing the testbench), plus
        the run.log pointer. ≤3 short lines; the full detail lives in the
        per-Target report JSON.
        """
        lines: list[str] = []
        build = self._compile_command_str(target)
        if build:
            lines.append(f"build: {build}")
        fileset = self._fileset_for_report(target)
        if fileset is not None:
            total = len(fileset["rtl"]) + len(fileset["tb"])
            lines.append(f"fileset: {total} files ({len(fileset['tb'])} tb)")
        if log_pointer:
            lines.append(f"log: {log_pointer}")
        return lines

    def _write_target_report(
        self,
        target: str,
        passed: bool,
        elapsed_s: float,
        error_output: str,
        log_pointer: str | None = None,
    ) -> None:
        """Write a per-Target JSON report alongside the EDA tool's report dir.

        Besides the verdict, the report names the generated build config that
        is otherwise invisible (benchmark finding: agents shelled out to
        recover the compile line and the fileset): the composed
        ``compile_command``, the rtl/tb-split ``fileset``, and the ``log``
        pointer to the full run.log. All best-effort — omitted when unknown.
        """
        report_dir = self.args.report_dir
        if report_dir is None:
            return
        report_dir.mkdir(parents=True, exist_ok=True)
        tail_chars = output_budget.scaled(_ERROR_TAIL_CHARS)
        report = {
            "flow": self.name,
            "target": target,
            "eda_tool": self._eda_tool_for(target),
            "timestamp": utc_now_rfc3339(),
            "elapsed_s": round(elapsed_s, 1),
            "passed": passed,
            "error_output": (error_output[-tail_chars:] if error_output else ""),
        }
        compile_command = self._compile_command_str(target)
        if compile_command is not None:
            report["compile_command"] = compile_command
        fileset = self._fileset_for_report(target)
        if fileset is not None:
            report["fileset"] = fileset
        if log_pointer is not None:
            report["log"] = log_pointer
        path = report_dir / f"elab_{target}.json"
        # ``log`` above is kept for back-compat; ``artifacts`` is the shared
        # shape every Booley Flow now emits, and it also names this file so a
        # consumer holding only the report can get back to the rest.
        artifacts = {"report": posix_relpath(path, self.args.work_dir)}
        if log_pointer is not None:
            artifacts["log"] = log_pointer
        report["artifacts"] = artifacts
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    def _validate_interactive_args(self) -> McpToolResult | None:
        """Interactive-Mode argument validation — see SimulateFlow._validate_interactive_args.

        tb_top left the surface (ADR 0021); it comes from the resolved Target,
        so only ``--target`` selection is validated here.
        """
        if not getattr(self.args, "target", "").strip():
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(
                    "elab: --target is required. Pass --target <name>; "
                    "available Targets are the .core sim Targets in "
                    ".booley_project/."
                ),
            )
        return None

    # ------------------------------------------------------------------
    # Standalone-elaboration check (`elaborate_standalone`)
    # ------------------------------------------------------------------

    def _standalone_requested(self) -> bool:
        """Whether this run must perform the standalone sweep.

        Two triggers, following the reviewer's pattern of keying eda_tool modes
        off the ticket's declared criteria: the ``elaborate_standalone``
        criterion present in state (Ticket Mode — the consuming project opted
        in), or the explicit ``--standalone`` flag (Interactive/human mode).
        """
        if getattr(self.args, "standalone", False):
            return True
        return self._state is not None and self._state.has_criterion(_STANDALONE_CRITERION)

    def _standalone_rtl_scope(self, targets: list[str]) -> list[str]:
        """Project-relative HDL files in the Targets' RTL source scope.

        Reuses the same fileset resolution the per-target report already
        relies on (``target_source_files``, dependency closure included): the
        RTL/TB partition comes from the ``.core`` ``tags:[tb]`` marker, so TB
        and vendor/harness files outside the declared RTL filesets are exempt
        by construction. Union across the selected Targets, first-seen order,
        narrowed to compiled (System)Verilog sources. Raises on resolution
        failure — the caller grades that a Flow ERROR (no verdict reached).
        """
        seen: dict[str, None] = {}
        for tgt in targets:
            sources = fusesoc_registry.target_source_files(
                self.args.work_dir,
                tgt,
                include_dependencies=True,
            )
            for rel in sources.rtl_source_files:
                seen.setdefault(rel, None)
        return [rel for rel in seen if Path(rel).suffix.lower() in _HDL_SUFFIXES]

    def _scan_standalone_scope(
        self,
        scope: list[str],
    ) -> tuple[list[tuple[str, str]], list[str]]:
        """Scan *scope* files → ((module, declaring file) pairs, shared files).

        Shared files are those declaring a ``package`` or ``interface``; they
        ride along on every per-module compile so cross-file imports of
        legitimately shared definitions never score as findings. Unreadable
        files are skipped best-effort — the per-target elaborate build reports
        missing fileset files with full compiler context already.
        """
        modules: list[tuple[str, str]] = []
        shared: list[str] = []
        for rel in scope:
            try:
                text = (Path(self.args.work_dir) / rel).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                logger.debug("standalone: could not read %s", rel, exc_info=True)
                continue
            mods, has_shared = _scan_hdl_declarations(text)
            modules.extend((m, rel) for m in mods)
            if has_shared:
                shared.append(rel)
        return modules, shared

    def _resolve_standalone_frontend(self) -> str:
        """The compiler the per-module probe drives (F-25).

        ``[flows.elab].standalone_frontend``: ``iverilog`` or
        ``verilator`` pins one, ``auto`` (the default) picks verilator when it
        is on PATH — the frontend the per-Target elaborate itself drives, so
        the probe cannot reject SystemVerilog the design demonstrably compiles
        — and falls back to iverilog otherwise. A wrong value is a loud config
        error, not a silently disarmed knob.
        """
        raw = _load_flow_config(self.name, Path(self.args.work_dir)).get(
            "standalone_frontend", _FRONTEND_AUTO
        )
        frontend = str(raw or _FRONTEND_AUTO).strip().lower()
        if frontend not in _STANDALONE_FRONTENDS:
            raise ValueError(
                "[flows.elab] 'standalone_frontend' must be one of "
                f"{', '.join(_STANDALONE_FRONTENDS)}; got {raw!r}"
            )
        if frontend != _FRONTEND_AUTO:
            return frontend
        return _FRONTEND_VERILATOR if shutil.which(_FRONTEND_VERILATOR) else _FRONTEND_IVERILOG

    def _parse_gap_is_credible(self, frontend: str, primary_ok: bool) -> bool:
        """Whether "the probe frontend just can't read this" is even arguable.

        Two conditions, both necessary:

        * the per-Target legs of this same invocation accepted the sources
          (*primary_ok*) — otherwise the design really is malformed and a
          syntax error is the finding, not an excuse;
        * the probe drives a *different* compiler than the Targets did.

        The second one used to be free: ADR 0042 hard-coded the probe to
        Icarus while the Targets built with Verilator, so the two were always
        different EDA tools. ``standalone_frontend`` defaults to ``auto``, which
        picks the very Verilator the Targets used — and then "this frontend
        cannot parse what the other one accepted" is not a coherent claim
        about the same compiler. What a same-frontend probe error means is the
        opposite: the module needs a ``+define+``/include path only the
        Target's full command line supplies, i.e. precisely the standalone
        defect ``elaborate_standalone`` exists to catch. So there, a parse
        error is a real finding.
        """
        if not primary_ok:
            return False
        target_frontends = {
            _EDA_TOOL_AS_FRONTEND[eda_tool]
            for eda_tool in getattr(self, "_target_eda_tools", {}).values()
            if eda_tool in _EDA_TOOL_AS_FRONTEND
        }
        return frontend not in target_frontends

    def _standalone_compile_command(
        self,
        module: str,
        rel: str,
        shared: list[str],
        frontend: str = _FRONTEND_IVERILOG,
    ) -> list[str]:
        """The per-module probe: elaborate *module* from its declaring file only.

        The declaring file plus the shared package/interface files (minus the
        declaring file itself when it is one of them). Parameter defaults only
        — no ``-P``/``-G`` overrides: a default parameterization that fails to
        elaborate is a finding.

        Prerequisites come FIRST on the command line: both frontends resolve
        ``import pkg::*`` during the parse, so a package declared in a file
        listed after its importer is "not found" — a fabricated finding that
        says nothing about the module.
        """
        prereqs = [s for s in shared if s != rel]
        if frontend == _FRONTEND_VERILATOR:
            # --lint-only elaborates the hierarchy (exactly what this check is
            # about) without emitting C++; -Wno-fatal keeps style warnings from
            # scoring as findings, the same severity choice the per-Target
            # elaborate makes (_ensure_warnings_nonfatal).
            return [
                "verilator",
                "--lint-only",
                "-Wno-fatal",
                "--top-module",
                module,
                *prereqs,
                rel,
            ]
        # ``-o`` goes to the null device so the probe never litters the worktree.
        return ["iverilog", "-g2012", "-o", os.devnull, "-s", module, *prereqs, rel]

    def _run_standalone_probes(
        self,
        modules: list[tuple[str, str]],
        shared: list[str],
        frontend: str,
        *,
        gap_is_credible: bool,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str], str]:
        """Probe every module and sort the outcomes into three buckets.

        Returns ``(failures, unparsed, log_chunks, eda_tool_error)``:

        * *failures* — the compiler ran and rejected the module: a design
          finding, the point of the criterion;
        * *unparsed* — the compiler could not read the construct at all, and
          (per *gap_is_credible*) a different frontend demonstrably could:
          no verdict about that module, but the sweep keeps going so the
          modules it CAN grade still get graded;
        * *eda_tool_error* — the probe never ran (spawn failure / timeout), which
          says nothing about any module and aborts the sweep.
        """
        failures: list[dict[str, str]] = []
        unparsed: list[dict[str, str]] = []
        log_chunks: list[str] = []
        for module, rel in modules:
            cmd = self._standalone_compile_command(module, rel, shared, frontend)
            proc = self._execute(cmd)
            combined = (proc.stdout + proc.stderr).strip()
            log_chunks.append(f"$ {shlex.join(cmd)}\n{combined}\n")
            if proc.returncode == 0:
                continue
            if proc.returncode < 0:
                # Spawn failure / timeout — no verdict about the RTL.
                eda_tool_error = (
                    f"{frontend} timed out after {self._get_timeout()}s"
                    if proc.timed_out
                    else f"{frontend} could not run (is it installed in the Session Runtime?)"
                )
                if combined:
                    eda_tool_error += f": {combined}"
                return failures, unparsed, log_chunks, eda_tool_error
            if gap_is_credible and _PARSE_GAP_RE.search(combined):
                # A *different* frontend compiled these very sources, so the
                # construct is legal and it is the probe frontend that is short
                # — no verdict about the module (F-25).
                unparsed.append({"module": module, "file": rel, "error": combined})
                continue
            failures.append({"module": module, "file": rel, "error": combined})
        return failures, unparsed, log_chunks, ""

    def _run_standalone_check(
        self,
        targets: list[str],
        *,
        primary_ok: bool = True,
    ) -> _StandaloneOutcome:
        """Run the standalone sweep, set the criterion, and report.

        Deterministic and cheap (one parse+elaborate per module), so it runs on
        every invocation — no one-shot machinery. The probes run as local
        subprocesses in the Session Runtime via the EDA tool's standard
        ``_execute`` path (both frontends are baked into the sandbox image):
        they are not Boundary-Command-Contract commands (ADR 0037 §5 crossings
        are relocatable ``make`` argvs only), so they never route to the host.

        *primary_ok* says whether the per-Target elaborate legs of this same
        invocation accepted the sources — one half of
        :meth:`_parse_gap_is_credible`, which decides whether a probe error
        that looks like a parse failure is graded as a capability gap (no
        verdict about that module) or as the design failure it usually is.
        A false FAIL is nearly as expensive as a false PASS (F-25); so is a
        false "no verdict", which is why the gap escape hatch is narrow and
        why modules it excuses never displace the ones that really failed.
        """
        t0 = time.monotonic()
        # F-26: same claim-before-you-run contract as the per-Target loop,
        # against the sweep's own variant work dir.
        self._open_run_log(
            "standalone",
            edam_layer.work_root_for(self.args.work_dir, "elab", "standalone", variant="sweep"),
        )
        try:
            frontend = self._resolve_standalone_frontend()
        except ValueError as exc:
            return self._standalone_error(str(exc))
        try:
            scope = self._standalone_rtl_scope(targets)
        except Exception as exc:  # noqa: BLE001 — resolution failure graded as a Flow ERROR, never a crash
            logger.debug("standalone: RTL scope resolution failed", exc_info=True)
            return self._standalone_error(
                f"could not resolve RTL source scope: {exc}",
            )
        modules, shared = self._scan_standalone_scope(scope)
        if not modules:
            # Zero modules would make a green criterion vacuous — the same
            # false-pass family the lint toplevel check hard-fails.
            return self._standalone_error(
                f"no module declarations found in the RTL source scope "
                f"({len(scope)} files) — the criterion would be vacuous. "
                "Check the Targets' RTL filesets.",
            )

        failures, unparsed, log_chunks, eda_tool_error = self._run_standalone_probes(
            modules,
            shared,
            frontend,
            gap_is_credible=self._parse_gap_is_credible(frontend, primary_ok),
        )

        elapsed = time.monotonic() - t0
        log_pointer = self._persist_standalone_log("".join(log_chunks))
        if eda_tool_error:
            return self._standalone_error(eda_tool_error, log_pointer=log_pointer)
        if unparsed and not failures:
            # Nothing else in the sweep reached a verdict either, so the run as
            # a whole reached none: report the capability gap and stop.
            return self._standalone_error(
                self._parse_gap_message(frontend, unparsed, len(modules)),
                log_pointer=log_pointer,
                extra_detail={
                    "frontend": frontend,
                    "unparsed": _standalone_entries(unparsed),
                },
            )

        passed = not failures
        header = (
            f"[elab] standalone ({len(modules)} modules, "
            f"{len(shared)} shared, {frontend}) "
            f"{'PASS' if passed else 'FAIL'}   {elapsed:.1f}s"
        )
        lines = [header]
        if failures:
            lines.extend(self._standalone_failure_lines(failures, log_pointer))
        # A capability gap on some modules must not erase the verdict on the
        # others: modules the probe genuinely rejected are named as failures
        # and the ungraded ones are listed alongside, so neither the real
        # defect nor the hole in coverage is silently dropped.
        if unparsed:
            lines.extend(self._standalone_ungraded_lines(frontend, unparsed, log_pointer))
        detail: dict[str, Any] = {
            "modules_checked": len(modules),
            "shared_files": shared,
            "frontend": frontend,
            "failures": _standalone_entries(failures),
        }
        if unparsed:
            detail["unparsed"] = _standalone_entries(unparsed)
        if log_pointer:
            detail["log"] = log_pointer
        self.set_criterion(_STANDALONE_CRITERION, passed, detail=detail)
        # `passed` implies no unparsed modules (a gap-only sweep returned above).
        ungraded = f" ({len(unparsed)} ungraded)" if unparsed else ""
        display = (
            f"standalone: {len(modules)} modules OK"
            if passed
            else f"standalone: {len(failures)}/{len(modules)} modules FAIL{ungraded}"
        )
        return _StandaloneOutcome(
            lines=lines,
            passed=passed,
            eda_tool_failed=False,
            detail=detail,
            display=display,
        )

    def _persist_standalone_log(self, combined: str) -> str | None:
        """Persist the sweep's full per-module compiler output as run.log.

        Same durable-copy contract as :meth:`_persist_run_log`, but under a
        dedicated ``standalone-sweep`` work dir (the ``variant`` mechanism) so
        it can never clobber — or be clobbered by — a per-Target build's
        run.log. Best-effort: a log-write failure must never fail the run.
        """
        try:
            log_dir = edam_layer.work_root_for(
                self.args.work_dir,
                "elab",
                "standalone",
                variant="sweep",
            )
            log_dir.mkdir(parents=True, exist_ok=True)
            path = write_run_log(log_dir, combined)
        except OSError:
            logger.debug("could not persist standalone run.log", exc_info=True)
            return None
        return posix_relpath(path, self.args.work_dir)

    def _standalone_failure_lines(
        self,
        failures: list[dict[str, str]],
        log_pointer: str | None,
    ) -> list[str]:
        """Per-failure console lines: module, declaring file, compiler stderr.

        Echoes the first few failures with their (tail-capped) compiler output;
        the complete output for every module lives in the standalone run.log.
        """
        lines: list[str] = []
        tail_chars = output_budget.scaled(_ERROR_TAIL_CHARS)
        for f in failures[:_MAX_ECHOED_STANDALONE_FAILURES]:
            lines.append(f"[elab]   {f['module']} ({f['file']}):")
            err = f["error"] or "(no compiler output)"
            if len(err) > tail_chars:
                where = f", full log: {log_pointer}" if log_pointer else ""
                lines.append(f"... (truncated to last {tail_chars} chars{where})")
                err = err[-tail_chars:]
            lines.append(err)
        if len(failures) > _MAX_ECHOED_STANDALONE_FAILURES:
            more = len(failures) - _MAX_ECHOED_STANDALONE_FAILURES
            where = f" (see {log_pointer})" if log_pointer else ""
            lines.append(f"[elab]   ... and {more} more{where}")
        return lines

    @staticmethod
    def _standalone_ungraded_lines(
        frontend: str,
        unparsed: list[dict[str, str]],
        log_pointer: str | None,
    ) -> list[str]:
        """Name the modules the probe could not grade, next to the real failures.

        A sweep that both found a genuine standalone failure and hit a probe
        capability gap has two things to say; the gap must not swallow the
        failure (nor the reverse). One compact line per ungraded module — the
        failures own the verbose per-module echo — so the reader still learns
        the criterion covered less than the module count suggests.
        """
        where = f" (see {log_pointer})" if log_pointer else ""
        lines = [
            f"[elab]   {len(unparsed)} module(s) ungraded — {frontend} "
            f"cannot parse them, other frontend accepted the sources{where}:"
        ]
        for u in unparsed[:_MAX_ECHOED_STANDALONE_FAILURES]:
            gist = _extract_error_gist(u["error"]) or "(no compiler output)"
            lines.append(f"[elab]     {u['module']} ({u['file']}): {gist}")
        if len(unparsed) > _MAX_ECHOED_STANDALONE_FAILURES:
            more = len(unparsed) - _MAX_ECHOED_STANDALONE_FAILURES
            lines.append(f"[elab]     ... and {more} more")
        return lines

    @staticmethod
    def _parse_gap_message(
        frontend: str,
        unparsed: list[dict[str, str]],
        total: int,
    ) -> str:
        """Explain a probe-frontend capability gap and name the way out (F-25).

        Deliberately not phrased as a design failure: the per-Target elaborate
        accepted these same sources in this same invocation, so the only thing
        established is that *this frontend* cannot read them.
        """
        other = _FRONTEND_IVERILOG if frontend == _FRONTEND_VERILATOR else _FRONTEND_VERILATOR
        first = unparsed[0]
        gist = _extract_error_gist(first["error"]) or "(no compiler output)"
        return (
            f"{frontend} cannot parse {len(unparsed)}/{total} module(s) in the RTL "
            f"scope, but the per-Target elaborate accepted the same sources — this "
            f"is a frontend capability gap, not a design defect, so the sweep "
            f"reached no verdict. First: {first['module']} ({first['file']}): {gist}. "
            f"Pin a frontend that reads this RTL with [flows.elab] "
            f'standalone_frontend = "{other}", or drop the elaborate_standalone '
            f"criterion for this project."
        )

    def _standalone_error(
        self,
        message: str,
        *,
        log_pointer: str | None = None,
        extra_detail: dict[str, Any] | None = None,
    ) -> _StandaloneOutcome:
        """A standalone sweep that reached no verdict — Flow ERROR, criterion unmet."""
        detail: dict[str, Any] = {"error": message}
        if extra_detail:
            detail.update(extra_detail)
        if log_pointer:
            detail["log"] = log_pointer
        self.set_criterion(_STANDALONE_CRITERION, False, detail=detail)
        return _StandaloneOutcome(
            lines=[f"[elab] standalone ERROR: {message}"],
            passed=False,
            eda_tool_failed=True,
            detail=detail,
            display="standalone: ERROR",
        )

    def _run(self) -> McpToolResult:
        """Run elaboration for each requested Target."""
        selection = self._exec_selection
        exec_error = self.validate_execution(selection)
        if exec_error is not None:
            return McpToolResult(exit_code=EXIT_ERROR, report_text=exec_error)
        if not selection.enabled:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text="elab is disabled ([flows.elab].enabled = false).",
            )
        if not self.args.target:  # ADR 0030: fall back to [flows.elab].default_target
            self.args.target = resolve_flow_default_target(self.name, self.args.work_dir)
        err = self._validate_interactive_args()  # after the fallback, or it
        if err is not None:  # refuses a target-less call the config satisfies
            return err
        targets = fusesoc_registry.resolve_target_selection(
            self.args.target,
            self.args.work_dir,
        )
        if not targets:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(
                    "elab: no Target selected. Pass --target <name> or set "
                    "[flows.elab].default_target; a bare name must be unambiguous, "
                    "else qualify it as vlnv#name."
                ),
            )

        if self.args.dry_run:
            commands = {tgt: self._dry_run_command(tgt) for tgt in targets}
            output = json.dumps(commands, indent=2)
            print(output)
            return McpToolResult(exit_code=EXIT_SUCCESS, report_text=output)

        results, stdout_lines, all_passed, eda_tool_failed = self._elaborate_targets(targets)

        # Standalone sweep (elaborate_standalone): pursued when the ticket
        # declares the criterion or --standalone asks for it. Runs after the
        # per-target builds and merges into the same verdict taxonomy: a
        # module that fails to elaborate standalone is a design FAIL, an
        # unresolvable scope / unrunnable iverilog is a Flow ERROR.
        standalone: _StandaloneOutcome | None = None
        if self._standalone_requested():
            # The per-Target verdict is the sweep's control: it says whether
            # the design's own frontend accepted these sources, which is what
            # separates "the probe frontend is short" from "the RTL is broken".
            standalone = self._run_standalone_check(targets, primary_ok=all_passed)
            stdout_lines.extend(standalone.lines)
            all_passed = all_passed and standalone.passed
            eda_tool_failed = eda_tool_failed or standalone.eda_tool_failed

        passed_count = sum(1 for r in results if r["passed"])
        # A compiler that ran and rejected the RTL is a design FAIL; a setup
        # failure reached no verdict about the design and is a Flow ERROR.
        # Same taxonomy as `lint` on the identical source (F-29).
        verdict = "PASS" if all_passed else ("ERROR" if eda_tool_failed else "FAIL")
        summary = f"RESULT: {verdict} ({passed_count}/{len(targets)})"
        stdout_lines.append("")
        stdout_lines.append(summary)
        report_text = "\n".join(stdout_lines)
        print(report_text)

        display_lines = self._build_display_lines(results)

        detail: dict[str, Any] = {"targets": results}
        # Keyed by target, mirroring simulate's shape, so the MCP layer's
        # oversized-report rescue can find the pointers: it scans ``detail``
        # and one level below it, and a bare per-entry "log" key inside the
        # ``targets`` LIST is not reachable that way.
        artifacts = {
            r["target"]: {"log": r["log"]} for r in results if isinstance(r.get("log"), str)
        }
        if artifacts:
            detail["artifacts"] = artifacts
        if standalone is not None:
            detail["standalone"] = standalone.detail
            display_lines.append(standalone.display)
        return McpToolResult(
            exit_code=_elaborate_exit_code(all_passed, eda_tool_failed),
            report_text=report_text,
            display_lines=display_lines,
            detail=detail,
        )

    def _elaborate_targets(
        self, targets: list[str]
    ) -> tuple[list[dict[str, Any]], list[str], bool, bool]:
        """Elaborate each Target in turn.

        Returns ``(results, stdout_lines, all_passed, eda_tool_failed)``.

        Per-target setup failures are isolated so the loop continues to the
        remaining Targets, and are tracked separately from design failures:
        a Target whose EDAM/configure blew up produced no verdict about the
        RTL, so it grades as a Flow ERROR rather than claiming the design
        failed to elaborate (F-29).

        The build ``make`` runs through the shared Session Runtime executor.
        """
        label = "session-runtime"
        results: list[dict[str, Any]] = []
        stdout_lines: list[str] = []
        all_passed = True
        eda_tool_failed = False

        for target in targets:
            t0 = time.monotonic()
            # Claim this Target's run.log before anything can be written to
            # it: until _persist_run_log lands below, the file still holds the
            # previous run's output and a tail would read it as live (F-26).
            self._open_run_log(
                target, edam_layer.work_root_for(self.args.work_dir, "elab", target)
            )
            try:
                cmd = self._prepare_elab_command(target)
            except Exception as exc:  # noqa: BLE001 — isolate per-target setup failure; recorded as a FAIL and loop continues
                elapsed = time.monotonic() - t0
                combined = f"elab setup failed: {exc}"
                logger.debug("elaborate EDAM/configure failed for %s", target, exc_info=True)
                passed = False
                all_passed = False
                eda_tool_failed = True
                stdout_lines.append(f"[elab] {target} ({label}) ERROR (setup)")
                stdout_lines.append(combined)
                # run.log parity even on setup failure: the message IS the
                # full output, and a durable copy keeps the report's log key
                # honest (never pointing at a stale earlier build's log).
                log_pointer = self._persist_run_log(target, combined)
                self._write_target_report(
                    target,
                    passed,
                    elapsed,
                    combined,
                    log_pointer=log_pointer,
                )
                self.set_criterion(
                    f"elab_pass_{target}",
                    passed,
                    source_target=target,
                    detail={
                        "target": target,
                        "elapsed_s": round(elapsed, 3),
                        "error_gist": _extract_error_gist(combined),
                    },
                )
                result = {
                    "target": target,
                    "passed": passed,
                    "error_gist": _extract_error_gist(combined),
                    "log": log_pointer,
                }
                self._append_target_result(results, result, stream=len(targets) > 1)
                continue
            # An ASIC Target runs Booley's own frontend chain directly (F-31);
            # everything else make-drives Edalize through the shared executor.
            if target in self._asic_targets():
                proc = self._execute(cmd)
            else:
                proc = self._execute_boundary(cmd)
            elapsed = time.monotonic() - t0
            combined = proc.stdout + proc.stderr
            passed = proc.returncode == 0
            if passed:
                # Clean elaboration: the build tree has served its purpose
                # (F-33). run.log is written below, from the outer work root.
                self._discard_build_dir(target)
            else:
                all_passed = False

            # Name the resolved EDA tool when known (resolution may have failed
            # or been mocked away).
            eda_tool = self._eda_tool_for(target)
            eda_tool_label = f"{label}, {eda_tool}" if eda_tool else label
            stdout_lines.append(
                f"[elab] {target} ({eda_tool_label})"
                f"{' ' * max(1, 12 - len(target))}"
                f"{'PASS' if passed else 'FAIL'}   {elapsed:.1f}s"
            )
            # Persist the full combined output as run.log on pass AND fail
            # (simulate/lint parity) — the console tail below is capped, and
            # without a durable copy the rest is gone after MCP truncation.
            log_pointer = self._persist_run_log(target, combined)
            if not passed:
                tail_chars = output_budget.scaled(_ERROR_TAIL_CHARS)
                if len(combined) > tail_chars:
                    # Truncation must be explicit (a silently clipped excerpt
                    # reads as the whole story) and must name the full log.
                    where = f", full log: {log_pointer}" if log_pointer else ""
                    stdout_lines.append(f"... (truncated to last {tail_chars} chars{where})")
                stdout_lines.append(combined[-tail_chars:])
                stdout_lines.extend(self._failure_context_lines(target, log_pointer))

            self._write_target_report(
                target,
                passed,
                elapsed,
                combined,
                log_pointer=log_pointer,
            )
            self.set_criterion(
                f"elab_pass_{target}",
                passed,
                source_target=target,
                detail={
                    "target": target,
                    "elapsed_s": round(elapsed, 3),
                    "error_gist": _extract_error_gist(combined) if not passed else "",
                },
            )
            result = {
                "target": target,
                "passed": passed,
                "error_gist": (_extract_error_gist(combined) if not passed else ""),
                # Carried per Target (not once per run) because each Target
                # writes its own run.log; this is the copy that reaches the
                # agent as MCP structuredContent, where the stdout pointer
                # above does not survive truncation.
                "log": log_pointer,
            }
            self._append_target_result(results, result, stream=len(targets) > 1)

        return results, stdout_lines, all_passed, eda_tool_failed

    def _append_target_result(
        self,
        results: list[dict[str, Any]],
        result: dict[str, Any],
        *,
        stream: bool,
    ) -> None:
        """Record a Target result and stream its final line when requested."""
        results.append(result)
        if stream:
            for line in self._target_display_lines(result):
                self.emit_completion(line, repeats_at_end=True)

    @staticmethod
    def _build_display_lines(results: list[dict[str, Any]]) -> list[str]:
        """Compact display lines for the terminal Flow box."""
        total = len(results)
        if total == 1:
            r = results[0]
            status = "PASS" if r["passed"] else "FAIL"
            line = f"{r['target']}: {status}"
            if r.get("error_gist") and not r["passed"]:
                return [line, f"  err: {r['error_gist']}"]
            return [line]
        passed_count = sum(1 for r in results if r["passed"])
        lines = [f"{passed_count}/{total} targets"]
        visible_results, omitted_line = cap_target_items(results)
        for r in visible_results:
            lines.extend(ElaborateFlow._target_display_lines(r))
        if omitted_line:
            lines.append(omitted_line)
        return lines

    @staticmethod
    def _target_display_lines(result: dict[str, Any]) -> list[str]:
        """Build the final display line for one completed Target."""
        icon = "+" if result["passed"] else "x"
        detail = ""
        if not result["passed"] and result.get("error_gist"):
            detail = f" {result['error_gist'][:50]}"
        return [f"  {icon} {result['target']}{detail}"]


if __name__ == "__main__":
    ElaborateFlow().cli()
