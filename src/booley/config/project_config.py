"""Project configuration — single source of truth loaded from project/ TOMLs.

Shared by simulation, synthesis, Flows, MCP tools, and pipeline scripts. Any config-related
change should happen in booley.toml and configs.toml in the project dir.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Load booley.toml (project dir first, fallback to .booley/ root)
# ---------------------------------------------------------------------------
import logging as _logging
import tomllib
from collections.abc import Mapping
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
from booley.core.boundary import as_dict, as_str, is_str_list
from booley.core.checkout_role import SourceCheckoutProjectError
from booley.core.config_paths import resolve_toml
from booley.core.project_dir import resolve_checkout_project_dir, resolve_project_dir

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

    No filesystem I/O runs until the first call. When the project dir or TOMLs
    are absent (inside Docker without the project mounted, ``bwave --help``, or
    MCP schema discovery), falls back to empty config so that callers which do
    not actually need project data keep working; callers that do get empty
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
        project_dir = resolve_project_dir()
        project_toml_path = resolve_toml(project_dir)
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
    except (FileNotFoundError, OSError, SourceCheckoutProjectError) as exc:
        _logger.debug("booley.toml not found -- using empty config: %s", exc)
        project_dir = None

    if project_dir is not None:
        tests_toml_path = project_dir / "tests.toml"
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
        "TEST_NAMES": {
            name: section["tests"]
            for name, section in normalized_tests.items()
            if "tests" in section
        },
        "TEST_SELECT": {
            name: section["select"]
            for name, section in normalized_tests.items()
            if "select" in section
        },
        # Per-target known-hang / known-fail skip list (tests.toml ``skip``).
        # Lets a project exclude tests that burn the full wall-clock budget
        # without a per-call ``--test`` filter dance. {target: [test_names]}.
        "TEST_SKIP": {
            name: section["skip"]
            for name, section in normalized_tests.items()
            if "skip" in section
        },
        # Per-Target environment injected into the simulator process (tests.toml
        # ``env``). Env-parameterized testbenches — a cocotb module branching on
        # ``os.getenv("FLAVOR")`` — are a common upstream pattern with no other
        # Booley home: pre_run_commands run in their own shell. {target: {NAME: VALUE}}.
        "TEST_ENV": {
            name: section["env"] for name, section in normalized_tests.items() if "env" in section
        },
    }
    return _CONFIG_CACHE


def _get(name: str) -> Any:
    """Return a lazy config value, honoring a test-injected module override.

    ``mock.patch("booley.config.project_config.TEST_NAMES", ...)`` sets a real module
    attribute; prefer it so patched and unpatched reads agree between internal
    callers (``resolve_test_name``) and external ones (``project_config.X``).
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
        project_dir = resolve_checkout_project_dir(work_dir)
        path = resolve_toml(project_dir)
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


def bare_target(target: str) -> str:
    """Strip a ``vlnv#name`` qualifier down to the bare Target name."""
    return target.rsplit("#", 1)[-1]


def lookup_target_section(sections: Mapping[str, Any], target: str) -> Any:
    """Find ``target``'s section, tolerating VLNV qualification on either side.

    A Target is named either bare (``sim``) or VLNV-qualified
    (``booley:ibex:port#sim``), and the two forms must not diverge: a
    multi-core repo may pass a qualified ``--target`` to disambiguate, while
    the tests.toml template and the scaffold both emit a bare section. Matching
    only on the literal string silently returns "no tests declared" — the test
    runs uninitialized instead of erroring, which is the worst possible
    failure mode. ``resolve_target`` already strips the qualifier for FuseSoC;
    this is the same normalization for the test registry.

    Exact match wins so a repo that deliberately declares two qualified
    sections sharing a bare name keeps them distinct.
    """
    if target in sections:
        return sections[target]
    bare = bare_target(target)
    for key, section in sections.items():
        if bare_target(key) == bare:
            return section
    return None


def load_test_configuration(work_dir: Path | str) -> dict[str, dict[str, Any]]:
    """Load ``tests.toml`` for one explicit checkout without using the CWD cache.

    The explicit checkout is authoritative. Compatibility constants remain
    available through ``_get`` for callers that intentionally use the active
    Project, but cannot influence a root-scoped read.
    """
    try:
        project_dir = resolve_checkout_project_dir(Path(work_dir))
        with (project_dir / "tests.toml").open("rb") as stream:
            return normalize_tests_toml(tomllib.load(stream))
    except FileNotFoundError:
        return {}


def load_test_configuration_field(
    work_dir: Path | str,
    field: str,
) -> dict[str, Any]:
    """Project-root-scoped mapping for one normalized ``tests.toml`` field."""
    return {
        target: section[field]
        for target, section in load_test_configuration(work_dir).items()
        if field in section
    }


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


def resolve_test_name(target: str, name: str) -> int:
    """Resolve a test name (or substring) to a numeric test index.

    Args:
        target: Target name (key in TEST_NAMES, e.g. "config_a", "config_b").
        name: Test name or substring (e.g. "my_test", "smoke").

    Returns:
        0-based test index.

    Raises:
        ValueError: If no match, multiple matches, or Target has no test map.
    """
    test_names = _get("TEST_NAMES")
    tests = lookup_target_section(test_names, target)
    if tests is None:
        raise ValueError(
            f"No test names defined for target '{target}'. "
            f"Targets with tests: {', '.join(sorted(test_names))}"
        )
    matches = [(i, t) for i, t in enumerate(tests) if name in t]

    if len(matches) == 0:
        raise ValueError(
            f"No test matching '{name}' for target '{target}'. "
            f"Available tests: {', '.join(f'{i}:{t}' for i, t in enumerate(tests))}"
        )
    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous test name '{name}' for target '{target}'. "
            f"Matches: {', '.join(f'{i}:{t}' for i, t in matches)}"
        )

    return matches[0][0]
