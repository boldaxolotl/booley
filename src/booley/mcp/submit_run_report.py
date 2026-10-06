"""SubmitRunReportMcpTool writes the final human-readable run report.

The developer calls this once, as its very last action, after all
mandatory acceptance criteria are met strictly or coverage is verified
provisionally for Human review. The MCP tool writes ``REPORT.md`` into
the logs directory and sets the internal ``_report_submitted`` criterion
so the harness can verify the report was actually produced.

The report has a fixed structure with two universal sections (summary,
uncertainties), a conditional unmet-optional-criteria section, plus one
type-specific section whose meaning depends on the ticket type:

  - bugfix       -> root_cause
  - feature      -> design_decisions
  - refactor     -> behavior_preservation
  - verification -> coverage_added

Exactly one of those type-specific fields is valid for a given ticket. For
example, a feature ticket must pass ``--design-decisions`` and must not also
pass ``--coverage-added`` even when the testbench changed.

The MCP tool reads the ticket type from ``$BOOLEY_TICKET_TYPE`` (set by the
developer) and rejects (exit 2) when the matching type-specific arg
is missing or a mismatched one was supplied, so the developer retries
with the right args rather than silently producing a malformed report.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from booley.flows.endpoint_session import PreparedExecution
from booley.runtime import job_records as jobrec
from booley.runtime.endpoint_execution import (
    EndpointOutcome,
    ExecutionResult,
    normalize_completion_error,
)
from booley.runtime.pid import is_pid_alive
from booley.runtime.session_paths import session_jobs_dir
from booley.runtime.timefmt import format_human_datetime, format_human_datetime_safe
from booley.ticket_board import report_submission as submission
from booley.ticket_board.ticket_repositories import TicketWorkspaceError, pending_ticket_changes

from .base import EXIT_ERROR, EXIT_SUCCESS, McpTool, McpToolResult
from .events import _emit_criteria_update
from .schema_extractor import extract_schema

logger = logging.getLogger(__name__)


# Maps ticket_type -> (CLI arg name, attribute on argparse Namespace, section heading).
# The CLI arg name uses argparse's hyphenated form (--root-cause); the attribute
# name is the underscored form argparse produces (args.root_cause).
_TYPE_FIELD: dict[str, tuple[str, str, str]] = {
    "bugfix": ("--root-cause", "root_cause", "Root cause"),
    "feature": ("--design-decisions", "design_decisions", "Design decisions"),
    "refactor": ("--behavior-preservation", "behavior_preservation", "Behavior preservation"),
    "verification": ("--coverage-added", "coverage_added", "Coverage added"),
}

_REPORT_CRITERION = "_report_submitted"


class SubmitRunReportMcpTool(McpTool):
    """Write the end-of-run review report and set ``_report_submitted``."""

    name: str = "submit_run_report"
    description: str = (
        "Write the final run report (REPORT.md) summarizing what was done, "
        "type-specific details (root cause / design decisions / "
        "behavior preservation / coverage added), reviewer uncertainties, "
        "and justification for every changed file and unmet optional criterion. "
        "For native MCP calls, pass type_specific_detail and do not pass "
        "root_cause/design_decisions/behavior_preservation/coverage_added. "
        "For CLI calls, pass exactly one legacy type-specific field: bugfix "
        "uses --root-cause, feature uses --design-decisions, refactor uses "
        "--behavior-preservation, verification uses --coverage-added. "
        "MUST be called exactly once as the developer's final action, after "
        "all intended changes are committed and every ticket repository is clean."
    )
    code_modifying: bool = False
    config_aware: bool = False

    def _submission_identity(self) -> dict:
        from booley.ticket_board.flow_execution import TicketAcceptanceRecorder

        recorder = self._acceptance_recorder
        if isinstance(recorder, TicketAcceptanceRecorder):
            return recorder._validated_ticket_identity()
        return {}

    def _validate_submission_authority(self) -> None:
        from booley.runtime.execution_lease import validate_recording

        validate_recording(getattr(self.args, "work_dir", None))
        if self._submission_identity() != self._submission_expected_identity:
            raise submission.ReportSubmissionError(
                "Ticket identity changed during report submission"
            )
        if os.environ.get("BOOLEY_EXECUTION_ID", "") != self._submission_execution_id:
            raise submission.ReportSubmissionError(
                "Ticket execution changed during report submission"
            )

    def record_acceptance(self, prepared: PreparedExecution, outcome: EndpointOutcome) -> None:
        if outcome.exit_code == EXIT_SUCCESS and getattr(self, "_submission", None) is not None:
            candidate = deepcopy(self.state)
            changes = candidate.set_criterion(
                _REPORT_CRITERION, True, detail=self._candidate_detail
            )
            if not changes:
                raise submission.ReportSubmissionError(
                    "report gate is undeclared; initialize Ticket Criteria before submitting"
                )
            self._record_acceptance_changes(changes)
            self._candidate_entry = candidate.criteria[_REPORT_CRITERION]
            self.state.criteria[_REPORT_CRITERION] = deepcopy(self._candidate_entry)
            self.state.criteria[_REPORT_CRITERION].met = False
        super().record_acceptance(prepared, outcome)

    def _post_run(self, result: EndpointOutcome, duration: float) -> None:
        from booley.flows.endpoint_reporting import _endpoint_timeline_args
        from booley.flows.execution_persistence import NoAcceptanceRecorder

        if isinstance(self._acceptance_recorder, NoAcceptanceRecorder):
            return
        if getattr(self, "_candidate_entry", None) is None:
            super()._post_run(result, duration)
            return
        self.state.record_mcp_tool_run(
            self.name,
            result.exit_code,
            endpoint_kind=self.endpoint_kind,
            duration_s=duration,
            criteria_set=list(self._pending_criteria_set or ()) or None,
            cost_usd=result.cost_usd or None,
            args=_endpoint_timeline_args(self),
        )
        candidate = deepcopy(self.state)
        candidate.criteria[_REPORT_CRITERION] = self._candidate_entry
        if candidate._file_path is not None:
            candidate.save()
            _emit_criteria_update(self.state)

    def finish_execution(
        self,
        prepared: PreparedExecution,
        outcome: EndpointOutcome,
        *,
        started: float | None,
        acceptance_recorded: bool,
    ) -> ExecutionResult:
        attempt = getattr(self, "_submission", None)
        try:
            try:
                result = super().finish_execution(
                    prepared, outcome, started=started, acceptance_recorded=acceptance_recorded
                )
            except Exception as exc:  # noqa: BLE001 - normalize the submission lifecycle boundary
                normalize_completion_error(outcome, exc, "publish report completion")
                result = ExecutionResult(exit_code=EXIT_ERROR, outcome=outcome)
            if attempt is None:
                return result
            if result.exit_code == EXIT_SUCCESS:
                try:
                    attempt.commit()
                except submission.UnknownReportCommitError as exc:
                    normalize_completion_error(result.outcome, exc, "commit report submission")
                    result.outcome.criterion_key = None
                    result.outcome.criterion_met = None
                    self.state.criteria[_REPORT_CRITERION].met = False
                    result.outcome.detail["report_submission_status"] = "unknown"
                    self._publish_failed_completion(result.outcome)
                    return ExecutionResult(exit_code=EXIT_ERROR, outcome=result.outcome)
                except Exception as exc:  # noqa: BLE001 - normalize the submission lifecycle boundary
                    normalize_completion_error(result.outcome, exc, "commit report submission")
            if attempt.committed:
                self.state.criteria[_REPORT_CRITERION].met = True
                return ExecutionResult(exit_code=result.outcome.exit_code, outcome=result.outcome)
            self._fail_submission(result.outcome)
            return ExecutionResult(exit_code=EXIT_ERROR, outcome=result.outcome)
        finally:
            if attempt is not None:
                attempt.close()

    def _fail_submission(self, outcome: EndpointOutcome) -> None:
        outcome.criterion_key = None
        outcome.criterion_met = None
        outcome.detail["report_submission_status"] = "uncommitted"
        for operation, action in (
            ("fence failed report", self._submission.fail),
            ("invalidate report gate", self._invalidate_report_gate),
        ):
            try:
                action()
            except Exception as exc:  # noqa: BLE001 - normalize the submission lifecycle boundary
                normalize_completion_error(outcome, exc, operation)
        self._publish_failed_completion(outcome)

    def _publish_failed_completion(self, outcome: EndpointOutcome) -> None:
        from booley.flows.endpoint_events import _endpoint_end_event, _write_display_event

        outcome.summary = "Report submission " + outcome.detail.get(
            "report_submission_status", "uncommitted"
        )
        for operation, action in (
            ("publish report failure diagnosis", lambda: self._publish_console_report(outcome)),
            (
                "publish report failure event",
                lambda: _write_display_event(
                    _endpoint_end_event(
                        self.name, None, outcome, 0, identity=self._display_identity
                    )
                ),
            ),
        ):
            try:
                action()
            except Exception as exc:  # noqa: BLE001 - completion publication boundary
                normalize_completion_error(outcome, exc, operation)
        for _attempt in range(2):
            try:
                self.write_report(outcome)
                return
            except Exception as exc:  # noqa: BLE001 - bounded structured report recovery
                normalize_completion_error(outcome, exc, "publish report failure")

    def _invalidate_report_gate(self) -> None:
        changes = self.state.set_criterion(_REPORT_CRITERION, False)
        try:
            self._record_acceptance_changes(changes)
        finally:
            self.state.save()

    def _pre_state_gate(self) -> McpToolResult | None:
        """Refuse the final report until every detached ticket job is terminal."""
        active = [
            rec
            for rec in jobrec.list_records(root=session_jobs_dir())
            if jobrec.is_active(rec, is_pid_alive)
        ]
        if not active:
            return None
        jobs = ", ".join(f"{rec.endpoint} ({rec.run_id})" for rec in active)
        return McpToolResult(
            exit_code=EXIT_ERROR,
            report_text=(
                "submit_run_report: outstanding ticket jobs are still running: "
                f"{jobs}. Poll or cancel them before submitting the final report."
            ),
        )

    def _add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--file-justifications",
            help=(
                "Required JSON object mapping every net changed file to its justification; "
                "use {} for no changes. Include added/deleted paths and both sides of renames."
            ),
        )
        parser.add_argument(
            "--summary",
            required=True,
            help="One paragraph: what was done in this ticket run.",
        )
        parser.add_argument(
            "--uncertainties",
            required=True,
            help=(
                "One paragraph: honest doubts a human reviewer should "
                "double-check. Frame as 'what would make me doubt this fix/"
                "feature' -- list test coverage gaps, assumptions made about "
                "the spec, edge cases not exercised. Empty/fluffy text is a "
                "failure mode; this field is load-bearing."
            ),
        )
        parser.add_argument(
            "--type-specific-detail",
            default=None,
            help=(
                "Preferred for MCP/native calls: the one ticket-type-specific "
                "detail section. The MCP tool maps it using BOOLEY_TICKET_TYPE "
                "(bugfix Root cause, feature Design decisions, refactor "
                "Behavior preservation, verification Coverage added). Do not "
                "combine with legacy type-specific args."
            ),
        )
        parser.add_argument(
            "--optional-criteria-justification",
            default=None,
            help=(
                "Required when any non-internal optional criterion remains unmet: "
                "explain why each one could not be completed. Omit when all optional "
                "criteria are met."
            ),
        )
        self._add_legacy_type_specific_args(parser)

    def _add_legacy_type_specific_args(self, parser: argparse.ArgumentParser) -> None:
        """Register the legacy per-ticket-type detail flags (CLI compat)."""
        # All type-specific fields are optional at the argparse layer; the
        # The MCP tool enforces the right one at runtime based on $BOOLEY_TICKET_TYPE.
        parser.add_argument(
            "--root-cause",
            default=None,
            help=(
                "bugfix only: what was actually wrong and why the fix addresses it. "
                "Do not combine with other type-specific report args."
            ),
        )
        parser.add_argument(
            "--design-decisions",
            default=None,
            help=(
                "feature only: non-obvious choices vs. the spec (defaults picked, "
                "ambiguities resolved). Use this, not --coverage-added, for feature "
                "tickets even when TB coverage improved."
            ),
        )
        parser.add_argument(
            "--behavior-preservation",
            default=None,
            help=(
                "refactor only: evidence behavior is unchanged (tests passed, "
                "equivalence argued). Do not combine with other type-specific report args."
            ),
        )
        parser.add_argument(
            "--coverage-added",
            default=None,
            help=(
                "verification only: what scenarios the new tests exercise. Do not use "
                "for feature tickets; put feature TB coverage notes in --design-decisions "
                "or --uncertainties."
            ),
        )

    def mcp_schema(self) -> dict:
        """Expose one generic detail field to MCP callers.

        The CLI keeps the legacy per-ticket-type flags for compatibility, but
        MCP models should not see four mutually-exclusive optional fields.
        """
        schema = extract_schema(self._parser)
        properties = schema.get("properties", {})
        for field in (
            "root_cause",
            "design_decisions",
            "behavior_preservation",
            "coverage_added",
        ):
            properties.pop(field, None)

        required = set(schema.get("required", []))
        required.update({"type_specific_detail", "file_justifications"})
        schema["required"] = sorted(required)
        return schema

    def _run(self) -> McpToolResult:
        ticket_type = os.environ.get("BOOLEY_TICKET_TYPE", "").strip()
        if not ticket_type:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(
                    "submit_run_report is Ticket Mode only; this session is not a Ticket run"
                ),
            )
        if gate := self._ticket_finalization_gate():
            return gate

        if ticket_type not in _TYPE_FIELD:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(
                    f"submit_run_report: BOOLEY_TICKET_TYPE={ticket_type!r} is not a "
                    f"recognized type. Expected one of: {sorted(_TYPE_FIELD)}. "
                    "This is an developer/harness bug -- the env var should be set."
                ),
            )

        gate = self._validate_type_specific_args(ticket_type)
        if gate is not None:
            return gate

        unmet_optional = self._unmet_optional_criteria()
        gate = self._validate_optional_criteria_justification(unmet_optional)
        if gate is not None:
            return gate

        return self._file_justifications_gate() or self._submit_report(ticket_type, unmet_optional)

    def _ticket_finalization_gate(self) -> McpToolResult | None:
        """Reject incomplete Ticket state before validating report contents."""
        return self._clean_worktree_gate() or self._criteria_freshness_gate()

    def _file_justifications_gate(self) -> McpToolResult | None:
        """Reject missing explanations before publishing the final report."""
        from .report_changes import changed_ticket_paths, validate_justifications

        try:
            self.file_justifications = validate_justifications(
                self.args.file_justifications, changed_ticket_paths(self._submission_worktree())
            )
        except (OSError, ValueError, TicketWorkspaceError) as exc:
            return McpToolResult(exit_code=EXIT_ERROR, report_text=f"submit_run_report: {exc}")
        return None

    def _submit_report(
        self,
        ticket_type: str,
        unmet_optional: list[str],
    ) -> McpToolResult:
        """Write and record a report after all finalization gates pass."""

        _cli_arg, _attr, heading = _TYPE_FIELD[ticket_type]
        type_specific_value = self._type_specific_value(ticket_type)

        if _REPORT_CRITERION not in self.state.criteria:
            raise submission.ReportSubmissionError(
                "report gate is undeclared; initialize Ticket Criteria before submitting"
            )
        report_path = self._write_report(
            ticket_type=ticket_type,
            summary=self.args.summary,
            type_heading=heading,
            type_value=type_specific_value,
            uncertainties=self.args.uncertainties,
            unmet_optional=unmet_optional,
            optional_criteria_justification=self.args.optional_criteria_justification,
        )

        if report_path is None:
            self._invalidate_report_gate()
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(
                    "Report directory unavailable; configure BOOLEY_LOGS_DIR "
                    "to the Ticket evidence directory before submitting."
                ),
            )

        self._candidate_detail = {
            "report_path": str(report_path),
            "ticket_type": ticket_type,
            "unmet_optional_criteria": unmet_optional,
            "file_justifications": self.file_justifications,
            submission.ID_KEY: self._submission.row["submission_id"],
            submission.DIGEST_KEY: self._submission.row["report_sha256"],
        }

        wrote = f"Wrote {report_path}"
        return McpToolResult(
            exit_code=EXIT_SUCCESS,
            criterion_key=None,
            criterion_met=None,
            summary="Report candidate uncommitted; awaiting completion commit.",
            report_text=self._confirmation_text(wrote)
            + "\nReport candidate pending completion commit.",
            detail={
                "report_submission_status": "pending",
                "report_submission_id": self._submission.row["submission_id"],
            },
        )

    def _clean_worktree_gate(self) -> McpToolResult | None:
        """Reject finalization until every repository in the Ticket Workspace is clean."""
        try:
            changes = pending_ticket_changes(
                self._submission_worktree(),
                require_paired=os.environ.get("BOOLEY_PAIRED_PROJECT_REPOSITORY") == "1",
            )
        except TicketWorkspaceError as exc:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(
                    f"submit_run_report: could not verify that the ticket worktree is clean: {exc}"
                ),
            )
        if not changes:
            return None

        shown = [
            f"  {(change.status.strip() or change.status)} {change.path}"
            for change in changes[:10]
        ]
        if len(changes) > len(shown):
            shown.append(f"  ... and {len(changes) - len(shown)} more")
        return McpToolResult(
            exit_code=EXIT_ERROR,
            report_text=(
                "submit_run_report: uncommitted changes remain:\n"
                + "\n".join(shown)
                + "\nCommit or restore them, then call submit_run_report again."
            ),
        )

    def _submission_worktree(self) -> Path:
        """Return the session ticket checkout, falling back to CLI scope in human mode."""
        ticket_worktree = os.environ.get("BOOLEY_WORKTREE", "").strip()
        return Path(ticket_worktree) if ticket_worktree else Path(self.args.work_dir)

    def _criteria_freshness_gate(self) -> McpToolResult | None:
        """Refresh source stamps and reject submission while mandatory work is unmet."""
        from booley.ticket_board.criteria_acceptance import refresh_verification_freshness

        stale = refresh_verification_freshness(
            self.state,
            work_dir=self._submission_worktree(),
        )
        if stale:
            _emit_criteria_update(self.state)
        unmet = self.state.unmet_mandatory()
        provisional = self._provisional_report_keys(unmet)
        unmet = [key for key in unmet if key not in provisional]
        if not unmet:
            return None
        stale_note = f" Newly stale: {', '.join(stale)}." if stale else ""
        return McpToolResult(
            exit_code=EXIT_ERROR,
            report_text=(
                "submit_run_report: mandatory criteria remain unmet: "
                f"{', '.join(unmet)}.{stale_note} Re-run the relevant Flow or "
                "Specialist before submitting the final report."
            ),
        )

    def _provisional_report_keys(self, unmet: list[str]) -> frozenset[str]:
        """Permit report submission for verified candidates without satisfying Criteria."""
        from booley.ticket_board.helpers import tickets_dir_from_project_root
        from booley.ticket_board.provisional_coverage import (
            TicketCoverageContext,
            evaluate_ticket_provisional_coverage,
        )

        slug = os.environ.get("BOOLEY_SLUG", "")
        control_root = os.environ.get("BOOLEY_CONTROL_PROJECT_ROOT", "")
        runtime_dir = os.environ.get("BOOLEY_RUNTIME_DIR", "")
        if not slug or not control_root or not runtime_dir:
            return frozenset()
        coverage = TicketCoverageContext(
            slug,
            tickets_dir_from_project_root(control_root),
            Path(runtime_dir).parent,
            self._submission_worktree(),
        )
        return evaluate_ticket_provisional_coverage(self.state, unmet, coverage).met_keys

    # --- helpers ---

    def _confirmation_text(self, wrote: str) -> str:
        """Assemble the submit receipt: what was written + what was captured.

        Field data: ~50 ticket runs ended with a wasteful pre-submit ritual
        because this MCP tool gave no confirmation of what the submission
        captured. The cleanliness gate and last simulation verdict make those
        facts explicit, so no manual re-verification is needed.
        """
        lines = [
            wrote,
            "",
            "Captured with this submission:",
            "  ticket worktree: clean (including staged and untracked files)",
        ]
        sim_line = self._last_simulate_line()
        if sim_line:
            lines.append(f"  {sim_line}")
        return "\n".join(lines)

    def _last_simulate_line(self) -> str | None:
        """Fingerprint of the newest simulate per-target report, or None.

        Reads the ``sim/<N>/targets/<target>/simulation.json`` files Simulation writes into the
        runtime flow-reports dir (``self.args.report_dir`` resolves there in
        ticket mode). Best-effort: absent/unreadable reports are omitted.
        """
        endpoint_report_dir = self.args.report_dir
        if endpoint_report_dir is None:
            return None
        report_dir = Path(endpoint_report_dir)
        if report_dir.name == "mcp-tool-reports":
            report_dir = report_dir.parent / "flow-reports"
        try:
            newest = max(
                Path(report_dir).glob("sim/*/targets/*/simulation.json"),
                key=lambda p: p.stat().st_mtime,
                default=None,
            )
            if newest is None:
                return None
            data = json.loads(newest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        target = data.get("target") or newest.parent.name
        stamp = data.get("timestamp")
        if stamp:
            stamp = format_human_datetime_safe(str(stamp), seconds=True)
        return f"last sim: {target} passed={data.get('passed')} at {stamp}"

    def _validate_type_specific_args(self, ticket_type: str) -> McpToolResult | None:
        """Reject when the wrong type-specific arg was supplied.

        The developer must pass exactly the arg matching the ticket type.
        Returning EXIT_ERROR lets it retry with the correct arg without a
        bogus REPORT.md hitting disk.
        """
        expected_cli, expected_attr, _ = _TYPE_FIELD[ticket_type]
        generic_value = getattr(self.args, "type_specific_detail", None)
        expected_value = generic_value or getattr(self.args, expected_attr, None)
        if not expected_value or not expected_value.strip():
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(
                    f"submit_run_report: ticket type is {ticket_type!r} but "
                    f"neither --type-specific-detail nor {expected_cli} was "
                    f"provided (or the provided value was empty). Re-run with "
                    '--type-specific-detail "..." for MCP/native calls, or '
                    f'{expected_cli} "..." for CLI calls.'
                ),
            )

        # Any *other* type-specific arg being set is a sign the developer
        # picked the wrong one -- reject so the user/agent notices.
        wrong: list[str] = []
        if generic_value and generic_value.strip():
            for _tt, (cli, attr, _) in _TYPE_FIELD.items():
                val = getattr(self.args, attr, None)
                if val and val.strip():
                    wrong.append(cli)
            if wrong:
                return McpToolResult(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        "submit_run_report: use --type-specific-detail by "
                        "itself, or use exactly one legacy field. Do not pass: "
                        f"{', '.join(wrong)}."
                    ),
                )

        for tt, (cli, attr, _) in _TYPE_FIELD.items():
            if tt == ticket_type:
                continue
            val = getattr(self.args, attr, None)
            if val and val.strip():
                wrong.append(cli)
        if wrong:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(
                    f"submit_run_report: ticket type is {ticket_type!r}. "
                    f"Use only {expected_cli}; do not pass: {', '.join(wrong)}."
                ),
            )

        return None

    def _type_specific_value(self, ticket_type: str) -> str:
        """Return the report detail value after validation has succeeded."""
        generic_value = getattr(self.args, "type_specific_detail", None)
        if generic_value and generic_value.strip():
            return generic_value

        _expected_cli, expected_attr, _ = _TYPE_FIELD[ticket_type]
        return getattr(self.args, expected_attr)

    def _unmet_optional_criteria(self) -> list[str]:
        """Return visible optional criteria that are not currently met."""
        return sorted(
            key
            for key, entry in self.state.criteria.items()
            if not key.startswith("_") and not entry.mandatory and not entry.met
        )

    def _validate_optional_criteria_justification(
        self, unmet_optional: list[str]
    ) -> McpToolResult | None:
        """Require a report explanation whenever optional criteria remain unmet."""
        value = self.args.optional_criteria_justification
        if not unmet_optional or (value and value.strip()):
            return None
        names = ", ".join(unmet_optional)
        return McpToolResult(
            exit_code=EXIT_ERROR,
            report_text=(
                "submit_run_report: optional criteria remain unmet: "
                f"{names}. Re-run with --optional-criteria-justification "
                '"..." explaining why each one could not be completed.'
            ),
        )

    @staticmethod
    def _optional_criteria_section(unmet_optional: list[str], justification: str | None) -> str:
        """Render the conditional unmet-optional-criteria report section."""
        if not unmet_optional:
            return ""
        assert justification is not None
        criteria = "\n".join(f"- `{key}`" for key in unmet_optional)
        return f"\n## Unmet optional criteria\n\n{criteria}\n\n{justification.strip()}\n"

    def _review_dispositions_section(self) -> str:
        """Render deterministic advisory findings and every accepted waiver."""
        from booley.evidence.review_dispositions import collect_review_dispositions

        rows = collect_review_dispositions(self.state.criteria)
        visible_dispositions = {"reported", "open", "waived"}
        visible = [row for row in rows if row["disposition"] in visible_dispositions]
        done_criteria = sorted(
            key
            for key, entry in self.state.criteria.items()
            if key.startswith("review_") and key.endswith("_done") and entry.met
        )
        if not visible and not done_criteria:
            return ""
        lines = ["", "## Review findings and waivers", ""]
        reported_criteria = {row["criterion"] for row in visible}
        for criterion in done_criteria:
            if criterion not in reported_criteria:
                lines.append(f"- **REVIEWED — NO FINDINGS** `{self._report_text(criterion)}`")
        for row in visible:
            location = f"{row['file']}:{row['line']}" if row["file"] else "location unavailable"
            label = {
                "reported": "REPORTED",
                "open": "OPEN",
                "waived": "WAIVED",
            }[row["disposition"]]
            finding_id = f" [{row['finding_id']}]" if row["finding_id"] else ""
            lines.append(
                f"- **{label} {self._report_text(row['severity'])}**{finding_id} "
                f"`{self._report_text(row['criterion'])}` at "
                f"`{self._report_text(location)}` — {self._report_text(row['summary'])}"
            )
            if row["reviewer_disposition"]:
                lines.append(
                    f"  - Reviewer disposition: {self._report_text(row['reviewer_disposition'])}"
                )
            if row["ticket_clause"]:
                lines.append(f"  - Ticket clause: {self._report_text(row['ticket_clause'])}")
            if row["disposition"] == "waived":
                lines.append(f"  - Justification: {self._report_text(row['justification'])}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _report_text(value: object) -> str:
        """Render persisted agent text inertly on one Markdown line."""
        text = " ".join(str(value).split())
        return text.replace("`", "'").replace("<", "&lt;").replace(">", "&gt;")

    def _file_justifications_section(self) -> str:
        """Render the validated explanations alongside the Developer's summary."""
        justifications = getattr(self, "file_justifications", {})
        rows = [
            f"- `{self._report_text(path)}`: {self._report_text(reason)}"
            for path, reason in justifications.items()
        ]
        return "\n## File justifications\n\n" + "\n".join(rows or ["- No changed files."]) + "\n"

    def _write_report(
        self,
        *,
        ticket_type: str,
        summary: str,
        type_heading: str,
        type_value: str,
        uncertainties: str,
        unmet_optional: list[str],
        optional_criteria_justification: str | None,
    ) -> str | None:
        """Render and stage REPORT.md, returning its path when configured."""
        logs_dir = os.environ.get("BOOLEY_LOGS_DIR", "")
        report_dir = Path(logs_dir) if logs_dir else self.args.report_dir
        if report_dir is None:
            logger.warning("submit_run_report: report directory is unavailable")
            return None

        slug = self.args.slug or "<unknown>"
        timestamp = format_human_datetime(datetime.now(UTC), seconds=True)
        optional_section = self._optional_criteria_section(
            unmet_optional, optional_criteria_justification
        )
        review_section = self._review_dispositions_section()
        file_section = self._file_justifications_section()
        content = (
            f"# Run report: {slug}\n"
            f"\n"
            f"- Ticket type: `{ticket_type}`\n"
            f"- Submitted: {timestamp}\n"
            f"\n"
            f"## Summary\n"
            f"\n"
            f"{summary.strip()}\n"
            f"\n"
            f"## {type_heading}\n"
            f"\n"
            f"{type_value.strip()}\n"
            f"\n"
            f"## Uncertainties (for the reviewer)\n"
            f"\n"
            f"{uncertainties.strip()}\n"
            f"{optional_section}"
            f"{review_section}"
            f"{file_section}"
        )

        return self._stage_report(report_dir, content)

    def _stage_report(self, report_dir: Path, content: str) -> str:
        """Stage bytes under one validated submission attempt."""
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "REPORT.md"
        from booley.runtime.execution_lease import validate_recording

        validate_recording(getattr(self.args, "work_dir", None))
        attempt_id = os.environ.get("BOOLEY_DISPLAY_INVOCATION_ID") or uuid.uuid4().hex
        self._submission_expected_identity = self._submission_identity()
        self._submission_execution_id = os.environ.get("BOOLEY_EXECUTION_ID", "")
        self._submission = submission.Submission(
            report_dir,
            attempt_id,
            self._submission_expected_identity,
            self._submission_execution_id,
            validate=self._validate_submission_authority,
        )
        self._submission.stage(content.encode("utf-8"))
        return str(report_path)


if __name__ == "__main__":
    SubmitRunReportMcpTool().cli()
