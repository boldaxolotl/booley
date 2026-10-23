"""Project configuration — single source of truth loaded from project/ TOMLs.

Shared by simulation, synthesis, Flows, MCP tools, and pipeline scripts. Any config-related
change should happen in booley.toml and configs.toml in the Booley Directory.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Load booley.toml (Booley Directory first, fallback to .booley/ root)
# ---------------------------------------------------------------------------
import logging as _logging
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from booley.config.tests_toml_rules import (
    DEFAULT_SEED,
    VERILATOR_SEED_OPTION,
    TestRunNameError,
    TestVariant,
    argument_token_problem,
    option_key,
    parse_test_variants,
    template_fields,
    validate_declared_env_names,
    validate_env_table,
    validate_seed,
    validate_seed_template,
    validate_test_or_variant_name,
)
from booley.core.booley_dir import resolve_booley_dir, resolve_checkout_booley_dir
from booley.core.boundary import as_dict, as_str, is_str_list
from booley.core.checkout_role import SourceCheckoutProjectError
from booley.core.config_paths import resolve_toml
from booley.targets.selection import target_section_key

_logger = _logging.getLogger(__name__)

_SCRIPT_DIR = Path(__file__).parent.resolve()
_PIPELINE_DIR = _SCRIPT_DIR.parent


# ---------------------------------------------------------------------------
# Lazy, import-safe config loading (Issue #1)
# ---------------------------------------------------------------------------
# Config is loaded on *first access*, never at import time, so that importing
# any Flow, MCP-tool, or harness module performs zero filesystem I/O. Import-time I/O here
# was the root cause that forced ~265 defensive function-local imports across
# the repo. Derived constants (PROJECT_NAME, ENV_PREFIX,
# TEST_NAMES, TEST_SELECT) are memoized in ``_CONFIG_CACHE`` and surfaced as
# module attributes via PEP 562 ``__getattr__``.

_CONFIG_CACHE: dict[str, Any] | None = None

# Names resolved lazily from project config. Exposed via ``__getattr__`` and
# read internally via ``_get`` (which honors test monkeypatches).
_LAZY_NAMES = frozenset(
    {
        "PROJECT_NAME",
        "ENV_PREFIX",
        "TEST_NAMES",
        "TEST_SELECT",
        "TEST_SKIP",
        "TEST_ENV",
    }
)


def _load_config() -> dict[str, Any]:
    """Load booley.toml + tests.toml and derive public config, memoized.

    No filesystem I/O runs until the first call. When the Booley Directory or TOMLs
    are absent (inside Docker without the project mounted, ``bwave --help``, or
    MCP schema discovery), falls back to empty config so that callers which do
    not actually need the contents of the Booley Directory keep working; callers that do get empty
    defaults (``rtl_project`` name, human-in-the-loop on, no tests).

    ``tests.toml`` holds verification-intent (a ``(Target, plusarg)`` pair) keyed
    by Target name — not design-description, which lives in the ``.core``
    (ADR 0022 decisions 15/16).
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    toml: dict = {}
    tests_toml: dict = {}
    try:
        booley_dir = resolve_booley_dir()
        project_toml_path = resolve_toml(booley_dir)
        root_toml_path = resolve_toml(_PIPELINE_DIR)
        toml_path = project_toml_path if project_toml_path.exists() else root_toml_path
        with toml_path.open("rb") as f:
            toml = tomllib.load(f)
        from booley.config.eda import EdaConfigError, parse_eda_config
        from booley.config.flow_enablement import retired_config_error

        migration = retired_config_error(toml)
        if migration:
            raise EdaConfigError(migration)
        parse_eda_config(toml.get("eda"))
        from booley.config.protected_inputs import parse_protected_inputs

        parse_protected_inputs(toml)
    except (FileNotFoundError, OSError, SourceCheckoutProjectError) as exc:
        _logger.debug("booley.toml not found -- using empty config: %s", exc)
        booley_dir = None

    if booley_dir is not None:
        tests_toml_path = booley_dir / "tests.toml"
        if tests_toml_path.exists():
            with tests_toml_path.open("rb") as f:
                tests_toml = tomllib.load(f)

    project_name = toml.get("project", {}).get("name", "rtl_project")
    normalized_tests = normalize_tests_toml(tests_toml) if tests_toml else {}

    _CONFIG_CACHE = {
        # [project]
        "PROJECT_NAME": project_name,
        "ENV_PREFIX": f"BOOLEY_{project_name.upper().replace('-', '_')}",
        # tests.toml verification-intent (decision 16): {target: [test_names]}
        # and {target: select_template}. Single mutable dict instances so that
        # tests may ``monkeypatch.setitem`` them.
        "TEST_NAMES": _field_map(normalized_tests, "tests"),
        "TEST_SELECT": _field_map(normalized_tests, "select"),
        # Per-target known-hang / known-fail skip list (tests.toml ``skip``).
        # Lets a project exclude tests that burn the full wall-clock budget
        # without a per-call ``--test`` filter dance. {target: [test_names]}.
        "TEST_SKIP": _field_map(normalized_tests, "skip"),
        # Per-Target environment injected into the simulator process (tests.toml
        # ``env``). Env-parameterized testbenches — a cocotb module branching on
        # ``os.getenv("FLAVOR")`` — are a common upstream pattern with no other
        # Booley home: pre_run_commands run in their own shell. {target: {NAME: VALUE}}.
        "TEST_ENV": _field_map(normalized_tests, "env"),
    }
    return _CONFIG_CACHE


class TargetFieldMap(dict[str, Any]):
    """One ``tests.toml`` field per section key, remembering every section key.

    A Target owns exactly one section; choosing it per field could mix sections
    and hide duplicate keys, so the key comes from ``section_keys``.
    """

    section_keys: frozenset[str] = frozenset()


def _field_map(sections: Mapping[str, Mapping[str, Any]], field: str) -> TargetFieldMap:
    """The *field* of every section that declares it, carrying all section keys."""
    fields = TargetFieldMap({key: body[field] for key, body in sections.items() if field in body})
    fields.section_keys = frozenset(sections)
    return fields


def _get(name: str) -> Any:
    """Return a lazy config value, honoring a test-injected module override.

    ``mock.patch("booley.config.project_config.TEST_NAMES", ...)`` sets a real module
    attribute; prefer it so patched and unpatched reads agree between internal
    callers and external ones (``project_config.X``).
    """
    if name in globals():
        return globals()[name]
    return _load_config()[name]


def __getattr__(name: str) -> Any:
    """PEP 562 lazy accessor for config-derived module constants."""
    if name in _LAZY_NAMES:
        return _get(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def project_name(work_dir: Path | None = None) -> str:
    """Return the Project name for an explicit checkout, or the active Project."""
    if work_dir is None:
        return str(_get("PROJECT_NAME"))
    try:
        booley_dir = resolve_checkout_booley_dir(work_dir)
        path = resolve_toml(booley_dir)
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except FileNotFoundError:
        return "rtl_project"
    except (OSError, tomllib.TOMLDecodeError) as exc:
        _logger.warning("could not read Project name for checkout %s: %s", work_dir, exc)
        return "rtl_project"
    project = as_dict(document.get("project"), default={}) or {}
    return as_str(project.get("name"), "rtl_project") or "rtl_project"


# ---------------------------------------------------------------------------
# Data from [flows.synth]
# ---------------------------------------------------------------------------

TEST_LISTS_TABLE = "test_lists"


def normalize_configs_toml(raw: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Resolve configs.toml-local test list references.

    ``[test_lists]`` is a reserved top-level table. Config sections may keep
    using inline ``tests = [...]`` or reference one shared list with
    ``test_list = "name"``.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("configs.toml must contain TOML tables")

    test_lists = _normalize_test_lists(raw.get(TEST_LISTS_TABLE, {}))
    return {
        config_name: _normalize_config_section(config_name, section, test_lists)
        for config_name, section in raw.items()
        if config_name != TEST_LISTS_TABLE
    }


def _normalize_test_lists(
    raw_test_lists: Any,
    *,
    source: str = "configs.toml",
) -> dict[str, list[str]]:
    if raw_test_lists is None:
        raw_test_lists = {}
    if not isinstance(raw_test_lists, Mapping):
        raise ValueError(f"{source} [test_lists] must be a table")

    test_lists: dict[str, list[str]] = {}
    for list_name, tests in raw_test_lists.items():
        if not isinstance(list_name, str) or not list_name.strip():
            raise ValueError(f"{source} [test_lists] names must be non-empty strings")
        if not is_str_list(tests):
            raise ValueError(f"{source} [test_lists].{list_name} must be list[str]")
        test_lists[list_name] = list(tests)
    return test_lists


def _normalize_config_section(
    config_name: str,
    section: Any,
    test_lists: Mapping[str, list[str]],
) -> dict[str, Any]:
    if not isinstance(section, Mapping):
        raise ValueError(f"configs.toml [{config_name}] must be a table")
    normalized = dict(section)
    list_ref = normalized.pop("test_list", None)
    if list_ref is None:
        return normalized
    if "tests" in normalized:
        raise ValueError(f"configs.toml [{config_name}] must not set both tests and test_list")
    if not isinstance(list_ref, str) or not list_ref.strip():
        raise ValueError(f"configs.toml [{config_name}].test_list must be a non-empty string")
    if list_ref not in test_lists:
        raise ValueError(
            f"configs.toml [{config_name}].test_list references unknown test list '{list_ref}'"
        )
    normalized["tests"] = list(test_lists[list_ref])
    return normalized


# ---------------------------------------------------------------------------
# tests.toml — Booley-owned per-Target test registry (ADR 0022 decision 16)
# ---------------------------------------------------------------------------

# Default per-Target test selector. Preserves the legacy ``+test_id=N`` TB
# contract for Targets that don't declare their own ``select`` template, so the
# migration is behaviour-preserving by default.
DEFAULT_TEST_SELECT = "+test_id={index}"


_SELECT_FIELDS = ("index", "name")


def _render_select_probe(target: str, template: str) -> str:
    """Render *template* once, refusing fields that do not select a Test.

    The fields are read with the format parser: an escaped ``{{name}}`` is
    literal text, would render the same argument for every Test, and is
    refused like a template without any field.
    """
    unknown = ValueError(
        f"tests.toml [{target}].select template {template!r} uses an unknown "
        "field; only {index} and {name} are supported"
    )
    try:
        fields = template_fields(template)
        probe = template.format(index=0, name="probe")
    except (KeyError, IndexError, ValueError) as exc:
        raise unknown from exc
    if any(name not in _SELECT_FIELDS or conversion for name, _spec, conversion in fields):
        raise unknown
    if not fields:
        raise ValueError(
            f"tests.toml [{target}].select template {template!r} selects no Test: "
            "it must contain the {index} or the {name} field"
        )
    return probe


def _validate_select_template(target: str, template: Any) -> str:
    """Validate a tests.toml ``select`` runtime-arg template (decision 16).

    The selector is verification-intent rendered by Booley, not a ``.core``
    parameter.  A template must be **exactly one option token, no embedded
    whitespace**, in one of two forms:

    * a plusarg — leading ``+`` — consumed by the TB's ``$value$plusargs``
      runtime, e.g. ``"+test_id={index}"`` / ``"+test={name}"``; or
    * a getopt argument — leading ``-`` / ``--`` — forwarded verbatim to the sim
      binary's own ``main`` (SETUP-7), e.g. a CPU core's
      ``"--meminit=ram,{name}"`` that no plusarg can express.

    The ``{index}`` (0-based) and ``{name}`` fields are the only substitutions;
    any other ``{field}`` is rejected here rather than at render time. The
    single-token rule keeps this verification-intent, not a free-form flag
    string.  Returns the template unchanged when valid; raises ``ValueError``.
    """
    if not isinstance(template, str) or not template.strip():
        raise ValueError(f"tests.toml [{target}].select must be a non-empty string")
    probe = _render_select_probe(target, template)
    tokens = probe.split()
    if len(tokens) != 1 or not tokens[0].startswith(("+", "-")):
        raise ValueError(
            f"tests.toml [{target}].select must be exactly one option token — a "
            f"plusarg ('+…') or a getopt argument ('-…' / '--…') with no embedded "
            f"whitespace, got {template!r}"
        )
    problem = argument_token_problem(probe)
    if problem:
        raise ValueError(f"tests.toml [{target}].select template {template!r} {problem}")
    return template


def normalize_tests_toml(raw: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Normalize tests.toml — the per-Target test registry (decisions 15/16).

    Each Target section may declare an ordered ``tests = [...]`` list (or
    reference a shared ``[test_lists]`` entry via ``test_list = "name"``, the
    same mechanism configs.toml used) plus an optional single-token ``select``
    option template, an optional ``skip = [...]`` known-hang exclusion list, and an
    optional ``env = { NAME = "value" }`` table injected into the simulator
    process. ``[test_lists]`` is a reserved top-level table.

    A section holds only those keys, and every Test name obeys the Test Run
    name rules (ADR 0072); anything else raises ``ValueError`` naming the
    section, so no declared configuration is silently ignored.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("tests.toml must contain TOML tables")
    test_lists = _normalize_test_lists(raw.get(TEST_LISTS_TABLE, {}), source="tests.toml")
    for list_name, names in test_lists.items():
        _validate_test_names(f"[{TEST_LISTS_TABLE}].{list_name}", names)
    return {
        target: _normalize_tests_section(target, section, test_lists)
        for target, section in raw.items()
        if target != TEST_LISTS_TABLE
    }


def lookup_target_section(sections: Mapping[str, Any], identity: str) -> Any:
    """The section of Target *identity* (``vlnv#name``), or None.

    A qualified key naming the Target wins over the bare key equal to its name,
    whatever the key order; two keys naming it raise ``TargetSectionConflictError``.
    Field maps pick the key among every section key, so all fields of a Target
    come from one section. A bare *identity* (no ``#``) matches only the bare key.
    """
    keys: Iterable[str] = (
        sections.section_keys if isinstance(sections, TargetFieldMap) else sections
    )
    key = target_section_key(keys, identity)
    return None if key is None else sections.get(key)


def load_test_configuration(work_dir: Path | str) -> dict[str, dict[str, Any]]:
    """Load ``tests.toml`` for one explicit checkout without using the CWD cache.

    The explicit checkout is authoritative. Compatibility constants remain
    available through ``_get`` for callers that intentionally use the active
    Project, but cannot influence a root-scoped read.
    """
    try:
        booley_dir = resolve_checkout_booley_dir(Path(work_dir))
        with (booley_dir / "tests.toml").open("rb") as stream:
            return normalize_tests_toml(tomllib.load(stream))
    except FileNotFoundError:
        return {}


def load_test_configuration_field(
    work_dir: Path | str,
    field: str,
) -> TargetFieldMap:
    """Project-root-scoped mapping for one normalized ``tests.toml`` field."""
    return _field_map(load_test_configuration(work_dir), field)


# Every key a Target section of tests.toml may hold. The set is closed: a key
# the Simulation Flow would not apply must fail to load rather than be ignored.
TESTS_SECTION_KEYS = (
    "tests",
    "test_list",
    "skip",
    "select",
    "env",
    "seed",
    "default_seed",
    "variants",
)


def _validate_test_names(where: str, names: list[str]) -> None:
    """Refuse a Test name that breaks the Test Run name rules (ADR 0072)."""
    for name in names:
        try:
            validate_test_or_variant_name(name, kind="Test")
        except TestRunNameError as exc:
            raise ValueError(f"tests.toml {where}: {exc}") from exc


def _validate_tests_section_keys(target: str, section: Mapping[str, Any]) -> None:
    """Refuse a Target section key outside :data:`TESTS_SECTION_KEYS`."""
    for key in section:
        if key not in TESTS_SECTION_KEYS:
            raise ValueError(
                f"tests.toml [{target}] has unknown key {key!r}; a Target section "
                f"holds only: {', '.join(TESTS_SECTION_KEYS)}"
            )


def _validate_unique_tests(where: str, names: list[str]) -> None:
    """Refuse a Test named twice in one Target's ``tests`` or ``skip`` list."""
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ValueError(
                f"tests.toml {where} names Test {name!r} more than once; each "
                "Test name must be unique within a Target"
            )
        seen.add(name)


def _resolve_test_list(
    target: str,
    normalized: dict[str, Any],
    test_lists: Mapping[str, list[str]],
) -> str | None:
    """Replace ``test_list`` by the shared list's ``tests``; return the list name."""
    list_ref = normalized.pop("test_list", None)
    if list_ref is None:
        return None
    if "tests" in normalized:
        raise ValueError(f"tests.toml [{target}] must not set both tests and test_list")
    if not isinstance(list_ref, str) or not list_ref.strip():
        raise ValueError(f"tests.toml [{target}].test_list must be a non-empty string")
    if list_ref not in test_lists:
        raise ValueError(
            f"tests.toml [{target}].test_list references unknown test list '{list_ref}'"
        )
    normalized["tests"] = list(test_lists[list_ref])
    return list_ref


def _normalize_tests_section(
    target: str,
    section: Any,
    test_lists: Mapping[str, list[str]],
) -> dict[str, Any]:
    if not isinstance(section, Mapping):
        raise ValueError(f"tests.toml [{target}] must be a table")
    _validate_tests_section_keys(target, section)
    normalized = dict(section)
    list_ref = _resolve_test_list(target, normalized, test_lists)
    # Say where a Test name was written: a shared list is not the section's own key.
    tests_where = (
        f"[{target}].tests"
        if list_ref is None
        else f"[{target}].test_list = {list_ref!r} ([{TEST_LISTS_TABLE}].{list_ref})"
    )

    if "tests" in normalized and not is_str_list(normalized["tests"]):
        raise ValueError(f"tests.toml [{target}].tests must be list[str]")
    if "skip" in normalized and not is_str_list(normalized["skip"]):
        raise ValueError(f"tests.toml [{target}].skip must be list[str]")
    if "tests" in normalized:
        _validate_test_names(tests_where, normalized["tests"])
        _validate_unique_tests(tests_where, normalized["tests"])
    if "skip" in normalized:
        _validate_test_names(f"[{target}].skip", normalized["skip"])
        _validate_unique_tests(f"[{target}].skip", normalized["skip"])
    if "select" in normalized:
        normalized["select"] = _validate_select_template(target, normalized["select"])
    if "env" in normalized:
        # Verification-intent, like ``select``: an env-parameterized testbench
        # (a cocotb module reading ``os.getenv("FLAVOR")``) declares here what
        # the simulator process must see. ``EnvTableError`` is a ``ValueError``.
        normalized["env"] = validate_declared_env_names(
            validate_env_table(normalized["env"], where=f"tests.toml [{target}].env"),
            where=f"tests.toml [{target}].env",
        )
    _normalize_seed_keys(target, normalized)
    if "variants" in normalized:
        normalized["variants"] = _validate_variants(target, normalized)
    return normalized


def _normalize_seed_keys(target: str, normalized: dict[str, Any]) -> None:
    """Validate the ``seed`` argument template and ``default_seed`` (ADR 0072)."""
    if "seed" in normalized:
        normalized["seed"] = validate_seed_template(
            normalized["seed"], where=f"tests.toml [{target}].seed"
        )
        select_key = _select_option_key(normalized)
        if option_key(normalized["seed"].format(seed=DEFAULT_SEED)) == select_key:
            raise ValueError(
                f"tests.toml [{target}].seed sets the same option as the select "
                f"template ({select_key}); the seed needs its own option"
            )
    if "default_seed" in normalized:
        normalized["default_seed"] = validate_seed(
            normalized["default_seed"], where=f"tests.toml [{target}].default_seed"
        )


def _select_option_key(normalized: Mapping[str, Any]) -> str:
    """The option the Target's effective ``select`` template sets."""
    template = normalized.get("select", DEFAULT_TEST_SELECT)
    return option_key(template.format(index=0, name="probe"))


def _reserved_option_keys(normalized: Mapping[str, Any]) -> dict[str, str]:
    """Options a Test Variant may not set, each with the reason."""
    reserved = {
        _select_option_key(normalized): "the select template sets it",
        VERILATOR_SEED_OPTION: "Booley passes the Verilator seed option itself",
    }
    if "seed" in normalized:
        seed_key = option_key(normalized["seed"].format(seed=DEFAULT_SEED))
        reserved[seed_key] = "the seed template sets it"
    return reserved


def _validate_variants(target: str, normalized: Mapping[str, Any]) -> dict[str, Any]:
    """Validate every Test's Variant table against the rest of the section.

    Returns the tables as written (plain TOML data, keyed by Test); readers
    rebuild the typed form with ``parse_test_variants``. A Variant belongs to a
    Test the Target runs, may not repeat the ``select`` or seed option, and may
    not set an environment variable the Target's own ``env`` table sets: the
    Target owns that value, and Target ``env`` is a build input.
    """
    where = f"tests.toml [{target}].variants"
    tables = as_dict(normalized["variants"])
    if tables is None:
        raise ValueError(f"{where} must be a table of Test names")
    tests = normalized.get("tests") or []
    reserved = _reserved_option_keys(normalized)
    target_env = normalized.get("env") or {}
    for test, raw in tables.items():
        if test not in tests:
            raise ValueError(
                f"{where}.{test} declares Test Variants for {test!r}, which is not "
                f"one of the Target's tests ({', '.join(tests) or 'none declared'})"
            )
        table = parse_test_variants(test, raw, where=f"{where}.{test}")
        for variant in table.variants:
            _validate_variant_inputs(
                f"{where}.{test}.{variant.name}", variant, reserved, target_env
            )
    return dict(tables)


def _validate_variant_inputs(
    where: str,
    variant: TestVariant,
    reserved: Mapping[str, str],
    target_env: Mapping[str, str],
) -> None:
    """Refuse a Variant argument or variable that something else already owns."""
    for argument in variant.args:
        reason = reserved.get(option_key(argument))
        if reason is not None:
            raise ValueError(f"{where} argument {argument!r} is not allowed: {reason}")
    for name, _value in variant.env:
        if name in target_env:
            raise ValueError(
                f"{where}.env may not set {name}: the Target's env table already "
                "sets it, and the Target owns that value"
            )


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------
# TEST_NAMES ({target: [test_names]}) and TEST_SELECT ({target: select_template})
# are derived from tests.toml in ``_load_config`` and exposed lazily via
# ``__getattr__``. Internal callers read them through ``_get``.
