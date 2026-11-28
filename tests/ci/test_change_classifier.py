"""Behavioral contracts for change-aware CI selection and aggregation."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / ".github/scripts"))

from ci_changes import (
    ALL_JOBS,
    CATEGORIES,
    MAIN_REF,
    STRESS_JOB,
    build_test_matrix,
    classify,
    required_jobs,
)
from picorv32_ci_inputs import RISCV_IMAGE_FILES
from tests.subprocess_budget import run_bounded_child

_CLASSIFIER = _ROOT / ".github/scripts/ci_changes.py"
_AGGREGATOR = _ROOT / ".github/scripts/ci_required.py"


def _git_identity_env() -> dict[str, str]:
    return os.environ | {
        "GIT_AUTHOR_NAME": "CI Test",
        "GIT_AUTHOR_EMAIL": "ci@example.invalid",
        "GIT_COMMITTER_NAME": "CI Test",
        "GIT_COMMITTER_EMAIL": "ci@example.invalid",
    }


def _git(repo: Path, *args: str) -> str:
    result = run_bounded_child(["git", "-C", str(repo), *args], check=True)
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    env = _git_identity_env()
    staged = run_bounded_child(["git", "-C", str(repo), "add", "-A"], env=env)
    assert staged.returncode == 0, staged.stderr
    run_bounded_child(["git", "-C", str(repo), "commit", "-m", message], check=True, env=env)
    return _git(repo, "rev-parse", "HEAD")


def _merge(repo: Path, branch: str) -> str:
    run_bounded_child(
        ["git", "-C", str(repo), "merge", "--no-ff", "-m", "merge", branch],
        check=True,
        env=_git_identity_env(),
    )
    return _git(repo, "rev-parse", "HEAD")


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    return repo, _commit(repo, "initial")


def _run_classifier(
    repo: Path,
    base: str,
    head: str,
    *,
    linux_options: dict[str, str] | None = None,
    **options: object,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the classifier CLI; each keyword becomes ``--kebab-name value`` (``None`` omits it)."""
    output = repo / "github-output.txt"
    command = [
        sys.executable,
        str(_CLASSIFIER),
        "--repo",
        str(repo),
        "--base",
        base,
        "--head",
        head,
        "--github-output",
        str(output),
    ]
    defaults: dict[str, object] = {
        "force_all": False,
        "windows_shard_count": 4,
        "windows_shard_benchmark": False,
    }
    for name, value in (defaults | options).items():
        if value is not None:
            text = str(value).lower() if isinstance(value, bool) else str(value)
            command.extend((f"--{re.sub('[^a-z0-9]', '-', name)}", text))
    for option, value in (linux_options or {}).items():
        command.extend((f"--linux-{option}", value))
    return run_bounded_child(command, check=False), output


def _classify(repo: Path, base: str, head: str, **options: object) -> dict[str, str]:
    result, output = _run_classifier(repo, base, head, **options)  # type: ignore[arg-type]
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())


def _write(repo: Path, path: str, text: str = "fixture\n") -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _required(outputs: dict[str, str]) -> set[str]:
    return set(filter(None, outputs["required_jobs"].split(",")))


def _jobs(outputs: dict[str, str]) -> dict[str, bool]:
    return json.loads(outputs["jobs"])


def _change(tmp_path: Path, *paths: str, text: str = "fixture\n") -> tuple[Path, str, str]:
    """Return a repository, its first commit, and a commit that writes every path."""
    repo, base = _repository(tmp_path)
    for path in paths:
        _write(repo, path, text)
    return repo, base, _commit(repo, "change")


@pytest.mark.parametrize("shard_count", [4, 6, 8, 10])
def test_windows_shard_experiment_builds_complete_matrix(shard_count: int) -> None:
    entries = build_test_matrix(shard_count, linux_shard_count=1)["include"]
    shards = [entry for entry in entries if entry["mode"] == "shard"]
    compatibility = [entry for entry in entries if entry["mode"] != "shard"]

    assert len(entries) == 3 + shard_count
    windows_mark = "os_specific and not native_bwave"
    assert all(entry["mark_expression"] == windows_mark for entry in shards)
    assert compatibility == [
        {
            "name": "ubuntu-3.11-full",
            "os": "ubuntu-latest",
            "python": "3.11",
            "mode": "full",
            "mark_expression": "not native_bwave",
        },
        {
            "name": "windows-3.11-compatibility",
            "os": "windows-latest",
            "python": "3.11",
            "mode": "compatibility",
            "mark_expression": windows_mark,
        },
        {
            "name": "windows-3.13-compatibility",
            "os": "windows-latest",
            "python": "3.13",
            "mode": "compatibility",
            "mark_expression": windows_mark,
        },
    ]
    assert [entry["shard_index"] for entry in shards] == list(range(shard_count))
    assert all(entry["shard_count"] == shard_count for entry in shards)
    assert len({entry["name"] for entry in entries}) == len(entries)


def test_constraints_file_runs_the_full_python_matrix() -> None:
    """AC4: a constraints-only change selects every job, including the Windows test legs."""
    categories = classify([".github/ci/constraints.txt"])

    assert "packaging" in categories
    assert required_jobs(categories) == set(ALL_JOBS) - {STRESS_JOB}


def test_dispatched_riscv_measurement_builds_riscv_image_without_riscv_changes(
    tmp_path: Path,
) -> None:
    """A measurement dispatch on main must yield a sample even when HEAD^..HEAD is unrelated."""
    repo, base = _repository(tmp_path)
    _write(repo, "docs/unrelated.md")
    head = _commit(repo, "unrelated docs change")

    outputs = _classify(
        repo, base, head, force_all=True, event_name="workflow_dispatch", riscv_measurement="cold"
    )

    assert outputs["riscv_image"] == "true"
    assert outputs["stable_base"] == "false"
    assert _jobs(outputs)["bwave-smoke"] is True
    assert _jobs(outputs)["riscv-image"] is True


_ORDINARY_PYTHON_JOBS = {
    "changes",
    "release-semantic",
    "lint",
    "test",
    "test-verify",
    "coverage-shards",
    "coverage",
    "build-dist",
    "package-artifacts",
}


@pytest.mark.parametrize(
    "option",
    [{"windows_shard_benchmark": True}, {"linux_options": {"shard-benchmark": "true"}}],
    ids=["windows", "linux"],
)
def test_shard_benchmark_selects_the_ordinary_python_job_set(
    tmp_path: Path, option: dict[str, object]
) -> None:
    repo, base, head = _change(tmp_path, ".github/workflows/test.yml")

    outputs = _classify(repo, base, head, event_name="workflow_dispatch", **option)

    assert _required(outputs) == _ORDINARY_PYTHON_JOBS
    assert {job for job, selected in _jobs(outputs).items() if selected} == (
        _ORDINARY_PYTHON_JOBS - {"changes", "release-semantic"}
    )


@pytest.mark.parametrize(
    ("options", "fragment"),
    [
        pytest.param(
            {"event_name": "push", "windows_shard_benchmark": True},
            "Windows shard benchmarking requires workflow_dispatch",
            id="windows-benchmark-event",
        ),
        pytest.param(
            {"event_name": "push", "riscv_measurement": "cold"},
            "RISC-V measurement arms require workflow_dispatch",
            id="riscv-arm-event",
        ),
        pytest.param(
            {
                "event_name": "workflow_dispatch",
                "windows_shard_benchmark": True,
                "riscv_measurement": "cold",
            },
            "cannot be combined",
            id="riscv-with-windows-benchmark",
        ),
        # ADR 0070 retired the Phase-1 containerd cache arms.
        pytest.param(
            {"event_name": "workflow_dispatch", "riscv_measurement": "baseline"},
            "invalid choice",
            id="retired-baseline-arm",
        ),
        pytest.param(
            {"event_name": "workflow_dispatch", "riscv_measurement": "warm"},
            "invalid choice",
            id="retired-warm-arm",
        ),
        pytest.param(
            {"event_name": "pull_request"},
            "pull_request head must be a two-parent merge commit",
            id="non-merge-head",
        ),
        pytest.param(
            {"event_name": "pull_requset"}, "invalid choice: 'pull_requset'", id="unknown-event"
        ),
        pytest.param(
            {"event_name": "push", "linux_options": {"shard-benchmark": "true"}},
            "Linux shard benchmarking requires workflow_dispatch",
            id="linux-benchmark-event",
        ),
        pytest.param(
            {"linux_options": {"shard-benchmark": "true"}},
            "Linux shard benchmarking requires workflow_dispatch",
            id="linux-benchmark-without-event",
        ),
        pytest.param(
            {
                "event_name": "workflow_dispatch",
                "riscv_measurement": "cold",
                "linux_options": {"shard-benchmark": "true"},
            },
            "Linux shard benchmarking cannot be combined",
            id="linux-benchmark-with-riscv-arm",
        ),
        *(
            pytest.param(
                {"event_name": "push", "linux_options": override},
                "Linux shard count and scheduler overrides require",
                id=f"linux-override-{next(iter(override))}-{next(iter(override.values()))}",
            )
            for override in (
                {"shard-count": "1"},
                {"shard-count": "3"},
                {"scheduler": "worksteal"},
            )
        ),
    ],
)
def test_classifier_rejects_invalid_options(
    tmp_path: Path, options: dict[str, object], fragment: str
) -> None:
    repo, base, head = _change(tmp_path, "src/booley/example.py")

    result, _output = _run_classifier(repo, base, head, **options)  # type: ignore[arg-type]

    assert result.returncode == 2
    assert fragment in result.stderr


def test_pull_request_uses_matching_merge_parent_instead_of_stale_event_base(
    tmp_path: Path,
) -> None:
    repo, stale_base = _repository(tmp_path)
    _git(repo, "switch", "-c", "feature")
    _write(repo, "src/booley/example.py")
    _write(repo, "tests/unit/test_example.py")
    _write(repo, "tests/os_specific.py", "OS_SPECIFIC_TESTS: tuple[str, ...] = ()\n")
    _commit(repo, "feature changes")

    _git(repo, "switch", "main")
    _write(repo, "src/booley/data/docker/Dockerfile.base", "FROM scratch\n")
    _write(repo, ".github/workflows/upstream.yml")
    current_base = _commit(repo, "upstream image changes")
    merge_head = _merge(repo, "feature")

    outputs = _classify(repo, stale_base, merge_head, event_name="pull_request")

    assert outputs["diff_base"] == current_base
    assert outputs["python_source"] == "true"
    assert outputs["python_tests"] == "true"
    assert outputs["stable_base"] == "false"
    assert outputs["docker_toolchain"] == "false"
    assert outputs["workflow"] == "false"
    assert outputs["riscv_image"] == "false"
    assert outputs["build_stable_base"] == "false"


def test_push_ending_in_merge_still_uses_event_base(tmp_path: Path) -> None:
    repo, before = _repository(tmp_path)
    _git(repo, "switch", "-c", "side-branch")
    _write(repo, "docs/side-branch.md")
    _commit(repo, "side branch")

    _git(repo, "switch", "main")
    _write(repo, "src/booley/data/docker/Dockerfile.base", "FROM scratch\n")
    _commit(repo, "stable base change")
    merge_head = _merge(repo, "side-branch")

    outputs = _classify(repo, before, merge_head, force_all=True, event_name="push")

    assert outputs["diff_base"] == before
    assert outputs["stable_base"] == "true"
    assert outputs["build_stable_base"] == "true"


_PROJECT_METADATA = """\
[project]
name = "demo"
dependencies = ["alpha>=1.0"]

[project.optional-dependencies]
dev = ["pytest>=8"]
"""


@pytest.mark.parametrize(
    ("edited", "stable_base"),
    [
        pytest.param(_PROJECT_METADATA.replace("pytest>=8", "pytest>=9"), False, id="extras-only"),
        pytest.param(_PROJECT_METADATA.replace("alpha>=1.0", "alpha>=2"), True, id="runtime"),
        pytest.param("[project\nbroken", True, id="unparsable"),
        pytest.param(None, True, id="deleted"),
    ],
)
def test_project_metadata_edit_selects_stable_base_only_for_runtime_changes(
    tmp_path: Path, edited: str | None, stable_base: bool
) -> None:
    repo, _seed = _repository(tmp_path)
    _write(repo, "pyproject.toml", _PROJECT_METADATA)
    base = _commit(repo, "metadata")
    if edited is None:
        (repo / "pyproject.toml").unlink()
    else:
        _write(repo, "pyproject.toml", edited)

    outputs = _classify(repo, base, _commit(repo, "edit metadata"))

    assert outputs["stable_base"] == str(stable_base).lower()
    assert outputs["build_stable_base"] == str(stable_base).lower()
    assert outputs["packaging"] == "true"


@pytest.mark.parametrize(
    ("path", "stable_base"),
    [
        # #829: the base inventory helper's bytes are a base compatibility input.
        ("src/booley/data/docker/base_package_inventory.py", True),
        # The pinned Python package set is installed by the base image.
        ("src/booley/data/docker/base-python-requirements.txt", True),
        # The contract gates every image lane but not the base.
        (".github/contracts/session-runtime.toml", False),
    ],
)
def test_package_inventory_inputs_select_their_image_gates(path: str, stable_base: bool) -> None:
    categories = classify([path])

    assert ("stable_base" in categories) is stable_base
    assert {"standard_image", "riscv_image"} <= categories
    assert "bwave-smoke" in required_jobs(categories)


@pytest.mark.parametrize(
    ("paths", "categories", "jobs"),
    [
        pytest.param(
            ["docs/setup.md", "src/booley/data/docker/Dockerfile"],
            {"docs", "docker_toolchain"},
            {"docs-check", "package-artifacts", "bwave-smoke"},
            id="docker-and-docs",
        ),
        pytest.param(
            ["tests/docker/test_image.py"],
            {"python_tests", "image_tests"},
            {"test", "package-artifacts", "bwave-smoke"},
            id="image-test",
        ),
        pytest.param(
            ["VERSION"],
            {"packaging", "release"},
            {
                "lint",
                "test",
                "test-verify",
                "coverage-shards",
                "coverage",
                "rust-test",
                "bwave-integration",
                "package-artifacts",
                "sidecar-smoke",
                "bwave-smoke",
            },
            id="version",
        ),
    ],
)
def test_paths_select_their_categories_and_jobs(
    paths: list[str], categories: set[str], jobs: set[str]
) -> None:
    selected = classify(paths)

    assert categories <= selected
    assert jobs <= required_jobs(selected)


_BASE_JOBS = {"changes", "release-semantic", "docs-check"}
_SITE_JOBS = _BASE_JOBS | {"site-build", "lint"}
# package-artifacts validates the distributions build-dist builds.
_PACKAGE_JOBS = {"build-dist", "package-artifacts"}


_PURE_DOCKER_JOBS = {
    "changes",
    "release-semantic",
    "build-dist",
    "package-artifacts",
    "bwave-smoke",
    "riscv-image",
}
_STABLE_BASE_JOBS = _ORDINARY_PYTHON_JOBS | {"bwave-smoke", "riscv-image"}


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        pytest.param(["README.md"], _BASE_JOBS | _PACKAGE_JOBS, id="readme"),
        pytest.param(["website/docs/index.md"], _SITE_JOBS, id="website-page"),
        pytest.param(["website/mkdocs.yml"], _SITE_JOBS, id="website-config"),
        # Python under website/ must still reach Ruff through ``lint``.
        pytest.param(["website/hooks/user_docs.py"], _SITE_JOBS, id="website-python"),
        pytest.param(["docs/user/USAGE.md"], _SITE_JOBS, id="user-docs"),
        # The install smoke runs the Install guide's commands.
        pytest.param(["docs/user/INSTALL.md"], _SITE_JOBS | _PACKAGE_JOBS, id="install-guide"),
        pytest.param(["docs/architecture.md"], _BASE_JOBS, id="internal-docs"),
        pytest.param(["AGENTS.md"], _BASE_JOBS, id="root-markdown"),
        pytest.param(["qa/AREAS.md"], _BASE_JOBS, id="qa-markdown"),
        # A changelog fragment is documentation, never the release category.
        pytest.param(["changelog.d/3606.fix.md"], _BASE_JOBS, id="changelog-fragment"),
        pytest.param(
            ["changelog.d/README.md", "changelog.d/fix-typo.fix.md"],
            _BASE_JOBS,
            id="changelog-fragment-readme",
        ),
        pytest.param(
            ["README.md", "website/docs/index.md"],
            _SITE_JOBS | _PACKAGE_JOBS,
            id="readme-and-website",
        ),
        pytest.param(["Dockerfile.tools"], _PURE_DOCKER_JOBS, id="pure-docker"),
        pytest.param(
            ["src/booley/data/docker/Dockerfile.base"], _STABLE_BASE_JOBS, id="stable-base"
        ),
    ],
)
def test_changes_select_their_exact_job_sets(paths: list[str], expected: set[str]) -> None:
    """README, website, documentation and Docker changes skip the lanes they do not exercise."""
    assert required_jobs(classify(paths)) == expected


def test_readme_and_website_pull_request_outputs_the_narrow_set(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    _write(repo, "README.md")
    _write(repo, "website/docs/index.md")
    head = _commit(repo, "readme and website")

    outputs = _classify(repo, base, head)

    expected = _SITE_JOBS | _PACKAGE_JOBS
    assert outputs["full"] == "false"
    assert outputs["packaging"] == "false"
    assert outputs["python_tests"] == "false"
    assert _required(outputs) == expected
    assert {job for job, selected in _jobs(outputs).items() if selected} == expected - {
        "changes",
        "release-semantic",
    }


_NOT_SELECTED_BY_SOURCE = {
    "bwave-integration",
    "bwave-smoke",
    "renovate-config",
    "riscv-image",
    "rust-test",
    "scanner-smoke",
    "sidecar-smoke",
}


@pytest.mark.parametrize(
    ("paths", "unselected"),
    [
        # Characterization: these sets are what was selected before the
        # documentation job set was narrowed, and must stay unchanged. Only a
        # RISC-V image input (pyproject.toml is one) selects `riscv-image`.
        pytest.param(["pyproject.toml"], set(), id="pyproject.toml"),
        *(
            pytest.param([path], {"riscv-image"}, id=path)
            for path in ("setup.cfg", "CHANGELOG.md", "release-assets/x", "unmapped/config.xyz")
        ),
        pytest.param(
            ["src/booley/example.py"],
            _NOT_SELECTED_BY_SOURCE | {"docs-check", "site-build"},
            id="source",
        ),
        pytest.param(
            ["tests/unit/test_example.py"],
            _NOT_SELECTED_BY_SOURCE | {"docs-check", "site-build"} | _PACKAGE_JOBS,
            id="tests",
        ),
        pytest.param(["README.md", "pyproject.toml"], set(), id="readme-and-packaging"),
        pytest.param(
            ["website/docs/index.md", "src/booley/example.py"],
            _NOT_SELECTED_BY_SOURCE,
            id="website-and-source",
        ),
        pytest.param(
            ["docs/architecture.md", "src/booley/example.py"],
            _NOT_SELECTED_BY_SOURCE | {"site-build"},
            id="internal-docs-and-source",
        ),
        # README stopped being a packaging file: with source it adds only its
        # own narrow jobs (documentation check, package build) to the source set.
        pytest.param(
            ["README.md", "src/booley/example.py"],
            _NOT_SELECTED_BY_SOURCE | {"site-build"},
            id="readme-and-source",
        ),
    ],
)
def test_non_documentation_changes_keep_their_job_sets(
    paths: list[str], unselected: set[str]
) -> None:
    assert required_jobs(classify(paths)) == set(ALL_JOBS) - {STRESS_JOB} - unselected


_RENOVATE_PATHS = (".github/renovate.json", ".github/workflows/renovate.yml")
_RENOVATE_LANE = {"changes", "release-semantic", "renovate-config", "lint"}


@pytest.mark.parametrize("path", _RENOVATE_PATHS)
def test_renovate_change_selects_validation_not_full_ci(path: str) -> None:
    """AC2: the Renovate configuration or runner selects its validation lane only."""
    assert classify([path]) == {"renovate"}
    assert required_jobs(classify([path])) == _RENOVATE_LANE


_CI_REQUIREMENT_FILES = (
    ".github/ci/requirements-build.txt",
    ".github/ci/requirements-lint.txt",
    ".github/ci/requirements-runners-release.txt",
    ".github/ci/requirements-runners.txt",
    ".github/ci/requirements-scanner.txt",
)


def test_ci_requirements_file_selects_ci_tooling_not_full() -> None:
    """A CI pin bump runs the jobs that install the pins, not the whole pipeline."""
    for path in _CI_REQUIREMENT_FILES:
        categories = classify([path])
        jobs = required_jobs(categories)

        assert categories == {"ci_tooling"}, path
        assert {
            "release-semantic",
            "package-artifacts",
            "sidecar-smoke",
            "bwave-smoke",
            "scanner-smoke",
            "lint",
            "test",
            "test-verify",
            "coverage-shards",
            "coverage",
        } <= jobs, path
        assert not jobs & {"docs-check", "site-build", "rust-test", "bwave-integration"}, path


def test_aggregate_waits_for_every_conditional_job() -> None:
    """ci-required must need each selectable job, or a selected job could be skipped unseen."""
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8"))

    assert set(ALL_JOBS) <= set(workflow["jobs"]["ci-required"]["needs"])


def _pull_request(
    tmp_path: Path, registered: tuple[str, ...] = ("tests/unit/test_kept.py",)
) -> tuple[Path, str]:
    """Return a repository whose merge head adds, modifies and deletes test modules."""
    repo, _ = _repository(tmp_path)
    _write(repo, "tests/os_specific.py", f"OS_SPECIFIC_TESTS: tuple[str, ...] = {registered!r}\n")
    _write(repo, "tests/unit/test_kept.py")
    _write(repo, "tests/unit/test_gone.py")
    _commit(repo, "seed tests")
    _git(repo, "checkout", "-b", "feature")
    _write(repo, "tests/unit/test_kept.py", "changed\n")
    _write(repo, "tests/unit/test_new.py")
    (repo / "tests/unit/test_gone.py").unlink()
    _commit(repo, "change tests")
    _git(repo, "checkout", "main")
    return repo, _merge(repo, "feature")


@pytest.mark.parametrize(
    "registered", [("tests/unit/test_kept.py",), ()], ids=["registered", "none-registered"]
)
def test_pull_request_test_changes_require_flake_stress(
    tmp_path: Path, registered: tuple[str, ...]
) -> None:
    repo, head = _pull_request(tmp_path, registered)

    outputs = _classify(repo, "", head, event_name="pull_request", head_ref="codex/feature")

    legs = {"ubuntu": ["tests/unit/test_kept.py", "tests/unit/test_new.py"]}
    if registered:
        legs["windows"] = list(registered)
    assert json.loads(outputs["stress_matrix"])["include"] == [
        {"name": f"{os_}-3.14", "os": f"{os_}-latest", "python": "3.14", "modules": modules}
        for os_, modules in legs.items()
    ]
    assert outputs["stress_count"] == "10"
    assert _jobs(outputs)[STRESS_JOB] is True
    assert STRESS_JOB in _required(outputs)


@pytest.mark.parametrize(
    ("event_name", "head_ref"),
    [("pull_request", "mergify/merge-queue/5f9ee8efb7"), ("push", None)],
)
def test_queue_drafts_and_pushes_skip_flake_stress(
    tmp_path: Path, event_name: str, head_ref: str | None
) -> None:
    repo, head = _pull_request(tmp_path)

    outputs = _classify(repo, f"{head}^", head, event_name=event_name, head_ref=head_ref)

    assert json.loads(outputs["stress_matrix"]) == {"include": []}
    assert _jobs(outputs)[STRESS_JOB] is False
    assert STRESS_JOB not in _required(outputs)
    assert "test" in _required(outputs)


@pytest.mark.parametrize(
    "path",
    [
        "tests/bwave/test_contract.py",
        "src/booley/bwave/cli.py",
        "tests/flows/sim/test_execution_engine.py",
    ],
)
def test_native_bwave_changes_require_the_owning_integration_job(path: str) -> None:
    categories = classify([path])

    assert "native_bwave" in categories
    assert "bwave-integration" in required_jobs(categories)


def test_rename_classifies_both_old_and_new_paths(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    _write(repo, "docs/example.py")
    _write(repo, "tests/unit/test_example.py")
    old_head = _commit(repo, "add old paths")
    (repo / "src/booley").mkdir(parents=True)
    _git(repo, "mv", "docs/example.py", "src/booley/example.py")
    (repo / "tests/unit/test_example.py").unlink()
    head = _commit(repo, "rename into source and delete a test")

    outputs = _classify(repo, old_head, head)

    assert outputs["docs"] == "true"
    assert outputs["python_source"] == "true"
    assert outputs["python_tests"] == "true"
    assert outputs["image_tests"] == "false"
    assert {"test", "package-artifacts"} <= _required(outputs)
    assert "bwave-smoke" not in _required(outputs)


@pytest.mark.parametrize(
    "path",
    sorted(
        RISCV_IMAGE_FILES
        | {
            ".dockerignore",
            "src/booley/data/docker/Dockerfile",
            "src/booley/data/docker/Dockerfile.riscv",
            # The tooling key and its publisher decide which toolchain the
            # RISC-V candidate composes, so they must run the full lane.
            ".github/scripts/riscv_tooling.py",
            ".github/workflows/riscv-tooling-publish.yml",
            "src/booley/runtime/dockerfile_syntax.py",
            ".github/actions/prepare-picorv32-demo/action.yml",
            "demo/picorv32.core",
            # The demo evidence input needs no git to select the RISC-V validation.
            "src/booley/evidence/acceptance.py",
        }
    ),
)
def test_riscv_image_input_requests_extended_image_smoke(path: str) -> None:
    categories = classify([path])

    assert "riscv_image" in categories
    assert {"package-artifacts", "bwave-smoke", "riscv-image"} <= required_jobs(categories)


@pytest.mark.parametrize(
    ("path", "riscv"),
    [
        # PR #298: image-contract code accepted an invalid runtime user.
        (".github/scripts/image_size_report.py", True),
        # PR #308: Simulation runtime overlays leaked into the issued image.
        ("src/booley/flows/sim/flow.py", False),
        # PR #315: host Doctor and Sandbox identity diverged.
        ("src/booley/harness/doctor.py", False),
        ("src/booley/harness/session_runtime.py", False),
        ("src/booley/mcp/server.py", False),
        ("tests/fixtures/cocotb_counter/README.md", False),
        # PR #350: shared size/resource contracts own both image variants.
        (".github/contracts/image-size-limits.toml", True),
        (".github/scripts/image_runtime_resources.py", True),
        # PR #351: synthesis changes must exercise the OpenROAD promotion probe.
        ("src/booley/flows/synth/flow.py", False),
        ("src/booley/mcp/project_flow_loader.py", False),
        ("src/booley/runtime/agent_execution.py", False),
        ("tests/runtime/test_agent_execution.py", False),
        # The OpenROAD probe belongs to the standard image only.
        (".github/scripts/verify_openroad_runtime.sh", False),
    ],
)
def test_release_regression_paths_request_focused_image_smoke(path: str, riscv: bool) -> None:
    categories = classify([path])
    jobs = required_jobs(categories)

    assert "release_sensitive" in categories
    assert "standard_image" in categories
    assert ("riscv_image" in categories) is riscv
    assert ("riscv-image" in jobs) is riscv
    assert {"package-artifacts", "bwave-smoke"} <= jobs


@pytest.mark.parametrize(
    ("path", "sidecar"),
    [
        *(
            (path, True)
            for path in (
                "src/booley/data/docker/Dockerfile.egress-proxy",
                "src/booley/data/docker/Dockerfile.flexnet-relay",
                "src/booley/data/docker/Dockerfile.reaper",
                "src/booley/docker/egress_proxy.py",
                "src/booley/eda/provisioning/licensing/relay.py",
                "src/booley/runtime/dockerfile_syntax.py",
                "tests/docker/test_sidecar_image_helpers.py",
                "tests/sidecar_image_helpers.py",
                "tests/docker/test_egress_proxy.py",
                "tests/docker/test_proxy_entry.py",
                "tests/docker/test_reaper.py",
                "tests/docker/test_egress_proxy_image_e2e.py",
                "tests/docker/test_reaper_image_e2e.py",
                "tests/docker/test_flexnet_relay_e2e.py",
            )
        ),
        ("docs/image-architecture.md", False),
    ],
)
def test_sidecar_input_requires_its_smoke_job(path: str, sidecar: bool) -> None:
    categories = classify(path.split())

    assert ("sidecar" in categories) is sidecar
    assert ("sidecar-smoke" in required_jobs(categories)) is sidecar


def test_main_push_runs_only_the_cache_writing_tier(tmp_path: Path) -> None:
    """The queue already validated main's tree; a main push runs only `build-dist` and `bwave-smoke`."""
    repo, base = _repository(tmp_path)
    _write(repo, "src/booley/data/docker/Dockerfile.base", "FROM scratch\n")
    head = _commit(repo, "stable base change")

    outputs = _classify(repo, base, head, force_all=True, event_name="push", ref=MAIN_REF)

    assert _required(outputs) == {"changes", "release-semantic", "build-dist", "bwave-smoke"}
    assert {job for job, selected in _jobs(outputs).items() if selected} == {
        "build-dist",
        "bwave-smoke",
    }
    # bwave-smoke still learns which images this push must build.
    assert outputs["build_stable_base"] == "true"
    # A RISC-V input changed too, but the lane is not a cache writer: the
    # queue candidate already ran `riscv-image` on this tree.
    assert outputs["riscv_image"] == "true"


@pytest.mark.parametrize(
    ("event_name", "ref"),
    [("schedule", MAIN_REF), ("workflow_dispatch", MAIN_REF), ("push", "refs/tags/v1.2.3")],
)
def test_nightly_manual_and_release_runs_keep_every_job(
    tmp_path: Path, event_name: str, ref: str
) -> None:
    repo, base = _repository(tmp_path)
    _write(repo, "docs/guide.md")
    head = _commit(repo, "docs change")

    outputs = _classify(repo, base, head, force_all=True, event_name=event_name, ref=ref)

    # `riscv-image` stays path-gated: forced runs rebuild the RISC-V image only
    # when its compatibility inputs changed.
    assert outputs["riscv_image"] == "false"
    assert _required(outputs) == set(ALL_JOBS) - {STRESS_JOB, "riscv-image"}


@pytest.mark.skipif(os.name == "nt", reason="Win32 rejects newlines in path components")
def test_rust_fixture_and_nul_unsafe_filename_are_not_lost(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    _write(repo, "crates/bwave/tests/fixtures/trace.fst")
    _write(repo, "docs/line\nbreak.md")
    head = _commit(repo, "unusual paths")

    outputs = _classify(repo, base, head)

    assert outputs["rust"] == "true"
    assert outputs["docs"] == "true"
    assert {"rust-test", "bwave-integration", "bwave-smoke"} <= _required(outputs)


def test_aggregate_accepts_intentional_skips_and_rejects_required_failure() -> None:
    needs = {
        "changes": {"result": "success"},
        "test": {"result": "success"},
        "rust-test": {"result": "skipped"},
        "bwave-smoke": {"result": "skipped"},
    }
    base_command = [
        sys.executable,
        str(_AGGREGATOR),
        "--required",
        "changes,test",
        "--needs-json",
        json.dumps(needs),
    ]

    accepted = subprocess.run(base_command, capture_output=True, text=True, check=False)
    needs["test"]["result"] = "skipped"
    skipped_required = subprocess.run(
        [*base_command[:-1], json.dumps(needs)], capture_output=True, text=True, check=False
    )
    needs["test"]["result"] = "failure"
    rejected = subprocess.run(
        [*base_command[:-1], json.dumps(needs)], capture_output=True, text=True, check=False
    )

    assert accepted.returncode == 0, accepted.stderr
    assert skipped_required.returncode == 1
    assert "test=skipped" in skipped_required.stderr
    assert rejected.returncode == 1
    assert "test=failure" in rejected.stderr


def _merged_source_change(tmp_path: Path) -> tuple[Path, str, str]:
    """Return a repo, its first commit, and a two-parent merge touching Python source."""
    repo, base = _repository(tmp_path)
    _git(repo, "switch", "-c", "feature")
    _write(repo, "src/booley/example.py")
    _commit(repo, "feature change")
    _git(repo, "switch", "main")
    _write(repo, "docs/upstream.md")
    _commit(repo, "upstream change")
    return repo, base, _merge(repo, "feature")


_TODAYS_LINUX_FULL_COMMAND = (
    "python .github/scripts/ci_pytest_rerun.py"
    ' --junitxml "${{ runner.temp }}/junit.xml" --basetemp "${{ runner.temp }}/pytest"'
    " pytest tests/ -q --tb=short -n 4 --dist=loadscope"
    ' -m "${{ env.TEST_MARK_EXPRESSION }}"'
    " --timeout=60 ${{ runner.os == 'Windows' && '"
    "--timeout-headroom-warn=0.5' || '' }}"
    " --durations=30 -p ci_pytest_shard"
    ' --ci-timing-output "${{ runner.temp }}/timing/test-timings.json"'
)


def test_default_linux_inputs_shard_ubuntu_in_two_and_keep_linux_command(tmp_path: Path) -> None:
    repo, base, head = _merged_source_change(tmp_path)
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8"))
    full_step = next(
        step
        for step in workflow["jobs"]["test"]["steps"]
        if step.get("name") == "Run full compatibility suite"
    )
    todays_matrix = {
        "include": [
            *(
                {
                    "name": f"ubuntu-{python}-shard-{index + 1}-of-2",
                    "os": "ubuntu-latest",
                    "python": python,
                    "mode": "shard",
                    "group": f"linux-{python}",
                    "shard_index": index,
                    "shard_count": 2,
                    "mark_expression": "not native_bwave",
                }
                for python in ("3.11",)
                for index in range(2)
            ),
            {
                "name": "windows-3.11-compatibility",
                "os": "windows-latest",
                "python": "3.11",
                "mode": "compatibility",
                "mark_expression": "os_specific and not native_bwave",
            },
            {
                "name": "windows-3.13-compatibility",
                "os": "windows-latest",
                "python": "3.13",
                "mode": "compatibility",
                "mark_expression": "os_specific and not native_bwave",
            },
            *(
                {
                    "name": f"windows-3.14-shard-{index + 1}-of-10",
                    "os": "windows-latest",
                    "python": "3.14",
                    "mode": "shard",
                    "group": "windows",
                    "shard_index": index,
                    "shard_count": 10,
                    "mark_expression": "os_specific and not native_bwave",
                }
                for index in range(10)
            ),
        ]
    }

    for event in ("pull_request", "push", "workflow_call"):
        outputs = _classify(
            repo,
            base,
            head,
            event_name=event,
            windows_shard_count=10,
            linux_options={
                "shard-count": "2",
                "scheduler": "loadscope",
                "shard-benchmark": "false",
            },
        )
        rendered = full_step["run"].replace(
            "${{ needs.changes.outputs.linux_scheduler }}", outputs["linux_scheduler"]
        )

        assert json.loads(outputs["test_matrix"]) == todays_matrix
        assert (outputs["linux_shard_count"], outputs["linux_scheduler"]) == ("2", "loadscope")
        assert " ".join(rendered.split()) == _TODAYS_LINUX_FULL_COMMAND


@pytest.mark.parametrize("count", [1, 3])
def test_linux_shard_dispatch_emits_matrix_for_a_count_and_scheduler(
    tmp_path: Path, count: int
) -> None:
    repo, base, head = _change(tmp_path, "src/booley/example.py")
    non_linux = build_test_matrix(4, linux_shard_count=1)["include"][1:]

    outputs = _classify(
        repo,
        base,
        head,
        event_name="workflow_dispatch",
        linux_options={"shard-count": str(count), "scheduler": "worksteal"},
    )
    entries = json.loads(outputs["test_matrix"])["include"]
    linux = [entry for entry in entries if entry["os"] == "ubuntu-latest"]

    assert (outputs["linux_shard_count"], outputs["linux_scheduler"]) == (str(count), "worksteal")
    assert entries[len(linux) :] == non_linux
    if count == 1:
        assert linux == build_test_matrix(4, linux_shard_count=1)["include"][:1]
        return
    assert [entry["name"] for entry in linux] == [
        f"ubuntu-3.11-shard-{index + 1}-of-{count}" for index in range(count)
    ]
    assert all(
        (entry["mode"], entry["group"], entry["shard_count"])
        == ("shard", f"linux-{entry['python']}", count)
        for entry in linux
    )
    assert [entry["shard_index"] for entry in linux] == list(range(count))


# The jobs that download the `pr-dist` artifact in test.yml.
_DIST_CONSUMERS = {"package-artifacts", "bwave-smoke", "riscv-image"}


@pytest.mark.parametrize("stress", [False, True], ids=["plain", "stress"])
@pytest.mark.parametrize("category", CATEGORIES)
def test_paired_jobs_run_together(category: str, stress: bool) -> None:
    """A consumer without build-dist would be skipped; build-dist alone wastes a runner.

    ``coverage-shards`` produces the data ``coverage`` combines, so either alone is a bug
    (914c8401d decoupled them once).
    """
    jobs = required_jobs({category}, stress=stress)
    assert ("build-dist" in jobs) == bool(jobs & _DIST_CONSUMERS), sorted(jobs)
    assert ("coverage" in jobs) == ("coverage-shards" in jobs), sorted(jobs)


def test_build_dist_selection_covers_both_outcomes() -> None:
    """Keeps the per-category test above from passing vacuously."""
    assert "build-dist" in required_jobs({"standard_image"})
    assert "build-dist" in required_jobs({"riscv_image"})
    assert "build-dist" in required_jobs(set(), main_push=True)
    assert "build-dist" not in required_jobs({"python_tests"})
    assert "build-dist" not in required_jobs({"renovate"})
