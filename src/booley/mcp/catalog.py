"""Static MCP schemas and pure exposure policy, independent of server transport."""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_DEFAULT_JOB_POLL_WAIT_SECONDS = 240.0
_STATUS_MCP_TOOL_NAME = "booley_status"
_COVERAGE_EVIDENCE_TOOL_NAME = "coverage_evidence"
_REPORT_MCP_TOOL_NAME = "booley_report"
POLL_MCP_TOOL_NAME = "booley_poll"

_POLL_MCP_TOOL_DESCRIPTION = (
    "Wait for long-running endpoints (sim / fpga / synth / reviewer / mutation_tester / "
    "coverage_analyst) that were started in the background. When such an endpoint takes "
    "longer than the inline wait, its call returns a 'run_id' instead of the result. With "
    "several Jobs running, call with no run_id: it returns the full result of the first of "
    "your Jobs to finish (or one that finished but whose result you have not read), lists "
    "your other Jobs, and answers at once when there is nothing to wait for. Pass a run_id "
    "only to wait for one specific Job. Pass 'wait_seconds' (0-270) to long-poll: the call "
    "blocks up to that long, turning a poll-loop into one blocking call; if nothing has "
    "finished afterwards it returns 'RUNNING' — call again to keep waiting. Claude and "
    "Codex: omit 'wait_seconds' to use the Sandbox's configured poll window. Codex: if its "
    "programmatic exec yields while this MCP call is still running, keep waiting on the same "
    "running cell in short slices; do not start another booley_poll call. A finished Job "
    "returns the full result (EXIT_CODE + report), exactly as the original call would have. "
    "Safe to call across a server restart: Jobs are tracked on disk, so a poll always "
    "returns a definite answer."
)
_CANCEL_MCP_TOOL_NAME = "booley_cancel"
_TARGETS_MCP_TOOL_NAME = "booley_targets"
_SLEEP_MCP_TOOL_NAME = "booley_sleep"


_BWAVE_MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "bwave",
        "description": (
            "RTL debug helper for simulation traces: investigate with it, not just confirm "
            "fixes. If the sim report does not localize a failure, or a reproducer "
            "unexpectedly passes, query a current trace (or rerun `sim` with `trace: true`) "
            "before acting on an RTL hypothesis. Walk the wrong value backwards cycle by "
            "cycle through its data, valid, select, and handshake drivers to the first "
            "divergent signal; for a passing reproducer, verify the suspected trigger "
            "actually occurred. Confirm the mechanism in the trace before editing RTL. "
            "Prefer B-Wave over $display prints; use prints only if B-Wave cannot capture "
            "the evidence, and say why. Diagnose compile errors from compiler output. "
            "Queries need no GUI; for humans, open a scoped, grouped view and relay "
            "omitted-signal "
            'warnings. First call extra_args=["skill"], then '
            'extra_args=["--help"] for syntax.'
        ),
        "schema": {
            "type": "object",
            "properties": {
                "extra_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Arguments after `bwave`, as documented by CLI help.",
                },
            },
            "required": ["extra_args"],
        },
    },
]


def _find_mcp_tool_class_in_module(
    mod: object,
    source_label: str,
) -> tuple[type, object, dict] | None:
    """Extract the first endpoint class, instance and schema, logging extraction failures."""
    from booley.flows.base import BooleyFlow, BuiltinFlow
    from booley.mcp.base import McpTool
    from booley.mcp.flow_adapter import flow_schema

    for attr_name in dir(mod):
        obj = getattr(mod, attr_name)
        if not (
            isinstance(obj, type)
            and issubclass(obj, (McpTool, BooleyFlow, BuiltinFlow))
            and obj is not McpTool
            and hasattr(obj, "name")
            and obj.name
        ):
            continue
        try:
            instance = obj()
            schema = flow_schema(instance)
        except Exception as exc:
            msg = f"SCHEMA EXTRACTION FAILED: {obj.name} in {source_label}: {exc}"
            logger.error(msg, exc_info=True)
            print(f"[mcp] ERROR {msg}", file=sys.stderr, flush=True)
            continue
        return obj, instance, schema
    return None


def _mcp_tool_def_from_class(
    cls: type,
    schema: dict,
    module_name: str,
    **extra: Any,
) -> dict[str, Any]:
    """Build an MCP tool definition dict from an endpoint class and extracted schema."""
    from booley.flows.base import BuiltinFlow, FlowMechanics

    result = {
        "name": cls.name,
        "module": module_name,
        "description": getattr(cls, "description", "") or "",
        "schema": schema,
        "default_timeout": getattr(cls, "default_timeout", 0),
        "is_specialist": getattr(cls, "endpoint_kind", None) == "specialist",
        "is_flow": issubclass(cls, FlowMechanics),
        "is_builtin_flow": issubclass(cls, BuiltinFlow),
        "non_persisting_dry_run": bool(getattr(cls, "non_persisting_dry_run", False)),
    }
    result.update(extra)
    return result


def _import_module_safe(fqn: str) -> tuple[object | None, str | None]:
    """Import *fqn* and return (module, None) or (None, error_msg)."""
    import importlib
    import traceback

    try:
        return importlib.import_module(fqn), None
    except Exception as exc:
        msg = f"IMPORT FAILED: {fqn}: {exc}"
        logger.error(msg, exc_info=True)
        print(f"[mcp] ERROR {msg}", file=sys.stderr, flush=True)
        tb = traceback.format_exc()
        print(f"[mcp] TRACEBACK:\n{tb}", file=sys.stderr, flush=True)
        return None, msg


def _import_and_extract(
    module_name: str,
    *,
    import_prefix: str = "booley.mcp",
) -> tuple[dict[str, Any] | None, str | None]:
    """Return an endpoint definition or its import/extraction error."""
    fqn = f"{import_prefix}.{module_name}"
    mod, err = _import_module_safe(fqn)
    if err is not None:
        return None, err

    found = _find_mcp_tool_class_in_module(mod, module_name)
    if found is None:
        msg = f"NO MCP ENDPOINT CLASS FOUND: {fqn} has no recognized MCP endpoint subclass"
        logger.warning(msg)
        return None, msg

    cls, _instance, schema = found
    is_custom = not import_prefix.startswith("booley.")
    return (
        _mcp_tool_def_from_class(
            cls,
            schema,
            module_name,
            is_custom=is_custom,
            module_path=f"{import_prefix}.{module_name}",
        ),
        None,
    )


def _status_mcp_tool_def() -> dict[str, Any]:
    """Return the synthetic Interactive Mode status MCP tool definition."""
    return {
        "name": _STATUS_MCP_TOOL_NAME,
        "description": "Show whether Booley Interactive Mode is ready in this tab.",
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
    }


def _report_mcp_tool_def() -> dict[str, Any]:
    """Return the synthetic report-fetch MCP tool definition."""
    return {
        "name": _REPORT_MCP_TOOL_NAME,
        "description": (
            "Fetch the most recent completed run report for a Booley endpoint (e.g. sim) from disk —"
            " including after a client-side timeout, when the inline MCP result was lost but the run"
            " finished server-side. Pass the optional 'endpoint' argument to target a specific "
            "endpoint's latest report; omit it for the most recent report across all endpoints."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "work_dir": {
                    "type": "string",
                    "description": "Goal worktree whose reports to fetch. Required while Goal Mode is active.",
                },
                "endpoint": {
                    "type": "string",
                    "description": (
                        "Endpoint name to fetch the latest report for (e.g. 'sim'). Omit for"
                        " the most recent report across all endpoints."
                    ),
                },
            },
            "additionalProperties": False,
        },
    }


def _poll_mcp_tool_def() -> dict[str, Any]:
    """Return the synthetic poll MCP tool definition."""
    return {
        "name": POLL_MCP_TOOL_NAME,
        "description": _POLL_MCP_TOOL_DESCRIPTION,
        "schema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": (
                        "Optional. The run_id returned by an endpoint that reported RUNNING, "
                        "to wait for that one Job. Omit to wait for the first of your Jobs "
                        "to finish."
                    ),
                },
                "wait_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 270,
                    "description": (
                        "Long-poll: block up to this many seconds for the run to finish "
                        "before answering (clamped to 0-270; longer would die at the HTTP "
                        "layer's ~300s cap). 0 answers with the current status immediately. "
                        "Omit to use the server default wait "
                        f"(BOOLEY_MCP_JOB_POLL_WAIT_SECONDS, "
                        f"{_DEFAULT_JOB_POLL_WAIT_SECONDS:g}s)."
                    ),
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    }


def _cancel_mcp_tool_def() -> dict[str, Any]:
    """Return the job cancellation tool, exposed wherever polling is available."""
    return {
        "name": _CANCEL_MCP_TOOL_NAME,
        "description": (
            "Cancel a QUEUED or RUNNING background job (pass the run_id a submit or poll reported). "
            "Running process groups receive SIGTERM, then SIGKILL after a bounded grace period. "
            "Polling the run_id afterwards reports the distinct CANCELLED terminal outcome."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "The run_id of the queued or running job to cancel.",
                },
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    }


def _targets_mcp_tool_def() -> dict[str, Any]:
    """Return the synthetic targets-listing MCP tool definition."""
    from booley.targets.target_surface import TARGET_AWARE_FLOWS

    return {
        "name": _TARGETS_MCP_TOOL_NAME,
        "description": (
            "List the project's FuseSoC .core Targets — the values a Booley Flow's 'target' argument"
            " accepts (ADR 0030). Cheap .core-YAML enumeration, no fusesoc run: per Target it "
            "reports the copy-pasteable selector (vlnv#name when the bare name is ambiguous), flow "
            "(sim/lint/generic), declared EDA tool, cocotb module, declared toplevel, which Doctor "
            "Flows the Target selects via flow_options.booley.doctor, and which Booley Flows could "
            "drive it. Optional filters: 'for_flow' (one of synth, fpga, sim, lint) keeps only "
            "Targets that Flow could drive; 'glob' matches the bare name or "
            "vendor:library:name#target (e.g. 'soc*', '*#lint'). Returns JSON."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "for_flow": {
                    "type": "string",
                    "enum": list(TARGET_AWARE_FLOWS),
                    "description": (
                        "Only Targets this Booley Flow could drive (compatibility, not wiring)."
                    ),
                },
                "glob": {
                    "type": "string",
                    "description": (
                        "Case-sensitive glob over the bare Target name or "
                        "vendor:library:name#target, e.g. 'soc*' or '*#lint'."
                    ),
                },
                "work_dir": {
                    "type": "string",
                    "description": (
                        "Optional worktree root to enumerate instead of the session "
                        "workspace (same validation as the Booley Flows' work_dir)."
                    ),
                },
            },
            "additionalProperties": False,
        },
    }


def _coverage_evidence_tool_def() -> dict[str, Any]:
    """Return the bound Coverage Analyst evidence-tool definition."""
    from booley.flows.sim.coverage_evidence import COVERAGE_POINT_REFERENCE_PATTERN

    return {
        "name": _COVERAGE_EVIDENCE_TOOL_NAME,
        "description": (
            "Query bounded, read-only evidence from the validated Coverage Campaign bound to this "
            "Coverage Analyst. Start with view='overview'; use view='zero_point_sources' to page "
            "through RTL sources with no native records, and view='points' for filtered exact "
            "Coverage Points and view='source' for verified excerpts associated with previously "
            "returned point_refs."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "view": {
                    "type": "string",
                    "enum": ["overview", "points", "source", "zero_point_sources"],
                },
                "metric": {"type": "string"},
                "source": {"type": "string"},
                "covered": {"type": "boolean"},
                "disposition": {
                    "type": "string",
                    "enum": ["eligible", "waived", "unscored"],
                },
                "point_refs": {
                    "type": "array",
                    "items": {"type": "string", "pattern": COVERAGE_POINT_REFERENCE_PATTERN},
                    "minItems": 1,
                    "maxItems": 10,
                },
                "cursor": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "context_lines": {"type": "integer", "minimum": 0, "maximum": 20},
            },
            "required": ["view"],
            "additionalProperties": False,
        },
    }


def _sleep_mcp_tool_def() -> dict[str, Any]:
    """Return the diagnostic sleep MCP tool definition."""
    return {
        "name": _SLEEP_MCP_TOOL_NAME,
        "description": (
            "Diagnostic MCP tool (exposed only when BOOLEY_MCP_DEBUG_TOOLS is set): hold this MCP "
            "tool call open for 'seconds' server-side, then return timing details. Exists to measure"
            " the MCP client's MCP-tool-call kill ceiling (ADR 0027) — it does no RTL work and is "
            "intended for timing diagnostics."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 86400,
                    "description": ("How long to hold the call open server-side."),
                },
            },
            "required": ["seconds"],
            "additionalProperties": False,
        },
    }


@dataclass(frozen=True)
class Exposure:
    """One immutable exposure value; only server wiring reads the environment."""

    interactive: bool = True
    nested: frozenset[str] | None = None
    explicit: frozenset[str] | None = None
    debug: bool = False
    campaign: bool = False
    project: bool = False

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> Exposure:
        from booley.targets.flow_names import canonical_set

        def names(key: str) -> frozenset[str]:
            return frozenset(
                canonical_set(t.strip() for t in values.get(key, "").split(",") if t.strip())
            )

        return cls(
            interactive=values.get("BOOLEY_MCP_MODE", "").strip().lower() == "interactive",
            nested=names("BOOLEY_NESTED_MCP_TOOLS")
            if values.get("BOOLEY_NESTED_AGENT") == "1"
            else None,
            explicit=names("BOOLEY_MCP_TOOLS") if "BOOLEY_MCP_TOOLS" in values else None,
            debug=values.get("BOOLEY_MCP_DEBUG_TOOLS", "").strip().lower()
            not in {"", "0", "false", "no"},
            campaign=bool(values.get("BOOLEY_COVERAGE_CAMPAIGN")),
            project=bool(values.get("BOOLEY_COVERAGE_PROJECT")),
        )


def tool_visible(
    name: str,
    exposure: Exposure,
) -> bool:
    from booley.targets.flow_names import canonical

    name = canonical(name)
    if exposure.nested is not None:
        return name in exposure.nested
    if exposure.explicit is not None:
        return name in exposure.explicit
    return True


def status_visible(exposure: Exposure) -> bool:
    return _interactive_utility_visible(_STATUS_MCP_TOOL_NAME, exposure)


def report_visible(exposure: Exposure) -> bool:
    return _interactive_utility_visible(_REPORT_MCP_TOOL_NAME, exposure)


def _interactive_utility_visible(name: str, exposure: Exposure) -> bool:
    if exposure.nested is not None:
        return name in exposure.nested
    return exposure.interactive or (exposure.explicit is not None and name in exposure.explicit)


def utilities_visible(exposure: Exposure) -> bool:
    return not exposure.campaign


def goals_visible(exposure: Exposure) -> bool:
    return exposure.interactive and exposure.nested is None


def coverage_visible(exposure: Exposure) -> bool:
    return (
        exposure.nested is not None
        and _COVERAGE_EVIDENCE_TOOL_NAME in exposure.nested
        and exposure.campaign
        and exposure.project
    )


def discover_builtin_definitions(
    discovered: Sequence[Any], exposure: Exposure
) -> tuple[list[dict[str, Any]], list[str]]:
    """Extract visible builtins, retaining discovery failures for the caller."""
    definitions, errors = [], []
    for info in discovered:
        path = Path(info.path)
        if path.is_absolute():
            continue
        prefix = "booley." + path.parent.as_posix().replace("/", ".")
        definition, error = _import_and_extract(path.stem, import_prefix=prefix)
        if error:
            errors.append(error)
        if definition is not None and tool_visible(definition["name"], exposure):
            definitions.append(definition)
    return definitions, errors


def visible_definitions(
    exposure: Exposure, endpoint_definitions: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Assemble supplemental tools with already-filtered endpoints, preserving objects."""
    from booley.mcp.goal_tools import goal_tool_defs

    definitions = [
        *endpoint_definitions,
        *(tool for tool in _BWAVE_MCP_TOOLS if tool_visible(tool["name"], exposure)),
    ]
    for visible, builder in (
        (status_visible(exposure), _status_mcp_tool_def),
        (report_visible(exposure), _report_mcp_tool_def),
        (utilities_visible(exposure), _poll_mcp_tool_def),
        (utilities_visible(exposure), _cancel_mcp_tool_def),
        (utilities_visible(exposure), _targets_mcp_tool_def),
        (coverage_visible(exposure), _coverage_evidence_tool_def),
        (exposure.debug, _sleep_mcp_tool_def),
    ):
        if visible:
            definitions.append(builder())
    if goals_visible(exposure):
        definitions.extend(goal_tool_defs())
    return sorted(definitions, key=lambda item: item["name"])


def _default_interactive_definitions() -> list[dict[str, Any]]:
    from booley.mcp.goal_tools import GOAL_TOOL_NAMES
    from booley.mcp.registry import discover_mcp_tools

    endpoints, errors = discover_builtin_definitions(discover_mcp_tools(), Exposure())
    if errors:
        raise RuntimeError("Interactive builtin discovery incomplete: " + "; ".join(errors))
    return [
        {**tool, "is_goal": True} if tool["name"] in GOAL_TOOL_NAMES else tool
        for tool in visible_definitions(Exposure(), endpoints)
    ]


def partition_work_dir(
    definitions: Sequence[dict[str, Any]],
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """Classify actual schemas, rejecting configurable endpoints that break the category rule."""
    accepting, nonaccepting = [], []
    for definition in sorted(definitions, key=lambda item: item["name"]):
        accepts = "work_dir" in definition["schema"].get("properties", {})
        if definition.get("module") and not accepts:
            raise ValueError(
                f"Configurable endpoint {definition['name']} lacks work_dir in its schema"
            )
        (accepting if accepts else nonaccepting).append(definition)
    return tuple(accepting), tuple(nonaccepting)


@cache
def work_dir_guidance() -> str:
    """Compose Goal routing text from the installed default Interactive schemas."""
    from booley.goals.rules import work_dir_guidance as render_guidance

    return render_guidance(partition_work_dir(_default_interactive_definitions()))
