"""SubmitRunReportMcpTool writes the final human-readable run report.

The developer calls this once, as its very last action, after all
mandatory acceptance criteria are met. The MCP tool writes ``REPORT.md`` into
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
from datetime import UTC, datetime
from pathlib import Path

from booley.runtime import job_records as jobrec
from booley.runtime.pid import is_pid_alive
from booley.runtime.ticket_repositories import TicketWorkspaceError, pending_ticket_changes
from booley.runtime.timefmt import format_human_datetime, format_human_datetime_safe

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
        "and justification for every unmet optional criterion. "
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

    def _pre_state_gate(self) -> McpToolResult | None:
        """Refuse the final report until every detached ticket job is terminal."""
        active = [rec for rec in jobrec.list_records() if jobrec.is_active(rec, is_pid_alive)]
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
        required.add("type_specific_detail")
        schema["required"] = sorted(required)
        return schema

    def _run(self) -> McpToolResult:
        if gate := self._clean_worktree_gate():
            return gate
        if gate := self._criteria_freshness_gate():
            return gate

        ticket_type = os.environ.get("BOOLEY_TICKET_TYPE", "").strip()
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

        return self._submit_report(ticket_type, unmet_optional)

    def _submit_report(
        self,
        ticket_type: str,
        unmet_optional: list[str],
    ) -> McpToolResult:
        """Write and record a report after all finalization gates pass."""

        _cli_arg, _attr, heading = _TYPE_FIELD[ticket_type]
        type_specific_value = self._type_specific_value(ticket_type)

        report_path = self._write_report(
            ticket_type=ticket_type,
            summary=self.args.summary,
            type_heading=heading,
            type_value=type_specific_value,
            uncertainties=self.args.uncertainties,
            unmet_optional=unmet_optional,
            optional_criteria_justification=self.args.optional_criteria_justification,
        )

        # Set the internal criterion so criteria_acceptance can verify the
        # report was submitted before transitioning the ticket to review.
        self.set_criterion(
            _REPORT_CRITERION,
            True,
            detail={
                "report_path": str(report_path) if report_path else "",
                "ticket_type": ticket_type,
                "unmet_optional_criteria": unmet_optional,
            },
        )

        wrote = (
            f"Wrote {report_path}"
            if report_path
            else "Report content prepared (no report_dir configured -- not written to disk)"
        )
        return McpToolResult(
            exit_code=EXIT_SUCCESS,
            criterion_key=_REPORT_CRITERION,
            criterion_met=True,
            report_text=self._confirmation_text(wrote),
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

        Reads the ``simulate_<target>.json`` files simulate writes into the
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
                Path(report_dir).glob("sim_*.json"),
                key=lambda p: p.stat().st_mtime,
                default=None,
            )
            if newest is None:
                return None
            data = json.loads(newest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        target = data.get("target") or newest.stem.removeprefix("sim_")
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
        from booley.dev_support.review_dispositions import collect_review_dispositions

        rows = collect_review_dispositions(self.state.criteria)
        visible_dispositions = {"reported", "advisory", "deferred", "out_of_scope", "waived"}
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
                "advisory": "ADVISORY",
                "deferred": "DEFERRED",
                "out_of_scope": "OUT OF SCOPE",
                "waived": "WAIVED",
            }[row["disposition"]]
            finding_id = f" [{row['finding_id']}]" if row["finding_id"] else ""
            lines.append(
                f"- **{label} {self._report_text(row['severity'])}**{finding_id} "
                f"`{self._report_text(row['criterion'])}` at "
                f"`{self._report_text(location)}` — {self._report_text(row['summary'])}"
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
        """Render REPORT.md and write it to the human-facing ticket log root.

        Returns the absolute path written, or None when no report_dir is
        configured (human / standalone mode).
        """
        logs_dir = os.environ.get("BOOLEY_LOGS_DIR", "")
        report_dir = Path(logs_dir) if logs_dir else self.args.report_dir
        if report_dir is None:
            logger.warning("submit_run_report: no report_dir configured, skipping file write")
            return None

        slug = self.args.slug or "<unknown>"
        timestamp = format_human_datetime(datetime.now(UTC), seconds=True)
        optional_section = self._optional_criteria_section(
            unmet_optional, optional_criteria_justification
        )
        review_section = self._review_dispositions_section()
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
        )

        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "REPORT.md"
        report_path.write_text(content, encoding="utf-8")
        return str(report_path)


if __name__ == "__main__":
    SubmitRunReportMcpTool().cli()
