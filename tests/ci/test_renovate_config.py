"""Guards for the Renovate runner and its repository configuration (#2996, tracker #2551).

Renovate lists every pinned dependency on its Dependency Dashboard; majors, Cargo
lock-file maintenance and the manual rows wait there for approval. These tests prove the parts that need no GitHub: custom managers
extract the pins on the real files, built-in managers see every pinned file, manual rows
stay approval-only in groups of their own, shared pins resolve to one group, release age
reaches only datasources with release timestamps, and the runner never exposes its token.

Renovate is the authority on real matching: the lookup run recorded on #2996
(``renovate --platform=local --dry-run=lookup``) re-checks these claims against the pinned
image. The rule resolver below is deliberately small: last match wins, exact names and
paths only, and an unknown ``match*`` key fails loudly (``prBodyNotes`` is the one
option it merges, as Renovate does).

Part 2 (#2998) adds the literal copies of each pin (docs, contract probes, source
constants): a custom manager rewrites each one in the same Renovate PR as its canonical
pin, joins the same branch, and the provenance comments no rewrite can reach are listed in
the PR body.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / ".github/scripts"))

from ci_changes import CONDITIONAL_JOBS

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
_EDA_DOC = "docs/user/SUPPORTED-EDA-TOOLS.md"
_SESSION_CONTRACT = ".github/contracts/session-runtime.toml"
_TROUBLESHOOTING = "docs/user/TROUBLESHOOTING.md"
_VERILATOR_IDENTITY = "src/booley/flows/sim/verilator_identity.py"
_DEVCONTAINER = "src/booley/runtime/devcontainer.py"
_VAPORVIEW_MODULE = "src/booley/runtime/vaporview.py"

# Dependency names exactly as the canonical pins (and so the copy managers) report them.
_VERILATOR = "verilator/verilator"
_YOSYS = "YosysHQ/yosys"
_ICARUS = "steveicarus/iverilog"
_SV2V = "zachjs/sv2v"
_VERIBLE = "chipsalliance/verible"
_XPACK = "xpack-dev-tools/riscv-none-elf-gcc-xpack"
_SPIKE = "riscv-isa-sim"
_VAPORVIEW = "lramseyer/vaporview"
_HDL = "mshr-h/veriloghdl"
_LIVE_SERVER = "ms-vscode/live-server"
_UBUNTU = "docker.io/library/ubuntu"
_OPENROAD_BUILDER = "docker.io/openroad/ubuntu26.04"
_OPENROAD_EDA_BUILDER = "docker.io/openroad/ubuntu26.04-dev"
_ACTIONLINT = "rhysd/actionlint"

# Every literal copy of a canonical pin that Renovate must rewrite with it: (file,
# dependency, number of places). A reworded line or a new copy fails the extraction test
# below until this table and renovate.json agree.
_COPIES: tuple[tuple[str, str, int], ...] = (
    (_SESSION_CONTRACT, _VERILATOR, 1),
    (_EDA_DOC, _VERILATOR, 1),
    (_VERILATOR_IDENTITY, _VERILATOR, 1),
    (_EDA_DOC, _YOSYS, 1),
    (_EDA_DOC, _ICARUS, 1),
    (_EDA_DOC, _SV2V, 1),
    (_EDA_DOC, _VERIBLE, 1),
    (_EDA_DOC, "fusesoc", 1),
    (_EDA_DOC, "edalize", 1),
    (_EDA_DOC, "cocotb", 1),
    (_SESSION_CONTRACT, "cocotb", 1),
    (_EDA_DOC, "cocotbext-axi", 1),
    (_EDA_DOC, "cocotbext-uart", 1),
    (_EDA_DOC, "numpy", 1),
    (_EDA_DOC, _XPACK, 1),
    (_SESSION_CONTRACT, _XPACK, 1),
    (_EDA_DOC, _SPIKE, 1),
    (_VAPORVIEW_MODULE, _VAPORVIEW, 1),
    (_TROUBLESHOOTING, _VAPORVIEW, 3),
    (_DEVCONTAINER, _HDL, 1),
    (_SESSION_CONTRACT, _HDL, 1),
    (_TROUBLESHOOTING, _HDL, 1),
    (_DEVCONTAINER, _LIVE_SERVER, 1),
    (_SESSION_CONTRACT, _LIVE_SERVER, 1),
    (_TROUBLESHOOTING, _LIVE_SERVER, 1),
    (_EDA_DOC, _UBUNTU, 1),
    (_DOCKERFILE_BASE, _UBUNTU, 1),
    (_DOCKERFILE_BASE, _OPENROAD_BUILDER, 1),
    (_DOCKERFILE_BASE, _OPENROAD_EDA_BUILDER, 1),
)
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
    """Merge every matching rule over the default (no hold), later rules winning.

    ``prBodyNotes`` is the exception: Renovate merges it (``mergeable``), so every
    matching rule's notes are kept, in rule order.
    """
    resolved: dict[str, Any] = {"dependencyDashboardApproval": False}
    notes: list[str] = []
    for rule in _rules():
        if _rule_matches(rule, dep):
            notes.extend(rule.get("prBodyNotes", []))
            resolved.update(
                {
                    k: v
                    for k, v in rule.items()
                    if not k.startswith("match") and k not in ("description", "prBodyNotes")
                }
            )
    if notes:
        resolved["prBodyNotes"] = notes
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
    span: tuple[int, int]
    versioning: str | None
    extract_version: str | None


def _copy_files() -> list[str]:
    """Files holding literal copies, in the order the table lists them, once each."""
    return list(dict.fromkeys(file for file, _dep, _count in _COPIES))


def _manager_universe() -> list[str]:
    """Files Renovate would show a manager: the pin roots plus the copy files, minus ignored."""
    ignored = _config()["ignorePaths"]
    paths = dict.fromkeys([*_pin_files(), *_copy_files()])
    return [path for path in paths if not _glob_matches(ignored, path)]


def _extract_custom() -> list[_Extracted]:
    """Run every custom manager's match strings over the files its patterns select."""
    extracted: list[_Extracted] = []
    universe = _manager_universe()
    for manager in _config()["customManagers"]:
        assert manager["customType"] == "regex"
        for path in universe:
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
                            span=found.span(),
                            versioning=groups.get("versioning")
                            or manager.get("versioningTemplate"),
                            extract_version=groups.get("extractVersion")
                            or manager.get("extractVersionTemplate"),
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
    # Renovate applies each upgrade by finding its matched text in the file, so two
    # matches that share a character would make the second upgrade fail.
    by_file: dict[str, list[_Extracted]] = {}
    for item in extracted:
        by_file.setdefault(item.file, []).append(item)
    for file, items in by_file.items():
        spans = sorted(item.span for item in items)
        for (_, end), (start, _) in pairwise(spans):
            assert end <= start, f"{file}: two custom matches overlap near offset {start}"


def test_custom_managers_extract_ci_and_contract_pins() -> None:
    """AC1: OpenROAD, CI tool pins, the demo contract and the Renovate image are extracted."""
    by_key = {(e.file, e.dep_name): e for e in _extract_custom()}

    # OpenROAD release label: a manual row, listed through its own manager.
    openroad = by_key[(_DOCKERFILE_BASE, "The-OpenROAD-Project/OpenROAD")]
    assert f"ARG OPENROAD_VERSION={openroad.current_value}\n" in _text(_DOCKERFILE_BASE)

    # cargo-fuzz and actionlint: the version is the one in the file, and actionlint's
    # step name and both URL occurrences sit inside one match so auto-replace rewrites all.
    fuzz = by_key[(_DEEP_TESTS, "cargo-fuzz")]
    assert f"cargo install cargo-fuzz --version {fuzz.current_value} " in _text(_DEEP_TESTS)
    actionlint = by_key[(_TEST_WORKFLOW, "rhysd/actionlint")]
    assert actionlint.current_value is not None
    assert actionlint.match.startswith(f"Install actionlint v{actionlint.current_value}")
    assert actionlint.match.count(actionlint.current_value) == 3

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
    """Return the PEP 503 normalized distribution name of a PEP 508 requirement string."""
    name = re.split(r"[^A-Za-z0-9._-]", requirement, maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


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

    # pyslang is an extra in pyproject.toml and a base-image pin: one PR moves both.
    assert "pyslang" in _pins(_text(_BASE_REQUIREMENTS))
    base = _resolve(_Dep("pyslang", _BASE_REQUIREMENTS, "pypi", "pip_requirements"))
    extra = _resolve(
        _Dep("pyslang", "pyproject.toml", "pypi", "poetry", "project.optional-dependencies")
    )
    assert base["groupName"] == extra["groupName"] == "sandbox-base"

    # The Ubuntu parent image is pinned in both Dockerfiles and held equal by a test.
    for dockerfile in (_DOCKERFILE_BASE, _RISCV):
        assert any(
            i.startswith("docker.io/library/ubuntu") for i in _FROM.findall(_text(dockerfile))
        )
        dep = _Dep("docker.io/library/ubuntu", dockerfile, "docker", "dockerfile", "stage")
        assert _resolve(dep)["groupName"] == "sandbox-base", dockerfile


def _repeated_pins(path: str, text: str) -> list[str]:
    """Name each distribution that a manifest writes more than once.

    The Python managers (Renovate 44.219.0) keep one dependency per name per file, so a
    second copy of a pin in the same file is never rewritten and goes stale (#3475).
    ``pyproject.toml`` counts the runtime dependencies and every extra; a self-reference
    (``booley-rtl[extra]``) is not a pin and is asserted to carry no version. A
    requirements file counts its ``name==version`` lines.
    """
    names: list[str] = []
    if path.endswith("pyproject.toml"):
        project = tomllib.loads(text)["project"]
        own_name = _requirement_name(project["name"])
        declared = list(project.get("dependencies", ()))
        for extra in project.get("optional-dependencies", {}).values():
            declared.extend(extra)
        for requirement in declared:
            name = _requirement_name(requirement)
            if name == own_name:
                assert re.fullmatch(rf"{re.escape(project['name'])}\[[\w,.-]+\]", requirement), (
                    f"{path}: self-reference {requirement!r} must name extras only"
                )
                continue
            names.append(name)
    else:
        names = list(_pins_in_order(text))
    return [
        f"{path} writes {name} {count} times"
        for name, count in sorted(Counter(names).items())
        if count > 1
    ]


def _pins_in_order(text: str) -> list[str]:
    """Lower-cased names of every exact ``name==version`` line, repeats kept."""
    return [
        found[1].lower().replace("_", "-")
        for line in text.splitlines()
        if (found := re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", line.strip()))
    ]


_SINGLE_PIN_MANIFESTS = (
    "pyproject.toml",
    *(
        path.relative_to(_ROOT).as_posix()
        for path in sorted((_ROOT / ".github/ci").glob("requirements-*.txt"))
    ),
    f"{_DOCKER_DIR}/base-python-requirements.txt",
    "website/requirements.txt",
)


def test_manifests_write_each_pin_once() -> None:
    """AC4: a pin Renovate updates has no second copy in its file that it would skip.

    Renovate's Python managers extract one dependency per name per file, so the narrow
    extras and ``dev`` must not both spell a pin: ``dev`` refers to the other extras
    (``booley-rtl[test]``) instead. Copies across files stay covered by
    ``test_shared_pins_share_one_group``.
    """
    repeated = [
        message for path in _SINGLE_PIN_MANIFESTS for message in _repeated_pins(path, _text(path))
    ]
    assert repeated == []


def test_repeated_pin_is_reported() -> None:
    """AC4: the guard fails on a repeated pin and ignores a self-reference."""
    project = (
        '[project]\nname = "demo"\nversion = "0"\ndependencies = []\n'
        "[project.optional-dependencies]\n"
        'quality = ["ruff==1", "pyright==2"]\n'
        'dev = ["ruff==1", "pyright==2"]\n'
    )
    assert _repeated_pins("pyproject.toml", project) == [
        "pyproject.toml writes pyright 2 times",
        "pyproject.toml writes ruff 2 times",
    ]
    spelled = project.replace('pyright==2"]\ndev', 'foo_bar==1"]\ndev').replace(
        'dev = ["ruff==1", "pyright==2"]', 'dev = ["foo-bar==1"]'
    )
    assert _repeated_pins("pyproject.toml", spelled) == ["pyproject.toml writes foo-bar 2 times"]
    composed = project.replace('dev = ["ruff==1", "pyright==2"]', 'dev = ["demo[quality]"]')
    assert _repeated_pins("pyproject.toml", composed) == []
    assert _repeated_pins("requirements-x.txt", "a==1\nA==2\nb==3\n") == [
        "requirements-x.txt writes a 2 times"
    ]
    versioned = composed.replace("demo[quality]", "demo[quality]>=1")
    with pytest.raises(AssertionError, match="self-reference"):
        _repeated_pins("pyproject.toml", versioned)


# A version hold: a comment block directly above an exact pin whose text says
# ``hold <version>``. Renovate learns it from a rule with ``allowedVersions`` (#3475).
_HOLD_COMMENT = re.compile(r"\bhold (\d\S*?)\.?(?:\s|$)", re.IGNORECASE)


def _holds(text: str) -> dict[str, str]:
    """Map each held name to its version, read from the comments above pins."""
    holds: dict[str, str] = {}
    block: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            block.append(stripped.lstrip("# ").strip())
            continue
        # A quoted pyproject entry (`"name==1",`) or a bare requirements line (`name==1`).
        pin = re.fullmatch(r'"?([A-Za-z0-9][A-Za-z0-9._-]*)==([^"\s]+?)"?,?', stripped)
        found = _HOLD_COMMENT.search(" ".join(block))
        if found and pin:
            assert pin[2] == found[1], (
                f"{pin[1]} is pinned {pin[2]} but its comment holds {found[1]}"
            )
            holds[pin[1].lower()] = found[1]
        block = []
    return holds


def _held_by_rules(rules: list[dict[str, Any]]) -> dict[str, str]:
    """Map each name a rule restricts with ``allowedVersions`` to the version it holds."""
    held: dict[str, str] = {}
    for rule in rules:
        if "allowedVersions" not in rule:
            continue
        names = rule.get("matchPackageNames", [])
        assert len(names) == 1, f"a hold rule names exactly one package: {rule}"
        allowed = rule["allowedVersions"]
        assert re.fullmatch(r"==\S+", allowed), f"a hold is an exact version, got {allowed!r}"
        assert rule.get("description", "").startswith("Hold:"), f"describe the hold: {names[0]}"
        held[names[0].lower()] = allowed.removeprefix("==")
    return held


def _hold_disagreements(text: str, rules: list[dict[str, Any]]) -> list[str]:
    """Return one sentence per way the manifest holds and the Renovate rules disagree."""
    in_manifest = _holds(text)
    in_rules = _held_by_rules(rules)
    reasons = [
        f"{name} is held only in a comment"
        for name in sorted(in_manifest.keys() - in_rules.keys())
    ]
    reasons += [
        f"{name} is held only in a rule" for name in sorted(in_rules.keys() - in_manifest.keys())
    ]
    reasons += [
        f"{name} is held at {in_manifest[name]} in a comment and {in_rules[name]} in a rule"
        for name in sorted(in_manifest.keys() & in_rules.keys())
        if in_manifest[name] != in_rules[name]
    ]
    return reasons


def test_version_holds_agree_with_renovate_rules() -> None:
    """AC3/AC5: every hold recorded beside a pin is a Renovate restriction, and no other exists."""
    # Every manifest Renovate updates, so a hold in a requirements file is checked too.
    manifests = "\n".join(_text(path) for path in _SINGLE_PIN_MANIFESTS)
    assert _hold_disagreements(manifests, _rules()) == []
    for name in _holds(_text("pyproject.toml")):
        dep = _Dep(name, "pyproject.toml", "pypi", "poetry", "project.optional-dependencies")
        resolved = _resolve(dep)
        assert resolved["allowedVersions"] == f"=={_holds(_text('pyproject.toml'))[name]}", name
        assert resolved["groupName"] == "dev-python", name


_FIXTURE_PIN = '    "coverage==7.17.0",\n'
_FIXTURE_RULE = {
    "description": "Hold: reason.",
    "matchPackageNames": ["coverage"],
    "allowedVersions": "==7.17.0",
}


@pytest.mark.parametrize(
    ("text", "rules", "expected"),
    [
        pytest.param(
            "    # Upstream bug; hold 7.17.0 until fixed.\n" + _FIXTURE_PIN,
            [_FIXTURE_RULE],
            [],
            id="agree",
        ),
        pytest.param(
            "    # Upstream bug; hold 7.17.0 until fixed.\n" + _FIXTURE_PIN,
            [],
            ["coverage is held only in a comment"],
            id="comment-without-rule",
        ),
        pytest.param(
            _FIXTURE_PIN,
            [_FIXTURE_RULE],
            ["coverage is held only in a rule"],
            id="rule-without-comment",
        ),
        pytest.param(
            "    # Upstream bug; hold 7.17.1 until fixed.\n" + _FIXTURE_PIN.replace('0"', '1"'),
            [_FIXTURE_RULE],
            ["coverage is held at 7.17.1 in a comment and 7.17.0 in a rule"],
            id="comment-version-differs-from-rule",
        ),
        pytest.param(
            "# Upstream bug; hold 7.17.0 until fixed.\ncoverage==7.17.0\n",
            [],
            ["coverage is held only in a comment"],
            id="requirements-file-comment-without-rule",
        ),
        pytest.param(
            "    # The pair holds them equal.\n" + _FIXTURE_PIN,
            [],
            [],
            id="holds-equal-is-not-a-hold",
        ),
        pytest.param(
            "    # Upstream bug until fixed, hold 7.17.0.\n" + _FIXTURE_PIN,
            [_FIXTURE_RULE],
            [],
            id="hold-at-end-of-comment",
        ),
    ],
)
def test_hold_disagreement_is_reported(
    text: str, rules: list[dict[str, Any]], expected: list[str]
) -> None:
    """AC5: the comparison fails when the comment and the rule disagree."""
    assert _hold_disagreements(text, rules) == expected


def test_hold_pin_must_match_its_comment() -> None:
    """AC5: a comment that holds one version above a pin of another fails."""
    with pytest.raises(AssertionError, match=r"holds 7\.17\.1"):
        _holds("    # Bug; hold 7.17.1 until fixed.\n" + _FIXTURE_PIN)


@pytest.mark.parametrize(
    "rule",
    [
        {**_FIXTURE_RULE, "matchPackageNames": ["coverage", "ruff"]},
        {**_FIXTURE_RULE, "allowedVersions": "<7.17.1"},
        {**_FIXTURE_RULE, "description": "Keep it."},
    ],
    ids=["two-packages", "range-not-exact", "description-not-hold"],
)
def test_malformed_hold_rule_is_rejected(rule: dict[str, Any]) -> None:
    """AC5: a rule restricts versions only as a documented single-package exact hold."""
    with pytest.raises(AssertionError):
        _held_by_rules([rule])


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
    run_step = next(step for step in steps if step.get("name") == "Run Renovate")
    return token_step, run_step


def _allowed_guard_emails() -> set[str]:
    """Author identities the confidential-content workflow trusts, unescaped from globs."""
    text = _text(".github/workflows/confidential-content.yml")
    emails: set[str] = set()
    for block in re.findall(r"BOOLEY_LEAK_GUARD_ALLOWED_AUTHORS: \|\n((?:\s+\S+\n)+)", text):
        for line in block.split():
            emails.add(line.replace("[[]", "[").replace("[]]", "]"))
    return emails


_CHECKOUT_MOUNT = "${GITHUB_WORKSPACE}:/opt/booley:ro"


def _docker_arguments(command: str) -> tuple[list[str], list[str]]:
    """Names passed as ``--env NAME`` (no value) and ``-v`` mounts of a ``docker run`` command."""
    tokens = shlex.split(command)
    assert tokens[:3] == ["docker", "run", "--rm"], tokens[:3]
    names: list[str] = []
    mounts: list[str] = []
    rest = tokens[3:]
    while rest and rest[0] in {"--env", "-v"}:
        (names if rest[0] == "--env" else mounts).append(rest[1])
        rest = rest[2:]
    assert rest == ["${IMAGE}"], f"unexpected docker arguments: {rest}"
    return names, mounts


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
    # The one checkout is the default-branch commit this workflow runs from. It is mounted
    # read-only for the post-upgrade scripts (#2999) and keeps no credential in its clone.
    [checkout] = [step for step in job["steps"] if "actions/checkout" in str(step.get("uses", ""))]
    assert re.fullmatch(r"actions/checkout@[0-9a-f]{40}", checkout["uses"])
    assert checkout["with"] == {"persist-credentials": False}  # and no `ref` override
    assert job["permissions"] == {"contents": "read"}  # what the checkout's own token needs

    token_step, run_step = _renovate_steps()
    text = _WORKFLOW_PATH.read_text(encoding="utf-8")
    assert re.fullmatch(r"actions/create-github-app-token@[0-9a-f]{40}", token_step["uses"])
    assert text.count("steps.app-token.outputs.token") == 1  # only the Renovate step's env
    assert "github.event" not in text  # no attacker-controlled expression reaches a script
    assert text.count("${{ secrets.") == 1  # only the App private key, only to the token step

    env = run_step["env"]
    passed, mounts = _docker_arguments(run_step["run"])
    assert mounts == [_CHECKOUT_MOUNT]
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


def _assert_config_never_automerges() -> None:
    """Nothing automerges or commits through the platform; the remaining holds stay.

    Majors, Cargo lock-file maintenance and every rule that sets a hold keep it.
    """
    config = _config()
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
    for rule in _rules():
        assert rule.get("dependencyDashboardApproval", True) is True, rule["description"]
        assert rule.get("automerge", False) is False, rule["description"]


def test_runner_exposes_nothing_and_config_never_automerges() -> None:
    """The runner cannot leak its token and nothing automerges after the cutover."""
    _assert_runner_exposes_nothing()
    _assert_config_never_automerges()


def test_cutover_lifts_the_global_hold_and_drops_dependabot() -> None:
    """AC1: no top-level approval hold remains and Dependabot version updates are gone."""
    assert "dependencyDashboardApproval" not in _config()
    assert not (_ROOT / ".github/dependabot.yml").exists()


def test_security_updates_open_immediately_with_priority_label() -> None:
    """AC2: vulnerability fixes are on, unscheduled, unheld, unaged, and labelled urgent."""
    alerts = _config()["vulnerabilityAlerts"]
    assert alerts["enabled"] is True
    assert alerts["schedule"] == []
    assert alerts["dependencyDashboardApproval"] is False
    assert alerts["minimumReleaseAge"] is None
    assert {"dependencies", "urgent"} <= set(alerts["labels"])


_UPDATE_GUIDE = "docs/internals/agents/dependency-updates.md"


def test_dependency_update_guide_is_linked_and_carries_the_checklist() -> None:
    """AC4: AGENTS.md links the guide, which repeats the OpenROAD checklist the PR note emits."""
    assert _UPDATE_GUIDE in _text("AGENTS.md")
    guide = _text(_UPDATE_GUIDE)
    for item in (*_OPENROAD_CHECKLIST_ARGS, *_OPENROAD_CHECKLIST_PATHS):
        assert item in guide, item


# --- Post-upgrade tasks (#2999) ----------------------------------------------------------

_SCRIPTS = "/opt/booley/.github/scripts"


def _post_upgrade_tasks() -> dict[str, Any]:
    tasks: dict[str, Any] = _config()["postUpgradeTasks"]
    return tasks


def _allowed_commands() -> list[str]:
    """The runner's ``RENOVATE_ALLOWED_COMMANDS``: a JSON array of regular expressions."""
    _, run_step = _renovate_steps()
    patterns: list[str] = json.loads(run_step["env"]["RENOVATE_ALLOWED_COMMANDS"])
    return patterns


def _written_files() -> set[str]:
    """Files the post-upgrade scripts write: paired values of each pin, the lock, constraints."""
    import pin_checksums
    import renovate_artifacts

    files = {renovate_artifacts.AGENT_CLI_LOCK, renovate_artifacts.CONSTRAINTS_FILE}
    for pin in pin_checksums.PINS.values():
        values = [*([pin.npm] if pin.npm else [])]
        for download in pin.downloads:
            values += [*download.sha, *download.url_copies]
        if pin.vsix:
            values += [pin.vsix.tag, pin.vsix.commit]
        files |= {value.file for value in values}
    return files


def test_checksum_pins_are_the_upgrades_renovate_extracts() -> None:
    """AC1: the script knows each pin by the name Renovate reports, in the file it reports."""
    import pin_checksums

    extracted = _extract_custom()
    tree = pin_checksums.Tree(_ROOT)
    for dep, pin in pin_checksums.PINS.items():
        value = tree.read(pin.version)
        reported = {
            (found.dep_name, found.file, found.current_value)
            for found in extracted
            if found.dep_name == dep
        }
        assert (dep, pin.version.file, value) in reported, f"{dep} is never extracted as {value}"


def test_post_upgrade_commands_are_allowlisted_default_branch_scripts() -> None:
    """AC4: only anchored, allowlisted commands run, and only default-branch repository code."""
    tasks = _post_upgrade_tasks()
    assert tasks["executionMode"] == "branch"
    patterns = _allowed_commands()
    assert patterns
    for pattern in patterns:
        assert pattern.startswith("^") and pattern.endswith("$"), pattern
    for command in tasks["commands"]:
        # Renovate tests each command against every pattern: exactly one must accept it.
        assert sum(bool(re.search(pattern, command)) for pattern in patterns) == 1, command
        program, script, *_ = shlex.split(command)
        assert program == "python3"
        assert script.startswith(f"{_SCRIPTS}/"), command
        assert (_ROOT / ".github/scripts" / script.removeprefix(f"{_SCRIPTS}/")).is_file()
    # Nothing but the commands is allowed: every pattern accepts one of them.
    assert all(
        any(re.search(pattern, command) for command in tasks["commands"]) for pattern in patterns
    )
    # A suffix, a prefix or a branch-relative script is not accepted.
    for command in tasks["commands"]:
        for tampered in (f"{command} && id", f"; {command}", command.replace("/opt/booley/", "")):
            assert not any(re.search(pattern, tampered) for pattern in patterns), tampered

    # The runner mounts its own credential-less default-branch checkout read-only and gives
    # Renovate no shell to run commands through.
    _, run_step = _renovate_steps()
    assert _docker_arguments(run_step["run"])[1] == [_CHECKOUT_MOUNT]
    assert run_step["env"]["RENOVATE_BINARY_SOURCE"] == "install"
    assert "SHELL_EXECUTOR" not in _WORKFLOW_PATH.read_text(encoding="utf-8")
    assert set(tasks["installTools"]) == {"python", "node", "uv"}
    # Only what the scripts write is committed.
    assert set(tasks["fileFilters"]) == _written_files()


def test_data_file_template_renders_what_the_scripts_read(tmp_path: Path) -> None:
    """The template (triple braces, one entry per upgrade) renders JSON the loader accepts."""
    import renovate_data_file

    template = _post_upgrade_tasks()["dataFileTemplate"]
    shape = re.fullmatch(
        r"(?P<head>.*)\{\{#each upgrades\}\}(?P<item>.*)"
        r"\{\{#unless @last\}\},\{\{/unless\}\}\{\{/each\}\}(?P<tail>.*)",
        template,
    )
    assert shape is not None, template
    upgrades = [
        {"depName": "zachjs/sv2v", "packageFile": "a/Dockerfile.base", "newValue": "v0.0.13"},
        {"depName": "", "packageFile": "Cargo.lock", "newValue": ""},
    ]
    items = []
    for upgrade in upgrades:
        item = shape["item"]
        for name, value in upgrade.items():
            item = item.replace(f"{{{{{{{name}}}}}}}", value)
        items.append(item)
    rendered = shape["head"] + ",".join(items) + shape["tail"]
    assert "{{" not in rendered
    path = tmp_path / "data.json"
    path.write_text(rendered, encoding="utf-8")

    loaded = renovate_data_file.load_upgrades(path)

    assert [(u.dep_name, u.package_file, u.new_value) for u in loaded] == [
        (u["depName"], u["packageFile"], u["newValue"]) for u in upgrades
    ]


# --- Literal copies of canonical pins (#2998) -------------------------------------------

_IMAGE = re.compile(r"(?P<name>[^:@\s]+)(?::(?P<tag>[\w.]+))?@(?P<digest>sha256:[0-9a-f]{64})")


@dataclass(frozen=True)
class _Pin:
    """A canonical pin read straight from its file, independent of the custom managers."""

    file: str
    manager: str
    dep_type: str
    datasource: str
    value: str | None
    digest: str | None
    tokens: dict[str, str]


def _annotation_tokens(dockerfile: str) -> dict[str, dict[str, str]]:
    """Map depName to the ``key=value`` tokens of its ``# renovate:`` annotation."""
    by_dep: dict[str, dict[str, str]] = {}
    for line in _text(dockerfile).splitlines():
        annotation = _ANNOTATION.fullmatch(line)
        if annotation is not None:
            tokens = dict(token.split("=", 1) for token in annotation["tokens"].split(" "))
            by_dep[tokens["depName"]] = tokens
    return by_dep


def _canonical(dep: str) -> _Pin:
    """Find the pin ``dep`` is tracked by: a Dockerfile annotation, a requirement or a ``FROM``."""
    for dockerfile in (_DOCKERFILE_BASE, _RISCV):
        tokens = _annotation_tokens(dockerfile)
        if dep in tokens:
            pin = next(p for p in _annotated_pins(dockerfile) if p[1] == dep)
            return _Pin(dockerfile, "regex", "", pin[0], pin[2], pin[3], tokens[dep])
    requirements = _pins(_text(_BASE_REQUIREMENTS))
    if dep in requirements:
        return _Pin(
            _BASE_REQUIREMENTS, "pip_requirements", "", "pypi", requirements[dep], None, {}
        )
    for image in _FROM.findall(_text(_DOCKERFILE_BASE)):
        reference = _IMAGE.fullmatch(image)
        if reference is not None and reference["name"] == dep:
            return _Pin(
                _DOCKERFILE_BASE,
                "dockerfile",
                "stage",
                "docker",
                reference["tag"],
                reference["digest"],
                {},
            )
    raise AssertionError(f"no canonical pin found for {dep}")


def _copy_value(canonical: str, extract_version: str | None) -> str:
    """Return the value a copy holds: the release tag after the copy's own ``extractVersion``.

    Every tag of the dependencies that use a template starts with ``v``, while the
    canonical pin may omit it (xPack), so the tag is rebuilt before the template runs.
    """
    if extract_version is None:
        return canonical
    tag = canonical if canonical.startswith("v") else f"v{canonical}"
    found = re.fullmatch(_python_regex(extract_version), tag)
    assert found is not None, f"extractVersion {extract_version} rejects the tag {tag}"
    return found["version"]


def _historical_paths() -> list[str]:
    """Dated records that mention old versions on purpose and must never be rewritten."""
    named = (
        "CHANGELOG.md",
        "src/booley/data/refs/CHANGELOG.md",
        "docs/internals/coverage-release-gate.md",
        "qa/shared/coverage/RUNBOOK.md",
        "qa/missions/uart/MISSION.md",
        "qa/missions/uart/evaluator/README.md",
    )
    globbed = [
        path.relative_to(_ROOT).as_posix()
        for pattern in ("docs/adr/*.md", "docs/internals/validation/**/*", "qa/missions/taxi/**/*")
        for path in _ROOT.glob(pattern)
        if path.is_file()
    ]
    assert all((_ROOT / path).is_file() for path in named)
    assert any(path.startswith("docs/adr/") for path in globbed)
    return [*named, *globbed]


def test_every_listed_copy_is_extracted_with_its_canonical_value() -> None:
    """AC1/AC2: each copy is extracted with the canonical value and digest; history is not."""
    extracted = _extract_custom()
    ignored = _config()["ignorePaths"]
    for file, dep, count in _COPIES:
        assert not _glob_matches(ignored, file), f"{file} is ignored by Renovate"
        found = [e for e in extracted if e.file == file and e.dep_name == dep]
        assert len(found) == count, (file, dep, [e.match for e in found])
        pin = _canonical(dep)
        for copy in found:
            assert copy.datasource == pin.datasource, (file, dep)
            expected = None if pin.value is None else _copy_value(pin.value, copy.extract_version)
            assert copy.current_value == expected, (file, dep, copy.match)
            assert copy.current_digest in (None, pin.digest), (file, dep, copy.match)
            if copy.current_value == pin.value:
                # A copy in the canonical form must compare versions the way the pin does.
                assert copy.versioning == pin.tokens.get("versioning"), (file, dep)
                assert copy.extract_version == pin.tokens.get("extractVersion"), (file, dep)
            if "packageName" in pin.tokens:
                assert copy.package_name == pin.tokens["packageName"], (file, dep)

    # Nothing outside the pin files and the listed copies is extracted, and no manager
    # selects a dated record.
    listed = {(file, dep) for file, dep, _count in _COPIES}
    pin_files = set(_pin_files())
    for item in extracted:
        assert item.file in pin_files or (item.file, item.dep_name) in listed, item
    patterns = [p for m in _config()["customManagers"] for p in m["managerFilePatterns"]]
    for path in _historical_paths():
        assert not _file_pattern_matches(patterns, path), f"{path} is a dated record"


def test_no_manager_names_rendered_doc_copies() -> None:
    """The packaged doc copies are Git-ignored build output; no rule may track them (#3658)."""
    config = _config()
    rendered = "src/booley/data/docs/CONFIG.md"
    patterns = [
        pattern
        for manager in config["customManagers"]
        for pattern in manager["managerFilePatterns"]
    ]
    names = [name for rule in config["packageRules"] for name in rule.get("matchFileNames", [])]
    assert not [pattern for pattern in patterns if "data/docs" in pattern]
    assert not [name for name in names if name.startswith("src/booley/data/docs")]
    assert not _file_pattern_matches(patterns, rendered)


def test_copy_match_strings_use_only_re2_syntax() -> None:
    """Match strings compile under RE2 in Renovate: reject lookaround and backreferences."""
    for manager in _config()["customManagers"]:
        for match_string in manager["matchStrings"]:
            assert not re.search(r"\(\?<?[=!]", match_string), match_string
            assert not re.search(r"\\[1-9]|\\k<", match_string), match_string


def _branch_view(resolved: dict[str, Any]) -> dict[str, Any]:
    """What puts two upgrades in one PR: everything resolved except the PR-body notes."""
    return {key: value for key, value in resolved.items() if key != "prBodyNotes"}


def test_copies_join_their_canonical_branch() -> None:
    """AC1/AC2: a copy resolves to the group, schedule and holds of its canonical pin."""
    extracted = _extract_custom()
    for file, dep, _count in _COPIES:
        pin = _canonical(dep)
        canonical = _Dep(dep, pin.file, pin.datasource, pin.manager, pin.dep_type)
        copies = [e for e in extracted if e.file == file and e.dep_name == dep]
        assert copies, f"no manager extracts {dep} from {file}"
        for copy in copies:
            copy_dep = _Dep(dep, file, copy.datasource, "regex")
            # Same depName and release gives the same branch topic when ungrouped; a
            # group name puts the copy in the group's single branch.
            assert _branch_view(_resolve(copy_dep)) == _branch_view(_resolve(canonical)), (
                file,
                dep,
            )
            if dep == _UBUNTU:
                # The Dockerfile manager sets Ubuntu versioning itself; a regex copy needs the
                # rule, or it proposes the next non-LTS release the pin does not follow.
                assert _resolve(copy_dep)["versioning"] == "ubuntu", file


# Where each dependency's version is recorded as prose, a comment or a behaviour claim no
# rewrite can reach: {dependency: {repository path: regex the file must still match}}.
_AGENT_CLI_PLACES = {
    "src/booley/config/gateway.py": r"(?i)codex",
    "src/booley/runtime/text_only_agent.py": r"(?i)codex",
    "docs/internals/GATEWAY.md": r"(?i)codex",
}
_FUSESOC_PLACES = {
    "src/booley/fusesoc/core_projection.py": r"FuseSoC",
    "src/booley/fusesoc/fusesoc_registry.py": r"FuseSoC",
}
_EDALIZE_PLACES = {
    "src/booley/flows/edam.py": r"(?i)edalize",
    "src/booley/flows/fpga/backends/vivado/edam.py": r"(?i)edalize",
    f"{_DOCKER_DIR}/Dockerfile": r"(?i)edalize",
}
_PROVENANCE: dict[str, dict[str, str]] = {
    "@openai/codex": _AGENT_CLI_PLACES,
    "@openai/codex-linux-x64": _AGENT_CLI_PLACES,
    _VERILATOR: {
        "src/booley/config/tests_toml_rules.py": r"Verilator",
        _DOCKERFILE_BASE: r"Verilator",
    },
    "cocotb": {
        "src/booley/flows/sim/backends/cocotb_results.py": r"cocotb",
        "src/booley/harness/init_cmd.py": r"cocotb",
        "src/booley/runtime/project_image.py": r"cocotb",
    },
    _VAPORVIEW: {
        "src/booley/bwave/cli.py": r"VaporView",
        "src/booley/runtime/vaporview_vsix.py": r"VaporView",
    },
    _HDL: {_DEVCONTAINER: r"veriloghdl"},
    _ACTIONLINT: {".github/actionlint.yaml": r"actionlint"},
    _SPIKE: {_EDA_DOC: r"Spike"},
    "fusesoc": _FUSESOC_PLACES,
    "edalize": _EDALIZE_PLACES,
    # Version-free needle: the prose may be re-read and reworded on a tag change.
    _UBUNTU: {
        _DOCKERFILE_BASE: r"Ubuntu \d+\.\d+",
        f"{_DOCKER_DIR}/Dockerfile": r"Ubuntu \d+\.\d+",
    },
}

# Mentions of an old version that stay on purpose: (file, dependency, fragment of the
# line, why). A line not listed here fails the old-version test. A fragment that is a
# claim about a release holds no version number: a reviewer who follows the PR note and
# rewrites the comment for the new release must not have to edit this test as well.
_LEFTOVERS: tuple[tuple[str, str, str, str], ...] = (
    (
        "src/booley/config/tests_toml_rules.py",
        _VERILATOR,
        "Largest seed Booley accepts or draws",
        "limit measured on that release; the PR note asks for re-verification",
    ),
    (
        _DOCKERFILE_BASE,
        _VERILATOR,
        "v5.052 is the first stable release after",
        "history of why the pin moved past v5.050; the PR note names the file",
    ),
    (
        "src/booley/flows/sim/backends/cocotb_results.py",
        "cocotb",
        "cocotb 2.0.1 and 2.1.0",
        "releases the parser was frozen against, not the image pin",
    ),
    (
        "src/booley/harness/init_cmd.py",
        "cocotb",
        "``cocotb 2.1.0 -> 1.5.1``",
        "example of an incident, a dated observation",
    ),
    (
        "src/booley/runtime/project_image.py",
        "cocotb",
        "``cocotb 2.1.0 -> 1.5.1``",
        "example of an incident, a dated observation",
    ),
    (
        "src/booley/bwave/cli.py",
        _VAPORVIEW,
        "ends with an empty view",
        "behaviour observed in that release; the PR note asks for re-verification",
    ),
    (
        _DEVCONTAINER,
        _HDL,
        "so nothing else is required",
        "dated grammar observation; the PR note asks for re-verification",
    ),
    (
        _DOCKERFILE_BASE,
        _UBUNTU,
        "builder ships",
        "prose about the OpenROAD builder image; the PR note names the file",
    ),
    (
        _DOCKERFILE_BASE,
        _UBUNTU,
        "image already occupies",
        "prose about the parent image; the PR note names the file",
    ),
    (
        _DOCKERFILE_BASE,
        _UBUNTU,
        "system Python refuses ensurepip",
        "prose about the parent image; the PR note names the file",
    ),
    (
        f"{_DOCKER_DIR}/Dockerfile",
        _UBUNTU,
        "final image's Ubuntu",
        "glibc claim about the final image; the PR note names the file",
    ),
    (
        "src/booley/fusesoc/core_projection.py",
        "fusesoc",
        "parses fileset paths as expressions",
        "behaviour of that release; the PR note asks for re-verification",
    ),
    (
        "src/booley/fusesoc/fusesoc_registry.py",
        "fusesoc",
        "EDAM writer",
        "behaviour of that release; the PR note asks for re-verification",
    ),
    (
        "src/booley/flows/edam.py",
        "edalize",
        "empirically confirmed",
        "behaviour of that release; the PR note asks for re-verification",
    ),
    (
        "src/booley/flows/edam.py",
        "edalize",
        "has no dedicated flow in edalize",
        "behaviour of that release; the PR note asks for re-verification",
    ),
    (
        "src/booley/flows/fpga/backends/vivado/edam.py",
        "edalize",
        "exposes no ``strategy`` option",
        "behaviour of that release; the PR note asks for re-verification",
    ),
    (
        "src/booley/flows/fpga/backends/vivado/edam.py",
        "edalize",
        "vivado flow has no OOC knob",
        "behaviour of that release; the PR note asks for re-verification",
    ),
    (
        f"{_DOCKER_DIR}/Dockerfile",
        "edalize",
        "lint flow has no Verible node",
        "reason for the patch layer; the PR note asks for re-verification",
    ),
)


def _old_forms(dep: str, extracted: list[_Extracted]) -> set[str]:
    """Every spelling of ``dep``'s current version and digest a file could still hold."""
    pin = _canonical(dep)
    forms: set[str] = set()
    values = [pin.value, *(e.current_value for e in extracted if e.dep_name == dep)]
    for value in values:
        # The Spike copy tracks a branch ("master"), not a version; only its digest counts.
        if value is not None and value != "master":
            bare = value.removeprefix("v")
            forms.update({value, bare, f"v{bare}"})
    if pin.digest is not None:
        forms.add(pin.digest)
    return forms


def _match_spans(
    text: str, dep: str, file: str, extracted: list[_Extracted]
) -> list[tuple[int, int]]:
    """Spans Renovate rewrites for ``dep`` in one file: custom matches and image references."""
    spans = [e.span for e in extracted if e.file == file and e.dep_name == dep]
    if _canonical(dep).datasource == "docker":
        reference = rf"{re.escape(dep)}(?::[\w.]+)?@sha256:[0-9a-f]{{64}}"
        spans += [found.span() for found in re.finditer(reference, text)]
    return spans


def test_no_old_version_outside_a_match_in_copy_files() -> None:
    """AC1: after a bump nothing old is left in a copy file except the listed leftovers.

    Auto-replace rewrites every occurrence of the old value inside a match, so an
    occurrence outside every match of that dependency is exactly what a bump leaves behind.
    """
    extracted = _extract_custom()
    stray: list[str] = []
    for dep in dict.fromkeys(dep for _file, dep, _count in _COPIES):
        files = {file for file, copy_dep, _count in _COPIES if copy_dep == dep}
        files |= set(_PROVENANCE.get(dep, {}))
        for file in sorted(files):
            text = _text(file)
            spans = _match_spans(text, dep, file, extracted)
            for form in sorted(_old_forms(dep, extracted)):
                for found in re.finditer(rf"(?<![\w.]){re.escape(form)}(?!\w|\.\w)", text):
                    if any(start <= found.start() and found.end() <= end for start, end in spans):
                        continue
                    line = text[text.rfind("\n", 0, found.start()) + 1 :].split("\n", 1)[0]
                    if not any(
                        entry[:2] == (file, dep) and entry[2] in line for entry in _LEFTOVERS
                    ):
                        stray.append(f"{file}: {dep} {form!r}: {line.strip()}")
    assert not stray, "old versions outside any match:\n" + "\n".join(stray)


_PATH_IN_NOTE = re.compile(r"`([\w.-]+(?:/[\w.-]+)+)`")


def _notes_for(dep: str) -> list[str]:
    """The PR-body notes a branch gains from ``dep`` (selected by name only)."""
    notes: list[str] = _resolve(_Dep(dep, _DOCKERFILE_BASE, "github-tags", "regex")).get(
        "prBodyNotes", []
    )
    return notes


def _noted_paths(dep: str) -> set[str]:
    """Repository paths the notes of ``dep`` name in backticks."""
    return {path for note in _notes_for(dep) for path in _PATH_IN_NOTE.findall(note)}


def test_provenance_dependencies_list_their_places() -> None:
    """AC3: a PR for a dependency with provenance comments names the places to re-verify."""
    for dep, places in _PROVENANCE.items():
        assert _noted_paths(dep) == set(places), dep
        for path, needle in places.items():
            assert re.search(needle, _text(path)), f"{path} no longer mentions {dep}"
    for file, dep, fragment, _reason in _LEFTOVERS:
        assert file in _noted_paths(dep), f"{file} must be named in the {dep} notes"
        assert fragment in _text(file), f"{file} no longer holds the leftover {fragment!r}"


_OPENROAD_CHECKLIST_ARGS = (
    "OPENROAD_SOURCE_REF",
    "OPENROAD_BINARY_VERSION",
    "OPENROAD_SOURCE_SENTINEL_SHA256",
)
_OPENROAD_CHECKLIST_PATHS = (
    _DOCKERFILE_BASE,
    f"{_DOCKER_DIR}/openroad-source-manifest.json",
    "THIRD_PARTY_NOTICES.md",
    _EDA_DOC,
    _SESSION_CONTRACT,
)


def test_openroad_prs_carry_the_regeneration_checklist() -> None:
    """AC3: every dependency of the openroad group resolves to the manual checklist."""
    extracted = _extract_custom()
    deps = list(_manual_rows()["openroad"])
    deps += [
        _Dep(e.dep_name, e.file, e.datasource, "regex")
        for e in extracted
        if e.file == _DOCKERFILE_BASE and "/openroad/" in e.dep_name
    ]
    assert len(deps) >= 5, "the label, two builder images and their LABEL copies"
    base = _text(_DOCKERFILE_BASE)
    for arg in _OPENROAD_CHECKLIST_ARGS:
        assert re.search(rf"^ARG {arg}=", base, re.MULTILINE), arg
    for path in _OPENROAD_CHECKLIST_PATHS:
        assert (_ROOT / path).is_file(), path
    for dep in deps:
        resolved = _resolve(dep)
        assert resolved["groupName"] == "openroad", dep
        notes = "\n".join(resolved.get("prBodyNotes", []))
        for item in (*_OPENROAD_CHECKLIST_ARGS, *_OPENROAD_CHECKLIST_PATHS):
            assert item in notes, (dep.name, item)


def _job_runs(job: dict[str, Any]) -> dict[str, str]:
    """Return the job's ``run`` scripts keyed by step name, line continuations joined."""
    return {
        step["name"]: " ".join(step["run"].replace("\\\n", " ").split())
        for step in job["steps"]
        if "run" in step
    }


def test_ci_job_validates_config_strictly_and_proves_rejection() -> None:
    """AC1: a required job validates the config strictly and proves an invalid one fails."""
    workflow = yaml.safe_load(_text(_TEST_WORKFLOW))
    assert "renovate-config" in CONDITIONAL_JOBS
    assert "renovate-config" in workflow["jobs"]["ci-required"]["needs"]
    job = workflow["jobs"]["renovate-config"]
    assert job["needs"] == "changes"
    assert "['renovate-config']" in job["if"]
    runs = _job_runs(job)

    # The #2996 guards, and the pin audit of every workflow file that renovate.yml is
    # subject to, no longer ride on a full CI run for a Renovate-only change.
    guards = runs["Run the Renovate configuration guards"]
    assert "pytest tests/ci/test_renovate_config.py tests/ci/test_ci_requirements.py -q" in guards

    # Every docker step gets its image from the runner reader's output, never from a
    # literal, and no job-level IMAGE can shadow it.
    steps = {step["name"]: step for step in job["steps"] if "name" in step}
    assert "IMAGE" not in job.get("env", {})
    image_step = next(step for step in job["steps"] if step.get("id") == "image")
    assert image_step["run"].split() == [
        "python",
        ".github/scripts/renovate_runner_image.py",
        ">>",
        '"${GITHUB_OUTPUT}"',
    ]
    for name in (
        "Pull the Renovate image",
        "Validate the Renovate configuration strictly",
        "Prove the validator rejects an invalid configuration",
    ):
        assert steps[name]["env"] == {"IMAGE": "${{ steps.image.outputs.image }}"}, name

    # Strict, repo-config mode, offline, on the image derived from the runner.
    validator = (
        'docker run --rm --network none -v "${GITHUB_WORKSPACE}:/repo:ro" -w /repo "${IMAGE}"'
        " renovate-config-validator --strict --no-global .github/renovate.json"
    )
    validate = runs["Validate the Renovate configuration strictly"]
    assert steps["Validate the Renovate configuration strictly"]["shell"] == "bash"
    assert validate.startswith("set -o pipefail docker run")  # tee must not mask the status
    assert validator in validate
    assert 'grep -Fq "as repo config"' in validate

    # The same command must fail on a copy with an unknown option, for that reason.
    probe = runs["Prove the validator rejects an invalid configuration"]
    assert validator.replace("${GITHUB_WORKSPACE}", "${probe}") in probe
    assert "booleyValidatorProbe" in probe
    assert "exit 1" in probe
    assert 'grep -Fq "Invalid configuration option: booleyValidatorProbe"' in probe


def test_validator_image_is_the_runner_image(capsys: pytest.CaptureFixture[str]) -> None:
    """AC1: the validator image is read from the runner, never pinned a second time."""
    # Imported here so a missing reader fails this test, not the whole module.
    import renovate_runner_image

    _, run_step = _renovate_steps()
    runner_text = _WORKFLOW_PATH.read_text(encoding="utf-8")
    assert renovate_runner_image.runner_image(runner_text) == run_step["env"]["IMAGE"]
    assert "renovate/renovate:" not in _text(_TEST_WORKFLOW)

    renovate_runner_image.main()
    assert capsys.readouterr().out == f"image={run_step['env']['IMAGE']}\n"
