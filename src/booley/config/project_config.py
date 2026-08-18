"""Project configuration — single source of truth loaded from project/ TOMLs.

Shared by simulation, synthesis, Flows, MCP tools, and pipeline scripts. Any config-related
change should happen in booley.toml and configs.toml in the project dir.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Load booley.toml (project dir first, fallback to .booley/ root)
# ---------------------------------------------------------------------------
import logging as _logging
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.core.boundary import is_str_list
from booley.core.config_paths import resolve_toml
from booley.runtime.project_dir import resolve_project_dir

_logger = _logging.getLogger(__name__)

_SCRIPT_DIR = Path(__file__).parent.resolve()
_PIPELINE_DIR = _SCRIPT_DIR.parent


# ---------------------------------------------------------------------------
# Lazy, import-safe config loading (Issue #1)
# ---------------------------------------------------------------------------
# Config is loaded on *first access*, never at import time, so that importing
# any Flow, MCP-tool, or harness module performs zero filesystem I/O. Import-time I/O here
# was the root cause that forced ~265 defensive function-local imports across
# the repo. Derived constants (PROJECT_NAME, ENV_PREFIX, HUMAN_IN_THE_LOOP,
# TEST_NAMES, TEST_SELECT) are memoized in ``_CONFIG_CACHE`` and surfaced as
# module attributes via PEP 562 ``__getattr__``.

_CONFIG_CACHE: dict[str, Any] | None = None

# Names resolved lazily from project config. Exposed via ``__getattr__`` and
# read internally via ``_get`` (which honors test monkeypatches).
_LAZY_NAMES = frozenset(
    {
        "PROJECT_NAME",
        "ENV_PREFIX",
        "HUMAN_IN_THE_LOOP",
        "RUN_REPORT",
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
        from booley.eda.config import EdaConfigError, parse_eda_config, retired_config_error

        migration = retired_config_error(toml)
        if migration:
            raise EdaConfigError(migration)
        parse_eda_config(toml.get("eda"))
    except (FileNotFoundError, OSError) as exc:
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
        # [developer] — whether a human operator is available to unblock the
        # agent. When True (default), the Developer Agent blocks on
        # missing/ambiguous spec and spec reviewers enforce strict grounding.
        # When False (benchmarks / unattended runs), the Developer Agent
        # resolves spec-silent points itself and reviewers tolerate
        # documented readings.
        "HUMAN_IN_THE_LOOP": bool(toml.get("developer", {}).get("human_in_the_loop", True)),
        # [developer] — whether ticket runs must end with a structured run
        # report (REPORT.md via submit_run_report). True (default) keeps the
        # report as a hard exit condition for the Developer Agent. False lets
        # projects that don't consume the reports (benchmarks, bulk unattended
        # runs) skip the report step and its per-run token cost entirely.
        "RUN_REPORT": bool(toml.get("developer", {}).get("run_report", True)),
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


def is_human_in_loop(work_dir: Path | None = None) -> bool:
    """Return the human_in_the_loop policy, resolved from booley.toml.

    Args:
        work_dir: Optional override for project discovery. When provided,
            looks for ``work_dir/.booley_project/booley.toml`` (and falls
            back to legacy ``pipeline.toml``). When ``None``, returns the
            value lazily loaded from the resolved project.

    Returns:
        True (default) when the flag is absent or unreadable -- safe-by-
        default behavior preserves the human-in-the-loop assumption.
    """
    if work_dir is None:
        return _get("HUMAN_IN_THE_LOOP")

    for candidate in (".booley_project/booley.toml", ".booley_project/pipeline.toml"):
        toml_path = work_dir / candidate
        if not toml_path.is_file():
            continue
        try:
            with toml_path.open("rb") as f:
                cfg = tomllib.load(f)
            return bool(cfg.get("developer", {}).get("human_in_the_loop", True))
        except (OSError, tomllib.TOMLDecodeError, TypeError):
            break
    # Caller explicitly scoped to work_dir; don't leak the module-cached value
    # loaded from CWD (which may belong to an entirely different project).
    return True


def is_run_report_enabled() -> bool:
    """Return the run_report policy, resolved from booley.toml.

    Returns:
        True (default) when the flag is absent or unreadable -- the run
        report stays a mandatory exit condition unless a project opts out
        with ``[developer] run_report = false``.
    """
    return _get("RUN_REPORT")


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
    try:
        probe = template.format(index=0, name="probe")
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(
            f"tests.toml [{target}].select template {template!r} uses an unknown "
            "field; only {index} and {name} are supported"
        ) from exc
    tokens = probe.split()
    if len(tokens) != 1 or not tokens[0].startswith(("+", "-")):
        raise ValueError(
            f"tests.toml [{target}].select must be exactly one option token — a "
            f"plusarg ('+…') or a getopt argument ('-…' / '--…') with no embedded "
            f"whitespace, got {template!r}"
        )
    return template


def normalize_tests_toml(raw: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Normalize tests.toml — the per-Target test registry (decisions 15/16).

    Each Target section may declare an ordered ``tests = [...]`` list (or
    reference a shared ``[test_lists]`` entry via ``test_list = "name"``, the
    same mechanism configs.toml used) plus an optional ``select`` plusarg
    template, an optional ``skip = [...]`` known-hang exclusion list, and an
    optional ``env = { NAME = "value" }`` table injected into the simulator
    process. ``[test_lists]`` is a reserved top-level table.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("tests.toml must contain TOML tables")
    test_lists = _normalize_test_lists(raw.get(TEST_LISTS_TABLE, {}), source="tests.toml")
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
    multi-core repo has to qualify ``[flows.*].default_target`` to disambiguate, while
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


def _normalize_tests_section(
    target: str,
    section: Any,
    test_lists: Mapping[str, list[str]],
) -> dict[str, Any]:
    if not isinstance(section, Mapping):
        raise ValueError(f"tests.toml [{target}] must be a table")
    normalized = dict(section)

    list_ref = normalized.pop("test_list", None)
    if list_ref is not None:
        if "tests" in normalized:
            raise ValueError(f"tests.toml [{target}] must not set both tests and test_list")
        if not isinstance(list_ref, str) or not list_ref.strip():
            raise ValueError(f"tests.toml [{target}].test_list must be a non-empty string")
        if list_ref not in test_lists:
            raise ValueError(
                f"tests.toml [{target}].test_list references unknown test list '{list_ref}'"
            )
        normalized["tests"] = list(test_lists[list_ref])

    if "tests" in normalized and not is_str_list(normalized["tests"]):
        raise ValueError(f"tests.toml [{target}].tests must be list[str]")
    if "skip" in normalized and not is_str_list(normalized["skip"]):
        raise ValueError(f"tests.toml [{target}].skip must be list[str]")
    if "select" in normalized:
        normalized["select"] = _validate_select_template(target, normalized["select"])
    if "env" in normalized:
        normalized["env"] = _validate_env_table(target, normalized["env"])
    return normalized


# Environment variable names are conventionally ``[A-Za-z_][A-Za-z0-9_]*``;
# anything else cannot be exported by a POSIX shell, so reject it at the config
# boundary rather than emitting a shell line that silently fails to parse.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_env_table(target: str, raw: Any) -> dict[str, str]:
    """Validate a tests.toml ``env`` table — the per-Target simulator env.

    Verification-intent, like ``select``: an env-parameterized testbench (a
    cocotb module reading ``os.getenv("FLAVOR")``) declares here what the
    simulator process must see, instead of the port being forced to rewrite a
    testbench it does not own. Values must be strings — a TOML integer would
    invite ``FLAVOR = 1`` to mean something different from ``"1"`` on the shell
    side — and names must be shell-exportable identifiers.
    """
    if not isinstance(raw, Mapping):
        raise ValueError(f'tests.toml [{target}].env must be a table of NAME = "value" pairs')
    env: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not _ENV_NAME_RE.match(name):
            raise ValueError(
                f"tests.toml [{target}].env key {name!r} is not a valid environment "
                "variable name ([A-Za-z_][A-Za-z0-9_]*)"
            )
        if not isinstance(value, str):
            raise ValueError(
                f"tests.toml [{target}].env.{name} must be a string — quote the "
                f"value (got {type(value).__name__})"
            )
        env[name] = value
    return env


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


def render_test_selector(target: str, index: int, name: str) -> str:
    """Render the per-Target plusarg that selects test ``index`` (decision 16).

    Booley renders the Target's ``select`` template (from tests.toml) into the
    concrete plusarg forwarded to the sim — e.g. ``"+test_id={index}"`` →
    ``"+test_id=3"``.  Targets without a declared template use
    :data:`DEFAULT_TEST_SELECT`, preserving the legacy ``+test_id=N`` contract.
    The returned string is the full plusarg, leading ``+`` included.
    """
    template = lookup_target_section(_get("TEST_SELECT"), target)
    if template is None:
        template = DEFAULT_TEST_SELECT
    return template.format(index=index, name=name)
