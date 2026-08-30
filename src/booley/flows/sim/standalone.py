"""Standalone module elaboration support for Simulation's elab-only mode."""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from booley.flows.eda_parsers import extract_error_gist
from booley.fusesoc import fusesoc_registry
from booley.targets.target import inspect_target

from .. import edam as edam_layer
from .. import output_budget
from ..flow_config import _load_flow_config

logger = logging.getLogger(__name__)

# Max chars of error output retained in the report / displayed. This is the
# 12KB-MCP-budget default; the effective cap scales with a raised
# BOOLEY_MCP_MAX_STDOUT_BYTES (see output_budget.scaled). The full untruncated
# output is persisted as run.log in the elaborate work root either way.
_ERROR_TAIL_CHARS = 2000

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
# `[flows.sim].standalone_frontend` values that pin one.
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


@dataclass
class _StandaloneOutcome:
    """Result of one standalone-elaboration sweep, ready to merge into _run."""

    lines: list[str] = field(default_factory=list)
    passed: bool = False
    eda_tool_failed: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    display: str = ""


class StandaloneMixin:
    """Standalone module sweep mixed into :class:`SimulateFlow`."""

    def _standalone_requested(self) -> bool:
        """Whether the caller explicitly requested the standalone sweep."""
        return bool(getattr(self.args, "standalone", False))

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
            for rel in inspect_target(self.args.work_dir, tgt).rtl_files:
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

        ``[flows.sim].standalone_frontend``: ``iverilog`` or
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
                "[flows.sim] 'standalone_frontend' must be one of "
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
        """Run the optional per-module sweep and record its criterion."""
        t0 = time.monotonic()
        prepared = self._prepare_standalone_check(targets)
        if isinstance(prepared, _StandaloneOutcome):
            return prepared
        frontend, modules, shared = prepared
        failures, unparsed, log_chunks, eda_tool_error = self._run_standalone_probes(
            modules,
            shared,
            frontend,
            gap_is_credible=self._parse_gap_is_credible(frontend, primary_ok),
        )
        log_pointer = self._persist_standalone_log("".join(log_chunks))
        error = self._standalone_probe_error(
            frontend,
            modules,
            failures,
            unparsed,
            eda_tool_error,
            log_pointer,
        )
        if error is not None:
            return error
        return self._standalone_verdict(
            frontend,
            modules,
            shared,
            failures,
            unparsed,
            log_pointer,
            time.monotonic() - t0,
        )

    def _prepare_standalone_check(
        self,
        targets: list[str],
    ) -> tuple[str, list[tuple[str, str]], list[str]] | _StandaloneOutcome:
        """Resolve the frontend and non-vacuous RTL module scope."""
        self._open_run_log(
            "standalone",
            edam_layer.work_root_for(self.args.work_dir, "sim", "standalone", variant="sweep"),
        )
        try:
            frontend = self._resolve_standalone_frontend()
        except ValueError as exc:
            return self._standalone_error(str(exc))
        try:
            scope = self._standalone_rtl_scope(targets)
        except (fusesoc_registry.FuseSocError, OSError) as exc:
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
        return frontend, modules, shared

    def _standalone_probe_error(
        self,
        frontend: str,
        modules: list[tuple[str, str]],
        failures: list[dict[str, str]],
        unparsed: list[dict[str, str]],
        eda_tool_error: str,
        log_pointer: str | None,
    ) -> _StandaloneOutcome | None:
        """Return the no-verdict outcome for probe infrastructure/gaps."""
        if eda_tool_error:
            return self._standalone_error(eda_tool_error, log_pointer=log_pointer)
        if unparsed and not failures:
            return self._standalone_error(
                self._parse_gap_message(frontend, unparsed, len(modules)),
                log_pointer=log_pointer,
                extra_detail={
                    "frontend": frontend,
                    "unparsed": _standalone_entries(unparsed),
                },
            )
        return None

    def _standalone_verdict(
        self,
        frontend: str,
        modules: list[tuple[str, str]],
        shared: list[str],
        failures: list[dict[str, str]],
        unparsed: list[dict[str, str]],
        log_pointer: str | None,
        elapsed: float,
    ) -> _StandaloneOutcome:
        """Compose and record one completed standalone sweep verdict."""
        passed = not failures
        header = (
            f"[sim:elab-only] standalone ({len(modules)} modules, "
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
        detail = self._standalone_detail(
            frontend, modules, shared, failures, unparsed, log_pointer
        )
        if self.args.state_file is not None:
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

    @staticmethod
    def _standalone_detail(
        frontend: str,
        modules: list[tuple[str, str]],
        shared: list[str],
        failures: list[dict[str, str]],
        unparsed: list[dict[str, str]],
        log_pointer: str | None,
    ) -> dict[str, Any]:
        """Build the durable detail payload for a completed sweep."""
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
        return detail

    def _persist_standalone_log(self, combined: str) -> str | None:
        """Persist the sweep's full per-module compiler output as run.log.

        Same durable-copy contract as :meth:`_persist_run_log`, but under a
        dedicated ``standalone-sweep`` work dir (the ``variant`` mechanism) so
        it can never clobber — or be clobbered by — a per-Target build's
        run.log. Best-effort: a log-write failure must never fail the run.
        """
        pointer = self._persist_elab_only_log("standalone", combined)
        return pointer or None

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
            lines.append(f"[sim:elab-only]   {f['module']} ({f['file']}):")
            err = f["error"] or "(no compiler output)"
            if len(err) > tail_chars:
                where = f", full log: {log_pointer}" if log_pointer else ""
                lines.append(f"... (truncated to last {tail_chars} chars{where})")
                err = err[-tail_chars:]
            lines.append(err)
        if len(failures) > _MAX_ECHOED_STANDALONE_FAILURES:
            more = len(failures) - _MAX_ECHOED_STANDALONE_FAILURES
            where = f" (see {log_pointer})" if log_pointer else ""
            lines.append(f"[sim:elab-only]   ... and {more} more{where}")
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
            f"[sim:elab-only]   {len(unparsed)} module(s) ungraded — {frontend} "
            f"cannot parse them, other frontend accepted the sources{where}:"
        ]
        for u in unparsed[:_MAX_ECHOED_STANDALONE_FAILURES]:
            gist = _extract_error_gist(u["error"]) or "(no compiler output)"
            lines.append(f"[sim:elab-only]     {u['module']} ({u['file']}): {gist}")
        if len(unparsed) > _MAX_ECHOED_STANDALONE_FAILURES:
            more = len(unparsed) - _MAX_ECHOED_STANDALONE_FAILURES
            lines.append(f"[sim:elab-only]     ... and {more} more")
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
            f"Pin a frontend that reads this RTL with [flows.sim] "
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
        """Return a no-verdict standalone Flow ERROR without changing Criteria."""
        detail: dict[str, Any] = {"error": message}
        if extra_detail:
            detail.update(extra_detail)
        if log_pointer:
            detail["log"] = log_pointer
        return _StandaloneOutcome(
            lines=[f"[sim:elab-only] standalone ERROR: {message}"],
            passed=False,
            eda_tool_failed=True,
            detail=detail,
            display="standalone: ERROR",
        )
