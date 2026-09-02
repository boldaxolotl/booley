"""Deterministic guards against confusing Verilog macros with top parameters."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol, cast


class ParameterIntegrityError(ValueError):
    """A Target requests a feature in a way that cannot affect its top parameter."""


_DEFINE_INT_RE = re.compile(
    r"^(?:(?P<width>\d+))?'[sS]?(?P<base>[bBoOdDhH])(?P<digits>[0-9a-fA-F_]+)$"
)
_MODULE_RE = re.compile(r"\bmodule\s+(?:(?:automatic|static)\s+)?(?P<name>\\\S+|[A-Za-z_$][\w$]*)")
_AMBIGUOUS_CONDITIONAL = "__BOOLEY_UNPROVEN_PREPROCESSOR_BRANCH__"


class _ResolvedFile(Protocol):
    @property
    def is_hdl(self) -> bool: ...

    @property
    def is_include(self) -> bool: ...

    def absolute(self, build_root: Path) -> Path: ...


class ResolvedTargetLike(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def parameters(self) -> Mapping[str, object]: ...

    @property
    def toplevel(self) -> str: ...

    @property
    def eda_tool(self) -> str | None: ...

    @property
    def build_root(self) -> Path: ...

    @property
    def files(self) -> Sequence[_ResolvedFile]: ...


def _string_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    raw = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        return None
    return cast(Mapping[str, object], raw)


def _define_value_is_nonzero(value: str) -> bool:
    """True when a define value is a known, integral non-zero literal."""
    compact = value.strip().replace("_", "")
    if compact.lower() in {"true", "yes", "on"}:
        return True
    try:
        return int(compact, 0) != 0
    except ValueError:
        match = _DEFINE_INT_RE.fullmatch(compact)
    if match is None:
        return False
    base = {"b": 2, "o": 8, "d": 10, "h": 16}[match.group("base").lower()]
    try:
        return int(match.group("digits"), base) != 0
    except ValueError:
        return False


def enabled_define_names(defines: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Names of bare or integral-nonzero preprocessor defines, in order."""
    names: list[str] = []
    for define in defines:
        name, separator, value = define.partition("=")
        if name and (not separator or _define_value_is_nonzero(value)):
            names.append(name)
    return tuple(names)


def enabled_vlogdefine_names(parameters: Mapping[str, object]) -> tuple[str, ...]:
    """Enabled ``vlogdefine`` names from a resolved CAPI2 parameter mapping."""
    defines: list[str] = []
    for name, spec in parameters.items():
        item = _string_mapping(spec)
        if item is None or item.get("paramtype") != "vlogdefine":
            continue
        default = item.get("default")
        if default is True:
            defines.append(str(name))
        elif default not in (False, None, ""):
            defines.append(f"{name}={default}")
    return enabled_define_names(defines)


def defined_vlogdefine_names(parameters: Mapping[str, object]) -> set[str]:
    """Names of every resolved ``vlogdefine`` the backend will emit."""
    names: set[str] = set()
    for name, spec in parameters.items():
        item = _string_mapping(spec)
        if item is None or item.get("paramtype") != "vlogdefine":
            continue
        if item.get("default") not in (False, None, ""):
            names.add(str(name))
    return names


def vlogdefine_names(parameters: Mapping[str, object]) -> set[str]:
    """Names whose defined/undefined state is fixed by resolved parameters."""
    return {
        str(name)
        for name, spec in parameters.items()
        if (item := _string_mapping(spec)) is not None and item.get("paramtype") == "vlogdefine"
    }


def vlogparam_values(parameters: Mapping[str, object]) -> dict[str, object]:
    """Concrete resolved ``vlogparam`` defaults, ready for an EDAM builder."""
    return {
        str(name): item["default"]
        for name, spec in parameters.items()
        if (item := _string_mapping(spec)) is not None
        and item.get("paramtype") == "vlogparam"
        and item.get("default") is not None
    }


def _without_comments(text: str) -> str:
    """Remove Verilog comments while preserving strings and line boundaries."""
    pattern = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.DOTALL)

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.startswith('"'):
            return token
        return "".join("\n" if char == "\n" else " " for char in token)

    return pattern.sub(replace, text)


def _without_strings(text: str) -> str:
    """Mask string contents so diagnostics cannot look like module instances."""
    return re.sub(
        r'"(?:\\.|[^"\\])*"',
        lambda match: " " * len(match.group(0)),
        text,
    )


def _without_explicit_generate_blocks(text: str) -> str:
    """Skip conditional generate regions that need real elaboration to resolve."""
    return re.sub(
        r"\bgenerate\b.*?\bendgenerate\b",
        lambda match: "".join("\n" if char == "\n" else " " for char in match.group(0)),
        text,
        flags=re.DOTALL,
    )


def _module_texts(source: str) -> dict[str, str]:
    """Return complete comment-free module declarations from one HDL source."""
    text = _without_strings(_without_comments(source))
    modules: dict[str, str] = {}
    for match in _MODULE_RE.finditer(text):
        name = match.group("name").removeprefix("\\").rstrip()
        end = re.search(r"\bendmodule\b", text[match.end() :])
        if end is not None:
            modules.setdefault(name, text[match.start() : match.end() + end.end()])
    return modules


def _literal_zero_parameter(module_text: str, name: str) -> bool:
    """True for a same-named parameter whose authored default is plainly zero."""
    escaped = re.escape(name)
    zero = r"(?:\d+\s*'\s*[sS]?[bBoOdDhH]\s*0+|'\s*0|0|false)"
    pattern = re.compile(
        rf"\bparameter\b(?:(?!\b(?:parameter|localparam)\b|;).)*?"
        rf"\b{escaped}\s*=\s*{zero}(?=\s*[,;)])",
        re.DOTALL | re.IGNORECASE,
    )
    return pattern.search(module_text) is not None


def _active_for_defines(
    module_text: str,
    defined: set[str],
    known: set[str] | None = None,
    ambiguous: set[str] | None = None,
) -> str:
    """Select known branches and mark source-controlled regions as unproven."""
    ambiguous = ambiguous or set()
    known = known or defined
    # Frames hold (parent-active, branch-taken, current-active, unknown).
    states: list[tuple[bool, bool, bool, bool]] = []
    active: list[str] = []
    directive = re.compile(r"^\s*`(ifdef|ifndef|elsif|else|endif)\b\s*([A-Za-z_$][\w$]*)?")
    for line in module_text.splitlines(keepends=True):
        match = directive.match(line)
        if match is None:
            if not states or states[-1][2]:
                active.append(line)
            continue
        kind, macro = match.groups()
        if kind in {"ifdef", "ifndef"}:
            parent = states[-1][2] if states else True
            unknown = macro not in known or macro in ambiguous
            if unknown:
                if parent:
                    active.append(f"{_AMBIGUOUS_CONDITIONAL}\n")
                states.append((parent, False, False, True))
                continue
            condition = macro in defined
            condition = condition if kind == "ifdef" else not condition
            states.append((parent, condition, parent and condition, False))
        elif kind == "elsif" and states:
            parent, taken, _current, unknown = states[-1]
            if unknown or macro not in known or macro in ambiguous:
                if parent and not taken:
                    active.append(f"{_AMBIGUOUS_CONDITIONAL}\n")
                states[-1] = (parent, taken, False, True)
                continue
            condition = macro in defined
            states[-1] = (parent, taken or condition, parent and not taken and condition, False)
        elif kind == "else" and states:
            parent, taken, _current, unknown = states[-1]
            states[-1] = (parent, True, parent and not taken and not unknown, unknown)
        elif kind == "endif" and states:
            states.pop()
    return "".join(active)


def _balanced_end(text: str, start: int) -> int | None:
    """Return the index after a balanced parenthesized expression."""
    if start >= len(text) or text[start] != "(":
        return None
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _module_instances(module_text: str, module_names: set[str]) -> list[tuple[str, str | None]]:
    """Find high-confidence unconditional instances and their overrides."""
    if not module_names:
        return []
    module_text = _without_explicit_generate_blocks(_without_strings(module_text))
    names = "|".join(re.escape(name) for name in sorted(module_names, key=len, reverse=True))
    instances: list[tuple[str, str | None]] = []
    # A direct declaration begins at module scope after a statement boundary.
    # Requiring that boundary deliberately skips implicit conditional-generate
    # forms such as ``if (0) child u()`` rather than guessing elaboration.
    pattern = rf"(?:^|;)\s*(?P<module>{names})\b"
    for match in re.finditer(pattern, module_text, re.MULTILINE):
        cursor = match.end()
        while cursor < len(module_text) and module_text[cursor].isspace():
            cursor += 1
        overrides: str | None = None
        if cursor < len(module_text) and module_text[cursor] == "#":
            cursor += 1
            while cursor < len(module_text) and module_text[cursor].isspace():
                cursor += 1
            end = _balanced_end(module_text, cursor)
            if end is None:
                continue
            overrides = module_text[cursor + 1 : end - 1]
            cursor = end
        instance = re.match(r"\s*(?:\\\S+|[A-Za-z_$][\w$]*)\s*\(", module_text[cursor:])
        if instance is not None:
            instances.append((match.group("module"), overrides))
    return instances


def _parameter_is_overridden(overrides: str | None, name: str) -> bool | None:
    """Return whether a named override can change the parameter from zero."""
    if overrides is None or not overrides.strip():
        return False
    if _AMBIGUOUS_CONDITIONAL in overrides:
        return None
    named = re.search(rf"\.\s*{re.escape(name)}\s*\(", overrides)
    if named is not None:
        start = named.end() - 1
        end = _balanced_end(overrides, start)
        if end is None:
            return None
        value = overrides[start + 1 : end - 1].strip().replace(" ", "")
        return re.fullmatch(r"(?:\d+'[sS]?[bBoOdDhH]0+|'0|0|false)", value, re.I) is None
    if re.search(r"\.\s*[A-Za-z_$][\w$]*\s*\(", overrides):
        return False
    return None


def _mismatched_modules(modules: Mapping[str, str], top: str, name: str) -> list[str]:
    """Find reachable zero-default parameters lacking a named override."""
    mismatches = [top] if _literal_zero_parameter(modules[top], name) else []
    pending = [top]
    visited: set[str] = set()
    while pending:
        parent = pending.pop()
        if parent in visited:
            continue
        visited.add(parent)
        for child, overrides in _module_instances(modules[parent], set(modules) - {parent}):
            pending.append(child)
            if _literal_zero_parameter(modules[child], name) and (
                _parameter_is_overridden(overrides, name) is False
            ):
                mismatches.append(child)
    return list(dict.fromkeys(mismatches))


def validate_top_parameter_intent(resolved: ResolvedTargetLike, *, flow: str) -> None:
    """Reject enabled macros that leave a reachable same-named parameter at zero.

    This is deliberately a high-confidence source guard, not a general HDL
    evaluator. Macro-driven defaults and explicit named overrides are allowed;
    positional overrides and complex expressions are left to the EDA backend.
    """
    parameters = resolved.parameters
    enabled = enabled_vlogdefine_names(parameters)
    top = resolved.toplevel
    if not enabled or not top:
        return
    sources, source_touched = _resolved_hdl_sources(resolved)
    defined = defined_vlogdefine_names(parameters) | _tool_builtin_defines(resolved, flow)
    known = vlogdefine_names(parameters) | _tool_known_macros()
    modules: dict[str, str] = {}
    for source in sources:
        selected = _active_for_defines(source, defined, known, source_touched)
        for module, module_text in _module_texts(selected).items():
            modules.setdefault(module, module_text)
    if top not in modules:
        return
    mismatches = {name: _mismatched_modules(modules, top, name) for name in enabled}
    mismatches = {name: locations for name, locations in mismatches.items() if locations}
    if not mismatches:
        return
    joined = ", ".join(
        f"{name} ({', '.join(locations)})" for name, locations in mismatches.items()
    )
    raise ParameterIntegrityError(
        f"{flow}: Target {resolved.name!r} enables {joined} as a vlogdefine, but "
        f"the hierarchy rooted at {top!r} contains the same parameter with literal "
        "default 0 and no explicit override. A preprocessor define cannot override "
        "a module parameter. Declare it as `paramtype: vlogparam`, explicitly "
        "override the instance parameter, or make the macro drive its default."
    )


def _tool_builtin_defines(resolved: ResolvedTargetLike, flow: str) -> set[str]:
    """Preprocessor names the selected backend defines without Target input."""
    eda_tool = str(resolved.eda_tool or "").lower()
    builtins = {
        "icarus": {"__ICARUS__"},
        "verilator": {"VERILATOR"},
    }.get(eda_tool, set())
    if flow == "fpga":
        builtins.add("SYNTHESIS")
    return builtins


def _tool_known_macros() -> set[str]:
    """Backend-owned macro names whose absence is also deterministic."""
    return {"VERILATOR", "__ICARUS__", "SYNTHESIS"}


def _resolved_hdl_sources(resolved: ResolvedTargetLike) -> tuple[list[str], set[str]]:
    """Read compiled HDL and find macros whose source state is mutable."""
    build_root = Path(resolved.build_root)
    sources: list[str] = []
    source_touched: set[str] = set()
    for resolved_file in resolved.files:
        if not resolved_file.is_hdl:
            continue
        path = resolved_file.absolute(build_root)
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        structural = _without_strings(_without_comments(source))
        source_touched.update(
            re.findall(r"(?m)^\s*`(?:define|undef)\s+([A-Za-z_$][\w$]*)\b", structural)
        )
        if not resolved_file.is_include:
            sources.append(source)
    return sources, source_touched
