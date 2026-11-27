"""The Run form's choices for one worktree as plain JSON values.

HTTP-free: ``web`` routes requests and calls these. They list Flows, Specialists, Targets,
Tests and forms, and quote, check and start a run. ``UnknownEntryError`` names
a Flow, Specialist or form kind that does not exist; any other trouble reading a worktree is a
``problem`` text beside what could still be read. ``InvalidRunError`` is a
request body of the wrong shape, ``RunRefusedError`` a run the form, the parser or the Agent
CLI's sign-in check refuses and ``CheckFailedError`` an argument check that itself raised.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TypeVar

from booley.core.boundary import is_str_list
from booley.harness.dashboard.launch import (
    NO_FACTS,
    ArgumentsError,
    Launcher,
    RunFacts,
    review_check,
    shell_command,
)
from booley.harness.dashboard.review import Occupants, agent_refusal, review_lines
from booley.harness.dashboard.run_form import NO_FORM, FormError, RunForm, form_argv
from booley.harness.dashboard.specialist_form import (
    COVERAGE_ANALYST,
    NO_COVERAGE_RECORDS,
    REVIEWER,
    CoverageRecordChoice,
    SimRun,
    SpecialistForm,
    finished_coverage_records,
)
from booley.harness.dashboard.targets import (
    RunChoices,
    TargetListing,
    TargetsReader,
    enabled_flows,
    first_line,
    runs_problem,
    targets_for,
    targets_problem,
)
from booley.harness.dashboard.web_snapshot import job_label
from booley.runtime.handles import HandleStore

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


class UnknownEntryError(LookupError):
    """The request names a built-in Flow, Specialist or form kind that does not exist."""


class InvalidRunError(ValueError):
    """The request body of a run is not the shape the routes take."""


class RunRefusedError(ValueError):
    """The form, the argument check or the Agent CLI's sign-in check refuses to run the request."""


class CheckFailedError(RuntimeError):
    """The argument check itself raised, so it neither passed nor rejected the arguments."""


def _cause(exc: Exception) -> str:
    """A read that failed, in words: the exception type, then its first line."""
    return f"{type(exc).__name__}: {first_line(exc)}"


def entries(launcher: Launcher, worktree: Path) -> dict[str, Any]:
    """The Flows and built-in Specialists *worktree* enables."""
    blurbs = {flow.name: flow.blurb for flow in launcher.flows()}
    specialists = launcher.specialists(worktree)
    return {
        "flows": [
            {"name": name, "blurb": blurbs[name]} for name in enabled_flows(blurbs, worktree)
        ],
        "specialists": [asdict(choice) for choice in specialists.choices],
        "specialists_problem": specialists.problem,
    }


def targets(
    launcher: Launcher, reader: TargetsReader, worktree: Path, flow: str
) -> dict[str, Any]:
    """The selectors of the Targets the built-in Flow *flow* can drive in *worktree*."""
    if flow not in {choice.name for choice in launcher.flows()}:
        raise UnknownEntryError(f"no such built-in Flow: {flow}")
    try:
        listing = reader.listing(worktree)
    except Exception as exc:
        logger.exception("Run Targets read failed")
        listing = TargetListing((), _cause(exc))
    return {"targets": list(targets_for(listing, flow)), "problem": targets_problem(listing, flow)}


def tests(reader: TargetsReader, worktree: Path, selector: str) -> dict[str, Any]:
    """The Test Runs of one Target: each Test, then ``test+variant`` per Test Variant."""
    try:
        choices = reader.test_runs(worktree, selector)
    except Exception as exc:
        logger.exception("Run Test Runs read failed")
        choices = RunChoices((), f"Tests unavailable: {_cause(exc)}")
    return {"runs": list(choices.runs), "problem": runs_problem(choices)}


def _known(launcher: Launcher, kind: str, name: str) -> None:
    """Raise ``UnknownEntryError`` unless *name* is a built-in Flow or Specialist of that *kind*."""
    known = {
        "flow": {choice.name for choice in launcher.flows()},
        "specialist": {REVIEWER, COVERAGE_ANALYST},
    }
    if name not in known.get(kind, ()):
        raise UnknownEntryError(f"no such built-in {kind}: {name}")


def _problem(exc: Exception) -> str:
    """Why a form could not be built: its own sentence, else the exception type and first line."""
    return str(exc) if isinstance(exc, FormError) else _cause(exc)


def _built(
    launcher: Launcher, worktree: Path, kind: str, name: str
) -> tuple[RunForm | SpecialistForm | None, str]:
    """The endpoint's form and why it could not be built (empty when it was).

    A Flow that cannot be built is ``NO_FORM``: its extra arguments alone. A Specialist that cannot be built is ``None``.
    """
    try:
        return launcher.form(kind, name, worktree), ""
    except Exception as exc:
        logger.exception("Run form build failed for %s %s in %s", kind, name, worktree)
        return NO_FORM if kind == "flow" else None, _problem(exc)


def form(
    launcher: Launcher,
    worktree: Path,
    kind: str,
    name: str,
    values: Mapping[str, str],
    coverage_records: Callable[[], tuple[SimRun, ...]],
) -> dict[str, Any]:
    """The form of a built-in Flow or Specialist: its fields for the *values* given.

    *coverage_records* reads the finished Simulation Jobs, called only for the coverage analyst.
    """
    _known(launcher, kind, name)
    built, problem = _built(launcher, worktree, kind, name)
    if built is None:
        return {"kind": kind, "problem": problem}
    if isinstance(built, RunForm):
        return {
            "kind": kind,
            "problem": problem,
            "fields": [asdict(field) for field in built.fields],
            "extra": {"hint": built.extra_hint(), "options": built.extras},
        }
    chosen = built.changed({**built.initial(), **values})
    records: tuple[CoverageRecordChoice, ...] = ()
    if name == COVERAGE_ANALYST:
        try:
            records = finished_coverage_records(worktree, coverage_records())
        except Exception:  # a crashed read lists none
            logger.exception("Run Coverage Records read failed")
    fields = [asdict(field) for field in built.fields(chosen)]
    for field in fields:
        if field["kind"] == "coverage_record" and not records:
            field["hint"] = NO_COVERAGE_RECORDS
    return {
        "kind": kind,
        "note": built.note,
        "values": chosen,
        "fields": fields,
        "coverage_records": [asdict(record) for record in records],
    }


@dataclass(frozen=True)
class RunRequest:
    """What a launch request names: the endpoint, the form's values and the extra arguments."""

    kind: str
    name: str
    values: Mapping[str, str | list[str]]
    extra: str


def run_request(body: Mapping[str, Any]) -> RunRequest:
    """The run a JSON *body* asks for: ``kind``, ``name``, optional ``values`` and ``extra``."""
    kind, name, extra = body.get("kind"), body.get("name"), body.get("extra", "")
    values = body.get("values", {})
    if not (isinstance(kind, str) and isinstance(name, str) and isinstance(extra, str)):
        raise InvalidRunError("kind, name and extra must be text")
    if not isinstance(values, dict) or not all(
        isinstance(value, str) or is_str_list(value) for value in values.values()
    ):
        raise InvalidRunError("values must map each field to text (a list of text for tests)")
    return RunRequest(kind, name, values, extra)


def _argv(launcher: Launcher, worktree: Path, run: RunRequest) -> tuple[str, ...]:
    """The words the form's values make.

    ``UnknownEntryError`` for an unknown endpoint, ``InvalidRunError`` for a value or extra
    text the endpoint's form does not take, ``RunRefusedError`` for the form's own error.
    """
    _known(launcher, run.kind, run.name)
    built, problem = _built(launcher, worktree, run.kind, run.name)
    if built is None:
        raise RunRefusedError(problem)
    chosen: Mapping[str, str | tuple[str, ...]]
    if isinstance(built, RunForm):
        tests = {field.dest for field in built.fields if field.kind == "tests"}
        if any(
            isinstance(value, list) and dest not in tests for dest, value in run.values.items()
        ):
            raise InvalidRunError("only a Flow's tests field takes a list")
        chosen = {
            dest: tuple(value) if isinstance(value, list) else value
            for dest, value in run.values.items()
        }
    else:
        texts = {dest: value for dest, value in run.values.items() if isinstance(value, str)}
        if run.extra or len(texts) != len(run.values):
            raise InvalidRunError("a Specialist takes text values and no extra arguments")
        chosen = built.changed({**built.initial(), **texts})
    try:
        return form_argv(built, chosen, run.extra)
    except ArgumentsError as exc:
        raise RunRefusedError(str(exc)) from exc


def command(launcher: Launcher, worktree: Path, run: RunRequest) -> dict[str, Any]:
    """The shell command of the run: it parses nothing and starts nothing."""
    return {"command": shell_command(worktree, run.kind, run.name, _argv(launcher, worktree, run))}


def _refusal(facts: RunFacts) -> str:
    return agent_refusal(facts.specialist) if facts.specialist is not None else ""


def _checked(worktree: Path, run: RunRequest, check_run: Callable[[], _T]) -> _T:
    """Run an argument check; ``CheckFailedError`` when it raises."""
    try:
        return check_run()
    except Exception as exc:
        logger.exception("Run argument check failed for %s %s in %s", run.kind, run.name, worktree)
        raise CheckFailedError(f"Argument check failed: {exc}") from exc


def check(
    launcher: Launcher, worktree: Path, run: RunRequest, occupants: Callable[[], Occupants]
) -> dict[str, Any]:
    """The review screen of the run: its command, its lines and why it may not launch.

    ``RunRefusedError`` when the endpoint's own parser rejects the arguments,
    ``CheckFailedError`` when the check itself raises.
    """
    argv = _argv(launcher, worktree, run)
    checked = _checked(
        worktree, run, lambda: review_check(launcher, worktree, run.kind, run.name, argv)
    )
    if checked.facts is None:
        raise RunRefusedError(checked.error)
    return {
        "command": shell_command(worktree, run.kind, run.name, argv),
        "lines": review_lines(checked.facts, occupants(), checked.uncommitted),
        "refusal": _refusal(checked.facts),
    }


def submit(
    launcher: Launcher, booley_dir: Path, worktree: Path, run: RunRequest
) -> dict[str, Any]:
    """Start the run as a Standalone Job and name its row as the next snapshot lists it.

    ``RunRefusedError`` for a parser rejection or an Agent CLI that cannot sign in, which
    start nothing; ``CheckFailedError`` when the check itself raises; ``OSError`` when the Job
    record cannot be written.
    """
    argv = _argv(launcher, worktree, run)
    checked = _checked(worktree, run, lambda: launcher.check(worktree, run.kind, run.name, argv))
    if checked.facts is None:
        raise RunRefusedError(checked.error or NO_FACTS)
    refusal = _refusal(checked.facts)
    if refusal:
        raise RunRefusedError(refusal)
    run_id = launcher.start(worktree, run.kind, run.name, argv)
    handles, _diagnostics = HandleStore(booley_dir, jobs=True).snapshot()
    return {"run_id": run_id, "handle": job_label(handles.get(run_id, ""), run.name)}
