"""Guards for the Renovate runner and its repository configuration (#2996, tracker #2551).

Renovate lists every pinned dependency on its Dependency Dashboard and holds each
update for approval. These tests prove the parts that need no GitHub: custom managers
extract the pins on the real files, built-in managers see every pinned file, manual rows
stay approval-only in groups of their own, shared pins resolve to one group, release age
reaches only datasources with release timestamps, and the runner never exposes its token.

Renovate is the authority on real matching: the lookup run recorded on #2996
(``renovate --platform=local --dry-run=lookup``) re-checks these claims against the pinned
image. The rule resolver below is deliberately small: last match wins, exact names and
paths only, and an unknown ``match*`` key fails loudly.
"""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).parents[2]
_CONFIG_PATH = _ROOT / ".github/renovate.json"
_WORKFLOW_PATH = _ROOT / ".github/workflows/renovate.yml"
_DOCKER_DIR = "src/booley/data/docker"
_DOCKERFILE_BASE = f"{_DOCKER_DIR}/Dockerfile.base"
_BASE_REQUIREMENTS = f"{_DOCKER_DIR}/base-python-requirements.txt"
_AGENT_CLIS = f"{_DOCKER_DIR}/agent-clis-package.json"
_TEST_WORKFLOW = ".github/workflows/test.yml"
_RISCV = f"{_DOCKER_DIR}/Dockerfile.riscv"
_REAPER = f"{_DOCKER_DIR}/Dockerfile.reaper"
_DEEP_TESTS = ".github/workflows/deep-tests.yml"
_DEMO_CONTRACT = ".github/contracts/picorv32-demo.toml"
_VENDORED_MANIFEST = "crates/bwave/vendor/fst-writer/Cargo.toml"

# Directories that hold the pins Renovate tracks outside Python and Rust manifests.
_PIN_ROOTS = (
    ".github/actions",
    ".github/ci",
    ".github/contracts",
    ".github/workflows",
    _DOCKER_DIR,
)

# Datasources whose lookups report release timestamps (spike #2552, Renovate 44.219.0).
# Docker, git-refs and github-digest report none: release age would hold them forever.
_TIMESTAMPED = frozenset(
    {"github-releases", "github-tags", "pypi", "npm", "crate", "node-version", "custom.openvsx"}
)

_ANNOTATION = re.compile(r"# renovate: (?P<tokens>.+)")
_ARG = re.compile(r"ARG (?P<name>\w+)=(?P<value>\S+)")
_COMPANION = re.compile(r"# pin-companion: \w+")
_FROM = re.compile(r"^FROM (?P<image>\S+)", re.MULTILINE)
_SHA256_IMAGE = re.compile(
    r"^renovate/renovate:(?P<version>\d+\.\d+\.\d+)@(?P<digest>sha256:[0-9a-f]{64})$"
)


def _config() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    return loaded


def _rules() -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = _config()["packageRules"]
    return rules


def _text(relative: str) -> str:
    return (_ROOT / relative).read_text(encoding="utf-8")


def _python_regex(renovate_pattern: str) -> str:
    """Translate RE2 named groups (``(?<n>``) to Python's ``(?P<n>`` form."""
    return re.sub(r"\(\?<(?![=!])", "(?P<", renovate_pattern)


def _file_pattern_matches(patterns: list[str], path: str) -> bool:
    """Return whether any ``/regex/`` manager file pattern matches ``path``."""
    return any(re.search(_python_regex(pattern.strip("/")), path) for pattern in patterns)


def _glob_matches(globs: list[str], path: str) -> bool:
    """Return whether a minimatch-style glob (``*``, ``**``, ``?``) matches the whole path."""
    for glob in globs:
        escaped = re.escape(glob).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*")
        if re.fullmatch(escaped.replace(r"\*", "[^/]*").replace(r"\?", "[^/]"), path):
            return True
    return False


def _pin_files() -> list[str]:
    """Repository-relative POSIX paths of every file under the pin roots."""
    return sorted(
        path.relative_to(_ROOT).as_posix()
        for root in _PIN_ROOTS
        for path in (_ROOT / root).rglob("*")
        if path.is_file()
    )


@dataclass(frozen=True)
class _Dep:
    """A dependency as Renovate would report it, reduced to what the rules select on."""

    name: str
    file: str
    datasource: str
    manager: str
    dep_type: str = ""


def _rule_matches(rule: dict[str, Any], dep: _Dep) -> bool:
    """Evaluate one rule: every ``match*`` key must hold (they are ANDed)."""
    exact = {
        "matchDepNames": dep.name,
        "matchPackageNames": dep.name,
        "matchDatasources": dep.datasource,
        "matchDepTypes": dep.dep_type,
        "matchManagers": dep.manager,
    }
    for key, wanted in rule.items():
        if key == "matchFileNames":
            held = _glob_matches(wanted, dep.file)
        elif key.startswith("match"):
            assert key in exact, f"the test resolver does not model {key}"
            held = exact[key] in wanted
        else:
            continue
        if not held:
            return False
    return True


def _resolve(dep: _Dep) -> dict[str, Any]:
    """Merge the top-level hold and every matching rule, later rules winning."""
    resolved: dict[str, Any] = {
        "dependencyDashboardApproval": _config().get("dependencyDashboardApproval", False)
    }
    for rule in _rules():
        if _rule_matches(rule, dep):
            resolved.update(
                {k: v for k, v in rule.items() if not k.startswith("match") and k != "description"}
            )
    return resolved


@dataclass(frozen=True)
class _Extracted:
    """One dependency produced by a custom regex manager."""

    file: str
    dep_name: str
    datasource: str
    current_value: str | None
    current_digest: str | None
    package_name: str | None
    match: str


def _extract_custom() -> list[_Extracted]:
    """Run every custom manager's match strings over the files its patterns select."""
    extracted: list[_Extracted] = []
    for manager in _config()["customManagers"]:
        assert manager["customType"] == "regex"
        for path in _pin_files():
            if not _file_pattern_matches(manager["managerFilePatterns"], path):
                continue
            text = _text(path)
            for match_string in manager["matchStrings"]:
                for found in re.finditer(_python_regex(match_string), text):
                    groups = found.groupdict()
                    dep_name = groups.get("depName") or manager["depNameTemplate"]
                    package = groups.get("packageName") or manager.get("packageNameTemplate")
                    if package is not None:
                        package = package.replace("{{{depName}}}", dep_name)
                    extracted.append(
                        _Extracted(
                            file=path,
                            dep_name=dep_name,
                            datasource=groups.get("datasource") or manager["datasourceTemplate"],
                            current_value=groups.get("currentValue")
                            or manager.get("currentValueTemplate"),
                            current_digest=groups.get("currentDigest"),
                            package_name=package,
                            match=found.group(0),
                        )
                    )
    return extracted


def _annotated_pins(dockerfile: str) -> list[tuple[str, str, str | None, str | None]]:
    """Independent oracle: ``(datasource, depName, value, digest)`` per annotation.

    Reads the line shape documented in ``tests/docker/test_dockerfile_pins.py``:
    annotation, then the ``ARG``, then an optional ``# pin-companion`` and its
    ``*_REF`` commit.
    """
    lines = _text(dockerfile).splitlines()
    pins: list[tuple[str, str, str | None, str | None]] = []
    for index, line in enumerate(lines):
        annotation = _ANNOTATION.fullmatch(line)
        if annotation is None:
            continue
        tokens = dict(token.split("=", 1) for token in annotation["tokens"].split(" "))
        arg = _ARG.fullmatch(lines[index + 1])
        assert arg is not None, f"{dockerfile}:{index + 2}: annotation without an ARG"
        value: str | None = arg["value"]
        digest: str | None = None
        if "currentValue" in tokens:
            value, digest = tokens["currentValue"], arg["value"]
        elif _COMPANION.fullmatch(lines[index + 2]):
            companion = _ARG.fullmatch(lines[index + 3])
            if companion is not None and companion["name"].endswith("_REF"):
                digest = companion["value"]
        pins.append((tokens["datasource"], tokens["depName"], value, digest))
    return pins


def test_custom_managers_extract_every_tracked_pin() -> None:
    """AC1: each pin the Dashboard must list is extracted, annotations exactly once."""
    extracted = _extract_custom()

    # Every Dockerfile annotation: once, with its version and its commit digest.
    for dockerfile in sorted(Path(_ROOT / _DOCKER_DIR).glob("Dockerfile*")):
        relative = dockerfile.relative_to(_ROOT).as_posix()
        expected = sorted(_annotated_pins(relative), key=str)
        got = sorted(
            (
                (e.datasource, e.dep_name, e.current_value, e.current_digest)
                for e in extracted
                if e.file == relative and e.match.startswith("# renovate:")
            ),
            key=str,
        )
        assert got == expected, relative
    assert _annotated_pins(_DOCKERFILE_BASE), "Dockerfile.base lost every pin annotation"
    assert _annotated_pins(_RISCV), "Dockerfile.riscv lost its pins"
    assert len({(e.file, e.dep_name) for e in extracted}) == len(
        {(e.file, e.dep_name, e.match) for e in extracted}
    ), "two custom managers extract the same dependency from one file"


def test_custom_managers_extract_ci_and_contract_pins() -> None:
    """AC1: OpenROAD, CI tool pins, the demo contract and the Renovate image are extracted."""
    by_key = {(e.file, e.dep_name): e for e in _extract_custom()}

    # OpenROAD release label: a manual row, listed through its own manager.
    openroad = by_key[(_DOCKERFILE_BASE, "The-OpenROAD-Project/OpenROAD")]
    assert f"ARG OPENROAD_VERSION={openroad.current_value}\n" in _text(_DOCKERFILE_BASE)

    # cargo-fuzz and actionlint: the version is the one in the file, and actionlint's
    # bare version appears twice inside the match so auto-replace rewrites both.
    fuzz = by_key[(_DEEP_TESTS, "cargo-fuzz")]
    assert f"cargo install cargo-fuzz --version {fuzz.current_value} " in _text(_DEEP_TESTS)
    actionlint = by_key[(_TEST_WORKFLOW, "rhysd/actionlint")]
    assert actionlint.current_value is not None
    assert actionlint.match.count(actionlint.current_value) == 2

    # Demo contract refs: both repositories, each with the commit the file pins.
    contract = tomllib.loads(_text(_DEMO_CONTRACT))
    for repository, ref in (("upstream", "upstream_ref"), ("project", "project_ref")):
        demo = by_key[(_DEMO_CONTRACT, contract[f"{repository}_repository"])]
        assert demo.datasource == "git-refs"
        assert demo.current_digest == contract[ref]
        assert demo.package_name == f"https://github.com/{demo.dep_name}"

    # The Renovate image the runner pins is tracked by version and digest.
    image = _SHA256_IMAGE.fullmatch(_renovate_steps()[1]["env"]["IMAGE"])
    assert image is not None
    renovate = by_key[(".github/workflows/renovate.yml", "renovate/renovate")]
    assert (renovate.datasource, renovate.current_value, renovate.current_digest) == (
        "docker",
        image["version"],
        image["digest"],
    )


def test_built_in_managers_see_every_pinned_file() -> None:
    """AC1: requirement files, the agent CLI manifest and the vendored crate are visible."""
    config = _config()

    # Renovate's default pip pattern misses requirements-runners-release.txt, so the
    # configured pattern must cover every CI requirement file by itself.
    configured = config["pip_requirements"]["managerFilePatterns"]
    requirement_files = sorted(
        path.relative_to(_ROOT).as_posix()
        for path in (_ROOT / ".github/ci").glob("requirements-*.txt")
    )
    assert ".github/ci/requirements-runners-release.txt" in requirement_files
    for path in requirement_files:
        assert _file_pattern_matches(configured, path), f"{path} is not a pip_requirements file"

    assert _file_pattern_matches(config["npm"]["managerFilePatterns"], _AGENT_CLIS)
    assert (_ROOT / _AGENT_CLIS).is_file()

    # Vendored upstream code is listed (approval-only); tests and fixtures stay out.
    ignored = config["ignorePaths"]
    assert "**/vendor/**" not in ignored
    for path in (*_pin_files(), "pyproject.toml", "crates/bwave/Cargo.toml", _VENDORED_MANIFEST):
        assert not _glob_matches(ignored, path), f"{path} must stay visible to Renovate"
    for path in ("tests/docker/Dockerfile.fixture", "src/booley/runtime/dockerfile_syntax.py"):
        assert _glob_matches(ignored, path), f"{path} must be ignored"

    # Local build-stage parents are not registry images.
    local_parents = {
        image
        for path in (_ROOT / _DOCKER_DIR).glob("Dockerfile*")
        for image in _FROM.findall(path.read_text(encoding="utf-8"))
        if "@" not in image
    }
    assert local_parents == set(config["ignoreDeps"])


def _manual_rows() -> dict[str, list[_Dep]]:
    """Manual rows by the group each must resolve to, built from the files they live in."""
    contract = tomllib.loads(_text(_DEMO_CONTRACT))
    vendored = tomllib.loads(_text(_VENDORED_MANIFEST))
    return {
        "openroad": [
            _Dep("The-OpenROAD-Project/OpenROAD", _DOCKERFILE_BASE, "github-tags", "regex"),
            *(
                _Dep(image.split("@")[0], _DOCKERFILE_BASE, "docker", "dockerfile", "stage")
                for image in _FROM.findall(_text(_DOCKERFILE_BASE))
                if "/openroad/" in image
            ),
        ],
        "bwave-vendor": [
            _Dep(name, _VENDORED_MANIFEST, "crate", "cargo", "dependencies")
            for name in vendored["dependencies"]
        ],
        "picorv32-demo": [
            _Dep(contract["upstream_repository"], _DEMO_CONTRACT, "git-refs", "regex"),
            _Dep(contract["project_repository"], _DEMO_CONTRACT, "git-refs", "regex"),
        ],
        "ci-python": [
            _Dep("python", _DEEP_TESTS, "github-releases", "github-actions", "uses-with")
        ],
        "ci-rust-nightly": [
            _Dep("rust", _DEEP_TESTS, "rust-version", "github-actions", "uses-with")
        ],
    }


# Representative dependencies of every normal group (their expected group is the key).
def _normal_rows() -> dict[str, list[_Dep]]:
    return {
        "ci-tooling": [
            _Dep("actions/checkout", _TEST_WORKFLOW, "github-tags", "github-actions", "action"),
            _Dep("build", ".github/ci/requirements-build.txt", "pypi", "pip_requirements"),
            _Dep("rhysd/actionlint", _TEST_WORKFLOW, "github-releases", "regex"),
            _Dep("cargo-fuzz", _DEEP_TESTS, "crate", "regex"),
            _Dep("renovate/renovate", ".github/workflows/renovate.yml", "docker", "regex"),
        ],
        "dev-python": [
            _Dep("ruff", "pyproject.toml", "pypi", "poetry", "project.optional-dependencies"),
            _Dep("pytest", ".github/ci/requirements-runners.txt", "pypi", "pip_requirements"),
        ],
        "sandbox-base": [
            _Dep("click", "pyproject.toml", "pypi", "poetry", "project.dependencies"),
            _Dep("numpy", _BASE_REQUIREMENTS, "pypi", "pip_requirements"),
            _Dep("node", _DOCKERFILE_BASE, "node-version", "regex"),
            _Dep("lramseyer/vaporview", _DOCKERFILE_BASE, "custom.openvsx", "regex"),
            _Dep("@openai/codex", _AGENT_CLIS, "npm", "npm", "dependencies"),
        ],
        "riscv": [_Dep("riscv/riscv-isa-manual", _RISCV, "github-releases", "regex")],
        "rust": [
            _Dep("clap", "crates/bwave/Cargo.toml", "crate", "cargo", "dependencies"),
            _Dep("rust", f"{_DOCKER_DIR}/Dockerfile", "docker", "dockerfile", "stage"),
        ],
        "website": [_Dep("mkdocs", "website/requirements.txt", "pypi", "pip_requirements")],
        "sidecars": [_Dep("python", _REAPER, "docker", "dockerfile", "final")],
    }


def test_manual_rows_stay_approval_only() -> None:
    """AC1: manual rows are listed, approval-only, in groups no normal update shares."""
    manual = _manual_rows()
    for group, deps in manual.items():
        assert deps, f"no dependency models the {group} row"
        for dep in deps:
            resolved = _resolve(dep)
            assert resolved["dependencyDashboardApproval"] is True, dep
            assert resolved["groupName"] == group, dep
            assert resolved["schedule"] == ["at any time"], dep

    # Reading the files proves the rows still exist where the rules expect them.
    deep_tests = _text(_DEEP_TESTS)
    assert re.search(
        r"uses: dtolnay/rust-toolchain@\S+[^\n]*\n\s+with:\n\s+toolchain: nightly-\d{4}-\d{2}-\d{2}\n",
        deep_tests,
    ), "the rust nightly toolchain is no longer an input the github-actions manager extracts"

    openroad = [_resolve(dep) for dep in manual["openroad"]]
    assert all("ready-for-human" in resolved["addLabels"] for resolved in openroad)

    # No normal dependency lands in a manual group, and the manual rules come last
    # so a later rule can neither unset approval nor pull a manual row into a normal group.
    for group, deps in _normal_rows().items():
        for dep in deps:
            resolved = _resolve(dep)
            assert resolved["groupName"] == group, dep
            assert resolved["groupName"] not in manual, dep
    rules = _rules()
    last_normal = max(i for i, r in enumerate(rules) if r.get("groupName") in _normal_rows())
    manual_indexes = [i for i, r in enumerate(rules) if r.get("groupName") in manual]
    assert manual_indexes and min(manual_indexes) > last_normal
    for index in manual_indexes:
        assert rules[index]["dependencyDashboardApproval"] is True
    assert set(manual) == {rules[i]["groupName"] for i in manual_indexes}


def _pins(text: str) -> dict[str, str]:
    """Map name to version for the exact ``name==version`` lines of a requirements file."""
    pins: dict[str, str] = {}
    for line in text.splitlines():
        found = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", line.strip())
        if found:
            pins[found[1].lower()] = found[2]
    return pins


def _pyproject_names(table: str) -> set[str]:
    """Names declared under ``project.dependencies`` or ``project.optional-dependencies``."""
    project = tomllib.loads(_text("pyproject.toml"))["project"]
    if table == "project.dependencies":
        items: list[str] = project["dependencies"]
    else:
        items = [item for extra in project["optional-dependencies"].values() for item in extra]
    return {_requirement_name(item) for item in items}


def _requirement_name(requirement: str) -> str:
    """Return the lower-cased distribution name of a PEP 508 requirement string."""
    return re.split(r"[^A-Za-z0-9._-]", requirement, maxsplit=1)[0].lower()


def test_shared_pins_share_one_group() -> None:
    """AC1: a version pinned in several files moves in one PR."""
    # CI requirement pins that pyproject extras also declare.
    extras = _pyproject_names("project.optional-dependencies")
    shared_ci: set[str] = set()
    for path in sorted((_ROOT / ".github/ci").glob("requirements-*.txt")):
        relative = path.relative_to(_ROOT).as_posix()
        for name in _pins(path.read_text(encoding="utf-8")):
            if name in extras:
                shared_ci.add(name)
                ci = _resolve(_Dep(name, relative, "pypi", "pip_requirements"))
                dev = _Dep(
                    name, "pyproject.toml", "pypi", "poetry", "project.optional-dependencies"
                )
                assert ci["groupName"] == _resolve(dev)["groupName"] == "dev-python", name
    assert {"pytest", "pytest-asyncio", "pytest-timeout", "cryptography"} <= shared_ci

    # Runtime pins that the base image repeats (edalize, fusesoc).
    runtime = _pyproject_names("project.dependencies")
    shared_runtime = {name for name in _pins(_text(_BASE_REQUIREMENTS)) if name in runtime}
    assert {"edalize", "fusesoc"} <= shared_runtime
    for name in shared_runtime:
        base = _resolve(_Dep(name, _BASE_REQUIREMENTS, "pypi", "pip_requirements"))
        project = _resolve(_Dep(name, "pyproject.toml", "pypi", "poetry", "project.dependencies"))
        assert base["groupName"] == project["groupName"] == "sandbox-base", name

    # The Ubuntu parent image is pinned in both Dockerfiles and held equal by a test.
    for dockerfile in (_DOCKERFILE_BASE, _RISCV):
        assert any(
            i.startswith("docker.io/library/ubuntu") for i in _FROM.findall(_text(dockerfile))
        )
        dep = _Dep("docker.io/library/ubuntu", dockerfile, "docker", "dockerfile", "stage")
        assert _resolve(dep)["groupName"] == "sandbox-base", dockerfile


def test_release_age_applies_only_to_timestamped_datasources() -> None:
    """AC4: no update waits on a release age its source cannot report."""
    config = _config()
    assert "minimumReleaseAge" not in config
    assert "minimumReleaseAgeBehaviour" not in config
    aged = [rule for rule in _rules() if "minimumReleaseAge" in rule]
    for rule in aged:
        assert rule["matchDatasources"], "a release-age rule must name its datasources"
        assert set(rule["matchDatasources"]) <= _TIMESTAMPED, rule["description"]

    untimestamped = [
        _Dep("docker.io/library/ubuntu", _DOCKERFILE_BASE, "docker", "dockerfile", "stage"),
        _Dep("python", _REAPER, "docker", "dockerfile", "final"),
        _Dep("riscv-isa-sim", _RISCV, "git-refs", "regex"),
        _Dep("YosysHQ/picorv32", _DEMO_CONTRACT, "git-refs", "regex"),
        _Dep("dtolnay/rust-toolchain", _DEEP_TESTS, "github-digest", "github-actions", "action"),
    ]
    for dep in untimestamped:
        assert "minimumReleaseAge" not in _resolve(dep), dep

    timestamped = [
        _Dep("pytest", "pyproject.toml", "pypi", "poetry", "project.optional-dependencies"),
        _Dep("YosysHQ/yosys", _DOCKERFILE_BASE, "github-tags", "regex"),
        _Dep("zachjs/sv2v", _DOCKERFILE_BASE, "github-releases", "regex"),
        _Dep("cargo-fuzz", _DEEP_TESTS, "crate", "regex"),
        _Dep("node", _DOCKERFILE_BASE, "node-version", "regex"),
        _Dep("lramseyer/vaporview", _DOCKERFILE_BASE, "custom.openvsx", "regex"),
    ]
    for dep in timestamped:
        assert _resolve(dep)["minimumReleaseAge"] == "3 days", dep

    agent_cli = _Dep("@openai/codex", _AGENT_CLIS, "npm", "npm", "dependencies")
    assert _resolve(agent_cli)["minimumReleaseAge"] == "1 day"


def _workflow() -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(_WORKFLOW_PATH.read_text(encoding="utf-8"))
    return loaded


def _renovate_steps() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the App-token step and the Renovate step."""
    steps: list[dict[str, Any]] = _workflow()["jobs"]["renovate"]["steps"]
    token_step = next(step for step in steps if step.get("id") == "app-token")
    run_step = next(step for step in steps if "run" in step)
    return token_step, run_step


def _allowed_guard_emails() -> set[str]:
    """Author identities the confidential-content workflow trusts, unescaped from globs."""
    text = _text(".github/workflows/confidential-content.yml")
    emails: set[str] = set()
    for block in re.findall(r"BOOLEY_LEAK_GUARD_ALLOWED_AUTHORS: \|\n((?:\s+\S+\n)+)", text):
        for line in block.split():
            emails.add(line.replace("[[]", "[").replace("[]]", "]"))
    return emails


def _docker_env_names(command: str) -> list[str]:
    """Names passed as ``--env NAME`` (no value) in a ``docker run`` command."""
    tokens = shlex.split(command)
    assert tokens[:3] == ["docker", "run", "--rm"], tokens[:3]
    names: list[str] = []
    rest = tokens[3:]
    while rest and rest[0] == "--env":
        names.append(rest[1])
        rest = rest[2:]
    assert rest == ["${IMAGE}"], f"unexpected docker arguments: {rest}"
    return names


def _assert_runner_exposes_nothing() -> None:
    """The workflow runs on schedule or dispatch only and passes its token nowhere else."""
    workflow = _workflow()
    triggers: dict[str, Any] = workflow[True]  # PyYAML (YAML 1.1) parses a bare `on` as True
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert workflow["permissions"] == {}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert list(workflow["jobs"]) == ["renovate"]
    job = workflow["jobs"]["renovate"]
    assert job["environment"] == "renovate"
    assert not any("actions/checkout" in str(step.get("uses", "")) for step in job["steps"])

    token_step, run_step = _renovate_steps()
    text = _WORKFLOW_PATH.read_text(encoding="utf-8")
    assert re.fullmatch(r"actions/create-github-app-token@[0-9a-f]{40}", token_step["uses"])
    assert text.count("steps.app-token.outputs.token") == 1  # only the Renovate step's env
    assert "github.event" not in text  # no attacker-controlled expression reaches a script
    assert text.count("${{ secrets.") == 1  # only the App private key, only to the token step

    env = run_step["env"]
    passed = _docker_env_names(run_step["run"])
    assert sorted(passed) == sorted(
        k for k in env if k.startswith("RENOVATE_") or k == "LOG_LEVEL"
    )
    assert "RENOVATE_TOKEN" in passed and "${{" not in run_step["run"]
    assert env["RENOVATE_ONBOARDING"] == "false"
    assert env["RENOVATE_REQUIRE_CONFIG"] == "required"

    # The commit identity is the one the content guard trusts.
    author = re.fullmatch(r"(?P<login>[^<]+) <(?P<email>[^>]+)>", env["RENOVATE_GIT_AUTHOR"])
    assert author is not None
    assert author["login"] == env["RENOVATE_USERNAME"] == "booley-renovate[bot]"
    allowed = _allowed_guard_emails()
    assert author["email"] in allowed
    assert "noreply@github.com" not in allowed


def _assert_config_holds_every_update() -> None:
    """Nothing updates unapproved, automerges, or commits through the platform."""
    config = _config()
    assert config["dependencyDashboardApproval"] is True
    # Lock-file maintenance runs for Cargo only (the one manager with lock files) and
    # waits for approval of its own.
    assert config["lockFileMaintenance"]["enabled"] is False
    cargo_maintenance = config["cargo"]["lockFileMaintenance"]
    assert cargo_maintenance["enabled"] is True
    assert cargo_maintenance["dependencyDashboardApproval"] is True
    assert config["major"]["dependencyDashboardApproval"] is True
    assert config["automerge"] is False
    assert config["platformCommit"] == "disabled"
    mergify = yaml.safe_load(_text(".mergify.yml"))
    assert config["stopUpdatingLabel"] == mergify["merge_queue"]["queued_label"]
    assert config["vulnerabilityAlerts"]["enabled"] is False
    assert "urgent" in config["vulnerabilityAlerts"]["labels"]
    for rule in _rules():
        assert rule.get("dependencyDashboardApproval", True) is True, rule["description"]
        assert rule.get("automerge", False) is False, rule["description"]


def test_runner_and_config_hold_every_update() -> None:
    """AC2 (pre-merge part): the runner cannot leak its token and nothing updates unapproved."""
    _assert_runner_exposes_nothing()
    _assert_config_holds_every_update()
