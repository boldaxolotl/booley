"""LintFlow вЂ” BooleyFlow running the Target's linter.

Runs the resolved lint Target's EDA tool (Verilator ``--lint-only`` or
Verible ``verible-verilog-lint``, ADR 0033) per build config, parses
warnings, and deduplicates warnings across configs.

Exit codes: 0 = clean, 1 = the design failed (warnings remain, or the
linter rejected the RTL), 2 = the linter could not run at all (missing
binary, setup failure, timeout). See docs/USAGE.md for the shared
Booley Flow exit-code taxonomy.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from booley.flows import eda_parsers
from booley.fusesoc import fusesoc_registry
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS, McpToolResult
from booley.runtime.platform_paths import posix_relpath
from booley.runtime.timefmt import utc_now_rfc3339
from booley.sim.sim_result import write_run_log
from booley.targets.flow_names import config_section
from booley.targets.target import select_target, select_targets

from .. import artifacts
from .. import edam as edam_layer
from ..base import BooleyFlow, SubprocessResult

logger = logging.getLogger(__name__)

# The Verilator/Verible warning/error regexes live in the shared parser
# module (single source of truth). QA-7 context for the error scan: ``parse_warnings`` only matches ``%Warning``
# lines, so an error run yields zero warnings and would otherwise score as a
# clean PASS.
_WARNING_RE = eda_parsers.VERILATOR_WARNING_RE
_ERROR_RE = eda_parsers.VERILATOR_ERROR_RE
_first_error_line = eda_parsers.first_error_line
# Verilator's warnings-were-fatal exit epilogue — location-less, and the only
# %Error line of a warnings-only run (see _classify_lint_failure).
_WARNING_EPILOGUE_RE = re.compile(r"%Error: Exiting due to \d+ warning\(s\)")
_VERIBLE_FINDING_RE = eda_parsers.VERIBLE_FINDING_RE
_verible_first_error_line = eda_parsers.verible_first_error_line


def _lint_eda_tool_family(eda_tool: str | None) -> str:
    """Normalize a Target's ``flow_options.tool`` to the lint parser family.

    Mirrors :func:`booley.flows.sim.edam.normalize_eda_tool` (ADR 0022
    decision 8: the EDA tool comes from the resolved Target, not the
    execution configuration). Everything that isn't Verible runs today's
    Verilator path byte-for-byte — the flow default.
    """
    if eda_tool and "verible" in eda_tool.lower():
        return "verible"
    return "verilator"


# Stale-image detection (ADR 0033 decision 8): the make step failing because
# the binary is absent must name the cause and the fix, not surface a generic
# spawn failure. make/sh phrase it as ``verible-verilog-lint: not found`` (sh)
# or ``No such file or directory`` (make's exec).
_VERIBLE_MISSING_RE = re.compile(
    r"verible-verilog-lint[^\n]*(?:not found|No such file)",
    re.IGNORECASE,
)
_VERIBLE_STALE_IMAGE_MSG = (
    "verible-verilog-lint is not installed in the Session Runtime — the "
    "sandbox image predates Verible support (ADR 0033). Rebuild the image "
    "(booley init) and retry."
)


def _verible_missing_msg() -> str:
    """Return the Session Runtime rebuild hint for missing Verible."""
    return _VERIBLE_STALE_IMAGE_MSG


@dataclass
class LintWarning:
    """Single parsed Verilator warning."""

    rule: str
    file: str
    line: int
    col: int
    message: str
    target: str  # which build Target produced it

    @property
    def dedup_key(self) -> tuple[str, str, int]:
        """Key for deduplication: (rule, file, line)."""
        return (self.rule, self.file, self.line)


@dataclass
class LintConfigResult:
    """Lint result for a single Target run."""

    target: str
    warnings: list[LintWarning] = field(default_factory=list)
    duration_s: float = 0.0
    returncode: int = -1
    error: str = ""
    error_is_eda_tool_failure: bool = False
    """True when ``error`` means the linter could not run at all (missing
    binary, spawn/timeout failure) rather than the linter running fine and
    rejecting the RTL. Only the former is an ERROR verdict: a compiler
    diagnostic about the design is a design FAIL, the same grading
    ``elaborate`` gives the identical source (F-29)."""
    eda_tool: str = ""
    """The EDA tool that actually linted (resolved ``flow_options.tool``, or
    the parser-family default). Empty when resolution never happened."""
    files_linted: int = -1
    """HDL source files fed to the linter (-1 = unknown before resolution)."""
    toplevel: str = ""
    """The Target's declared toplevel module (resolved EDAM), when known."""
    toplevel_linted: bool = True
    """False when *toplevel* is declared by none of the linted sources — the
    Target's fileset excludes the top module, so the run would lint nothing
    real and pass vacuously. Hard-fails the Target (same spirit as the ADR
    0026 doctor hard-fail: an integrity hole must not stay a silent WARN).
    Defaults True to avoid false alarms when unknown."""
    log_path: str = ""
    """Project-relative path of this Target's persisted ``run.log``. The parsed
    report carries rule/file:line/message per warning, but the linter's raw
    output — banner, include resolution, the lines around a diagnostic — only
    exists there. Empty when the log could not be written."""


def parse_warnings(output: str, target: str) -> list[LintWarning]:
    """Parse Verilator warnings from combined stdout+stderr."""
    warnings: list[LintWarning] = []
    for match in _WARNING_RE.finditer(output):
        warnings.append(
            LintWarning(
                rule=match.group("rule"),
                file=match.group("file"),
                line=int(match.group("line")),
                col=int(match.group("col")),
                message=match.group("message").strip(),
                target=target,
            )
        )
    return warnings


def parse_verible_warnings(output: str, target: str) -> list[LintWarning]:
    """Parse Verible lint findings from combined stdout+stderr (ADR 0033).

    ``file:line:col: message [rule]`` lines map into the same
    :class:`LintWarning` shape as Verilator's (``rule`` = the Verible rule
    name), so dedup keys, scope filtering, criteria, and the report are
    shared unchanged. Findings arrive with rc 0 (``--parse_fatal
    --lint_fatal=false``); parse errors are the caller's rc!=0 branch.
    """
    warnings: list[LintWarning] = []
    for match in _VERIBLE_FINDING_RE.finditer(output):
        warnings.append(
            LintWarning(
                rule=match.group("rule"),
                file=match.group("file"),
                line=int(match.group("line")),
                col=int(match.group("col")),
                message=match.group("message").strip(),
                target=target,
            )
        )
    return warnings


def _errored_verdict(errored: list[LintConfigResult]) -> tuple[int, str]:
    """Grade a run in which at least one Target errored.

    Any hard error outranks the warning tally: never report a PASS/WARN keyed
    off the parsed ``%Warning`` count alone (QA-7). Which verdict depends on
    who failed — a linter that could not run at all is an ERROR; a linter that
    ran and rejected the design is a design FAIL, matching ``elaborate`` on
    the identical source (F-29). A single EDA-tool failure decides the run, since
    an unusable linter means the other Targets' verdicts are not trustworthy
    evidence of a clean design.
    """
    eda_tool_failed = any(cr.error_is_eda_tool_failure for cr in errored)
    label = "ERROR" if eda_tool_failed else "FAIL"
    summary = f"RESULT: {label} — " + "; ".join(f"{cr.target}: {cr.error}" for cr in errored)
    return (EXIT_ERROR if eda_tool_failed else EXIT_FAILURE), summary


def _classify_lint_failure(
    result: LintConfigResult,
    family: str,
    combined: str,
) -> None:
    """Record *why* a non-zero lint run failed, and whose fault it was.

    A non-zero make/linter return code is never a clean lint (Verilator: an
    undeclared signal -> ``%Error`` -> rc=2; Verible: a parse failure under
    ``--parse_fatal``). Without recording it the verdict keys only off the
    parsed warning count, so RTL that fails to elaborate/parse would satisfy
    ``lint_clean`` — a false green on the gate (QA-7).

    Whether that grades ERROR or FAIL depends on who failed. A linter that ran
    and rejected the design is the linter working, so it is a design FAIL —
    the same grading ``elaborate`` gives the identical source (F-29). A
    missing binary or spawn/timeout failure means no verdict was reached at
    all, which stays an ERROR and names the image-rebuild fix.
    """
    if family == "verible":
        if _VERIBLE_MISSING_RE.search(combined):
            result.error = _verible_missing_msg()
            result.error_is_eda_tool_failure = True
            return
        result.error = (
            _verible_first_error_line(combined) or f"lint eda_tool exited {result.returncode}"
        )
        return
    first = _first_error_line(combined)
    # Verilator treats warnings as fatal at exit by default: a warnings-only
    # run exits 2 with the location-less "%Error: Exiting due to N warning(s)"
    # epilogue as its only %Error line (real %Error findings would precede
    # it). Grading that epilogue a hard failure would make
    # [flows.lint].warnings_as_errors=false inert on the builtin path — the
    # verdict must flow through the parsed warning tally + the knob instead
    # (the adapter-era QA-5 lesson, re-learned on the C910 re-port).
    if first and _WARNING_EPILOGUE_RE.search(first) and result.warnings:
        return
    result.error = first or f"lint eda_tool exited {result.returncode}"


def deduplicate_warnings(warnings: list[LintWarning]) -> list[LintWarning]:
    """Deduplicate warnings across Targets: keep first occurrence per (rule, file, line)."""
    seen: dict[tuple[str, str, int], LintWarning] = {}
    for w in warnings:
        if w.dedup_key not in seen:
            seen[w.dedup_key] = w
    return list(seen.values())


def filter_by_scope(warnings: list[LintWarning], scope: str) -> list[LintWarning]:
    """Filter warnings to only files matching comma-separated scope paths."""
    scope_paths = [s.strip() for s in scope.split(",") if s.strip()]
    if not scope_paths:
        return warnings
    return [w for w in warnings if any(sp in w.file for sp in scope_paths)]


def _scoped_warning_count(warnings: list[LintWarning], scope: str) -> int:
    """Count deduplicated warnings after applying the invocation scope."""
    unique = deduplicate_warnings(warnings)
    return len(filter_by_scope(unique, scope)) if scope else len(unique)


def _toplevel_declared(
    hdl_files: list[Any],
    build_root: Path,
    toplevel: str,
) -> bool:
    """True when *toplevel* is declared by one of the linted HDL sources.

    A Target may lint a fileset that excludes its own toplevel (e.g. a style
    fileset trimmed of macro-heavy files, or a stale ``toplevel`` naming a
    renamed module); nothing in the linter output says so, and the run then
    lints nothing real and passes vacuously. Scan the resolved sources for a
    ``module <toplevel>`` declaration so the caller can hard-fail that
    integrity hole. Best-effort: any read failure counts as declared, so an
    I/O hiccup never fabricates a vacuous-lint failure.
    """
    decl_re = re.compile(
        rf"^\s*(?:macro)?module\s+(?:automatic\s+)?{re.escape(toplevel)}\b", re.MULTILINE
    )
    for f in hdl_files:
        try:
            text = f.absolute(build_root).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return True
        if decl_re.search(text):
            return True
    return False


def _lint_warnings_as_errors(work_dir: Path) -> bool:
    """Read ``[flows.lint].warnings_as_errors`` (default True).

    True (the historical behavior) makes any surviving warning exit 1 — a CI
    gate treats warnings as failure. Projects that gate on errors only set it
    to false: warnings still print and land in lint_report.json, but the run
    exits 0. Best-effort read; any config failure keeps the strict default.
    """
    try:
        from booley.runtime.shared_infra import _load_rtl_config

        cfg = _load_rtl_config(work_dir)
    except Exception:  # noqa: BLE001 — best-effort config read; strict default
        return True
    if not cfg:
        return True
    flows = cfg.get("flows", {})
    return bool(config_section(flows, "lint").get("warnings_as_errors", True))


# Warnings echoed inline on the console (A-2); the full list is always in
# lint_report.json.
_MAX_ECHOED_WARNINGS = 5


def _echo_warnings(unique: list[LintWarning]) -> None:
    """Print the first few warnings with file:line so they are actionable."""
    for w in unique[:_MAX_ECHOED_WARNINGS]:
        print(f"[lint]   %Warning-{w.rule}: {w.file}:{w.line}:{w.col} {w.message}")
    if len(unique) > _MAX_ECHOED_WARNINGS:
        print(f"[lint]   ... and {len(unique) - _MAX_ECHOED_WARNINGS} more (see lint_report.json)")


def _target_summary_line(result: LintConfigResult) -> str:
    """Format the existing per-Target completion line."""
    wcount = len(result.warnings)
    label = "warning" if wcount == 1 else "warnings"
    if result.error:
        return f"[lint] {result.target:<12} ERROR: {result.error}"
    files = f", {result.files_linted} files" if result.files_linted >= 0 else ""
    return (
        f"[lint] {result.target:<12} [{result.eda_tool}] {wcount} {label} "
        f"(target total){files}, {result.duration_s:.1f}s"
    )


def _lint_result_parts(
    targets: list[str],
    unique: list[LintWarning],
    elapsed: float,
    target_results: list[LintConfigResult] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Build (display_lines, detail) for the lint McpToolResult.

    One shared builder for the concise display line plus the
    targets/count/elapsed detail dict.
    Names the EDA tool(s) that actually linted — the linter comes from the
    resolved Target, so without this the output never says which one ran.
    """
    display = [f"{len(unique)} warnings"]
    detail: dict[str, Any] = {
        "targets": targets,
        "total_warnings": len(unique),
        "elapsed_s": round(elapsed, 2),
    }
    eda_tools = sorted({cr.eda_tool for cr in (target_results or []) if cr.eda_tool})
    if eda_tools:
        display.append(f"linter: {', '.join(eda_tools)}")
        detail["eda_tools"] = {cr.target: cr.eda_tool or None for cr in (target_results or [])}
    return display, detail


def _build_warning_details(
    unique_warnings: list[LintWarning],
) -> list[dict[str, Any]]:
    """Build warning detail dicts for reports."""
    details: list[dict[str, Any]] = []
    for w in unique_warnings:
        details.append(
            {
                "rule": w.rule,
                "file": w.file,
                "line": w.line,
                "message": w.message,
            }
        )
    return details


class LintFlow(BooleyFlow):
    """Run the Target's linter for one or more Targets."""

    name: str = "lint"
    description: str = (
        "Run lint for one or more Targets. The linter comes from the "
        "resolved Target's flow_options.tool (Verilator or Verible)."
    )
    code_modifying: bool = False
    satisfies: ClassVar[list[str]] = ["lint_clean"]

    # The built-in path is make-driven end-to-end in the Session Runtime.
    def _add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--scope",
            default="",
            help="Comma-separated file paths to filter warnings",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print lint commands without executing",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=120000,
            help="Per-config lint timeout in milliseconds",
        )

    # --- Command building ---

    def _build_command(self) -> list[str]:
        """Not used вЂ” LintFlow overrides _run() for multi-config logic."""
        return []

    def _interpret_result(self, result: SubprocessResult) -> McpToolResult:
        """Not used вЂ” LintFlow overrides _run()."""
        return McpToolResult()

    def _get_timeout(self) -> int:
        """Per-config timeout in seconds (CLI flag is ms)."""
        return self.args.timeout // 1000

    def _get_targets(self) -> list[str]:
        """Validated Target selection for this run (ADR 0030).

        Drives off canonical Target selection: each ``--target`` token is
        validated (a bare name must be unambiguous, a ``vlnv#name`` qualifier
        disambiguates; unknown/ambiguous names raise). An empty ``--target``
        returns ``[]`` — there is **no** enumerate-all sweep (ADR 0030): to lint
        several Targets, name them (``--target a,b``). An empty ``--target``
        returns no selection rather than linting every core.
        """
        return [target.selector for target in select_targets(self.args.work_dir, self.args.target)]

    def _prepare_lint_command(
        self,
        target: str,
    ) -> tuple[list[str], fusesoc_registry.ResolvedTarget]:
        """Resolve the lint Target through FuseSoC; return (make command, resolved).

        The :class:`ResolvedTarget` rides along so the caller can report what
        the resolution already knows — the actual EDA tool, the linted file
        list, and the declared toplevel — instead of discarding it.

        ADR 0022 (decision 4): FuseSoC owns design-description. ``resolve_target``
        runs ``fusesoc run --setup``, which itself runs Edalize ``configure()``
        and leaves a ready-to-``make`` build dir — superseding the Booley-built
        EDAM of 0019. Booley contributes nothing to the design here: sources,
        ``--lint-only``/``-Wall`` (the lint flow + the Target's
        ``flow_options.verilator_options``), the top module, and every define
        (now a declared ``vlogdefine``, decision 8 — ``-d/--define`` is gone)
        all come from the ``.core`` lint Target. Verilator's ``%Warning-``
        output is still parsed downstream by Booley (interpretation stays
        verification-intent, 0019 dec. 4).

        ``target`` is the FuseSoC Target name (decision 10). The resolved build
        dir is relocatable (FuseSoC copies sources in and references them
        relatively), so ``make -C <relpath>`` is independent of the Runtime's
        absolute workspace path. Raises on any setup failure so
        the caller records it as a Flow error.
        """
        build_root = edam_layer.work_root_for(self.args.work_dir, "lint", target)
        resolved = fusesoc_registry.resolve_target(
            target,
            project_root=self.args.work_dir,
            build_root=build_root,
        )
        rel = edam_layer.relpath_for_make(resolved.build_root, self.args.work_dir)
        return edam_layer.make_command(rel), resolved

    def _dry_run_command(self, target: str) -> list[str]:
        """Build a side-effect-free ``--dry-run`` preview for one Target.

        Mirrors :meth:`SimulateFlow._dry_run_command`: unlike
        :meth:`_prepare_lint_command` (which runs ``fusesoc run --setup`` to
        resolve the build dir), dry-run **does not resolve**. It shows the
        ``fusesoc run --setup`` command resolution *would* execute (via
        :func:`fusesoc_registry.setup_command`, a cheap ``.core`` YAML read — no
        subprocess, works on the host with no ``fusesoc`` on PATH) chained to the
        deterministic lint ``make``. The previewed ``make -C`` names the outer
        build root; resolution nests the Makefile one level deeper, so the
        preview shows *what would run*, not a byte-exact runnable command. An
        unauthored Target yields a clean ``ERROR`` entry rather than raising.
        """
        build_root = edam_layer.work_root_for(self.args.work_dir, "lint", target)
        try:
            setup_cmd = fusesoc_registry.setup_command(
                target,
                project_root=self.args.work_dir,
                build_root=build_root,
            )
        except fusesoc_registry.TargetResolutionError as exc:
            return [f"ERROR: lint dry-run: {exc}"]
        rel = edam_layer.relpath_for_make(build_root, self.args.work_dir)
        script = f"{shlex.join(setup_cmd)} && {shlex.join(edam_layer.make_command(rel))}"
        return ["sh", "-c", script]

    def _dry_run(self, targets: list[str]) -> McpToolResult:
        """Print the side-effect-free ``fusesoc run --setup`` + ``make`` preview.

        One ``sh -c`` script per Target, emitted as JSON — the same shape the
        simulate/elaborate built-ins use, so a dry-run never invokes fusesoc.
        """
        commands = {tgt: self._dry_run_command(tgt) for tgt in targets}
        output = json.dumps(commands, indent=2)
        print(output)
        return McpToolResult(exit_code=EXIT_SUCCESS, report_text="Dry run complete")

    def _target_lint_family(self, target: str) -> str:
        """The lint parser family for *target* — ``verilator`` or ``verible``.

        A cheap ``.core`` YAML read (no subprocess), the same
        ``flow_options.tool`` field :func:`fusesoc_registry.resolve_target`
        later sees in the resolved EDAM. Any lookup failure falls back to the
        Verilator family, keeping the historical path byte-for-byte.
        """
        try:
            ref = select_target(self.args.work_dir, target)
        except Exception:  # noqa: BLE001 — best-effort EDA-tool lookup; default preserves behavior
            return "verilator"
        return _lint_eda_tool_family(ref.eda_tool)

    def _warn_non_lint_flow(self, targets: list[str]) -> None:
        """Warn when a selected Target isn't a lint-flow Target.

        Lint inherits the EDA tool from whatever Target it resolves; when
        ``--target`` names e.g. a sim Target, the
        run silently lints with that Target's eda_tool. Best-effort — a Target
        with no declared flow (legacy authoring) stays silent.
        """
        for tgt in targets:
            try:
                ref = select_target(self.args.work_dir, tgt)
            except Exception:  # noqa: BLE001 — advisory only; resolution errors surface later
                continue
            if ref.flow and ref.flow != "lint":
                print(
                    f"[lint] WARN: Target '{tgt}' declares flow '{ref.flow}', not "
                    f"'lint' — linting anyway with its eda_tool "
                    f"({ref.eda_tool or 'verilator'}). Check --target."
                )

    def _run_lint_target(
        self,
        target: str,
    ) -> LintConfigResult:
        """Run lint for a single Target via the Edalize lint flow, parse warnings.

        ``configure()`` runs in-process (pure file generation) to materialize
        the Edalize work dir; the generated ``make`` command runs locally in
        the Session Runtime. Warning parsing is keyed off the resolved Target's
        eda_tool (ADR 0033): Verilator
        ``%Warning`` lines or Verible ``file:line:col: msg [rule]`` findings —
        everything else (run.log, criteria, report, QA-7 error handling) is
        one shared path.
        """
        result = LintConfigResult(target=target)
        family = self._target_lint_family(target)
        # Claim this Target's run.log up front: it is only WRITTEN at the end
        # of the run below, so until then it still holds the previous run's
        # findings and a tail would read them as this run's (F-26).
        self._open_run_log(target, edam_layer.work_root_for(self.args.work_dir, "lint", target))

        try:
            cmd, resolved = self._prepare_lint_command(target)
        except Exception as exc:  # noqa: BLE001 — isolate per-target setup failure
            result.error = f"lint setup failed: {exc}"
            result.error_is_eda_tool_failure = True
            logger.debug("lint EDAM/configure failed for %s", target, exc_info=True)
            return result

        if not self._record_coverage_facts(result, resolved, family):
            return result

        start = time.monotonic()
        proc = self._execute_boundary(cmd)
        result.duration_s = time.monotonic() - start
        result.returncode = proc.returncode

        if proc.timed_out:
            result.error = f"Timed out after {self._get_timeout()}s"
            result.error_is_eda_tool_failure = True
            return result

        # Verilator writes warnings to stderr (and sometimes stdout)
        combined = proc.stdout + "\n" + proc.stderr
        # Persist the raw output beside the build dir (A-2): the parsed report
        # carries counts, but the actionable ``%Warning-...: file:line`` text
        # otherwise exists only on the transient stdout — an agent chasing a
        # lint regression had nothing on disk to act on.
        log_path: Path | None = None
        try:
            build_root = edam_layer.work_root_for(self.args.work_dir, "lint", target)
            build_root.mkdir(parents=True, exist_ok=True)
            # write_run_log, not a bare write_text: it is atomic (no torn read
            # for a concurrent tail) and preserves the run header above.
            log_path = write_run_log(build_root, combined)
            result.log_path = posix_relpath(log_path, self.args.work_dir)
        except OSError:
            logger.debug("could not persist lint run.log for %s", target, exc_info=True)
        if family == "verible":
            result.warnings = parse_verible_warnings(combined, target)
        else:
            result.warnings = parse_warnings(combined, target)
        if proc.returncode != 0 and not result.error:
            _classify_lint_failure(result, family, combined)
            if result.error and log_path is not None:
                # The classified error cites only the FIRST error line; the
                # rest of the linter's output was already persisted above, so
                # point at it — this-invocation-written, never a stale log.
                pointer = posix_relpath(log_path, self.args.work_dir)
                result.error += f" (full log: {pointer})"
        return result

    def _record_coverage_facts(
        self,
        result: LintConfigResult,
        resolved: fusesoc_registry.ResolvedTarget,
        family: str,
    ) -> bool:
        """Record what the resolution already knows; False = vacuous hard-fail.

        Coverage facts the linter output never states: the actual EDA tool,
        how many HDL sources it saw, and whether the Target's own toplevel is
        among them. A Target whose toplevel no fileset file declares (a style
        fileset trimmed of the top module, or a stale ``toplevel`` naming a
        renamed module) would lint against nothing real and pass green — a
        configuration/integrity failure, so it grades ERROR (F-29 taxonomy:
        the linter reached no verdict about the design), matching the ADR
        0026 doctor hard-fail spirit rather than a skippable WARN. On the
        False return the make never runs: its verdict would be untrustworthy
        by construction.
        """
        result.eda_tool = resolved.eda_tool or family
        hdl_files = [f for f in resolved.files if f.is_hdl and not f.is_include]
        result.files_linted = len(hdl_files)
        result.toplevel = resolved.toplevel
        if resolved.toplevel:
            result.toplevel_linted = _toplevel_declared(
                hdl_files,
                resolved.build_root,
                resolved.toplevel,
            )
        if not result.toplevel_linted:
            result.error = (
                f"lint toplevel '{resolved.toplevel}' is not declared by any "
                f"fileset file ({result.files_linted} HDL sources) — lint "
                "would be vacuous. Fix the Target's `toplevel` or add the "
                "declaring file to its filesets."
            )
            result.error_is_eda_tool_failure = True
            return False
        return True

    # --- Structured report ---

    def _write_lint_report(
        self,
        targets: list[str],
        unique_warnings: list[LintWarning],
        elapsed_s: float,
        errored: list[LintConfigResult] | None = None,
        target_results: list[LintConfigResult] | None = None,
    ) -> Path | None:
        """Write lint_report.json to the report dir (plus a per-run copy).

        ``report_dir/lint_report.json`` stays the stable "latest run" path the
        console summary points at, but each run also lands a copy in the
        numbered ``flow-reports/lint/<N>/`` invocation dir so consecutive runs
        (e.g. a Verilator pass then a Verible pass) stop clobbering each other.
        """
        report_dir = self.args.report_dir
        if report_dir is None:
            return None
        report_dir.mkdir(parents=True, exist_ok=True)

        warning_details = _build_warning_details(unique_warnings)
        errors = [{"target": cr.target, "message": cr.error} for cr in (errored or [])]
        # A hard Flow error fails the lint gate regardless of the warning tally.
        passed = len(unique_warnings) == 0 and not errors

        report = {
            "flow": "lint",
            "targets": targets,
            "eda_tools": {cr.target: cr.eda_tool or None for cr in (target_results or [])},
            "timestamp": utc_now_rfc3339(),
            "elapsed_s": round(elapsed_s, 2),
            "passed": passed,
            "total_warnings": len(unique_warnings),
            "warnings": warning_details,
            "errors": errors,
            "target_results": [
                {
                    "target": cr.target,
                    "eda_tool": cr.eda_tool or None,
                    "warnings": len(cr.warnings),
                    "files_linted": cr.files_linted if cr.files_linted >= 0 else None,
                    "toplevel": cr.toplevel or None,
                    "toplevel_linted": cr.toplevel_linted,
                    "duration_s": round(cr.duration_s, 2),
                    "error": cr.error or None,
                    "log": cr.log_path or None,
                }
                for cr in (target_results or [])
            ],
        }
        report_path = report_dir / "lint_report.json"
        # Self-locating: the console summary already names lint_report.json on
        # WARN, but a consumer reading the JSON straight off disk (triage, the
        # MCP poll path) had no way back to the raw linter output per Target.
        report["artifacts"] = {
            "report": posix_relpath(report_path, self.args.work_dir),
            **{f"log_{cr.target}": cr.log_path for cr in (target_results or []) if cr.log_path},
        }
        payload = json.dumps(report, indent=2)
        report_path.write_text(payload, encoding="utf-8")
        invocation_dir = self.reserve_invocation_dir()
        if invocation_dir is not None:
            (invocation_dir / "lint_report.json").write_text(payload, encoding="utf-8")
        return report_path

    # --- Main execution ---

    def _run_all_targets(
        self,
        targets: list[str],
    ) -> tuple[list[LintConfigResult], list[LintWarning]]:
        """Run lint per Target sequentially, print per-Target summary."""
        all_warnings: list[LintWarning] = []
        target_results: list[LintConfigResult] = []
        for tgt in targets:
            cr = self._run_lint_target(tgt)
            target_results.append(cr)
            line = _target_summary_line(cr)
            print(line)
            if len(targets) > 1:
                self.emit_completion(line)
            # A vacuous toplevel (cr.toplevel_linted False) hard-fails inside
            # _run_lint_target now, so cr.error already carries the message —
            # no separate WARN line.
            all_warnings.extend(cr.warnings)
        return target_results, all_warnings

    @staticmethod
    def _build_summary(
        unique: list[LintWarning],
        report_path: Path | None = None,
        warnings_as_errors: bool = True,
    ) -> tuple[int, str]:
        """Compute (exit_code, summary_string) from results.

        On WARN, point the caller at ``lint_report.json`` — it carries the full
        per-warning detail (rule/file:line/message) that the count-only console
        output omits, so there's no need to drop to the raw make target to see
        what actually fired.

        ``warnings_as_errors`` (``[flows.lint].warnings_as_errors``, default
        True) decides whether surviving warnings fail the run: False keeps the
        WARN verdict text but exits 0, so a CI gate can pass on warnings-only
        while ``lint_clean`` criteria still record the truth.
        """
        if unique:
            summary = f"RESULT: WARN ({len(unique)} warnings)"
            if not warnings_as_errors:
                summary += " — non-blocking ([flows.lint].warnings_as_errors=false)"
            if report_path is not None:
                summary += (
                    f"\nSee {report_path} for the full warning list (rule/file:line/message)."
                )
            return (EXIT_FAILURE if warnings_as_errors else EXIT_SUCCESS), summary
        return EXIT_SUCCESS, "RESULT: PASS"

    def _validate_interactive_args(self) -> McpToolResult | None:
        """Interactive-Mode argument validation.

        In Interactive Mode there is no project_config import path priming
        the discovery list, so silently falling back to the hardcoded
        ``["lite", "full", "combo"]`` defaults runs lint against configs that
        don't exist.  Refuse explicitly instead.
        """
        if self._state is not None and self._state._file_path is not None:
            return None  # Ticket Mode вЂ” existing discovery behaviour applies
        if not getattr(self.args, "target", "").strip():
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(
                    "lint: --target is required when running outside a ticket. "
                    "Pass --target <name>; available Targets are the .core "
                    "lint Targets in .booley_project/."
                ),
            )
        return None

    def _run(self) -> McpToolResult:
        """Run lint across configured build Targets."""
        err = self._validate_interactive_args()
        if err is not None:
            return err
        targets = self._get_targets()
        if not targets:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(
                    "lint: no Target selected. Pass --target <name> (or "
                    "--target a,b for several); a bare name must be unambiguous, "
                    "else qualify it as vlnv#name. There is no lint-all sweep."
                ),
            )
        # Resolve enablement before running any target.
        if not self._flow_enabled():
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text="lint is disabled ([flows.lint].enabled = false).",
            )
        # Dry-run output is machine-parsed JSON — keep advisory prints out of it.
        if not self.args.dry_run:
            self._warn_non_lint_flow(targets)
        # Builtin (FuseSoC) dry-run: a cheap side-effect-free preview that never
        # resolves — short-circuits before the per-Target make loop below.
        if self.args.dry_run:
            return self._dry_run(targets)

        overall_start = time.monotonic()
        target_results, all_warnings = self._run_all_targets(targets)

        # Deduplicate and scope-filter
        unique = deduplicate_warnings(all_warnings)
        if self.args.scope:
            unique = filter_by_scope(unique, self.args.scope)
        print(f"[lint] {len(unique)} unique in-scope warning{'s' if len(unique) != 1 else ''}")
        _echo_warnings(unique)

        # Set per-Target criteria
        errored = [cr for cr in target_results if cr.error]
        for cr in target_results:
            key = f"lint_clean_{cr.target}"
            warning_count = _scoped_warning_count(cr.warnings, self.args.scope)
            is_clean = warning_count == 0 and not cr.error
            detail: dict[str, Any] = {"warnings": warning_count}
            if cr.error:
                detail["error"] = cr.error
            self.set_criterion(key, is_clean, detail=detail, source_target=cr.target)

        elapsed = time.monotonic() - overall_start
        report_path = self._write_lint_report(
            targets,
            unique,
            elapsed,
            errored,
            target_results=target_results,
        )

        display, detail = _lint_result_parts(targets, unique, elapsed, target_results)
        # The copy that reaches the agent as MCP structuredContent: the stdout
        # "See <report> ..." line is tail-truncatable, ``detail`` is not.
        artifacts.merge_artifacts(
            detail,
            artifacts.artifacts_block(
                self.args.work_dir,
                report=report_path,
                **{f"log_{cr.target}": cr.log_path for cr in target_results if cr.log_path},
            ),
        )
        self._eda_tool = ", ".join(sorted({cr.eda_tool for cr in target_results if cr.eda_tool}))

        if errored:
            exit_code, summary = _errored_verdict(errored)
        else:
            exit_code, summary = self._build_summary(
                unique,
                report_path,
                warnings_as_errors=_lint_warnings_as_errors(self.args.work_dir),
            )
        print(f"\n{summary}")

        return McpToolResult(
            exit_code=exit_code,
            report_text=summary,
            detail=detail,
            display_lines=display,
        )


if __name__ == "__main__":
    LintFlow().cli()
