"""CI tool pins live in ``.github/ci/requirements-*.txt``, never inline in workflows (#2557).

Project installs in CI also pass the committed constraints file (#2707).
"""

from __future__ import annotations

import re
import shlex
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / ".github/scripts"))

from ci_changes import classify, required_jobs

_REQUIREMENTS_DIR = _ROOT / ".github/ci"
_REQUIREMENT_FILES = sorted(_REQUIREMENTS_DIR.glob("requirements-*.txt"))
# Longest first, so "python -m pip" is not mistaken for a bare "python".
_INSTALLERS = (("uv", "pip"), ("python", "-m", "pip"), ("pip",), ("pip3",), ("pipx",))
_COMMAND_SEPARATORS = re.compile(r"\n|&&|\|\||;|\|")
_CONSTRAINTS = ".github/ci/constraints.txt"
# A project install targets the checkout: ".", "./", ".[extras]", "-e .", or a workspace
# variable, optionally behind an "-e" / "--editable=" prefix glued to the target.
_PROJECT_TARGET = re.compile(
    r"(?:-e|--editable=)?(?:\.|\./|\$GITHUB_WORKSPACE|\$\{GITHUB_WORKSPACE\}|\$PWD)(?:\[[^\]]*\])?"
)
_INSTALLING_ACTION = "./.github/actions/prepare-picorv32-demo"
_PROJECT_INSTALL_LOCATIONS = {
    "test.yml:docs-check",
    "test.yml:lint",
    "test.yml:test",
    "test.yml:flake-stress",
    "test.yml:coverage-shards",
    "test.yml:coverage",
    "test.yml:bwave-integration",
    "deep-tests.yml:mutation",
    "deep-tests.yml:bwave-fuzz",
    "deep-tests.yml:worktree-mounts",
    "full-python-matrix.yml:test",
    ".github/actions/prepare-picorv32-demo/action.yml",
}
_PIN = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[0-9][0-9A-Za-z.!+_-]*)$")


def _workflow_texts() -> list[tuple[str, str]]:
    """Return ``(location, run script)`` for every workflow and composite-action step."""
    scripts: list[tuple[str, str]] = []
    for path in sorted((_ROOT / ".github/workflows").glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in document["jobs"].items():
            for step in job.get("steps", ()):
                if "run" in step:
                    scripts.append((f"{path.name}:{job_name}", step["run"]))
                # Parallel step groups carry their own commands.
                scripts.extend(
                    (f"{path.name}:{job_name}", item["run"])
                    for item in step.get("parallel", ())
                    if "run" in item
                )
    for path in sorted((_ROOT / ".github/actions").rglob("action.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        scripts.extend(
            (str(path.relative_to(_ROOT)), step["run"])
            for step in document["runs"].get("steps", ())
            if "run" in step
        )
    return scripts


def _install_commands(script: str) -> list[list[str]]:
    """Return the token list of each ``pip install`` style command in *script*."""
    joined = script.replace("\\\n", " ")
    commands: list[list[str]] = []
    for segment in _COMMAND_SEPARATORS.split(joined):
        try:
            tokens = shlex.split(segment, comments=True)
        except ValueError:
            tokens = segment.split()
        # A split inside `bash -c "..."` leaves stray quotes on the edge tokens.
        tokens = [token.strip("\"'") for token in tokens]
        resume_at = 0
        for index in range(len(tokens)):
            if index < resume_at:
                continue  # inside an installer already counted ("python -m pip" is not also "pip")
            for installer in _INSTALLERS:
                end = index + len(installer)
                if tuple(tokens[index:end]) == installer and "install" in tokens[end : end + 1]:
                    commands.append(tokens[end + 1 :])
                    resume_at = end
                    break
    return commands


def _requirement_packages(path: Path, seen: frozenset[Path] = frozenset()) -> dict[str, str]:
    """Resolve a requirements file (and nested ``-r`` files) to ``{name: version}``."""
    assert path not in seen, f"{path.name} includes itself"
    packages: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            nested = path.parent / line.removeprefix("-r ").strip()
            packages |= _requirement_packages(nested, seen | {path})
            continue
        match = _PIN.fullmatch(line)
        assert match, f"{path.name}: {line!r} is not an exact name==version pin"
        packages[match["name"].lower()] = match["version"]
    return packages


def _release_tuple(version: str) -> tuple[int, ...]:
    assert re.fullmatch(r"\d+(\.\d+)*", version), f"pin {version!r} is not a plain release"
    return tuple(int(part) for part in version.split("."))


def _pyproject_requirements() -> dict[str, str]:
    """Map each ``test`` extra requirement name to its specifier text."""
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared: dict[str, str] = {}
    for item in project["optional-dependencies"]["test"]:
        match = re.fullmatch(r"([A-Za-z0-9._-]+)\s*(.*)", item)
        assert match
        declared[match[1].lower()] = match[2]
    return declared


def test_workflows_install_no_inline_exact_pins() -> None:
    """AC1: a pinned install is ``-r .github/ci/...``, not ``pip install name==x.y``."""
    offenders = [
        f"{location}: {' '.join(tokens)}"
        for location, script in _workflow_texts()
        for tokens in _install_commands(script)
        if any("==" in token for token in tokens)
    ]
    assert offenders == []


def test_inline_pin_detector_sees_continued_and_container_commands() -> None:
    """Guard the guard: the detector finds pins in folded, continued, and quoted scripts."""
    found = _install_commands(
        'booley-test bash -c "pip install --break-system-packages pytest==9.2.0 &&\n'
        'python -m pytest x"\n'
        "python -m pip install \\\n  build==1.6.1\n"
    )
    assert any("pytest==9.2.0" in tokens for tokens in found)
    assert any("build==1.6.1" in tokens for tokens in found)
    assert _install_commands("pip install -r .github/ci/requirements-build.txt") == [
        ["-r", ".github/ci/requirements-build.txt"]
    ]


def test_ci_requirements_match_project_pins() -> None:
    """AC1: each file holds only exact pins, and shared packages agree with pyproject."""
    declared = _pyproject_requirements()
    for path in _REQUIREMENT_FILES:
        for name, version in _requirement_packages(path).items():
            specifier = declared.get(name)
            if specifier is None:
                continue
            if specifier.startswith("=="):
                assert specifier == f"=={version}", f"{path.name}: {name} drifted from pyproject"
            elif specifier.startswith(">="):
                floor = specifier.removeprefix(">=").split(",")[0]
                assert _release_tuple(version) >= _release_tuple(floor), (
                    f"{path.name}: {name}=={version} is below pyproject's {specifier}"
                )
            else:
                pytest.fail(f"{name}: unsupported pyproject specifier {specifier!r}")
    # The runner file has to cover the plugins every consumer job relies on.
    runners = _requirement_packages(_REQUIREMENTS_DIR / "requirements-runners.txt")
    assert {"pytest", "pytest-asyncio"} <= set(runners)


def test_hypothesis_is_pinned_wherever_installed() -> None:
    """AC2: hypothesis is never installed by bare name; its requirement line is exact."""
    for location, script in _workflow_texts():
        for tokens in _install_commands(script):
            named = [token for token in tokens if token.lower().startswith("hypothesis")]
            assert named == [], f"{location} installs {named} inline"
    release = _requirement_packages(_REQUIREMENTS_DIR / "requirements-runners-release.txt")
    assert "hypothesis" in release
    for path in _REQUIREMENT_FILES:
        for raw in path.read_text(encoding="utf-8").splitlines():
            if raw.lower().startswith("hypothesis"):
                assert re.fullmatch(r"hypothesis==\S+", raw.split("#")[0].strip()), raw


def test_pull_request_scanner_smoke_installs_pr_requirements() -> None:
    """AC5: a pull-request job installs the PR's scanner requirements and runs self-test."""
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    assert "pull_request" in triggers
    job = workflow["jobs"]["scanner-smoke"]
    assert job["needs"] == "changes"
    assert "scanner-smoke" in job["if"]
    runs = [step["run"] for step in job["steps"] if "run" in step]
    assert any(
        run.strip() == "python -m pip install -r .github/ci/requirements-scanner.txt"
        for run in runs
    )
    assert any(
        run.strip() == "python .github/scripts/confidential_content_guard.py self-test"
        for run in runs
    )
    assert "scanner-smoke" in workflow["jobs"]["ci-required"]["needs"]
    selected = required_jobs(classify([".github/ci/requirements-scanner.txt"]))
    assert "scanner-smoke" in selected


def _project_installs() -> list[tuple[str, list[str]]]:
    """Return ``(location, tokens)`` for every install of the project or its extras."""
    return [
        (location, tokens)
        for location, script in _workflow_texts()
        for tokens in _install_commands(script)
        if any(_PROJECT_TARGET.fullmatch(token) for token in tokens)
    ]


def _constraint_paths(tokens: list[str]) -> list[str]:
    """Return the values passed to ``-c`` / ``--constraint`` in an install command."""
    paths: list[str] = []
    for index, token in enumerate(tokens):
        if token in {"-c", "--constraint"} and index + 1 < len(tokens):
            paths.append(tokens[index + 1])
        elif token.startswith("--constraint="):
            paths.append(token.removeprefix("--constraint="))
        elif token.startswith("-c") and not token.startswith("--"):
            paths.append(token.removeprefix("-c"))
    return paths


def test_project_installs_pass_the_constraints_file() -> None:
    """AC1: every CI install of the project or its extras passes the constraints file."""
    installs = _project_installs()
    assert {location for location, _ in installs} >= _PROJECT_INSTALL_LOCATIONS
    offenders = [
        f"{location}: {' '.join(tokens)}"
        for location, tokens in installs
        if not any(path.endswith(_CONSTRAINTS) for path in _constraint_paths(tokens))
    ]
    assert offenders == []


def test_project_pip_caches_key_on_the_constraints_file() -> None:
    """AC1: a pip cache serving a project install is keyed on pyproject and the constraints."""
    installing_jobs = {location for location, _ in _project_installs()}
    offenders: list[str] = []
    for path in sorted((_ROOT / ".github/workflows").glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in document["jobs"].items():
            # A job also installs the project when it calls the composite action that does.
            uses_installing_action = any(
                str(step.get("uses", "")).startswith(_INSTALLING_ACTION)
                for step in job.get("steps", ())
            )
            if f"{path.name}:{job_name}" not in installing_jobs and not uses_installing_action:
                continue
            for step in job.get("steps", ()):
                settings = step.get("with", {})
                if str(step.get("uses", "")).startswith("actions/setup-python") and (
                    settings.get("cache") == "pip"
                ):
                    keyed = settings.get("cache-dependency-path", "").split()
                    if not {"pyproject.toml", _CONSTRAINTS} <= set(keyed):
                        offenders.append(f"{path.name}:{job_name}")
    assert offenders == []


@pytest.mark.parametrize(
    ("script", "installs"),
    [
        ("python -m pip install -e .[test]", 1),
        ("uv pip install --system -r x.txt", 1),
        ("pip install a && pip3 install b", 2),
    ],
)
def test_install_scanner_counts_each_install_once(script: str, installs: int) -> None:
    """A multi-token installer ("python -m pip") is one install, not two."""
    assert len(_install_commands(script)) == installs


@pytest.mark.parametrize(
    "command",
    [
        'pip install "$GITHUB_WORKSPACE"[test]',
        "pip install ./",
        "pip install -e. -c x",
        "pip install --editable=.[test]",
    ],
)
def test_project_target_recognises_checkout_spellings(command: str) -> None:
    """The AC1 scanner treats every spelling of the checkout as a project install."""
    (tokens,) = _install_commands(command)
    assert any(_PROJECT_TARGET.fullmatch(token) for token in tokens)


@pytest.mark.parametrize(
    "tokens",
    [["-c", "k.txt"], ["--constraint", "k.txt"], ["--constraint=k.txt"], ["-ck.txt"]],
)
def test_constraint_paths_recognise_every_flag_spelling(tokens: list[str]) -> None:
    """``-c`` / ``--constraint`` count in all their argparse spellings."""
    assert _constraint_paths(tokens) == ["k.txt"]
