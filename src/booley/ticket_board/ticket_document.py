"""One boundary for converting an authored Ticket into its executable model."""

from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
from yaml.tokens import AliasToken, AnchorToken, TagToken

from booley.core.models import OnSuccess, TargetPlan, TargetPlanEntry, TargetPlanRole
from booley.criteria.templates import _validate_criterion_params, encode_criterion_component
from booley.criteria.thresholds import describe_threshold
from booley.targets.domain import FuseSocError, UnknownTargetError

TicketStage = Literal["draft", "executable"]
_MANDATORY = "CRITERIA_MANDATORY"
_OPTIONAL = "CRITERIA_OPTIONAL"
_FLAGS = frozenset({"triage_report", "review", "merge", "cleanup"})
_OLD_FIELDS = frozenset({"ticket_format", "criteria", "target_plan", "acceptance_basis"})
_ANNOTATION = re.compile(r"^(?P<selector>.+?) \((?P<role>new|temp|replaces .+)\)$")
_CAPABILITIES = frozenset(
    {
        "LINT",
        "ELAB",
        "ELAB_STANDALONE",
        "SIM",
        "CYCLE_COUNT",
        "SYNTH",
        "FPGA",
        "REVIEW",
        "MUTATION",
        "COVERAGE",
    }
)
_FLOW = {
    "LINT": "lint",
    "ELAB": "sim",
    "SIM": "sim",
    "CYCLE_COUNT": "sim",
    "SYNTH": "synth",
    "FPGA": "fpga",
    "MUTATION": "sim",
    "COVERAGE": "sim",
}
_REVIEW_FOCUS = {
    "rtl": frozenset({"bugs", "spec", "protocol", "security", "optimization", "code_style"}),
    "tb": frozenset({"quality"}),
}
_COVERAGE_METRICS = frozenset({"line", "branch", "expression", "toggle", "cover_property"})


@dataclass(frozen=True)
class TicketDiagnostic:
    """An authored-ticket error with a source position."""

    message: str
    line: int
    column: int
    code: str = "invalid"


@dataclass(frozen=True)
class TargetMention:
    """A Target selector and optional authored lifecycle annotation."""

    selector: str
    role: str | None
    predecessor: str | None
    line: int
    column: int


@dataclass(frozen=True)
class TicketPreview:
    """Syntax-level information available before new Targets resolve."""

    summary: str
    on_success: tuple[str, ...]
    targets: tuple[TargetMention, ...]
    references: tuple[str, ...]
    fields: Mapping[str, Any]


@dataclass(frozen=True)
class TicketCriterion:
    """One atomic acceptance requirement."""

    identity: str
    capability: str
    mandatory: bool
    target: str | None
    test: str | None
    parameter: str | None
    value: Any
    line: int
    column: int


@dataclass(frozen=True)
class TicketSpec:
    """Normalized meaning of the authored Ticket."""

    fields: Mapping[str, Any]
    body: str
    criteria: tuple[TicketCriterion, ...]
    targets: tuple[TargetMention, ...]
    on_success: tuple[str, ...]
    target_plan: TargetPlan | None

    @property
    def completion_policy(self) -> OnSuccess:
        """Return the internal completion policy derived from authored flags."""
        return OnSuccess.from_flags(self.on_success)

    def semantic_record(self) -> dict[str, Any]:
        """Return canonical authored meaning without generated metadata or source spans."""
        ordinary = {
            key: value
            for key, value in self.fields.items()
            if key
            not in {
                _MANDATORY,
                _OPTIONAL,
                "on_success",
                "machine",
                "acceptance_amendment",
                "created",
                "feature_branch",
            }
        }
        criteria = [
            {
                "identity": row.identity,
                "capability": row.capability,
                "mandatory": row.mandatory,
                "target": row.target,
                "test": row.test,
                "parameter": row.parameter,
                "value": row.value,
            }
            for row in self.criteria
        ]
        return {
            "fields": ordinary,
            "body": self.body,
            "criteria": sorted(criteria, key=lambda row: row["identity"]),
            "target_plan": sorted(self.target_plan.as_list(), key=lambda row: row["target"])
            if self.target_plan
            else [],
            "on_success": list(self.on_success),
        }

    def semantic_digest(self) -> str:
        """Hash the authored meaning for Basis drift checks."""
        payload = json.dumps(
            self.semantic_record(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class TicketDocument:
    """Resolved authoring meaning and separately generated metadata."""

    spec: TicketSpec
    generated: Mapping[str, Any]


@dataclass(frozen=True)
class TicketConversion:
    """One conversion result, including an unresolved draft preview."""

    preview: TicketPreview | None
    diagnostics: tuple[TicketDiagnostic, ...]
    document: TicketDocument | None


@dataclass(frozen=True)
class TicketAuthoringView:
    """Frozen Target and registered-test access supplied by the Board."""

    resolve_target: Callable[[str, str | None], str]
    tests_for_target: Callable[[str], tuple[str, ...]]
    project_scalar_criteria: frozenset[str] = frozenset()


@dataclass(frozen=True)
class TicketConversionContext:
    """Trusted Board stage and resolver for the appropriate Project view."""

    stage: TicketStage
    resolve_view: Callable[[Mapping[str, Any]], TicketAuthoringView]


class _TargetResolutionError(ValueError):
    """A source-located Target error with explicit draft-preview eligibility."""

    def __init__(self, message: str, *, unresolved_draft: bool) -> None:
        super().__init__(message)
        self.unresolved_draft = unresolved_draft


def ticket_authoring_view(project_root: Path | str) -> TicketAuthoringView:
    """Capture Target selection and registered tests for one Project checkout."""
    from booley.config.project_config import load_test_configuration, lookup_target_section
    from booley.criteria.templates import load_project_criteria
    from booley.runtime.project_dir import resolve_checkout_project_dir
    from booley.targets.catalog import TargetCatalog

    root = Path(project_root)
    catalog = TargetCatalog.build(root)
    tests = load_test_configuration(root)

    def resolve_target(selector: str, flow: str | None) -> str:
        return catalog.select(selector, for_flow=flow).identity

    def tests_for_target(target: str) -> tuple[str, ...]:
        section = lookup_target_section(tests, target)
        if not isinstance(section, Mapping):
            return ()
        names = section.get("tests", ())
        return tuple(names) if isinstance(names, list) else ()

    project_criteria = load_project_criteria(resolve_checkout_project_dir(root) / "criteria.toml")
    scalar_names = frozenset(
        definition.name.upper() for definition in project_criteria if not definition.per_target
    )
    return TicketAuthoringView(resolve_target, tests_for_target, scalar_names)


@contextmanager
def ticket_conversion_context(
    project_root: Path | str, slug: str, stage: TicketStage
) -> Iterator[TicketConversionContext]:
    """Supply the converter with the draft workspace or frozen Basis Project view.

    Keep the materialized executable checkout alive through conversion because
    TargetCatalog checks its source snapshot when resolving each selector.
    """
    root = Path(project_root).resolve()
    if stage == "draft":
        from booley.runtime.project_dir import resolve_project_dir

        def resolve_view(_generated: Mapping[str, Any]) -> TicketAuthoringView:
            workspace = resolve_project_dir(root) / "worktrees" / slug
            authoring_root = workspace if workspace.is_dir() else root
            return ticket_authoring_view(authoring_root)

        yield TicketConversionContext("draft", resolve_view)
        return

    from .ticket_baseline import materialize_ticket_commits, ticket_baseline_from_machine

    with tempfile.TemporaryDirectory(prefix="booley-ticket-conversion-") as temporary:
        destination = Path(temporary) / "checkout"

        def resolve_view(generated: Mapping[str, Any]) -> TicketAuthoringView:
            pointer = generated.get("machine")
            if not isinstance(pointer, Mapping):
                raise ValueError("Executable Ticket has no valid machine baseline")
            basis = ticket_baseline_from_machine(pointer)
            authoring = {item.role: item.authoring_sha for item in basis.participants}
            checkout = materialize_ticket_commits(root, basis, destination, authoring)
            return ticket_authoring_view(checkout)

        yield TicketConversionContext("executable", resolve_view)


def _source_position(node: Node) -> tuple[int, int]:
    return node.start_mark.line + 2, node.start_mark.column + 1


def _diagnostic(
    exc: ValueError | OSError | yaml.YAMLError, *, code: str = "invalid"
) -> TicketDiagnostic:
    if isinstance(exc, yaml.YAMLError):
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            return TicketDiagnostic(str(exc), mark.line + 2, mark.column + 1, code)
    message = str(exc)
    match = re.search(r" at (\d+):(\d+)$", message)
    if match is not None:
        return TicketDiagnostic(message, int(match.group(1)), int(match.group(2)), code)
    return TicketDiagnostic(message, 1, 1, code)


def _frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        raise ValueError("Ticket must begin with YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("Ticket YAML frontmatter is not terminated")
    return text[4:end], text[end + 5 :]


def _reject_yaml_tokens(source: str) -> None:
    for token in yaml.scan(source):
        if isinstance(token, (AliasToken, AnchorToken, TagToken)):
            raise ValueError(
                f"YAML aliases, anchors, and tags are not allowed at "
                f"{token.start_mark.line + 2}:{token.start_mark.column + 1}"
            )


def _mapping_locations(
    node: Node, path: tuple[str, ...], result: dict[tuple[str, ...], tuple[int, int]]
) -> None:
    if isinstance(node, SequenceNode):
        for index, value in enumerate(node.value):
            item_path = (*path, str(index))
            result[item_path] = _source_position(value)
            _mapping_locations(value, item_path, result)
        return
    if not isinstance(node, MappingNode):
        return
    seen: set[str] = set()
    for key_node, value_node in node.value:
        if not isinstance(key_node, ScalarNode) or key_node.tag != "tag:yaml.org,2002:str":
            line, column = _source_position(key_node)
            raise ValueError(f"Ticket mapping keys must be strings at {line}:{column}")
        key = key_node.value
        line, column = _source_position(key_node)
        if key == "<<":
            raise ValueError(f"YAML merge keys are not allowed at {line}:{column}")
        if key in seen:
            raise ValueError(f"Duplicate Ticket key {key!r} at {line}:{column}")
        seen.add(key)
        location = (*path, key)
        result[location] = (line, column)
        _mapping_locations(value_node, location, result)


def _parse_yaml(source: str) -> tuple[dict[str, Any], dict[tuple[str, ...], tuple[int, int]]]:
    _reject_yaml_tokens(source)
    node = yaml.compose(source, Loader=yaml.SafeLoader)
    if not isinstance(node, MappingNode):
        raise ValueError("Ticket frontmatter must be a YAML mapping")
    locations: dict[tuple[str, ...], tuple[int, int]] = {}
    _mapping_locations(node, (), locations)
    parsed = yaml.safe_load(source)
    if not isinstance(parsed, dict):
        raise ValueError("Ticket frontmatter must be a YAML mapping")
    _require_json_values(parsed, "Ticket frontmatter")
    return parsed, locations


def _require_json_values(value: Any, path: str) -> None:
    """Reject implicit YAML types that cannot survive a canonical Basis record."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must not contain NaN or infinity")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _require_json_values(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} has a non-string key")
            _require_json_values(item, f"{path}.{key}")
        return
    raise ValueError(f"{path} contains unsupported YAML value {type(value).__name__}")


def _mention(token: str, line: int, column: int) -> TargetMention:
    match = _ANNOTATION.fullmatch(token)
    if match is None:
        return TargetMention(token, None, None, line, column)
    role = match.group("role")
    predecessor = role.removeprefix("replaces ") if role.startswith("replaces ") else None
    return TargetMention(
        match.group("selector"), "replaces" if predecessor else role, predecessor, line, column
    )


def _targets_from_section(
    section: Mapping[str, Any],
    section_name: str,
    locations: Mapping[tuple[str, ...], tuple[int, int]],
) -> tuple[TargetMention, ...]:
    mentions: list[TargetMention] = []
    for capability, declaration in section.items():
        if capability == "REVIEW" or capability not in _CAPABILITIES:
            continue
        if capability == "ELAB_STANDALONE":
            tokens = declaration if isinstance(declaration, list) else []
            for index, token in enumerate(tokens):
                if isinstance(token, str):
                    line, column = locations.get((section_name, capability, str(index)), (1, 1))
                    mentions.append(_mention(token, line, column))
            continue
        if not isinstance(declaration, dict):
            continue
        for token in declaration:
            if isinstance(token, str):
                line, column = locations.get((section_name, capability, token), (1, 1))
                mentions.append(_mention(token, line, column))
    return tuple(mentions)


def _reference_selectors(
    mandatory: Mapping[str, Any],
    optional: Mapping[str, Any],
    mentions: tuple[TargetMention, ...],
) -> tuple[str, ...]:
    """Expose every Target selector needed by dependency preflight."""
    references = [mention.selector for mention in mentions]
    references.extend(
        mention.predecessor for mention in mentions if mention.predecessor is not None
    )
    for section in (mandatory, optional):
        for capability in ("SYNTH", "FPGA", "CYCLE_COUNT"):
            declaration = section.get(capability)
            if not isinstance(declaration, Mapping):
                continue
            for policy in declaration.values():
                policies = (
                    policy.values()
                    if capability == "CYCLE_COUNT" and isinstance(policy, Mapping)
                    else (policy,)
                )
                for item in policies:
                    if isinstance(item, Mapping) and isinstance(item.get("baseline"), str):
                        references.append(item["baseline"])
    return tuple(dict.fromkeys(references))


def _on_success(fields: Mapping[str, Any]) -> tuple[str, ...]:
    raw = fields.get("on_success")
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ValueError("on_success must be a YAML list of flags")
    if len(raw) != len(set(raw)):
        raise ValueError("on_success has duplicate flags")
    unknown = set(raw) - _FLAGS
    if unknown:
        raise ValueError(f"on_success has unknown flags: {', '.join(sorted(unknown))}")
    return tuple(sorted(raw))


def _validate_top_level(
    fields: Mapping[str, Any],
    body: str,
    locations: Mapping[tuple[str, ...], tuple[int, int]],
) -> None:
    old = _OLD_FIELDS & fields.keys()
    if old or re.search(r"^## Criteria\b", body, re.MULTILINE):
        raise ValueError(
            "Old Ticket format is unsupported; rewrite Criteria in CRITERIA_MANDATORY"
        )
    required = {"summary", "type", "branch", "scope", "on_success", _MANDATORY}
    missing = required - fields.keys()
    if missing:
        raise ValueError(f"Ticket is missing required fields: {', '.join(sorted(missing))}")
    mandatory = fields[_MANDATORY]
    optional = fields.get(_OPTIONAL, {})
    if not isinstance(mandatory, dict):
        raise ValueError("CRITERIA_MANDATORY must be a mapping")
    if not isinstance(optional, dict):
        raise ValueError("CRITERIA_OPTIONAL must be a mapping")
    if not mandatory and not optional:
        raise ValueError("Ticket needs at least one Criterion")
    from .constants import KNOWN_FIELDS

    retired = {"base_sha", "target_contract", "target_contract_history"}
    if set(fields) & retired:
        raise ValueError(
            f"Unsupported retired Ticket fields: {', '.join(sorted(set(fields) & retired))}"
        )
    allowed = (KNOWN_FIELDS - _OLD_FIELDS - retired) | {_MANDATORY, _OPTIONAL}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unknown Ticket fields: {', '.join(sorted(unknown))}")
    _validate_authored_fields(fields, body, locations)


def _validate_authored_fields(
    fields: Mapping[str, Any],
    body: str,
    locations: Mapping[tuple[str, ...], tuple[int, int]],
) -> None:
    from .constants import VALID_PRIORITIES, VALID_TYPES

    for key in ("summary", "branch"):
        value = fields[key]
        if not isinstance(value, str) or not value.strip():
            line, column = locations.get((key,), (1, 1))
            raise ValueError(f"Ticket {key} must be a nonempty string at {line}:{column}")
    if not isinstance(fields["type"], str) or fields["type"] not in VALID_TYPES:
        line, column = locations.get(("type",), (1, 1))
        raise ValueError(
            f"Ticket type must be one of {', '.join(sorted(VALID_TYPES))} at {line}:{column}"
        )
    if "priority" in fields and (
        not isinstance(fields["priority"], str) or fields["priority"] not in VALID_PRIORITIES
    ):
        line, column = locations.get(("priority",), (1, 1))
        raise ValueError(
            f"Ticket priority must be one of {', '.join(sorted(VALID_PRIORITIES))} "
            f"at {line}:{column}"
        )
    for key in ("spec", "project_destination_ref"):
        if key in fields and not isinstance(fields[key], str):
            line, column = locations.get((key,), (1, 1))
            raise ValueError(f"Ticket {key} must be a string at {line}:{column}")
    for key in ("scope", "dependencies"):
        if key not in fields:
            continue
        value = fields[key]
        if (
            not isinstance(value, list)
            or any(not isinstance(item, str) or not item.strip() for item in value)
            or len(value) != len(set(value))
        ):
            line, column = locations.get((key,), (1, 1))
            raise ValueError(f"Ticket {key} must be a list of unique names at {line}:{column}")
    if "## Description" not in body:
        raise ValueError("Ticket body must contain a ## Description section")


def convert_ticket_document(text: str, context: TicketConversionContext) -> TicketConversion:
    """Convert the complete human Ticket through one validated boundary."""
    try:
        if context.stage not in {"draft", "executable"}:
            raise ValueError(f"Unknown Ticket conversion stage {context.stage!r}")
        yaml_source, body = _frontmatter(text)
        fields, locations = _parse_yaml(yaml_source)
        _validate_top_level(fields, body, locations)
        flags = _on_success(fields)
        mandatory = fields[_MANDATORY]
        optional = fields.get(_OPTIONAL, {})
        mentions = (
            *_targets_from_section(mandatory, _MANDATORY, locations),
            *_targets_from_section(optional, _OPTIONAL, locations),
        )
        _validate_criterion_syntax(mandatory, optional, locations)
        preview = TicketPreview(
            str(fields["summary"]),
            flags,
            tuple(mentions),
            _reference_selectors(mandatory, optional, tuple(mentions)),
            fields,
        )
        generated = {
            key: fields[key]
            for key in ("machine", "acceptance_amendment", "created", "feature_branch")
            if key in fields
        }
        if context.stage == "draft" and generated:
            raise ValueError("Draft Ticket cannot contain generated execution metadata")
        if context.stage == "executable" and "machine" not in generated:
            raise ValueError("Executable Ticket requires machine baseline metadata")
        view = context.resolve_view(generated)
        criteria = _normalize_criteria(mandatory, optional, view, locations)
        target_plan = _derive_target_plan(tuple(mentions), criteria, flags, view)
        spec = TicketSpec(fields, body, criteria, tuple(mentions), flags, target_plan)
        return TicketConversion(preview, (), TicketDocument(spec, generated))
    except _TargetResolutionError as exc:
        code = (
            "unresolved_target" if context.stage == "draft" and exc.unresolved_draft else "invalid"
        )
        return TicketConversion(locals().get("preview"), (_diagnostic(exc, code=code),), None)
    except UnknownTargetError as exc:
        return TicketConversion(
            locals().get("preview"), (_diagnostic(ValueError(str(exc))),), None
        )
    except FuseSocError as exc:
        return TicketConversion(
            locals().get("preview"), (_diagnostic(ValueError(str(exc))),), None
        )
    except (ValueError, OSError, yaml.YAMLError) as exc:
        return TicketConversion(locals().get("preview"), (_diagnostic(exc),), None)


def serialize_ticket_document(document: TicketDocument, context: TicketConversionContext) -> str:
    """Render v2 frontmatter and prove it preserves the converted Ticket meaning."""
    generated_keys = {"machine", "acceptance_amendment", "created", "feature_branch"}
    if set(document.generated) - generated_keys:
        raise ValueError("Ticket has unsupported generated metadata")
    fields = {
        key: value for key, value in document.spec.fields.items() if key not in generated_keys
    }
    fields.update(document.generated)
    rendered = (
        "---\n"
        + yaml.safe_dump(fields, sort_keys=False, allow_unicode=True)
        + "---\n"
        + document.spec.body
    )
    converted = convert_ticket_document(rendered, context)
    if converted.document is None:
        detail = "; ".join(item.message for item in converted.diagnostics)
        raise ValueError(f"Serialized Ticket is invalid: {detail}")
    if (
        converted.document.spec.semantic_digest() != document.spec.semantic_digest()
        or converted.document.generated != document.generated
    ):
        raise ValueError("Serialized Ticket changed authored or generated meaning")
    return rendered


def _validate_criterion_syntax(
    mandatory: Mapping[str, Any],
    optional: Mapping[str, Any],
    locations: Mapping[tuple[str, ...], tuple[int, int]],
) -> None:
    """Check all declarations before a missing draft Target can defer resolution."""
    names = {"__registered_for_syntax__"}
    for section in (mandatory, optional):
        for capability, declaration in section.items():
            if not isinstance(declaration, Mapping):
                continue
            if capability in {"SIM", "CYCLE_COUNT"}:
                for requirements in declaration.values():
                    if isinstance(requirements, Mapping):
                        names.update(str(test) for test in requirements if test != "all")
            elif capability == "COVERAGE":
                for requirements in declaration.values():
                    if isinstance(requirements, Mapping):
                        tests = requirements.get("tests")
                        if isinstance(tests, list):
                            names.update(str(test) for test in tests)
    view = TicketAuthoringView(
        resolve_target=lambda selector, _flow: selector,
        tests_for_target=lambda _target: tuple(sorted(names)),
        project_scalar_criteria=frozenset(
            name
            for section in (mandatory, optional)
            for name in section
            if name not in _CAPABILITIES
        ),
    )
    _normalize_criteria(mandatory, optional, view, locations)


def _normalize_criteria(
    mandatory: Mapping[str, Any],
    optional: Mapping[str, Any],
    view: TicketAuthoringView,
    locations: Mapping[tuple[str, ...], tuple[int, int]],
) -> tuple[TicketCriterion, ...]:
    """Expand authored capability blocks into stable atomic requirements."""
    rows: list[TicketCriterion] = []
    seen: set[str] = set()
    annotations: dict[str, tuple[str | None, str | None]] = {}
    for section_name, section, required in (
        (_MANDATORY, mandatory, True),
        (_OPTIONAL, optional, False),
    ):
        unknown = set(section) - _CAPABILITIES - view.project_scalar_criteria
        if unknown:
            raise ValueError(f"Unknown Ticket capabilities: {', '.join(sorted(unknown))}")
        for capability, declaration in section.items():
            parsed = _capability_rows(
                capability, declaration, section_name, required, view, locations, annotations
            )
            if not parsed:
                raise ValueError(f"{section_name}.{capability} must not be empty")
            for row in parsed:
                if row.identity in seen:
                    raise ValueError(f"Duplicate atomic Criterion {row.identity}")
                seen.add(row.identity)
                rows.append(row)
    if not rows:
        raise ValueError("Ticket needs at least one atomic Criterion")
    _validate_cross_criteria(rows, annotations)
    return tuple(rows)


def _validate_cross_criteria(
    rows: list[TicketCriterion],
    annotations: Mapping[str, tuple[str | None, str | None]],
) -> None:
    baselines: dict[tuple[str, str, str], str | None] = {}
    policies: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        if row.capability not in {"SYNTH", "FPGA", "CYCLE_COUNT"}:
            continue
        if row.parameter == "run":
            continue
        policy = row.value
        base = policy.get("baseline") if isinstance(policy, dict) else None
        if base is not None and annotations.get(base, (None, None))[0] is not None:
            raise ValueError(
                f"{row.capability} baseline {base!r} is authored by this Ticket; "
                "choose an existing Target"
            )
        identity = (row.capability, row.target or "", row.test or "")
        prior = baselines.setdefault(identity, base)
        if prior != base:
            raise ValueError(f"{row.capability} Target/test has conflicting baselines")
        parameter = row.parameter or ""
        threshold = policy["threshold"]
        descriptor = describe_threshold(parameter)
        if descriptor is not None and descriptor.unit == "percent":
            threshold = f"{threshold}%"
        policies.setdefault(identity, {})[parameter] = threshold
    for (capability, _target, _test), policy in policies.items():
        key = {"SYNTH": "synthesis_ok", "FPGA": "fpga_impl_ok", "CYCLE_COUNT": "cycle_count"}[
            capability
        ]
        _validate_criterion_params(key, policy)


_RUNTIME_FAMILY = {
    "LINT": "lint_clean",
    "ELAB": "elab_pass",
    "SIM": "sim_pass",
    "CYCLE_COUNT": "cycle_count",
    "SYNTH": "synthesis_ok",
    "FPGA": "fpga_impl_ok",
    "MUTATION": "mutation_score",
    "COVERAGE": "coverage",
}


def _identity(capability: str, target: str | None, test: str | None, parameter: str | None) -> str:
    """Use collision-free atomic keys that retain the producing Flow family."""
    if capability == "REVIEW":
        return f"review_{target}_{test}_{parameter}"
    if capability == "ELAB_STANDALONE":
        return "elaborate_standalone"
    if capability not in _RUNTIME_FAMILY:
        return capability.lower()
    parts = [encode_criterion_component(target or "")]
    if test is not None:
        parts.append(encode_criterion_component(test))
    if parameter is not None:
        parts.append(encode_criterion_component(parameter))
    return "_".join((_RUNTIME_FAMILY[capability], *parts))


def _criterion(
    capability: str,
    required: bool,
    target: str | None,
    test: str | None,
    parameter: str | None,
    value: Any,
    line: int,
    column: int,
) -> TicketCriterion:
    identity = _identity(capability, target, test, parameter)
    return TicketCriterion(
        identity, capability, required, target, test, parameter, value, line, column
    )


def _target_rows(
    capability: str,
    declaration: Any,
    section_name: str,
    view: TicketAuthoringView,
    locations: Mapping[tuple[str, ...], tuple[int, int]],
    annotations: dict[str, tuple[str | None, str | None]],
) -> tuple[tuple[str, Any, int, int], ...]:
    if not isinstance(declaration, dict) or not declaration:
        raise ValueError(f"{section_name}.{capability} must map Targets to requirements")
    rows: list[tuple[str, Any, int, int]] = []
    seen: set[str] = set()
    for token, value in declaration.items():
        if not isinstance(token, str):
            raise ValueError(f"{section_name}.{capability} Target selectors must be strings")
        line, column = locations.get((section_name, capability, token), (1, 1))
        mention = _mention(token, line, column)
        try:
            target = view.resolve_target(mention.selector, _FLOW[capability])
        except UnknownTargetError as exc:
            raise _TargetResolutionError(
                f"{exc} at {line}:{column}", unresolved_draft=mention.role is not None
            ) from exc
        if target in seen:
            raise ValueError(f"{section_name}.{capability} repeats Target {target!r}")
        seen.add(target)
        role = (mention.role, mention.predecessor)
        prior = annotations.setdefault(target, role)
        if prior != role:
            raise ValueError(f"Target {target!r} has inconsistent lifecycle annotations")
        rows.append((target, value, line, column))
    return tuple(rows)


def _capability_rows(
    capability: str,
    declaration: Any,
    section_name: str,
    required: bool,
    view: TicketAuthoringView,
    locations: Mapping[tuple[str, ...], tuple[int, int]],
    annotations: dict[str, tuple[str | None, str | None]],
) -> tuple[TicketCriterion, ...]:
    if capability not in _CAPABILITIES:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", capability) or declaration is not True:
            raise ValueError(f"{capability} project Criterion must be true")
        line, column = locations.get((section_name, capability), (1, 1))
        return (_criterion(capability, required, None, None, None, True, line, column),)
    if capability == "REVIEW":
        return _review_rows(declaration, section_name, required, locations)
    if capability == "ELAB_STANDALONE":
        return _standalone_rows(declaration, section_name, required, view, locations, annotations)
    rows: list[TicketCriterion] = []
    for target, value, line, column in _target_rows(
        capability, declaration, section_name, view, locations, annotations
    ):
        rows.extend(
            _target_criteria(
                capability, required, target, value, line, column, view, annotations[target]
            )
        )
    return tuple(rows)


def _target_criteria(
    capability: str,
    required: bool,
    target: str,
    value: Any,
    line: int,
    column: int,
    view: TicketAuthoringView,
    annotation: tuple[str | None, str | None],
) -> tuple[TicketCriterion, ...]:
    if capability in {"LINT", "ELAB"}:
        expected = "clean" if capability == "LINT" else "pass"
        if value != expected:
            raise ValueError(f"{capability} Target requires {expected!r}")
        return (_criterion(capability, required, target, None, None, value, line, column),)
    if capability == "SIM":
        return _sim_rows(target, value, required, line, column, view)
    if capability == "CYCLE_COUNT":
        return _cycle_rows(target, value, required, line, column, view, annotation)
    if capability in {"SYNTH", "FPGA"}:
        return _implementation_rows(
            capability, target, value, required, line, column, view, annotation
        )
    if capability == "MUTATION":
        policy = _validate_criterion_params("mutation_score", _nonempty_mapping(value, "MUTATION"))
        return (_criterion(capability, required, target, None, None, policy, line, column),)
    return _coverage_rows(target, value, required, line, column, view)


def _nonempty_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{label} requires a nonempty mapping")
    return value


def _sim_rows(
    target: str,
    value: Any,
    required: bool,
    line: int,
    column: int,
    view: TicketAuthoringView,
) -> tuple[TicketCriterion, ...]:
    tests = _nonempty_mapping(value, "SIM")
    registered = set(view.tests_for_target(target))
    if not registered:
        raise ValueError(f"SIM Target {target!r} has no registered tests")
    rows = []
    for test, verdict in tests.items():
        if test != "all" and test not in registered:
            raise ValueError(f"SIM Target {target!r} has unregistered test {test!r}")
        if verdict not in ("pass", "fail -> pass") or (test == "all" and verdict != "pass"):
            raise ValueError(f"SIM {target!r}/{test!r} needs pass or named-test fail -> pass")
        rows.append(_criterion("SIM", required, target, test, None, verdict, line, column))
    return tuple(rows)


def _cycle_rows(
    target: str,
    value: Any,
    required: bool,
    line: int,
    column: int,
    view: TicketAuthoringView,
    annotation: tuple[str | None, str | None],
) -> tuple[TicketCriterion, ...]:
    tests = _nonempty_mapping(value, "CYCLE_COUNT")
    registered = set(view.tests_for_target(target))
    rows = []
    for test, policy in tests.items():
        if test not in registered:
            raise ValueError(f"CYCLE_COUNT Target {target!r} has unregistered test {test!r}")
        raw = _nonempty_mapping(policy, "CYCLE_COUNT test")
        baseline = raw.get("baseline")
        thresholds = {key: val for key, val in raw.items() if key != "baseline"}
        normalized = _validate_criterion_params("cycle_count", thresholds)
        if not normalized:
            raise ValueError("CYCLE_COUNT test needs at least one threshold")
        baseline = _resolve_metric_baseline(
            "CYCLE_COUNT", target, baseline, normalized, view, annotation
        )
        if baseline is not None and test not in view.tests_for_target(baseline):
            raise ValueError(
                f"CYCLE_COUNT test {test!r} is not registered on baseline Target {baseline!r}"
            )
        for parameter, threshold in normalized.items():
            rows.append(
                _criterion(
                    "CYCLE_COUNT",
                    required,
                    target,
                    test,
                    parameter,
                    {"threshold": threshold, "baseline": baseline},
                    line,
                    column,
                )
            )
    return tuple(rows)


def _implementation_rows(
    capability: str,
    target: str,
    value: Any,
    required: bool,
    line: int,
    column: int,
    view: TicketAuthoringView,
    annotation: tuple[str | None, str | None],
) -> tuple[TicketCriterion, ...]:
    if value == "pass":
        return (_criterion(capability, required, target, None, "run", "pass", line, column),)
    raw = _nonempty_mapping(value, capability)
    baseline = raw.get("baseline")
    thresholds = {key: val for key, val in raw.items() if key != "baseline"}
    key = "synthesis_ok" if capability == "SYNTH" else "fpga_impl_ok"
    normalized = _validate_criterion_params(key, thresholds)
    if not normalized:
        raise ValueError(f"{capability} needs a threshold or scalar pass")
    baseline = _resolve_metric_baseline(capability, target, baseline, normalized, view, annotation)
    return tuple(
        _criterion(
            capability,
            required,
            target,
            None,
            parameter,
            {"threshold": threshold, "baseline": baseline},
            line,
            column,
        )
        for parameter, threshold in normalized.items()
    )


def _resolve_metric_baseline(
    capability: str,
    target: str,
    explicit: Any,
    thresholds: Mapping[str, Any],
    view: TicketAuthoringView,
    annotation: tuple[str | None, str | None],
) -> str | None:
    relative = any(
        bool(describe_threshold(key) and describe_threshold(key).relative) for key in thresholds
    )
    if explicit is not None and (not isinstance(explicit, str) or not explicit.strip()):
        raise ValueError(f"{capability} baseline must be an existing Target selector")
    if explicit is not None and not relative:
        raise ValueError(f"{capability} baseline requires a relative threshold")
    if not relative:
        return None
    if explicit is not None:
        return view.resolve_target(explicit, _FLOW[capability])
    role, predecessor = annotation
    if role == "replaces" and predecessor is not None:
        return view.resolve_target(predecessor, _FLOW[capability])
    if role in {"new", "temp"}:
        raise ValueError(
            f"{capability} new Target {target!r} needs an explicit baseline for relative thresholds"
        )
    return target


def _review_rows(
    declaration: Any,
    section_name: str,
    required: bool,
    locations: Mapping[tuple[str, ...], tuple[int, int]],
) -> tuple[TicketCriterion, ...]:
    categories = _nonempty_mapping(declaration, "REVIEW")
    rows = []
    for category, focuses in categories.items():
        if category not in _REVIEW_FOCUS:
            raise ValueError(f"REVIEW category {category!r} is unknown")
        for focus, outcomes in _nonempty_mapping(focuses, "REVIEW category").items():
            if focus not in _REVIEW_FOCUS[category]:
                raise ValueError(f"REVIEW {category} focus {focus!r} is unknown")
            values = outcomes if isinstance(outcomes, list) else [outcomes]
            if (
                not values
                or any(not isinstance(value, str) for value in values)
                or len(values) != len(set(values))
                or set(values) - {"done", "clean"}
            ):
                raise ValueError("REVIEW outcome must be done, clean, or [done, clean]")
            line, column = locations.get((section_name, "REVIEW", category, focus), (1, 1))
            rows.extend(
                _criterion("REVIEW", required, category, focus, outcome, outcome, line, column)
                for outcome in values
            )
    return tuple(rows)


def _standalone_rows(
    declaration: Any,
    section_name: str,
    required: bool,
    view: TicketAuthoringView,
    locations: Mapping[tuple[str, ...], tuple[int, int]],
    annotations: dict[str, tuple[str | None, str | None]],
) -> tuple[TicketCriterion, ...]:
    if not isinstance(declaration, list) or not declaration:
        raise ValueError("ELAB_STANDALONE requires a nonempty Target list")
    targets: list[str] = []
    for index, token in enumerate(declaration):
        if not isinstance(token, str):
            raise ValueError("ELAB_STANDALONE Target selectors must be strings")
        line, column = locations.get((section_name, "ELAB_STANDALONE", str(index)), (1, 1))
        mention = _mention(token, line, column)
        try:
            target = view.resolve_target(mention.selector, None)
        except UnknownTargetError as exc:
            raise _TargetResolutionError(
                f"{exc} at {line}:{column}", unresolved_draft=mention.role is not None
            ) from exc
        if target in targets:
            raise ValueError(f"ELAB_STANDALONE repeats Target {target!r}")
        targets.append(target)
        role = (mention.role, mention.predecessor)
        prior = annotations.setdefault(target, role)
        if prior != role:
            raise ValueError(f"Target {target!r} has inconsistent lifecycle annotations")
    line, column = locations.get((section_name, "ELAB_STANDALONE"), (1, 1))
    return (
        _criterion(
            "ELAB_STANDALONE", required, None, None, None, tuple(sorted(targets)), line, column
        ),
    )


def _coverage_rows(
    target: str,
    value: Any,
    required: bool,
    line: int,
    column: int,
    view: TicketAuthoringView,
) -> tuple[TicketCriterion, ...]:
    raw = _nonempty_mapping(value, "COVERAGE")
    if set(raw) != {"tests", "metrics"}:
        raise ValueError("COVERAGE requires exactly tests and metrics")
    tests = raw["tests"]
    registered = set(view.tests_for_target(target))
    if tests != "all" and (
        not isinstance(tests, list)
        or not tests
        or any(not isinstance(test, str) for test in tests)
        or len(tests) != len(set(tests))
        or set(tests) - registered
    ):
        raise ValueError(f"COVERAGE {target!r} needs all or registered named tests")
    if tests == "all" and not registered:
        raise ValueError(f"COVERAGE {target!r} has no registered tests")
    metrics = _nonempty_mapping(raw["metrics"], "COVERAGE metrics")
    if set(metrics) - _COVERAGE_METRICS:
        raise ValueError(
            f"COVERAGE has unknown metrics: {sorted(set(metrics) - _COVERAGE_METRICS)}"
        )
    rows = []
    for metric, policy in metrics.items():
        if not isinstance(policy, dict) or set(policy) != {"min_pct"}:
            raise ValueError(f"COVERAGE {metric} requires min_pct")
        threshold = policy["min_pct"]
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not 0 < threshold <= 100
        ):
            raise ValueError(f"COVERAGE {metric} min_pct must be greater than 0 and at most 100")
        rows.append(
            _criterion(
                "COVERAGE",
                required,
                target,
                None,
                metric,
                {"tests": tests, "min_pct": threshold},
                line,
                column,
            )
        )
    return tuple(rows)


def _derive_target_plan(
    mentions: tuple[TargetMention, ...],
    criteria: tuple[TicketCriterion, ...],
    flags: tuple[str, ...],
    view: TicketAuthoringView,
) -> TargetPlan | None:
    entries: dict[str, TargetPlanEntry] = {}
    for mention in mentions:
        if mention.role is None:
            continue
        target = view.resolve_target(mention.selector, None)
        predecessor = view.resolve_target(mention.predecessor, None) if mention.predecessor else ""
        if predecessor == target:
            raise ValueError(f"Replacement Target {target!r} cannot replace itself")
        role = {
            "new": TargetPlanRole.PERSISTENT,
            "temp": TargetPlanRole.EPHEMERAL,
            "replaces": TargetPlanRole.REPLACEMENT,
        }[mention.role]
        entry = TargetPlanEntry(target, role, predecessor)
        if target in entries and entries[target] != entry:
            raise ValueError(f"Target {target!r} has inconsistent lifecycle annotations")
        entries[target] = entry
    if not entries:
        return None
    if "merge" not in flags:
        raise ValueError("Tickets with annotated Targets require on_success.merge")
    for target in entries:
        if not any(
            row.mandatory
            and (
                row.target == target
                or (row.capability == "ELAB_STANDALONE" and target in row.value)
            )
            for row in criteria
        ):
            raise ValueError(f"Ticket-authored Target {target!r} needs a mandatory Flow Criterion")
    return TargetPlan.from_value([entry.as_dict() for entry in entries.values()])
