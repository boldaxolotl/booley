"""Standalone module elaboration support for Simulation's elab-only mode."""

from __future__ import annotations

import logging
import math
import os
import re
import shlex
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any, cast

from booley.core.build_paths import work_root_for
from booley.core.hdl import declared_module_names, strip_comments
from booley.flows.eda_parsers import extract_error_gist
from booley.flows.sim.live_progress import attempt_scope, observe_stage
from booley.fusesoc import fusesoc_registry
from booley.runtime.timefmt import format_duration
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetInspection

from .. import output_budget
from .mode import SimulationMode

logger = logging.getLogger(__name__)


# Max chars of error output retained in the report / displayed. This is the
# 12KB-MCP-budget default; the effective cap scales with a raised
# BOOLEY_MCP_MAX_STDOUT_BYTES (see output_budget.scaled). The full untruncated
# output is persisted as run.log in the elaborate work root either way.
_ERROR_TAIL_CHARS = 2000

# The compiler-error gist extractor lives in the shared parser module.
_extract_error_gist = extract_error_gist


# One probe outcome: module, file, frontend, dialect flags, fallback flag, and
# compiler output.
_ProbeRecord = dict[str, Any]


def _standalone_entries(records: list[_ProbeRecord]) -> list[dict[str, Any]]:
    """Report-shaped rows for a list of standalone probe outcomes.

    The full compiler output stays in the sweep's run.log; the report keeps
    only the gist. Shared by the ``failures`` and ``unparsed`` lists so the two
    never drift into different shapes. Each row names the frontend that
    produced it, the dialect flags it ran with, and whether that frontend was a
    fallback probe.
    """
    return [
        {
            "module": r["module"],
            "file": r["file"],
            "frontend": r["frontend"],
            "dialect_flags": r["dialect_flags"],
            "fallback": r["fallback"],
            "error_gist": _extract_error_gist(r["error"]),
        }
        for r in records
    ]


# ---------------------------------------------------------------------------
# Standalone-elaboration check (`elaborate_standalone` criterion, ADR 0089)
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

# Shared-prerequisite markers: a file declaring a `package` or `interface` is
# included on EVERY per-module compile line, so a legitimate `import pkg::*`
# or interface port never scores as a cross-file dependency (false positive).
_SHARED_DECL_RE = re.compile(r"^\s*(?:package|interface)\s+", re.MULTILINE)

# Frontends the per-module standalone probe can drive. ADR 0089: each module is
# probed with the simulator of the Target(s) that use it, so the probe compiles
# the module with the compiler the Target build itself drives (an Icarus-only
# project never meets a Verilator-only parse gap, and vice versa).
_FRONTEND_IVERILOG = "iverilog"
_FRONTEND_VERILATOR = "verilator"

# Edalize eda_tool name -> the probe frontend that is *the same compiler*.
# Only the two the probe can drive need an entry; every other eda_tool a Target
# may resolve to is by construction a
# different compiler from the probe. Edalize spells Icarus `icarus`; older
# Booley configs said `iverilog` (see normalize_eda_tool).
_EDA_TOOL_AS_FRONTEND = {
    "verilator": _FRONTEND_VERILATOR,
    "icarus": _FRONTEND_IVERILOG,
    "iverilog": _FRONTEND_IVERILOG,
}

# Target option key that carries each frontend's compiler options (Edalize
# spells them ``<tool>_options``), in both ``flow_options`` and the legacy
# ``tools: <tool>:`` section.
_FRONTEND_OPTIONS_KEY = {
    _FRONTEND_IVERILOG: "iverilog_options",
    _FRONTEND_VERILATOR: "verilator_options",
}

# Dialect flags a fallback probe runs with: the Target's own flags do not apply
# to a compiler the Target does not use. Icarus needs SystemVerilog enabled to
# parse the sources at all; Verilator's default already accepts them.
_FALLBACK_FLAGS = {
    _FRONTEND_IVERILOG: ("-g2012",),
    _FRONTEND_VERILATOR: (),
}

# Verilator options that select the language generation, each followed by its
# value token (``--default-language 1364-2005``).
_VERILATOR_LANGUAGE_OPTIONS = frozenset(
    {"--language", "-language", "--default-language", "-default-language"}
)

# Verilator ``+<generation>ext+<extensions>`` language-extension options. A
# closed generation list keeps ``+libext+``, ``+incdir+`` and ``+define+`` out.
_VERILATOR_LANGUAGE_EXT_RE = re.compile(
    r"^\+(?:1364-\d{4}|1800-\d{4}|1800\+VAMS|systemverilog|verilog1995|verilog2001)ext\+"
)

# Compiler output that means "this frontend cannot parse the construct",
# as opposed to "your design is broken": Icarus emits a bare syntax error on
# SystemVerilog it never implemented, Verilator names it `Unsupported:`.
# A genuinely malformed source produces the identical text, so this pattern is
# only ever consulted when the claim "the *other* frontend accepted these very
# sources" can actually be made — only a fallback probe (a Target simulator the
# probe cannot drive) can make it; see _StandaloneProbe.fallback.
_PARSE_GAP_RE = re.compile(r"syntax error|Unsupported:|sorry: ", re.IGNORECASE)


def _scan_hdl_declarations(text: str) -> tuple[list[str], bool]:
    """(declared module names, declares-a-package-or-interface) for one file.

    Module names skip comments and strings; shared markers use comment-stripped
    text. Preprocessor-conditional declarations are taken at face value: a
    module only declared under an ``ifdef`` still gets probed with defaults,
    which is the check's intent (default parameterization / default defines
    must elaborate).
    """
    stripped = strip_comments(text)
    modules = declared_module_names(text)
    has_shared = bool(_SHARED_DECL_RE.search(stripped))
    return modules, has_shared


@dataclass(frozen=True, order=True)
class _ProbeDialect:
    """A probe compiler plus the language-dialect flags of the Target using it."""

    frontend: str
    flags: tuple[str, ...]


def _is_option_sequence(value: object) -> bool:
    """Whether *value* is an authored option list (a list or the tuple it is frozen into)."""
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _option_tokens(options: object) -> list[str]:
    """Flatten authored option entries into one shell-split token list.

    Edalize joins every entry with spaces, so ``["--default-language",
    "1364-2005"]`` and ``["--default-language 1364-2005"]`` are the same
    command line. A value that is not a sequence (or is a bare string)
    contributes nothing; an entry that does not shell-split is kept whole.
    ``TargetInspection`` freezes authored lists into tuples, so any sequence
    counts.
    """
    if not _is_option_sequence(options):
        return []
    tokens: list[str] = []
    for option in cast("Sequence[object]", options):
        try:
            tokens.extend(shlex.split(str(option)))
        except ValueError:
            tokens.append(str(option))
    return tokens


def _dialect_flags(frontend: str, options: object) -> tuple[str, ...]:
    """The language-dialect flags among a Target's compiler *options*, in order.

    Only the flags that change which language the compiler accepts are kept:
    Icarus ``-g*`` generation selectors; Verilator ``--language`` /
    ``--default-language`` with their value and ``+<generation>ext+`` options.
    Defines, include paths and every other option are Target build settings,
    not dialect, and never reach the probe. Authored order is preserved because
    both compilers let the last language flag win.
    """
    tokens = _option_tokens(options)
    flags: list[str] = []
    take_value = False
    for token in tokens:
        if take_value:
            flags.append(token)
            take_value = False
        elif frontend == _FRONTEND_IVERILOG and token.startswith("-g"):
            flags.append(token)
            # A bare ``-g`` takes its generation as the next token.
            take_value = token == "-g"
        elif frontend == _FRONTEND_VERILATOR and token in _VERILATOR_LANGUAGE_OPTIONS:
            flags.append(token)
            take_value = True
        elif frontend == _FRONTEND_VERILATOR and _VERILATOR_LANGUAGE_EXT_RE.match(token):
            flags.append(token)
    return tuple(flags)


def _probe_label(frontend: str, flags: tuple[str, ...]) -> str:
    """Human label naming a probe's compiler and dialect, e.g. ``iverilog -g2005``."""
    return shlex.join([frontend, *flags]) if flags else f"{frontend} (default dialect)"


def _target_dialect(inspection: TargetInspection) -> _ProbeDialect | None:
    """The probe dialect a Target's simulator implies, or ``None`` if not drivable.

    Options are read the way the Target build reads them: modern
    ``flow_options`` first, then the legacy ``tools: <tool>:`` options.
    """
    eda_tool = inspection.eda_tool
    frontend = _EDA_TOOL_AS_FRONTEND.get(eda_tool.lower()) if isinstance(eda_tool, str) else None
    if frontend is None:
        return None
    key = _FRONTEND_OPTIONS_KEY[frontend]
    options: list[object] = []
    for authored in (inspection.flow_options.get(key), inspection.tool_options.get(key)):
        if _is_option_sequence(authored):
            options.extend(cast("Sequence[object]", authored))
    return _ProbeDialect(frontend, _dialect_flags(frontend, options))


@dataclass
class _StandaloneOutcome:
    """Result of one standalone-elaboration sweep, ready to merge into _run."""

    lines: list[str] = field(default_factory=list)
    passed: bool = False
    eda_tool_failed: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    display: str = ""


@dataclass(frozen=True)
class _StandaloneProbe:
    """One planned per-module compile: compiler, dialect flags, and fallback role."""

    module: str
    file: str
    # "iverilog" | "verilator"
    frontend: str
    # Language-dialect flags of the Target(s) this probe stands for; empty means
    # the compiler's default dialect.
    flags: tuple[str, ...]
    # True only for a fallback probe that no covering Target's own compiler matches.
    fallback: bool
    # Shared package/interface files this probe compiles before the module.
    prereqs: tuple[str, ...]


@dataclass(frozen=True)
class _StandalonePlanRecipe:
    """Resolved standalone sweep shared by planning and execution."""

    probes: tuple[_StandaloneProbe, ...]
    shared: tuple[str, ...]
    commands: tuple[tuple[str, ...], ...]

    @property
    def modules(self) -> tuple[tuple[str, str], ...]:
        """Distinct ``(module, file)`` pairs in first-seen order."""
        return tuple(dict.fromkeys((p.module, p.file) for p in self.probes))

    @property
    def frontends(self) -> tuple[str, ...]:
        """Distinct probe frontends, sorted."""
        return tuple(sorted({p.frontend for p in self.probes}))

    @property
    def frontend_label(self) -> str:
        """``+``-joined frontends; the plain frontend name for a one-frontend sweep."""
        return "+".join(self.frontends)


class StandaloneMixin:
    """Standalone module sweep mixed into :class:`SimulateFlow`."""

    def _standalone_requested(self) -> bool:
        """Whether the caller explicitly requested the standalone sweep."""
        mode = getattr(self.args, "mode", None)
        return isinstance(mode, SimulationMode) and mode.includes_standalone

    def _standalone_rtl_scope(self, targets: list[str]) -> dict[str, set[_ProbeDialect | None]]:
        """Project-relative HDL files in the Targets' RTL scope, with their dialects.

        Reuses the same fileset resolution the per-target report already
        relies on (``target_source_files``, dependency closure included): the
        RTL/TB partition comes from the ``.core`` ``tags:[tb]`` marker, so TB
        and vendor/harness files outside the declared RTL filesets are exempt
        by construction. Union across the selected Targets, first-seen order,
        narrowed to compiled (System)Verilog sources. Each file maps to the
        native probe dialects (frontend plus the Target's language flags) of
        the Targets covering it; ``None`` means a
        covering Target simulates with a tool the probe cannot drive and needs
        a fallback. Raises on resolution failure — the caller grades that a
        Flow ERROR (no verdict reached).
        """
        coverage: dict[str, set[_ProbeDialect | None]] = {}
        catalog = TargetCatalog.build(self.args.work_dir)
        for tgt in targets:
            inspection = catalog.inspect(self._target_handle(tgt))
            native = _target_dialect(inspection)
            for rel in inspection.rtl_files:
                coverage.setdefault(rel, set()).add(native)
        return {
            rel: needs
            for rel, needs in coverage.items()
            if Path(rel).suffix.lower() in _HDL_SUFFIXES
        }

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

    @staticmethod
    def _standalone_fallback_frontend() -> str:
        """Probe compiler for Targets whose simulator the probe cannot drive.

        Verilator when it is on PATH (the stricter, SystemVerilog-complete
        frontend), otherwise Icarus.
        """
        return _FRONTEND_VERILATOR if shutil.which(_FRONTEND_VERILATOR) else _FRONTEND_IVERILOG

    @staticmethod
    def _file_probes(
        needs: set[_ProbeDialect | None], fallback: str | None
    ) -> list[tuple[_ProbeDialect, bool]]:
        """``(dialect, is_fallback)`` probes one file needs, in stable order.

        Every distinct native dialect of a covering Target probes once. A
        Target whose simulator is not drivable adds the fallback probe, unless
        the fallback compiler already is one of the native frontends — then
        that native probe is graded normally.
        """
        probes = [(dialect, False) for dialect in sorted(d for d in needs if d is not None)]
        if (
            None in needs
            and fallback is not None
            and all(dialect.frontend != fallback for dialect, _ in probes)
        ):
            probes.append((_ProbeDialect(fallback, _FALLBACK_FLAGS[fallback]), True))
        return probes

    def _standalone_probes(
        self,
        modules: list[tuple[str, str]],
        shared: list[str],
        coverage: dict[str, set[_ProbeDialect | None]],
    ) -> list[_StandaloneProbe]:
        """Expand every ``(module, file)`` into one probe per covering dialect.

        A probe compiles only the shared package/interface files covered by the
        Targets that contribute its frontend and dialect flags, so an Icarus
        probe never builds a package only a Verilator Target uses, nor a
        ``-g2005`` probe one only a ``-g2012`` Target uses.
        """
        fallback = (
            self._standalone_fallback_frontend()
            if any(None in needs for needs in coverage.values())
            else None
        )
        probes: list[_StandaloneProbe] = []
        for module, rel in modules:
            for dialect, is_fallback in self._file_probes(coverage[rel], fallback):
                contributors: set[_ProbeDialect | None] = {None} if is_fallback else {dialect}
                if not is_fallback and dialect.frontend == fallback and None in coverage[rel]:
                    contributors.add(None)
                prereqs = tuple(s for s in shared if s != rel and coverage[s] & contributors)
                probes.append(
                    _StandaloneProbe(
                        module, rel, dialect.frontend, dialect.flags, is_fallback, prereqs
                    )
                )
        return probes

    def _standalone_compile_command(
        self,
        module: str,
        rel: str,
        shared: list[str],
        frontend: str,
        flags: tuple[str, ...],
    ) -> list[str]:
        """The per-module probe: elaborate *module* from its declaring file only.

        The declaring file plus the shared package/interface files (minus the
        declaring file itself when it is one of them), compiled with the
        Target's language-dialect *flags* and nothing else of its options.
        Parameter defaults only — no ``-P``/``-G`` overrides: a default
        parameterization that fails to elaborate is a finding.

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
                *flags,
                "--top-module",
                module,
                *prereqs,
                rel,
            ]
        # ``-o`` goes to the null device so the probe never litters the worktree.
        return ["iverilog", *flags, "-o", os.devnull, "-s", module, *prereqs, rel]

    def _run_standalone_probes(
        self,
        probes: tuple[_StandaloneProbe, ...],
        *,
        primary_ok: bool,
    ) -> tuple[list[_ProbeRecord], list[_ProbeRecord], list[str], str]:
        """Probe every planned compile under one deadline and classify its outcome.

        Only a fallback probe can be graded "ungraded" (a parse gap), and only
        when the per-Target legs accepted the sources (*primary_ok*): a probe
        that runs the Target's own compiler has no excuse for a parse error.
        """
        failures: list[_ProbeRecord] = []
        unparsed: list[_ProbeRecord] = []
        log_chunks: list[str] = []
        label = "+".join(sorted({p.frontend for p in probes}))
        deadline = monotonic() + (self._effective_timeout_ms() / 1000)
        for probe in probes:
            remaining_s = deadline - monotonic()
            if remaining_s <= 0:
                return (
                    failures,
                    unparsed,
                    log_chunks,
                    f"{label} standalone sweep timed out after {self._get_timeout()}s",
                )
            record, log_chunk, eda_tool_error = self._run_standalone_probe(
                probe,
                timeout_s=max(1, math.ceil(remaining_s)),
            )
            role = " (fallback)" if probe.fallback else ""
            via = _probe_label(probe.frontend, probe.flags)
            log_chunks.append(
                f"# standalone probe: {probe.module} ({probe.file}) via {via}{role}\n{log_chunk}"
            )
            if eda_tool_error:
                return failures, unparsed, log_chunks, eda_tool_error
            if record is not None:
                record["frontend"] = probe.frontend
                record["dialect_flags"] = list(probe.flags)
                record["fallback"] = probe.fallback
                parse_gap = probe.fallback and primary_ok and _PARSE_GAP_RE.search(record["error"])
                destination = unparsed if parse_gap else failures
                destination.append(record)
        return failures, unparsed, log_chunks, ""

    def _run_standalone_probe(
        self,
        probe: _StandaloneProbe,
        *,
        timeout_s: int,
    ) -> tuple[_ProbeRecord | None, str, str]:
        """Run one bounded probe and return its finding, log, and tool error."""
        module, rel, frontend = probe.module, probe.file, probe.frontend
        with attempt_scope(module, identity=module, operation="standalone"):
            command = self._standalone_compile_command(
                module, rel, list(probe.prereqs), frontend, probe.flags
            )
            observe_stage(module, "building")
            proc = self._execute(command, timeout=timeout_s)
        combined = (proc.stdout + proc.stderr).strip()
        log_chunk = f"$ {shlex.join(command)}\n{combined}\n"
        if proc.returncode == 0:
            return None, log_chunk, ""
        if proc.returncode >= 0:
            return {"module": module, "file": rel, "error": combined}, log_chunk, ""
        message = (
            f"{frontend} timed out after {self._get_timeout()}s"
            if proc.timed_out
            else f"{frontend} could not run (is it installed in the Sandbox?)"
        )
        if combined:
            message += f": {combined}"
        return None, log_chunk, message

    def _run_standalone_check(
        self,
        targets: list[str],
        *,
        primary_ok: bool = True,
    ) -> _StandaloneOutcome:
        """Run the optional per-module sweep and record its criterion."""
        t0 = monotonic()
        recipe = self._prepare_standalone_check(targets)
        if isinstance(recipe, _StandaloneOutcome):
            return recipe
        # Only a one-frontend sweep names a run-level tool; a mixed sweep leaves
        # the scalar unset rather than inventing one.
        if self._eda_tool is None and len(recipe.frontends) == 1:
            self._eda_tool = recipe.frontends[0]
        failures, unparsed, log_chunks, eda_tool_error = self._run_standalone_probes(
            recipe.probes,
            primary_ok=primary_ok,
        )
        log_pointer = self._persist_standalone_log("".join(log_chunks))
        error = self._standalone_probe_error(
            recipe,
            failures,
            unparsed,
            eda_tool_error,
            log_pointer,
        )
        if error is not None:
            return error
        return self._standalone_verdict(
            recipe,
            failures,
            unparsed,
            log_pointer,
            monotonic() - t0,
        )

    def _prepare_standalone_check(
        self,
        targets: list[str],
    ) -> _StandalonePlanRecipe | _StandaloneOutcome:
        """Return the planned (or freshly planned) recipe, or the error outcome."""
        self._open_run_log(
            "standalone",
            work_root_for(self.args.work_dir, "sim", "standalone", variant="sweep"),
        )
        planned = getattr(self, "_standalone_plan_recipe", None)
        if isinstance(planned, _StandalonePlanRecipe):
            return planned
        try:
            return self._plan_standalone_check(targets)
        except ValueError as exc:
            return self._standalone_error(str(exc))

    def _plan_standalone_check(self, targets: list[str]) -> _StandalonePlanRecipe:
        """Resolve the complete standalone scope without opening logs or running tools."""
        bound = self.state.criteria.get(_STANDALONE_CRITERION)
        expected = (bound.params or {}).get("targets") if bound is not None else None
        if isinstance(expected, list):
            catalog = TargetCatalog.build(self.args.work_dir)
            selected = {catalog.select(target).identity for target in targets}
            if selected != set(expected):
                raise ValueError(
                    "elab-only-standalone must select exactly the Criterion's frozen Target set"
                )
        try:
            coverage = self._standalone_rtl_scope(targets)
        except (fusesoc_registry.FuseSocError, OSError) as exc:
            logger.debug("standalone: RTL scope resolution failed", exc_info=True)
            raise ValueError(f"could not resolve RTL source scope: {exc}") from exc
        modules, shared = self._scan_standalone_scope(list(coverage))
        if not modules:
            # Zero modules would make a green criterion vacuous — the same
            # false-pass family the lint toplevel check hard-fails.
            raise ValueError(
                f"no module declarations found in the RTL source scope "
                f"({len(coverage)} files) — the criterion would be vacuous. "
                "Check the Targets' RTL filesets.",
            )
        probes = self._standalone_probes(modules, shared, coverage)
        commands = tuple(
            tuple(
                self._standalone_compile_command(
                    probe.module, probe.file, list(probe.prereqs), probe.frontend, probe.flags
                )
            )
            for probe in probes
        )
        return _StandalonePlanRecipe(
            probes=tuple(probes),
            shared=tuple(shared),
            commands=commands,
        )

    def _standalone_probe_error(
        self,
        recipe: _StandalonePlanRecipe,
        failures: list[_ProbeRecord],
        unparsed: list[_ProbeRecord],
        eda_tool_error: str,
        log_pointer: str | None,
    ) -> _StandaloneOutcome | None:
        """Return the no-verdict outcome for probe infrastructure/gaps."""
        if eda_tool_error:
            return self._standalone_error(eda_tool_error, log_pointer=log_pointer)
        if unparsed and not failures:
            return self._standalone_error(
                self._parse_gap_message(unparsed, len(recipe.modules)),
                log_pointer=log_pointer,
                extra_detail={
                    "frontend": recipe.frontend_label,
                    "unparsed": _standalone_entries(unparsed),
                },
            )
        return None

    def _standalone_verdict(
        self,
        recipe: _StandalonePlanRecipe,
        failures: list[_ProbeRecord],
        unparsed: list[_ProbeRecord],
        log_pointer: str | None,
        elapsed: float,
    ) -> _StandaloneOutcome:
        """Compose and record one completed standalone sweep verdict."""
        passed = not failures
        module_count = len(recipe.modules)
        # A module shared by two frontends can fail under both: count it once.
        failed_modules = len({(f["module"], f["file"]) for f in failures})
        header = (
            f"[sim:elab-only] standalone ({module_count} modules, "
            f"{len(recipe.shared)} shared, {recipe.frontend_label}) "
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
            lines.extend(self._standalone_ungraded_lines(unparsed, log_pointer))
        detail = self._standalone_detail(recipe, failures, unparsed, log_pointer)
        detail["mode"] = self.args.mode.value
        self.set_criterion(_STANDALONE_CRITERION, passed, detail=detail)
        # `passed` implies no unparsed modules (a gap-only sweep returned above).
        ungraded = f" ({len(unparsed)} ungraded)" if unparsed else ""
        display = (
            f"standalone: {module_count} modules OK"
            if passed
            else f"standalone: {failed_modules}/{module_count} modules FAIL{ungraded}"
        )
        return _StandaloneOutcome(
            lines=lines,
            passed=passed,
            eda_tool_failed=False,
            detail=detail,
            display=f"{display} · {format_duration(elapsed)}",
        )

    @staticmethod
    def _standalone_detail(
        recipe: _StandalonePlanRecipe,
        failures: list[_ProbeRecord],
        unparsed: list[_ProbeRecord],
        log_pointer: str | None,
    ) -> dict[str, Any]:
        """Build the durable detail payload for a completed sweep.

        ``frontend`` is the ``+``-joined probe frontend label (the plain
        frontend name for a one-frontend sweep); ``probes`` groups the
        ``(module, file)`` pairs by ``(frontend, dialect flags, fallback)``.
        """
        groups: dict[tuple[str, tuple[str, ...], bool], list[dict[str, str]]] = {}
        for probe in recipe.probes:
            groups.setdefault((probe.frontend, probe.flags, probe.fallback), []).append(
                {"module": probe.module, "file": probe.file}
            )
        detail: dict[str, Any] = {
            "modules_checked": len(recipe.modules),
            "shared_files": list(recipe.shared),
            "frontend": recipe.frontend_label,
            "probes": [
                {
                    "frontend": frontend,
                    "dialect_flags": list(flags),
                    "fallback": fallback,
                    "modules": modules,
                }
                for (frontend, flags, fallback), modules in sorted(groups.items())
            ],
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
        pointer = self._persist_elab_only_log("standalone-sweep", combined)
        return pointer or None

    def _standalone_failure_lines(
        self,
        failures: list[_ProbeRecord],
        log_pointer: str | None,
    ) -> list[str]:
        """Per-failure console lines: module, declaring file, probe dialect, compiler stderr.

        Echoes the first few failures with their (tail-capped) compiler output;
        the complete output for every module lives in the standalone run.log.
        """
        lines: list[str] = []
        tail_chars = output_budget.scaled(_ERROR_TAIL_CHARS)
        for f in failures[:_MAX_ECHOED_STANDALONE_FAILURES]:
            via = _probe_label(f["frontend"], tuple(f["dialect_flags"]))
            lines.append(f"[sim:elab-only]   {f['module']} ({f['file']}) via {via}:")
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
        unparsed: list[_ProbeRecord],
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
        frontends = "+".join(sorted({u["frontend"] for u in unparsed}))
        lines = [
            f"[sim:elab-only]   {len(unparsed)} module(s) ungraded — {frontends} is only a "
            f"fallback for a Target simulator it cannot drive, and the per-Target "
            f"build accepted the sources{where}:"
        ]
        for u in unparsed[:_MAX_ECHOED_STANDALONE_FAILURES]:
            gist = _extract_error_gist(u["error"]) or "(no compiler output)"
            via = _probe_label(u["frontend"], tuple(u["dialect_flags"]))
            lines.append(f"[sim:elab-only]     {u['module']} ({u['file']}) via {via}: {gist}")
        if len(unparsed) > _MAX_ECHOED_STANDALONE_FAILURES:
            more = len(unparsed) - _MAX_ECHOED_STANDALONE_FAILURES
            lines.append(f"[sim:elab-only]     ... and {more} more")
        return lines

    @staticmethod
    def _parse_gap_message(
        unparsed: list[_ProbeRecord],
        total: int,
    ) -> str:
        """Explain a fallback-probe capability gap and name the way out.

        Deliberately not phrased as a design failure: the per-Target elaborate
        accepted these same sources in this same invocation, so the only thing
        established is that the *fallback* frontend cannot read them.
        """
        first = unparsed[0]
        frontends = "+".join(sorted({u["frontend"] for u in unparsed}))
        gist = _extract_error_gist(first["error"]) or "(no compiler output)"
        return (
            f"{frontends} cannot parse {len(unparsed)}/{total} module(s) in the RTL "
            f"scope, but the per-Target elaborate accepted the same sources — "
            f"{frontends} is only a fallback probe for a Target simulator it cannot drive, "
            f"so this is a frontend capability gap, not a design defect, and the "
            f"sweep reached no verdict. First: {first['module']} ({first['file']}): "
            f"{gist}. Use --mode elab-only for the Target elaboration check."
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
