"""Every retired-key refusal tells the user how to fix it (#4251).

A removed key's refusal says to delete or remove it; a moved key's refusal names the
replacement. ``CASES`` triggers each refusal the audit found and applies that rule to the
text a user sees. A source scan then fails when a refusal template under ``src`` is not
reached by a case, so a retired key added without coverage fails here.

``BASELINE`` lists cases that still break the rule (fixing one without removing its entry
fails); ``NO_REMEDY`` lists refusals no text could fix; ``NOT_A_KEY`` lists phrase hits
that are not configuration-key refusals. Each carries a one-line reason.
"""

from __future__ import annotations

import argparse
import ast
import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from tests.source_checkout_support import source_checkout_files

from booley.audit import agent_schema, flow_schema, project_schema
from booley.audit.config_common import ConfigTableAudit
from booley.config import agent as agent_config
from booley.config.eda import parse_eda_config
from booley.config.flow_enablement import flow_enabled_from_config
from booley.config.host_config import (
    load_host_policy,
    plan_host_egress,
    retired_project_policy_message,
)
from booley.config.jobs import RETIRED_JOBS_KEYS
from booley.config.retired import refuse_retired_config
from booley.core import booley_layout
from booley.flows.sim.config import parse_run_cwd_template
from booley.flows.synth.backends.yosys import core as yosys_core
from booley.flows.synth.ppa_config import append_ppa_args
from booley.flows.synth.recipe import resolve_synth_mode
from booley.flows.synth.timing import SynthMode
from booley.fusesoc.fusesoc_registry import core_schema_errors
from booley.harness import doctor
from booley.mcp.endpoint_config import parse_endpoint_config
from booley.mcp.endpoint_validation import validate_custom_endpoints
from booley.mcp.live_catalog import custom_directory
from booley.runtime import booley_dir as booley_dir_module
from booley.targets.flow_names import LEGACY_TO_CANONICAL, RETIRED_SPECIALIST_TO_FLOW

_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Env:
    """What a trigger may use: a private directory and an environment it may change."""

    tmp: Path
    mp: pytest.MonkeyPatch


@dataclass(frozen=True)
class Refusal:
    """One refusal text a user can see for a retired key.

    ``sites`` are the ``src/...py::<qualname>`` places whose string literals hold the text;
    ``key`` is a fragment of the retired key the text must contain; ``fix`` is ``None`` for a
    removed key, else the replacement's spelling a moved key's text must contain.
    """

    id: str
    sites: tuple[str, ...]
    key: str
    fix: str | None
    trigger: Callable[[Env], str]


# --- Triggers: each runs the real refusal and returns the text the user sees. ---


def _raised(call: Callable[[], object]) -> str:
    try:
        call()
    except SystemExit as exit_:
        return str(exit_.code)
    except Exception as error:  # noqa: BLE001 - the refusal's own type is part of what is shown
        return str(error)
    pytest.fail("the call did not refuse")


def _audit(result: ConfigTableAudit) -> str:
    return "\n".join(f"{item.message}\n{item.fix or ''}" for item in result.findings)


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _logged(call: Callable[[], object]) -> str:
    handler, root = _Records(), logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        call()
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    return "\n".join(handler.messages)


def _booley_dir(env: Env, text: str, name: str = "booley.toml") -> Path:
    directory = env.tmp / ".booley"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(text.encode("utf-8"))
    return directory


def _flow(raw: dict) -> Callable[[Env], str]:
    return lambda _env: _raised(lambda: flow_enabled_from_config("sim", raw))


def _flow_audit(flow: str, section: dict) -> Callable[[Env], str]:
    return lambda _env: _audit(flow_schema.audit_flow_table(flow, section))


def _flows_audit(flows: dict) -> Callable[[Env], str]:
    return lambda _env: _audit(flow_schema.audit_flow_tables({"flows": flows}, []))


def _models(raw: dict) -> Callable[[Env], str]:
    return lambda _env: _raised(
        lambda: agent_config.parse_models_section(raw, ("claude",), "claude")
    )


def _sandbox_warning(toml: str) -> Callable[[Env], str]:
    def trigger(env: Env) -> str:
        env.mp.setattr(agent_config, "_agent_settings", None)
        directory = _booley_dir(env, toml)
        return _logged(lambda: agent_config.load_agent_settings(env.tmp, booley_dir=directory))

    return trigger


def _refuse_config(toml: str) -> Callable[[Env], str]:
    def trigger(env: Env) -> str:
        _booley_dir(env, toml)
        env.mp.setenv("BOOLEY_DIR", str(env.tmp / ".booley"))
        booley_dir_module.reset_cache()
        return _raised(lambda: refuse_retired_config(env.tmp))

    return trigger


def _jobs_doctor(toml: str) -> Callable[[Env], str]:
    def trigger(env: Env) -> str:
        path = _booley_dir(env, toml) / "booley.toml"
        return _audit(project_schema.audit_retired_jobs_keys(path))

    return trigger


def _goals_doctor(env: Env) -> str:
    path = _booley_dir(env, "[goals]\n") / "booley.toml"
    return _audit(project_schema.audit_retired_goals_section(path))


def _host_file(toml: str) -> Callable[[Env], Path]:
    def write(env: Env) -> Path:
        path = env.tmp / "config.toml"
        path.write_bytes(toml.encode("utf-8"))
        return path

    return write


def _host_deprecation(toml: str) -> Callable[[Env], str]:
    def trigger(env: Env) -> str:
        messages: list[str] = []
        load_host_policy(_host_file(toml)(env), on_deprecation=messages.append)
        return "\n".join(messages)

    return trigger


def _host_project_policy(env: Env) -> str:
    message = retired_project_policy_message(
        {"interactive": {"max_sessions": 2}}, destination=env.tmp / "host.toml"
    )
    assert message is not None
    return message


def _host_egress(env: Env) -> str:
    path = _host_file("[interactive]\nmax_sessions = 2\n")(env)
    return _raised(lambda: plan_host_egress("unproxied", path))


def _legacy_pipeline_toml(env: Env) -> str:
    env.mp.setattr(agent_config, "_agent_settings", None)
    directory = _booley_dir(env, "", "pipeline.toml")
    return _logged(lambda: agent_config.load_agent_settings(env.tmp, booley_dir=directory))


def _endpoint_sandbox_attr(env: Env) -> str:
    mcp_tools = _booley_dir(env, "") / "mcp_tools"
    mcp_tools.mkdir()
    (mcp_tools / "custom.py").write_bytes(
        b"class Custom(McpTool):\n    name = 'custom_check'\n"
        b"    description = 'Check the design'\n"
        b"    sandbox = 'host'\n"
    )
    return _logged(lambda: validate_custom_endpoints(env.tmp))


def _mcp_tools_directory(env: Env) -> str:
    (env.tmp / "tools").mkdir()
    return _raised(lambda: custom_directory(env.tmp))


def _synth_timing_engine(_env: Env) -> str:
    return _raised(lambda: resolve_synth_mode({"timing_engine": "x"}, target="t"))


def _synth_expert_table(table: str) -> Callable[[Env], str]:
    return lambda _env: _raised(
        lambda: append_ppa_args(
            [], {table: {}}, argparse.Namespace(ppa_profile=None), synth_mode=SynthMode.PHYSICAL
        )
    )


def _synth_timing(timing: str) -> Callable[[Env], str]:
    def trigger(env: Env) -> str:
        _booley_dir(env, f"[flows.synth.timing]\n{timing}\n")
        return _raised(lambda: yosys_core.synth_timing_config(project_root=env.tmp))

    return trigger


def _core_doctor_elab(env: Env) -> str:
    core = env.tmp / "t.core"
    core.write_bytes(
        b"CAPI=2:\nname: x:y:z:1.0\ntargets:\n  default:\n"
        b"    flow_options:\n      booley:\n        doctor: [sim, elab]\n"
    )
    return "\n".join(core_schema_errors(core))


def _doctor_auth_auto(env: Env) -> str:
    env.mp.setattr("booley.harness.init_cmd.detected_auth_policy", lambda _cli: "api_key")
    audit = doctor.ProjectAudit(
        project_root=env.tmp,
        booley_dir=env.tmp / ".booley",
        booley_toml={"agent": {"clis": ["claude"], "default": "claude", "auth": "auto"}},
        configs_toml={},
        first_target="",
    )
    reporter = doctor._Reporter.create()
    doctor._check_auth_recorded(audit, ("claude",), reporter)
    assert reporter.findings is not None
    return "\n".join(f"{item.message}\n{item.fix or ''}" for item in reporter.findings)


def _env_project_dir(env: Env) -> str:
    env.mp.setenv("BOOLEY_PROJECT_DIR", "x")
    return _raised(booley_layout.validate_environment)


def _layout_retired_path(env: Env) -> str:
    return _raised(lambda: booley_layout.reject_retired_path(env.tmp / ".booley_project"))


def _layout_retired_dir(env: Env) -> str:
    (env.tmp / ".booley_project").mkdir()
    return _raised(lambda: booley_layout.require_current_layout(env.tmp))


# --- Cases ---

_FLOW_ENABLEMENT = "src/booley/config/flow_enablement.py"
_PROJECT_SCHEMA = "src/booley/audit/project_schema.py"
_FLOW_SCHEMA = "src/booley/audit/flow_schema.py"
_AGENT = "src/booley/config/agent.py"
_HOST = "src/booley/config/host_config.py"
_LAYOUT = "src/booley/core/booley_layout.py"
_YOSYS = "src/booley/flows/synth/backends/yosys/core.py"
_RECIPE = "src/booley/flows/synth/recipe_migrations.py"
_RETIRED_FLOW = f"{_FLOW_ENABLEMENT}::retired_config_error"
_RETIRED_FLOW_KEY = f"{_FLOW_ENABLEMENT}::_retired_flow_key_error"
_RETIRED_FLOW_KEYS = f"{_FLOW_SCHEMA}::_retired_flow_key_findings"
_RETIRED_FLOWS = f"{_FLOW_SCHEMA}::_retired_flow_findings"
_SANDBOX_PARSE = f"{_AGENT}::_parse_sandbox_config"
_RETIRED_MODELS = f"{_AGENT}::_retired_models_error"
_UNKNOWN_MODELS = f"{_AGENT}::unknown_models_key_message"
_RETIRED_OPTION = f"{_RECIPE}::retired_option_message"
_YOSYS_TIMING = f"{_YOSYS}::_load_and_validate_timing_config"
_RETIRED_CONFIG = "src/booley/config/retired.py::refuse_retired_config"

# A new member of one of these production mappings gets a case with no edit to the cases
# below, and a new member with no entry here fails with ``KeyError``.
_MODEL_KEY_FIX = {"heavy": None, "light": None, "standard": "reviewer =", "roles": "reviewer ="}
_TABLE_FIX = {
    "mcp_tools": "[specialists.*]",
    "notifications": None,
    "tools": "[flows.*]",
    "fusesoc": None,
    "sources": None,
}
_CONSTRAINT_FIX = "file_type: SDC"


def _mapping_cases() -> Iterator[Refusal]:
    for key in RETIRED_JOBS_KEYS:
        yield Refusal(
            f"jobs-{key}",
            ("src/booley/config/jobs.py::retired_jobs_keys_message", _RETIRED_CONFIG),
            key,
            None,
            _refuse_config(f"[jobs]\n{key} = 1\n"),
        )
    for key in sorted(agent_config.RETIRED_MODEL_KEYS):
        raw = {"reviewer": "standard"} if key == "roles" else "claude-opus-5-5"
        fix = _MODEL_KEY_FIX[key]
        # Only heavy and light are named in the text; the other keys share its first line.
        shown = "tier keys" if fix else key
        yield Refusal(f"models-{key}", (_RETIRED_MODELS,), shown, fix, _models({key: raw}))
    for name in RETIRED_SPECIALIST_TO_FLOW:
        yield Refusal(
            f"models-{name}", (_UNKNOWN_MODELS,), name, None, _models({name: "claude-opus-5-5"})
        )
    for old, new in LEGACY_TO_CANONICAL.items():
        yield Refusal(
            f"flows-legacy-{old}",
            (_RETIRED_FLOWS,),
            f"[flows.{old}]",
            f"[flows.{new}]",
            _flows_audit({old: {}}),
        )
    for table in project_schema.RETIRED_BOOLEY_TOML_TABLES:
        yield Refusal(
            f"table-{table}",
            (f"{_PROJECT_SCHEMA}::<module>", f"{_PROJECT_SCHEMA}::audit_known_tables"),
            f"[{table}]",
            _TABLE_FIX[table],
            lambda _env, table=table: _audit(project_schema.audit_known_tables({table: {}})),
        )
    for key in sorted(yosys_core._MIGRATED_CONSTRAINT_KEYS):
        yield Refusal(
            f"synth-constraint-{key}",
            (_YOSYS_TIMING,),
            repr(key),
            _CONSTRAINT_FIX,
            _synth_timing(f"{key} = 1"),
        )


def _flow_cases() -> Iterator[Refusal]:
    flow_keys: dict[str, tuple[str, dict, str | None]] = {
        "selftest": ("sim", {"selftest": {}}, None),
        "sandbox": ("sim", {"sandbox": "x"}, None),
        "keep-build-dir": ("sim", {"keep_build_dir": True}, None),
        "sim-output-dir": ("sim", {"output_dir": "x"}, None),
        "target": ("lint", {"target": "x"}, None),
        "standalone-frontend-doctor": ("sim", {"standalone_frontend": "auto"}, None),
    }
    for name, (flow, section, fix) in flow_keys.items():
        key = next(iter(section))
        site = _RETIRED_FLOW_KEYS
        if key == "standalone_frontend":
            site = f"{_FLOW_SCHEMA}::_flow_shape_findings"
        yield Refusal(f"flow-{name}", (site,), key, fix, _flow_audit(flow, section))


CASES: list[Refusal] = [
    Refusal(
        "flows-elab",
        (_RETIRED_FLOW,),
        "[flows.elab]",
        "--mode elab-only",
        _flow({"flows": {"elab": {}}}),
    ),
    Refusal(
        "flows-venue",
        (_RETIRED_FLOW, _RETIRED_FLOW_KEY),
        "venue",
        None,
        _flow({"flows": {"venue": "x"}}),
    ),
    Refusal(
        "flows-sim-backend",
        (_RETIRED_FLOW, _RETIRED_FLOW_KEY),
        "backend",
        None,
        _flow({"flows": {"sim": {"backend": "docker"}}}),
    ),
    Refusal(
        "flows-sim-backend-none",
        (_RETIRED_FLOW_KEY,),
        "backend",
        "enabled = false",
        _flow({"flows": {"sim": {"backend": "none"}}}),
    ),
    Refusal(
        "flows-sim-standalone-frontend",
        (_RETIRED_FLOW,),
        "standalone_frontend",
        None,
        _flow({"flows": {"sim": {"standalone_frontend": "auto"}}}),
    ),
    Refusal(
        "sandbox-passthrough-env",
        (_RETIRED_FLOW,),
        "passthrough_env",
        "License Profile",
        _flow({"sandbox": {"passthrough_env": []}}),
    ),
    Refusal(
        "audit-eda-retired",
        (f"{_PROJECT_SCHEMA}::audit_eda_config",),
        "venue",
        None,
        lambda _env: _audit(project_schema.audit_eda_config({"flows": {"venue": "x"}})),
    ),
    Refusal(
        "agent-auth-auto",
        (f"{_AGENT}::_retired_auth_error",),
        "auth",
        '"subscription"',
        lambda _env: _raised(lambda: agent_config.parse_agent_section({"auth": "auto"})),
    ),
    Refusal(
        "sandbox-mode-warning",
        (_SANDBOX_PARSE,),
        "[sandbox].mode",
        None,
        _sandbox_warning('[sandbox]\nmode = "docker"\n'),
    ),
    Refusal(
        "sandbox-memory-table",
        (_SANDBOX_PARSE,),
        "memory tier table",
        "memory =",
        _sandbox_warning('[sandbox]\nmemory = {default = "6g"}\n'),
    ),
    Refusal(
        "sandbox-memory-tiers",
        (_SANDBOX_PARSE,),
        "memory_tiers",
        "memory =",
        _sandbox_warning("[sandbox]\nmemory_tiers = {}\n"),
    ),
    Refusal(
        "eda-installation",
        ("src/booley/config/eda.py::_parse_kind",),
        "installation",
        "booley eda grant add --installation",
        lambda _env: _raised(lambda: parse_eda_config({"vivado": {"installation": "x"}})),
    ),
    Refusal(
        "jobs-doctor",
        (f"{_PROJECT_SCHEMA}::audit_retired_jobs_keys",),
        "max_heavy",
        None,
        _jobs_doctor("[jobs]\nmax_heavy = 1\n"),
    ),
    Refusal("goals-section", (_RETIRED_CONFIG,), "[goals]", None, _refuse_config("[goals]\n")),
    Refusal(
        "goals-doctor",
        (
            f"{_PROJECT_SCHEMA}::audit_retired_goals_section",
            "src/booley/config/retired.py::goals_section_fix",
        ),
        "[goals]",
        None,
        _goals_doctor,
    ),
    Refusal(
        "host-interactive",
        (f"{_HOST}::_legacy_host_policy_message",),
        "[interactive]",
        "[sandbox]",
        _host_deprecation("[interactive]\nmax_sessions = 2\n"),
    ),
    Refusal(
        "host-interactive-with-sandbox",
        (f"{_HOST}::_legacy_host_policy_message",),
        "[interactive]",
        None,
        _host_deprecation("[interactive]\nmax_sessions = 2\n[sandbox]\nmax_sessions = 3\n"),
    ),
    Refusal(
        "project-host-policy",
        (f"{_HOST}::retired_project_policy_message", f"{_HOST}::<module>"),
        "[interactive]",
        "host.toml",
        _host_project_policy,
    ),
    Refusal(
        "host-egress-interactive",
        (f"{_HOST}::plan_host_egress", f"{_HOST}::<module>"),
        "[interactive]",
        "[sandbox]",
        _host_egress,
    ),
    Refusal(
        "legacy-pipeline-toml",
        (f"{_AGENT}::_resolve_booley_toml",),
        "pipeline.toml",
        "booley.toml",
        _legacy_pipeline_toml,
    ),
    Refusal(
        "specialist-to-flow",
        (f"{_PROJECT_SCHEMA}::audit_specialist_table",),
        "mutation_tester",
        "[flows.mutation]",
        lambda _env: _audit(
            project_schema.audit_specialist_table(
                {"specialists": {"mutation_tester": {"enabled": False}}}, {"reviewer"}
            )
        ),
    ),
    Refusal(
        "feedback-mode",
        (f"{_PROJECT_SCHEMA}::_feedback_field_findings",),
        "[feedback].mode",
        None,
        lambda _env: _audit(project_schema.audit_feedback_table({"feedback": {"mode": "off"}})),
    ),
    Refusal(
        "sandbox-mode-doctor",
        (f"{_PROJECT_SCHEMA}::audit_sandbox_table",),
        "[sandbox].mode",
        None,
        lambda _env: _audit(project_schema.audit_sandbox_table({"sandbox": {"mode": "docker"}})),
    ),
    Refusal(
        "interactive-app",
        (f"{_PROJECT_SCHEMA}::audit_interactive_table",),
        "[interactive].app",
        None,
        lambda _env: _audit(
            project_schema.audit_interactive_table({"interactive": {"app": "claude"}})
        ),
    ),
    Refusal(
        "ignored-developer",
        (f"{_PROJECT_SCHEMA}::audit_ignored_settings",),
        "[developer]",
        None,
        lambda _env: _audit(project_schema.audit_ignored_settings({"developer": {}})),
    ),
    Refusal(
        "ignored-jobs-max-tickets",
        (f"{_PROJECT_SCHEMA}::audit_ignored_settings",),
        "max_tickets",
        None,
        lambda _env: _audit(project_schema.audit_ignored_settings({"jobs": {"max_tickets": 1}})),
    ),
    Refusal(
        "agent-primary",
        ("src/booley/audit/agent_schema.py::audit_agent_table",),
        "primary",
        "[agent].clis",
        lambda _env: _audit(agent_schema.audit_agent_table({"agent": {"primary": "claude"}})),
    ),
    Refusal(
        "flows-tools",
        (f"{_FLOW_SCHEMA}::audit_flow_tables",),
        "[tools]",
        "[flows.*]",
        lambda _env: _audit(flow_schema.audit_flow_tables({"tools": {}}, [])),
    ),
    Refusal("flows-builtin", (_RETIRED_FLOWS,), "builtin", None, _flows_audit({"builtin": []})),
    Refusal(
        "flows-elab-doctor",
        (_RETIRED_FLOWS,),
        "[flows.elab]",
        "--mode elab-only",
        _flows_audit({"elab": {}}),
    ),
    Refusal(
        "synth-timing-engine",
        (_RETIRED_OPTION,),
        "timing_engine",
        "flow_options.synth_mode",
        _synth_timing_engine,
    ),
    Refusal(
        "synth-yosys",
        (_RETIRED_OPTION,),
        "flow_options.yosys",
        "flow_options.advanced_settings_yosys",
        _synth_expert_table("yosys"),
    ),
    Refusal(
        "synth-openroad",
        (_RETIRED_OPTION,),
        "flow_options.openroad",
        "flow_options.advanced_settings_openroad",
        _synth_expert_table("openroad"),
    ),
    Refusal(
        "synth-timing-engine-table",
        (_YOSYS_TIMING,),
        "'engine'",
        "flow_options.synth_mode",
        _synth_timing('engine = "opensta"'),
    ),
    Refusal(
        "core-doctor-elab",
        ("src/booley/fusesoc/fusesoc_registry.py::_check_booley_target_metadata",),
        "elab",
        "doctor: [sim]",
        _core_doctor_elab,
    ),
    Refusal(
        "doctor-auth-auto",
        ("src/booley/harness/doctor.py::_check_auth_recorded",),
        "'auto'",
        'auth = "api_key"',
        _doctor_auth_auto,
    ),
    Refusal(
        "env-booley-project-dir",
        (f"{_LAYOUT}::validate_environment",),
        "BOOLEY_PROJECT_DIR",
        "BOOLEY_DIR",
        _env_project_dir,
    ),
    Refusal(
        "layout-retired-path",
        (f"{_LAYOUT}::reject_retired_path",),
        ".booley_project",
        ".booley",
        _layout_retired_path,
    ),
    Refusal(
        "layout-retired-dir",
        (f"{_LAYOUT}::require_current_layout",),
        ".booley_project",
        ".booley",
        _layout_retired_dir,
    ),
    Refusal(
        "endpoint-tools",
        ("src/booley/mcp/endpoint_config.py::parse_endpoint_config",),
        "[mcp_tools]",
        "[flows.*]",
        lambda _env: _raised(lambda: parse_endpoint_config({"mcp_tools": {}})),
    ),
    Refusal(
        "endpoint-sandbox-attr",
        ("src/booley/mcp/endpoint_validation.py::_warn_retired_sandbox_attr",),
        "sandbox",
        None,
        _endpoint_sandbox_attr,
    ),
    Refusal(
        "sim-run-cwd-campaign",
        ("src/booley/flows/sim/config.py::parse_run_cwd_template",),
        "{campaign}",
        "{record}",
        lambda _env: _raised(lambda: parse_run_cwd_template("{campaign}")),
    ),
    Refusal(
        "mcp-tools-directory",
        ("src/booley/mcp/live_catalog.py::custom_directory",),
        "tools",
        "mcp_tools",
        _mcp_tools_directory,
    ),
    *_flow_cases(),
    *_mapping_cases(),
]

#: Case ids whose refusal still breaks the rule. Fixing one fails until its entry goes.
BASELINE: frozenset[str] = frozenset()

#: Case ids whose refusal no text could remedy, each with a one-line reason.
NO_REMEDY: dict[str, str] = {}

#: Sites whose phrase hit is not a configuration-key refusal, each with a one-line reason.
#: ``site#fragment`` exempts only the message containing the fragment; a bare site, all of it.
NOT_A_KEY: dict[str, str] = {
    "src/booley/config/agent.py::decode_cli_policies": (
        "a policy Booley itself recorded in an older issued Sandbox spec, not user configuration"
    ),
    "src/booley/config/agent.py::parse_agent_section#old spelling of default": (
        "provider is still accepted as the old spelling of default; this refuses two "
        "conflicting values, not a retired key"
    ),
    "src/booley/eda/cli.py::run": "a deprecated CLI command, not configuration",
    "src/booley/flows/cli_selection.py::warn_alias": "a deprecated CLI option alias",
    "src/booley/flows/sim/cli.py::SimArguments.normalize": "deprecated CLI flags",
    "src/booley/flows/synth/warnings.py::<module>": "a pattern for OpenROAD's own warning text",
    "src/booley/harness/auth_cmd.py::_clear": "reports a removed credential",
    "src/booley/harness/auth_cmd.py::_host_policy": (
        "an environment hand-off outside a Sandbox, not a retired key"
    ),
    "src/booley/harness/booley.py::_RetiredEdaToolOptionAction.__call__": (
        "a retired CLI option, not configuration"
    ),
    "src/booley/harness/doctor.py::_check_goal_leftovers": (
        "leftover Goal record files, not configuration"
    ),
    "src/booley/harness/host_sidecars.py::_remove_egress": "reports a removed Docker resource",
    "src/booley/harness/init_cmd.py::_sandbox_precondition_failed": (
        "a repository precondition, not a retired key"
    ),
    "src/booley/harness/setup/scaffold.py::_cocotb_testbench": (
        "scaffold text about a renamed cocotb file, not configuration"
    ),
    "src/booley/harness/worktree_rm.py::_move_evidence": "a failure report",
}


# --- The rule ---

_REMOVE = re.compile(r"\b(?:delete|remove)\b", re.IGNORECASE)


def _names_the_replacement(text: str, fix: str) -> bool:
    """Whether *text* has *fix* as a whole token (``.booley`` is not in ``.booley_project``)."""
    before = r"(?<!\w)" if re.match(r"\w", fix) else ""
    after = r"(?!\w)" if re.search(r"\w$", fix) else ""
    return re.search(before + re.escape(fix) + after, text) is not None


def _lacks_the_fix(case: Refusal, text: str) -> bool:
    if case.fix is None:
        return _REMOVE.search(text) is None
    return not _names_the_replacement(text, case.fix)


class _Texts:
    """Runs each case's trigger once and keeps the text it returned."""

    def __init__(self, tmp_path_factory: pytest.TempPathFactory) -> None:
        self._factory = tmp_path_factory
        self._texts: dict[str, str] = {}

    def of(self, case: Refusal) -> str:
        if case.id not in self._texts:
            with pytest.MonkeyPatch.context() as mp:
                try:
                    self._texts[case.id] = case.trigger(Env(self._factory.mktemp(case.id), mp))
                finally:
                    booley_dir_module.reset_cache()
        return self._texts[case.id]


@pytest.fixture(scope="module")
def texts(tmp_path_factory: pytest.TempPathFactory) -> _Texts:
    return _Texts(tmp_path_factory)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_retired_key_refusal_names_the_fix(case: Refusal, texts: _Texts) -> None:
    text = texts.of(case)
    assert case.key in text, f"{case.id} reached another refusal:\n{text}"
    if case.id in NO_REMEDY:
        return
    lacks = _lacks_the_fix(case, text)
    if case.id in BASELINE:
        assert lacks, f"fixed: remove {case.id} from BASELINE"
        return
    wanted = "say to delete or remove it" if case.fix is None else f"name {case.fix!r}"
    assert not lacks, f"{case.id}: the refusal must {wanted}:\n{text}"


def test_exception_lists_have_one_line_reasons() -> None:
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids)), "two cases share an id"
    assert set(ids) >= BASELINE, f"BASELINE names no case: {sorted(BASELINE - set(ids))}"
    assert set(NO_REMEDY) <= set(ids), (
        f"NO_REMEDY names no case: {sorted(set(NO_REMEDY) - set(ids))}"
    )
    assert not BASELINE & set(NO_REMEDY), "a case is in both BASELINE and NO_REMEDY"
    for name, reason in {**NO_REMEDY, **NOT_A_KEY}.items():
        assert reason.strip() and "\n" not in reason, f"{name} needs a one-line reason"


# --- Coverage: every refusal template under src is reached by a case ---

_VOCABULARY = re.compile(
    r"\b(?:is|are|was|were) (?:retired|removed|deprecated)\b"
    r"|\bretired (?:key|auth|Flow value|\[)|\bno longer (?:used|read|a Specialist)\b"
    r"|\bneeds migration\b|\blegacy (?:\[|key\b|pipeline)|\bold spelling\b"
    r"|\bis ignored\b|^retired\b|\bremoved in\b|\bwas renamed\b"
)
_PLACEHOLDER = re.compile(r"%[sr]")


@dataclass(frozen=True)
class Template:
    site: str
    line: int
    pattern: re.Pattern[str]
    literal: str


def _template(node: ast.expr) -> tuple[re.Pattern[str], str] | None:
    """A regex for the text *node* can produce, or ``None`` when it is not retirement text."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        parts: list[str | None] = [node.value]
    elif isinstance(node, ast.JoinedStr):
        parts = [
            value.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
            else None
            for value in node.values
        ]
    else:
        return None
    if not any(part is not None and _VOCABULARY.search(part) for part in parts):
        return None
    pieces: list[str] = []
    for part in parts:
        if part is None:
            pieces.append(".*?")
        else:
            pieces.append(".*?".join(re.escape(piece) for piece in _PLACEHOLDER.split(part)))
    return re.compile("".join(pieces), re.DOTALL), "".join(part or "" for part in parts)


def _docstring_ids(tree: ast.Module) -> set[int]:
    owners = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, owners)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }


def _templates_in(relative: str, tree: ast.Module) -> Iterator[Template]:
    docstrings = _docstring_ids(tree)

    def walk(node: ast.AST, scope: str) -> Iterator[Template]:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = f"{scope}.{child.name}" if scope else child.name
            if isinstance(child, ast.expr) and id(child) not in docstrings:
                found = _template(child)
                if found is not None:
                    site = f"{relative}::{scope or '<module>'}"
                    yield Template(site, child.lineno, *found)
                    continue  # the parts of a template are not templates themselves
            yield from walk(child, inner)

    yield from walk(tree, "")


def _source_templates() -> list[Template]:
    found: list[Template] = []
    for path in source_checkout_files(_ROOT, "src"):
        if path.suffix == ".py":
            relative = path.relative_to(_ROOT).as_posix()
            tree = ast.parse(path.read_bytes().decode("utf-8"), filename=relative)
            found.extend(_templates_in(relative, tree))
    return found


def test_every_retirement_refusal_has_a_case(texts: _Texts) -> None:
    templates = _source_templates()
    scanned = {template.site for template in templates}
    declared = {site for case in CASES for site in case.sites}
    problems = [
        f"declared site not found by the scan: {site}" for site in sorted(declared - scanned)
    ]
    exempt = [key.partition("#")[::2] for key in NOT_A_KEY]
    problems += [
        f"NOT_A_KEY site not found by the scan: {site}"
        for site in sorted({site for site, _ in exempt} - scanned)
    ]
    problems += [
        f"NOT_A_KEY site also has a case: {site}"
        for site in sorted({site for site, _ in exempt} & declared)
    ]
    for template in templates:
        if any(
            site == template.site and fragment in template.literal for site, fragment in exempt
        ):
            continue
        reached = any(
            template.pattern.search(texts.of(case))
            for case in CASES
            if template.site in case.sites
        )
        if not reached:
            problems.append(
                f"{template.site} (line {template.line}) refuses with text no case triggers: "
                f"add a Refusal to CASES, or list the site in NOT_A_KEY with a reason"
            )
    assert not problems, "\n".join(problems)
