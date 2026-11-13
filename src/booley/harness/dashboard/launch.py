"""The process boundary of the Dashboard's Run wizard: list, form, check, quote and submit a Flow or Specialist.

No Textual here. The wizard calls a :class:`Launcher`; the argument check runs
the shell's own parse of a built-in Flow or Specialist in a child process (``main`` below), so
nothing it prints or sets reaches the Dashboard, and relative paths resolve in
the chosen worktree.
"""

from __future__ import annotations

import importlib
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

from booley.mcp.registry import McpToolInfo, discover_mcp_inventory, flow_blurb
from booley.runtime import detached_job

if TYPE_CHECKING:
    from booley.config.agent import AgentSettings
    from booley.flows.base import BuiltinFlow
    from booley.flows.endpoint_state import EndpointState
    from booley.harness.dashboard.run_form import RunForm
    from booley.harness.dashboard.specialist_form import ReviewerForm
    from booley.runtime.job_slots import AdmissionOutlook

# The child parses and builds a request only; a minute covers a cold import on a slow host.
CHECK_TIMEOUT_S = 60
_ERROR_MARKER = ": error: "
NO_FACTS = "argument check gave no run facts"
# Flows whose default per-work-unit timeout reaches this get a timeout line on the review screen.
_LONG_TIMEOUT_MS = 30 * 60_000
_Endpoint = TypeVar("_Endpoint", bound="EndpointState")


class ArgumentsError(ValueError):
    """The arguments text cannot be split into shell words."""


@dataclass(frozen=True)
class FlowChoice:
    """One built-in Flow the wizard offers, with the first sentence of its description."""

    name: str
    blurb: str


@dataclass(frozen=True)
class SpecialistChoice:
    """One enabled built-in Specialist; ``selectable`` is whether the wizard has a form for it."""

    name: str
    blurb: str
    selectable: bool


@dataclass(frozen=True)
class SpecialistListing:
    """The Specialists the choice step lists, or ``problem``: why the worktree's settings cannot be read."""

    choices: tuple[SpecialistChoice, ...]
    problem: str = ""


def _builtin_infos(kind: str) -> list[McpToolInfo]:
    """Built-in endpoints of *kind* only: no Project tools directory is passed to discovery."""
    return sorted(
        (info for info in discover_mcp_inventory() if info.kind == kind),
        key=lambda info: info.name,
    )


def builtin_flows() -> tuple[FlowChoice, ...]:
    """Every built-in Flow by name, as ``booley flow`` runs them whatever ``[flows]`` enables."""
    return tuple(
        FlowChoice(info.name, flow_blurb(info.description)) for info in _builtin_infos("flow")
    )


def builtin_specialists(worktree: Path) -> SpecialistListing:
    """The built-in Specialists *worktree*'s ``[specialists]`` leaves enabled.

    Any failure reading the settings (a bad ``booley.toml``, a Booley Directory the
    layout rules refuse) becomes the listing's ``problem``, as for the Target reads.
    """
    from booley.harness.dashboard.specialist_form import REVIEWER
    from booley.harness.dashboard.targets import first_line
    from booley.mcp.endpoint_config import endpoint_is_enabled, get_endpoint_config

    try:
        specialists, flows = get_endpoint_config(worktree)
    except Exception as exc:  # noqa: BLE001 - every failure is a reason shown in the choice step
        return SpecialistListing((), f"{type(exc).__name__}: {first_line(exc)}")
    return SpecialistListing(
        tuple(
            SpecialistChoice(info.name, flow_blurb(info.description), info.name == REVIEWER)
            for info in _builtin_infos("specialist")
            if endpoint_is_enabled(info.name, "specialist", specialists, flows)
        )
    )


def split_arguments(text: str) -> tuple[str, ...]:
    """Split the arguments text the way a shell would."""
    try:
        return tuple(shlex.split(text))
    except ValueError as exc:
        raise ArgumentsError(f"unbalanced quotes: {exc}") from exc


def shell_command(worktree: Path, kind: str, name: str, argv: tuple[str, ...]) -> str:
    """The shell command equal to the submitted Job: type it in *worktree*."""
    return f"cd {shlex.quote(str(worktree))} && {shlex.join(['booley', kind, name, *argv])}"


@dataclass(frozen=True)
class SpecialistFacts:
    """What the check child resolved for a Specialist run, for the review screen.

    ``model``, ``cli`` and ``policy`` are empty when the settings could not be read
    (``refusal`` then says why). ``timeout_ms`` is what each model call will use and
    ``asked_ms`` what the form asked for. ``refusal`` is a failed auth check or an error
    the check could not evaluate (empty: the run may launch); ``warning`` is the sign-in
    fix shown when ``auth = "auto"`` found no credential (empty: none).
    """

    model: str
    cli: str
    policy: str
    timeout_ms: int
    asked_ms: int
    refusal: str = ""
    warning: str = ""


@dataclass(frozen=True)
class RunFacts:
    """What the child's parse says about the run, for the review screen.

    ``job_class`` is the admission class (``None``: no slot); ``timeout_ms`` is
    set only for Flows with a long default budget, and ``timeout_problem`` says
    why the configured value cannot be resolved. ``specialist`` is set for a Specialist.
    """

    job_class: str | None
    dry_run: bool
    timeout_ms: int | None = None
    timeout_problem: str = ""
    specialist: SpecialistFacts | None = None

    @property
    def takes_slot(self) -> bool:
        """True when admission would queue this run: a classed Flow that is not a dry run."""
        return self.job_class is not None and not self.dry_run


@dataclass(frozen=True)
class CheckResult:
    """The argument check's answer: the shell's rejection (``error``) or the run's facts."""

    error: str = ""
    facts: RunFacts | None = None


def form(flow: str) -> RunForm:
    """The Run form of the built-in Flow *flow*, read from its human parser.

    Built in this process: the code is the tree the Job's worker runs, and building
    a parser prints nothing and sets nothing. Parsing a typed command stays in the
    child of :func:`check`.
    """
    from booley.flows.base import BuiltinFlow
    from booley.flows.builtin_cli import build_cli_parser
    from booley.flows.cli_selection import CliInvocation
    from booley.harness.dashboard.run_form import describe

    return describe(build_cli_parser(_load_endpoint(BuiltinFlow, flow), CliInvocation()))


def _error_text(stderr: str, returncode: int) -> str:
    """argparse's message without its usage block, else the last line the child printed."""
    lines = [line for line in stderr.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if _ERROR_MARKER in line:
            return "\n".join(lines[index:])
    return lines[-1] if lines else f"argument check failed (exit {returncode})"


def _parse_facts(stdout: str) -> RunFacts | None:
    """The facts line the child printed last, or ``None`` when there is none to read."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    try:
        payload: object = json.loads(lines[-1]) if lines else None
        if not isinstance(payload, dict):
            return None
        data = cast("dict[str, Any]", payload)
        timeout = cast("dict[str, Any]", data.get("timeout") or {})
        job_class = data["job_class"]
        timeout_ms = timeout.get("ms")
        timeout_problem = timeout.get("problem", "")
        if not (
            (job_class is None or isinstance(job_class, str))
            and isinstance(data["dry_run"], bool)
            and (timeout_ms is None or isinstance(timeout_ms, int))
            and isinstance(timeout_problem, str)
        ):
            return None
        specialist = _parse_specialist_facts(data.get("specialist"))
        if data.get("specialist") is not None and specialist is None:
            return None
        return RunFacts(job_class, data["dry_run"], timeout_ms, timeout_problem, specialist)
    except (ValueError, KeyError, AttributeError):
        return None


def _parse_specialist_facts(raw: object) -> SpecialistFacts | None:
    """The child's ``specialist`` object, or ``None`` when absent or malformed.

    Empty strings are normal values here (settings that could not be read), so only the type is checked.
    """
    if not isinstance(raw, dict):
        return None
    data = cast("dict[str, Any]", raw)
    texts = [data.get(key) for key in ("model", "cli", "policy", "refusal", "warning")]
    timeouts = [data.get("timeout_ms"), data.get("asked_ms")]
    if not (
        all(isinstance(text, str) for text in texts)
        and all(isinstance(ms, int) and not isinstance(ms, bool) for ms in timeouts)
    ):
        return None
    model, cli, policy, refusal, warning = cast("list[str]", texts)
    timeout_ms, asked_ms = cast("list[int]", timeouts)
    return SpecialistFacts(model, cli, policy, timeout_ms, asked_ms, refusal, warning)


def check(worktree: Path, kind: str, name: str, argv: tuple[str, ...]) -> CheckResult:
    """Why the shell would reject ``booley <kind> <name> *argv`` in *worktree*, else the run's facts."""
    # -P: the Project's own modules never shadow what the check imports, as in the shell's script.
    command = [sys.executable, "-B", "-P", "-m", __name__, kind, name, "--", *argv]
    # The worker's environment, so the check sees the code and settings the Job will run with.
    environment = {**detached_job.worker_environment(), "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        result = subprocess.run(
            command,
            cwd=worktree,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=CHECK_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(f"argument check timed out after {CHECK_TIMEOUT_S}s")
    except OSError as exc:
        return CheckResult(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
    if result.returncode != 0:
        return CheckResult(_error_text(result.stderr, result.returncode))
    facts = _parse_facts(result.stdout)
    if facts is None:
        return CheckResult(NO_FACTS)
    return CheckResult(facts=facts)


class Launcher:
    """What the Run wizard needs from the outside world; one per Dashboard."""

    def __init__(self, booley_dir: Path) -> None:
        self._booley_dir = booley_dir

    def flows(self) -> tuple[FlowChoice, ...]:
        return builtin_flows()

    def specialists(self, worktree: Path) -> SpecialistListing:
        return builtin_specialists(worktree)

    def form(self, kind: str, name: str, worktree: Path) -> RunForm | ReviewerForm:
        """The form of a Flow, or the reviewer's, its spec path prefilled from *worktree*'s Goal."""
        if kind == "flow":
            return form(name)
        from booley.harness.dashboard.specialist_form import ReviewerForm, spec_prefill

        return ReviewerForm(spec_prefill(self._booley_dir, worktree), agent_note(worktree, name))

    def check(self, worktree: Path, kind: str, name: str, argv: tuple[str, ...]) -> CheckResult:
        return check(worktree, kind, name, argv)

    def outlook(self, job_class: str) -> AdmissionOutlook:
        """What an interactive claim on *job_class* would meet; changes nothing. ``OSError`` if unreadable."""
        from booley.config.jobs import parse_caps
        from booley.runtime.job_slots import SlotStore, slots_dir
        from booley.runtime.shared_infra import load_job_budget_config

        root = slots_dir(self._booley_dir)
        assert root is not None  # a Booley Directory was given, so the store is resolvable
        caps = parse_caps(load_job_budget_config(booley_dir=self._booley_dir))
        return SlotStore(root, caps).outlook(job_class)

    def uncommitted(self, worktree: Path) -> int:
        """Uncommitted changes in *worktree*, counted as ``booley worktree list`` counts them.

        Raises ``WorktreeListError`` when Git fails and ``BooleyLayoutError`` for a retired layout.
        """
        from booley.core.booley_dir import own_booley_dir_repo_path
        from booley.harness.worktree_list import count_uncommitted

        return count_uncommitted(worktree, own_booley_dir_repo_path(worktree).as_posix())

    def submit(self, worktree: Path, kind: str, name: str, argv: tuple[str, ...]) -> str:
        """Start the Standalone Job; return its Jobs window row key. ``OSError`` if it cannot be recorded."""
        from booley.harness.standalone_job import standalone_jobs_dir
        from booley.harness.standalone_worker import StandaloneRequest, submit

        run_id = submit(self._booley_dir, StandaloneRequest(worktree, kind, name, argv))
        return json.dumps([str(standalone_jobs_dir(self._booley_dir)), run_id])


def _load_endpoint(base: type[_Endpoint], name: str) -> _Endpoint:
    """The built-in endpoint *name* of the kind *base* builds, imported from its own module."""
    info = next(info for info in _builtin_infos(base.endpoint_kind) if info.name == name)
    module = importlib.import_module(
        "booley." + Path(info.path).with_suffix("").as_posix().replace("/", ".")
    )
    endpoint_class = next(
        obj
        for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, base) and obj.name == name
    )
    return endpoint_class()


def _timeout_facts(flow: BuiltinFlow, request: Any) -> dict[str, Any]:
    """The timeout the run will resolve, for Flows with a long default; empty for the others."""
    from booley.core.boundary import BoundaryError
    from booley.flows.invocation import default_timeout_ms, resolve_timeout_ms

    if default_timeout_ms(flow.name) < _LONG_TIMEOUT_MS:
        return {}
    try:
        return {"ms": resolve_timeout_ms(flow.name, request.work_dir, request.timeout_ms)}
    except BoundaryError as exc:
        return {"problem": str(exc)}


def main(argv: list[str]) -> int:
    """The child of :func:`check`: ``<kind> <name> -- <arguments>``; 0 when the shell would accept them.

    Parses like ``booley flow`` or ``booley specialist`` (one leading ``--`` is dropped) but
    never applies the environment or runs the endpoint. Argparse's exit code 2 stands for a
    rejection; on success the last stdout line is the run's facts as JSON (see :class:`RunFacts`).
    """
    from booley.flows.base import BuiltinFlow
    from booley.flows.builtin_cli import build_cli_parser, prepare_request
    from booley.flows.cli_selection import CliInvocation
    from booley.harness.endpoint_help import tail_project_selections
    from booley.runtime.job_slots import CLASS_MEMORY

    kind, name, _separator, *typed = argv
    if typed[:1] == ["--"]:
        typed = typed[1:]
    if kind == "specialist":
        return _check_specialist(name, typed)
    flow = _load_endpoint(BuiltinFlow, name)
    parser = build_cli_parser(flow, CliInvocation())
    # The worktree chooses the Project: a selector would send the Job elsewhere.
    if tail_project_selections(parser, typed):
        parser.error("the worktree chooses the Project; remove -C/--project/--work-dir")
    try:
        request, _quiet = prepare_request(flow, typed, CliInvocation())
    except SystemExit as exit_request:
        if exit_request.code == 0:  # argparse printed help and stopped
            parser.error("--help shows help; it is not a run")
        raise
    facts = {
        "job_class": CLASS_MEMORY if flow._memory_admitted() else flow._resolve_job_class(),
        "dry_run": bool(request.dry_run and flow.non_persisting_dry_run),
        "timeout": _timeout_facts(flow, request),
    }
    print(json.dumps(facts))
    return 0


def run_choice(settings: AgentSettings, name: str) -> tuple[str, str]:
    """The (model, Agent CLI) a launch of Specialist *name* runs on under *settings*.

    A string pin or none: the resolved model, as ``call_with_fallback`` calls it. A hidden
    ``[models]`` fallback list: its first entry the launch's no-model-call skip rule accepts.
    ``ValueError`` (with every entry's reason) when no entry is usable.
    """
    from booley.runtime.model_fallback import entry_skip_reason

    candidates = settings.model_candidates(name)
    if candidates is None:
        return settings.model_for_specialist(name), settings.cli_for_specialist(name)
    reasons: list[str] = []
    for model in candidates:
        reason = entry_skip_reason(settings, model, remembered=False)
        if reason is None:
            return model, settings.cli_for_model_id(model)
        reasons.append(f"{model}: {reason}")
    raise ValueError(f"no usable [models] {name} entry: {'; '.join(reasons)}")


def agent_note(worktree: Path, name: str) -> str:
    """The form's note: the model and Agent CLI *name* runs on in *worktree*, or why that cannot be read.

    Read in this process from the worktree's settings. It is a preview: the review screen's
    lines come from the check child, which has the worker's environment.
    """
    from booley.config.agent import worktree_agent_settings
    from booley.harness.dashboard.review import model_line
    from booley.harness.dashboard.targets import first_line

    try:
        return model_line(*run_choice(worktree_agent_settings(worktree), name))
    except (OSError, ValueError, RuntimeError) as exc:
        return f"Cannot read the agent settings: {type(exc).__name__}: {first_line(exc)}"


def _agent_facts(name: str, asked_ms: int, timeout_ms: int) -> dict[str, Any]:
    """The model, Agent CLI, policy and auth verdict of Specialist *name* in the current directory.

    Resolves what the worker resolves before the run (the child is a throwaway process, so
    installing the settings is safe). The model and CLI are filled before the health check,
    so a credential error still shows them.
    """
    from booley.config.agent import set_agent_settings, worktree_agent_settings
    from booley.harness.dashboard.targets import first_line
    from booley.runtime import auth_token
    from booley.runtime.agent_config import get_backend_config

    facts: dict[str, Any] = {
        "model": "",
        "cli": "",
        "policy": "",
        "timeout_ms": timeout_ms,
        "asked_ms": asked_ms,
        "refusal": "",
        "warning": "",
    }
    try:
        settings = worktree_agent_settings(Path.cwd())
        set_agent_settings(settings)
        model, cli = run_choice(settings, name)
        policy = settings.auth_for(cli)
        facts.update(model=model, cli=cli, policy=policy)
        problem = get_backend_config().backend_for(cli).health_check()
        if problem:
            facts["refusal"] = problem
        elif policy == "auto" and auth_token.credential_plan(cli, policy).selected is None:
            facts["warning"] = auth_token.missing_credential_fix(cli, policy)
    except (OSError, ValueError, RuntimeError) as exc:
        facts["refusal"] = (
            f"cannot check the agent settings: {type(exc).__name__}: {first_line(exc)}"
        )
    return facts


def _check_specialist(name: str, typed: list[str]) -> int:
    """Parse *typed* as ``booley specialist <name>`` would and print how the Job is admitted.

    The wizard writes these words from a form that never offers a Project selector, help
    or ``--dry-run``, so the parse is the whole argument check; the Agent CLI's auth check
    and the timeout the run will use follow it (see :class:`SpecialistFacts`).
    """
    from booley.flows.endpoint_cli import human_endpoint_parser
    from booley.specialists.specialist import Specialist

    specialist = _load_endpoint(Specialist, name)
    asked_ms: int = human_endpoint_parser(specialist).parse_args(typed).timeout_ms
    facts = {
        "job_class": specialist._resolve_job_class(),
        "dry_run": False,
        "timeout": {},
        "specialist": _agent_facts(name, asked_ms, specialist.model_call_timeout_ms(asked_ms)),
    }
    print(json.dumps(facts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
