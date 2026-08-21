"""TbCoderSpecialist — testbench/verification Specialist.

Reads an instruction markdown file, validates scope against the fixed "tb"
category, spawns an LLM agent to apply changes, and commits from Python.

The agent writes code but does NOT run git commands. After the agent
finishes, Python stages scoped files and commits with the message
from the agent's structured output (or a default fallback).

Exit codes: 0 = committed, 1 = no changes / escalation, 2 = bad args / scope mismatch.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, ClassVar

from booley.dev_support.workspace_isolation import (
    CATEGORY_GUARD,
    build_category_deny_patterns,
    clean_sim_artifacts,
    filter_state_file_for_category,
    hide_opposite_sources,
    hide_specific_files,
    validate_scope_category,
)
from booley.mcp.base import (
    EXIT_ERROR,
    EXIT_FAILURE,
    EXIT_SUCCESS,
    McpToolResult,
    read_source_dirs_from_toml,
)
from booley.runtime.paths import refs_dir

from .specialist import Specialist, _git_head_sha

logger = logging.getLogger(__name__)

# tb_coder authors testbench/verification code only; the category is permanent.
_CATEGORY = "tb"

# Artifact written by the in-context planning phase (see _verification_plan_path).
_PLAN_FILENAME = "verification_plan.md"

# Verification-planning guidance, inherited from the retired planner
# specialist and trimmed to the plan structure only:
# the spec-ambiguity manifest (it fed the deleted spec_arbiter) is intentionally
# omitted. tb_coder plans and implements in ONE isolated,
# RTL-blind agent context, so this is a preamble to the implementation task —
# not a separate specialist session.
_VERIFICATION_PLAN_GUIDANCE = """\
Before writing any testbench code, produce a verification plan with these sections:

1. **Existing TB Assessment** -- classify the testbench already in scope:
   - Display-only (just instantiates and dumps waves)?
   - Hardcoded vectors (directed but no checking)?
   - Has assertions (self-checking)?
   - What does it cover? What does it miss?

2. **Features to Verify** -- for each spec/ticket requirement:
   - Requirement description
   - Currently covered? (yes/partial/no)
   - Action: keep / modify / add

3. **Verification Approach** -- checking mechanism:
   - Self-checking assertions, scoreboard, reference model, or combination
   - TB architecture (monitor, driver, checker structure)

4. **Stimulus Plan** -- three mandatory subsections:
   - **Directed vectors** -- specific test scenarios
   - **Corner cases** -- boundary values, error conditions, edge cases
   - **Randomized inputs** -- ranges, constraints, iteration counts

5. **Stimulus Breadth** -- scenarios the TB should exercise:
   - Boundary, state, protocol, and data cases the stimulus/checks should cover
   - Do NOT request covergroups, coverpoints, cross-coverage, coverage counters,
     or coverage pass/fail thresholds unless the task explicitly requires coverage
     instrumentation. The TB should be self-checking; mutation score and configured
     criteria judge test strength.

6. **Simulation Configs** -- the configs the testbench must pass.

Derive the DUT interface from the requirements/notes below and the TB scope files
you can read. The RTL implementation is hidden from you BY DESIGN: plan the
verification independently from the spec, not by mirroring an implementation you
cannot see. If a point is genuinely ambiguous, pick exactly one reading and state
it explicitly in the plan."""


def _is_plan_capability_output(content: str) -> bool:
    """Return true when content starts with plan-agent-capability YAML frontmatter."""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return any(line.strip() == "source: plan agent capability" for line in lines[1:idx])
    return False


def _strip_plan_capability_narration(content: str) -> str:
    """Remove status prose from plan-agent-capability output before prompt injection.

    Pre-authored plan files generated after an arbiter rewrite (a retired
    pipeline) can contain the authoring agent's
    progress updates between YAML frontmatter and the first real plan heading.
    Those updates are not implementation instructions, so keep the metadata and
    the sectioned plan while dropping the interstitial prose.
    """
    if not _is_plan_capability_output(content):
        return content

    lines = content.splitlines()
    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return content

    first_heading_idx: int | None = None
    for idx in range(end_idx + 1, len(lines)):
        if lines[idx].startswith("#"):
            first_heading_idx = idx
            break
    if first_heading_idx is None:
        return content

    cleaned_lines = [*lines[: end_idx + 1], "", *lines[first_heading_idx:]]
    return "\n".join(cleaned_lines) + ("\n" if content.endswith("\n") else "")


def _strip_markdown_section(content: str, heading: str) -> str:
    """Drop a markdown section by exact heading, preserving surrounding text."""
    lines = content.splitlines()
    cleaned: list[str] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if line.strip() != heading:
            cleaned.append(line)
            idx += 1
            continue

        level = len(line) - len(line.lstrip("#"))
        idx += 1
        while idx < len(lines):
            candidate = lines[idx]
            candidate_level = len(candidate) - len(candidate.lstrip("#"))
            if (
                candidate_level > 0
                and candidate.startswith("#" * candidate_level)
                and candidate_level <= level
            ):
                break
            idx += 1

        while cleaned and cleaned[-1] == "":
            cleaned.pop()

    return "\n".join(cleaned) + ("\n" if content.endswith("\n") else "")


def _strip_arbiter_provenance(content: str) -> str:
    """Remove arbiter provenance phrases the retired planner folded into plans."""
    content = re.sub(
        r"\s+This follows spec arbiter decision D\d+, whose ruling is `[^`]+`\.",
        ".",
        content,
    )
    content = re.sub(r"\bPer spec arbiter decision D\d+,\s*", "", content)
    content = re.sub(r"\s+as required by arbiter decision D\d+", "", content)
    content = re.sub(r",?\s+aligning with arbiter decision D\d+", "", content)
    content = re.sub(r"\s+to match arbiter decision D\d+", "", content)
    return content


def _prepare_instruction_content(content: str) -> str:
    """Clean a pre-authored plan file for the testbench/verification coder."""
    content = _strip_plan_capability_narration(content)
    if not _is_plan_capability_output(content):
        return content

    content = _strip_markdown_section(content, "## RTL Implementation Steps")
    return _strip_arbiter_provenance(content)


# Style guides per category — resolved at call time via booley.runtime.paths
def _category_guides() -> dict[str, list[str]]:
    """Build style guide paths: package default + project override."""
    rd = str(refs_dir())
    return {
        "tb": [
            f"{rd}/tb_style_guide.md",
            ".booley_project/tb_style_guide.md",
        ],
    }


def _resolve_scope_files(scope: str, work_dir: Path) -> list[str]:
    """Expand comma-separated globs into matching file paths (POSIX-style relative).

    Exact paths (no glob metacharacters) are included even if the file doesn't
    exist yet — this allows the agent to create new files within scope.
    """
    import glob as globmod

    _GLOB_META = set("*?[")
    patterns = [s.strip() for s in scope.split(",") if s.strip()]
    matched: list[str] = []
    for pattern in patterns:
        # stdlib glob's root_dir (relative results) + recursive `**` semantics
        # differ from Path.glob (dotfile handling, absolute results); keep as-is.
        hits = globmod.glob(pattern, root_dir=str(work_dir), recursive=True)  # noqa: PTH207
        if hits:
            matched.extend(hits)
        elif not _GLOB_META.intersection(pattern):
            matched.append(pattern)
    # Normalise to forward-slash relative paths
    return [p.replace("\\", "/") for p in matched]


def _category_dir_prefixes(work_dir: Path) -> tuple[str, ...]:
    """Return testbench source-dir prefixes (POSIX, no trailing slash variants).

    Reuses the same booley.toml lookup (``read_source_dirs_from_toml``) as the
    other category-aware endpoints so the
    set of permitted directories stays consistent across endpoints.  Falls back to
    ``("tb",)`` when no toml is available.
    """
    parsed = read_source_dirs_from_toml(work_dir)
    if parsed is not None:
        _rtl_dirs, tb_dirs = parsed
        return tuple(sorted({d.rstrip("/\\") for d in tb_dirs}))
    return ("tb",)


def _category_globs(work_dir: Path) -> list[str]:
    """Return ``<dir>/*.{sv,svh,v}`` globs for every testbench dir.

    Used by scope narrowing — the Coder may create new files under any of these
    directories without triggering an out-of-scope rejection in the pre-commit
    hook.
    """
    suffixes = ("sv", "svh", "v")
    globs: list[str] = []
    for d in _category_dir_prefixes(work_dir):
        for sfx in suffixes:
            globs.append(f"{d}/*.{sfx}")
    return globs


@contextlib.contextmanager
def _narrowed_scope_file(work_dir: Path, narrowed: list[str]):
    """Temporarily overwrite ``<work_dir>/.scope.json`` with the narrowed list.

    Restores the original contents on context exit (success or exception).  When
    no scope file exists (legacy / standalone mode) or *narrowed* is empty,
    the narrowing is a no-op so the broader ticket scope stays in effect.
    """
    scope_path = work_dir / ".scope.json"
    if not narrowed or not scope_path.exists():
        yield
        return
    try:
        original_bytes = scope_path.read_bytes()
    except OSError as exc:
        logger.warning("Could not read .scope.json for narrowing: %s", exc)
        yield
        return
    try:
        scope_path.write_text(
            json.dumps({"scope": narrowed}, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.debug("Narrowed .scope.json to %d entries", len(narrowed))
        yield
    finally:
        try:
            scope_path.write_bytes(original_bytes)
            logger.debug("Restored .scope.json from snapshot")
        except OSError as exc:
            logger.error(
                "CRITICAL: failed to restore .scope.json (%s); ticket scope may be incorrect",
                exc,
            )


def _format_commit_stdout(commit: dict[str, Any]) -> str:
    """Format the human-readable stdout for a committed change.

    ``commit`` is the dict returned by ``Specialist._commit_agent_changes`` —
    every field this needs (sha/subject/stat_summary/changed_files/
    reverted_files) already lives there, so callers pass it through as-is
    instead of unpacking it field by field. The category is always the
    module-level ``tb`` constant.
    """
    sha = commit["sha"]
    subject = commit["subject"]
    stat_summary = commit["stat_summary"]
    changed_files = commit["changed_files"]
    reverted_files: list[dict[str, str]] = commit.get("reverted_files", [])

    files_line = ", ".join(changed_files) if changed_files else "(no files)"
    stdout = (
        f'[implement] {_CATEGORY}\n  {stat_summary}\n  {files_line}\n  commit: {sha} "{subject}"\n'
    )
    if reverted_files:
        names = ", ".join(r["original_path"] for r in reverted_files)
        stdout += (
            f"  ⚠ {len(reverted_files)} out-of-scope file(s) reverted (saved to logs): {names}\n"
        )
    stdout += "\nRESULT: PASS"
    return stdout


def _build_commit_display_lines(commit: dict[str, Any]) -> list[str]:
    """Build rich display lines for the terminal endpoint box from a commit dict."""
    sha = commit["sha"]
    subject = commit["subject"]
    stat_summary = commit["stat_summary"]
    file_stats: dict[str, tuple[int, int]] = commit.get("file_stats", {})
    reverted_files: list[dict[str, str]] = commit.get("reverted_files", [])

    lines: list[str] = []
    if file_stats:
        max_name = max(len(f) for f in file_stats) if file_stats else 0
        for fname, (added, deleted) in file_stats.items():
            lines.append(f"  {fname:<{max_name}}  +{added} -{deleted}")
    else:
        lines.append(stat_summary)
    lines.append(f'  {sha} "{subject}"')
    if reverted_files:
        lines.append(f"  ⚠ reverted {len(reverted_files)} out-of-scope file(s)")
    return lines


class TbCoderSpecialist(Specialist):
    """Testbench/verification code-modifying agent -- applies changes from instruction file and commits."""

    name: str = "tb_coder"
    description: str = (
        "Testbench/verification code-modifying agent -- applies changes "
        "from instruction file and commits"
    )
    code_modifying: bool = True
    config_aware: bool = False
    min_model: str = "standard"

    default_timeout: int = 1800  # 30 min
    min_timeout: int = 900  # 15 min
    _default_commit_message = "feat: apply changes"
    agent_capabilities: ClassVar[list[str]] = ["Edit", "Write", "Read", "Grep", "Glob", "Bash"]
    # Nested-MCP allowlist lives in booley.runtime.nested_mcp_capabilities.
    satisfies: ClassVar[list[str]] = []  # code-modifying Specialist, no criterion of its own

    @property
    def display_tag(self) -> str | None:
        return _CATEGORY

    def _disallowed_agent_capabilities(self) -> list[str] | None:
        return build_category_deny_patterns(_CATEGORY, self.args.work_dir)

    def _output_format(self) -> dict[str, Any] | None:
        return {
            "type": "object",
            "properties": {
                "commit_message": {
                    "type": "string",
                    "description": (
                        "Conventional commit: '<type>(<scope>): <summary>'. "
                        "Types: feat/fix/refactor/test/review/wip/docs. "
                        "Scope: lowercase module name. Summary: max 72 chars."
                    ),
                },
            },
            "required": ["commit_message"],
            "additionalProperties": False,
        }

    def _add_agent_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--instruction-file",
            type=Path,
            required=True,
            help="Path to instruction markdown file",
        )
        parser.add_argument(
            "--scope",
            required=True,
            help=(
                "Comma-separated file globs to modify. "
                "All files must live under the testbench/verification dirs "
                "(tb/,verif/,data/). "
                "Out-of-scope (e.g. RTL) files → rejected."
            ),
        )
        parser.add_argument(
            "--steer",
            action="append",
            default=[],
            help=(
                "Developer Agent context to inject into prompt. Repeatable — "
                'over MCP this is an array of strings (pass ["..."], not a '
                "bare string)."
            ),
        )
        parser.add_argument(
            "--hide-files",
            default="",
            help="Comma-separated file paths to hide from the agent",
        )
        parser.add_argument(
            "--skip-plan",
            action="store_true",
            help=(
                "Skip the in-context verification-planning phase and treat the "
                "instruction file as a complete, ready-to-implement plan. Default "
                "(planning on): the agent first writes a verification plan, then "
                "implements the testbench from it in the same isolated context."
            ),
        )

    def _verification_plan_path(self) -> Path:
        """Resolve where the agent should write the verification plan artifact.

        Prefer the developer logs dir (where downstream reviewers look), then
        an explicit ``--report-dir``, then the work dir as a last resort.
        """
        logs_dir_env = os.environ.get("BOOLEY_LOGS_DIR", "")
        if logs_dir_env:
            return Path(logs_dir_env) / _PLAN_FILENAME
        report_dir = getattr(self.args, "report_dir", None)
        if report_dir:
            return Path(report_dir) / _PLAN_FILENAME
        return self.args.work_dir / _PLAN_FILENAME

    def _build_prompt(self) -> str:
        """Build the prompt from the instruction file and args.

        With planning on (the default), the agent plans the verification
        strategy and implements the testbench in one isolated, RTL-blind
        context.  ``--skip-plan`` treats the instruction file as a finished
        plan and implements it directly (legacy behavior).
        """
        instruction_path: Path = self.args.instruction_file
        scope: str = self.args.scope
        planning: bool = not self.args.skip_plan
        instruction_content = _prepare_instruction_content(
            instruction_path.read_text(encoding="utf-8"),
        )
        guard = CATEGORY_GUARD[_CATEGORY]

        parts: list[str] = [
            "# Implementation Task\n",
            f"**Category:** {_CATEGORY}\n",
            f"**Scope:** {scope}\n",
            self._task_section(scope, planning),
            f"\n## Scope Guard\n\n{guard}\n",
        ]

        self._append_style_guides(parts)
        if planning:
            self._append_planning_section(parts)
        boundary_label = (
            "Verification Requirements"
            if planning
            else "Verification/Testbench Implementation Plan"
        )
        parts.append(f"\n## Begin {boundary_label}\n")
        parts.append(instruction_content)
        parts.append(f"\n## End {boundary_label}\n")

        self._append_restrictions_and_context(parts)
        return "\n".join(parts)

    @staticmethod
    def _task_section(scope: str, planning: bool) -> str:
        """Return the direct implementation mandate for the specialist."""
        if planning:
            return (
                "\n## Task\n\n"
                "Your task is to FIRST produce a verification plan, then implement "
                "the testbench/verification in SystemVerilog from that plan.\n"
                f"Create or modify only `{scope}`. Do not edit or inspect RTL files. "
                "Treat the requirements below as the source of truth; plan the "
                "verification independently and then build it.\n"
            )
        return (
            "\n## Task\n\n"
            "Your task is to implement the verification/testbench plan below in "
            "SystemVerilog.\n"
            f"Create or modify only `{scope}`. Do not edit or inspect RTL files. "
            "Treat the verification steps as actionable; RTL implementation "
            "notes are context only unless the scope guard explicitly allows them.\n"
        )

    def _append_planning_section(self, parts: list[str]) -> None:
        """Append the in-context verification-planning phase to the prompt."""
        plan_path = self._verification_plan_path()
        parts.append("\n## Verification Planning (do this FIRST)\n")
        parts.append(_VERIFICATION_PLAN_GUIDANCE)
        parts.append(
            f"\n\nWrite the completed verification plan to `{plan_path}` with the "
            "Write agent capability BEFORE editing any testbench file, then implement the "
            "testbench to satisfy that plan. The plan file is a review artifact, "
            "not part of the design — do not treat writing it as work the ticket "
            "asked for.\n"
        )

    def _append_style_guides(self, parts: list[str]) -> None:
        """Resolve and append style guide references to prompt parts."""
        guides: list[str] = []
        for g in _category_guides()[_CATEGORY]:
            gp = Path(g)
            if gp.is_absolute():
                if not gp.is_file():
                    logger.warning("Style guide missing, omitting from prompt: %s", gp)
                    continue
                guides.append(g)
            else:
                path = self.args.work_dir / g
                if path.is_file():
                    guides.append(str(path))
        if guides:
            parts.append("\n## Style Guides\n")
            parts.append("You MUST read these guides before making any changes:\n")
            for g in guides:
                parts.append(f"- `{g}`\n")
            parts.append(
                "Only read project-specific extension files if they are listed "
                "above; optional links inside guides may be absent.\n",
            )

    def _append_restrictions_and_context(self, parts: list[str]) -> None:
        """Append restrictions, commit guidance, escalation, and context."""
        parts.append("\n## Restrictions\n")
        parts.append(
            "Do NOT run git commands — commits are handled automatically.\n"
            "Do NOT run a full simulator or linter.\n"
            "Do NOT create a booley/ directory or any files under booley/ — "
            "the booley package is pre-installed and creating local files "
            "shadows it, breaking subsequent agent-capability calls.\n"
            "Do NOT add `$dumpfile` or `$dumpvars` to any TB or RTL file. "
            "The harness manages tracing via `+tracefile` plusargs and an "
            "auxiliary module; user-authored dump calls override that path "
            "and break trace collection (coverage_analyst, bwave).\n"
        )
        parts.append("\n## Before You Submit: Elaborate\n")
        changed_scope = "testbench/verification changes"
        parts.append(
            "Call the **elab** MCP tool on every config in scope to confirm "
            f"your {changed_scope} compile and elaborate cleanly. If it reports "
            "errors, read them, fix the offending file, and re-run elab. "
            "Only submit once elab passes — sending non-elaborating code "
            "burns a full developer round.\n"
        )
        parts.append(f"\n{self.COMMIT_MSG_GUIDANCE}\n")
        banned_note = self.commit_msg_banned_phrase_note()
        if banned_note:
            parts.append(f"\n{banned_note}\n")
        parts.append("\n## Escalation Protocol\n")
        parts.append(
            "If critical info is missing, stop with `[ESCALATION]` prefix explaining what's missing.\n"
        )
        steer_text = self.steering_text()
        if steer_text:
            parts.append("\n## Developer Agent Context\n")
            parts.append(steer_text)
            parts.append("\n")
        if self.args.instruction:
            parts.append("\n## Additional Notes\n")
            parts.append(self.args.instruction)
            parts.append("\n")

    def _interpret_output(self, output: str, structured: dict | None) -> McpToolResult:
        """Interpret agent output: check for escalation, commit changes from Python."""
        # Check for escalation
        if "[ESCALATION]" in (output or ""):
            logger.warning("Agent reported escalation")
            return McpToolResult(
                exit_code=EXIT_FAILURE,
                report_text="BLOCKED",
                detail={"reason": "escalation", "output": (output or "")[:500]},
            )

        # Commit from Python — agent writes code, we handle git
        commit, error_result = self._try_commit(structured)
        if error_result is not None:
            return error_result
        if not commit:
            logger.warning("No changes to commit after agent run")
            return McpToolResult(
                exit_code=EXIT_FAILURE,
                report_text="No commit produced",
                detail={"reason": "no_commit"},
            )

        return self._build_commit_result(commit)

    def _try_commit(
        self,
        structured: dict | None,
    ) -> tuple[dict[str, Any] | None, McpToolResult | None]:
        """Attempt to commit agent changes. Returns (commit, error_result)."""
        msg = self._extract_commit_message(structured) or self._default_commit_message
        scope_files = self._resolve_effective_scope_files()
        try:
            commit = self._commit_agent_changes(msg, scope_files=scope_files)
        except self.GitStatusError as exc:
            return None, McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=f"Git broken — cannot commit: {exc}",
                detail={"reason": "git_error", "error": str(exc)},
            )
        return commit, None

    def _build_commit_result(self, commit: dict[str, Any]) -> McpToolResult:
        """Build McpToolResult from a successful commit."""
        sha = commit["sha"]
        subject = commit["subject"]
        stat_summary = commit["stat_summary"]
        changed_files = commit["changed_files"]
        file_stats: dict[str, tuple[int, int]] = commit.get("file_stats", {})
        reverted_files: list[dict[str, str]] = commit.get("reverted_files", [])

        stdout = _format_commit_stdout(commit)
        display_lines = _build_commit_display_lines(commit)

        detail: dict[str, Any] = {
            "commit_sha": sha,
            "commit_subject": subject,
            "stat_summary": stat_summary,
            "changed_files": changed_files,
            "file_stats": file_stats,
            "category": _CATEGORY,
        }
        if reverted_files:
            detail["reverted_files"] = reverted_files

        return McpToolResult(
            exit_code=EXIT_SUCCESS,
            report_text=stdout,
            display_lines=display_lines,
            detail=detail,
        )

    def _run(self) -> McpToolResult:
        """Pre-validate inputs, then invoke the agent."""
        # --- Validation ---
        instruction_path: Path = self.args.instruction_file
        if not instruction_path.exists():
            logger.error("Instruction file not found: %s", instruction_path)
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=f"Instruction file not found: {instruction_path}",
            )
        if not instruction_path.is_file():
            logger.error("--instruction-file is not a file: %s", instruction_path)
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(f"--instruction-file must be a file path, got: {instruction_path}"),
            )

        # Validate scope against category
        scope_files = self._resolve_effective_scope_files()
        if not scope_files:
            logger.error(
                "No files matched scope pattern %r in %s",
                self.args.scope,
                self.args.work_dir,
            )
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=f"No files matched scope pattern: {self.args.scope!r}",
            )
        err = validate_scope_category(scope_files, _CATEGORY, self.args.work_dir)
        if err:
            logger.error("Scope/category mismatch: %s", err)
            return McpToolResult(exit_code=EXIT_ERROR, report_text=err)

        self.emit_progress(f"scope validated ({len(scope_files)} files)")

        # Fixed "tb" category drives criteria invalidation.
        self.modifies_category = _CATEGORY

        # Snapshot HEAD before agent invocation (used by _commit_agent_changes)
        self._before_sha = _git_head_sha(self.args.work_dir)

        clean_sim_artifacts(self.args.work_dir)

        hide_files = [f.strip() for f in (self.args.hide_files or "").split(",") if f.strip()]
        narrowed = self._build_narrowed_scope()
        # Defense-in-depth: hide_opposite_sources covers the worktree, but
        # the coder agent also reads /ticket-logs/.runtime/booley_state.json which
        # carries opposite-category reviewer findings (RTL signal names,
        # fix-suggestions naming RTL ports). Project that file for the
        # duration of the agent run so the boundary is consistent across
        # both data channels.
        state_path = getattr(self.args, "state_file", None)
        with (
            hide_opposite_sources(self.args.work_dir, _CATEGORY),
            filter_state_file_for_category(state_path, _CATEGORY),
        ):
            self.emit_progress(f"workspace isolated, invoking {_CATEGORY} coder")
            with _narrowed_scope_file(self.args.work_dir, narrowed):
                if hide_files:
                    with hide_specific_files(self.args.work_dir, hide_files):
                        return super()._run()
                return super()._run()

    def _resolve_effective_scope_files(self) -> list[str]:
        """Resolve the requested scope into concrete files."""
        return _resolve_scope_files(self.args.scope, self.args.work_dir)

    def _build_narrowed_scope(self) -> list[str]:
        """Compute the narrowed ``.scope.json`` payload for this invocation.

        Source Isolation is directory-categorical: the Coder is narrowed to the
        glob set of its own category's source directories (``rtl``/``fw`` vs
        ``tb``), letting it create or edit files anywhere under those prefixes
        without an out-of-scope rejection while the ticket **Scope** pre-commit
        hook still bounds the commit. The RTL/TB file partition comes from
        FuseSoC ``tags:[tb]``, so narrowing reads category directories rather
        than a separate per-half file list.
        """
        # tb_coder is permanently TB-category, so its scope is exactly the
        # testbench directory globs derived from FuseSoC ``tags:[tb]``.
        return _category_globs(self.args.work_dir)


if __name__ == "__main__":
    TbCoderSpecialist().cli()
