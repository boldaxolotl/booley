"""Behavioral contracts for change-aware CI selection and aggregation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]
_CLASSIFIER = _ROOT / ".github/scripts/ci_changes.py"
_AGGREGATOR = _ROOT / ".github/scripts/ci_required.py"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    env = os.environ | {
        "GIT_AUTHOR_NAME": "CI Test",
        "GIT_AUTHOR_EMAIL": "ci@example.invalid",
        "GIT_COMMITTER_NAME": "CI Test",
        "GIT_COMMITTER_EMAIL": "ci@example.invalid",
    }
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", message],
        check=True,
        capture_output=True,
        env=env,
    )
    return _git(repo, "rev-parse", "HEAD")


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    return repo, _commit(repo, "initial")


def _classify(repo: Path, base: str, head: str, *, force_all: bool = False) -> dict[str, str]:
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
        "--force-all",
        str(force_all).lower(),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
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


def test_mixed_docker_and_docs_changes_require_image_path(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    _write(repo, "docs/setup.md")
    _write(repo, "src/booley/data/docker/Dockerfile", "FROM scratch\n")
    head = _commit(repo, "docker and docs")

    outputs = _classify(repo, base, head)

    assert outputs["docs"] == "true"
    assert outputs["docker_toolchain"] == "true"
    assert {"docs-check", "package-artifacts", "bwave-smoke"} <= _required(outputs)


def test_pure_docker_change_requires_only_image_build_path(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    _write(repo, "Dockerfile.tools", "FROM scratch\n")
    head = _commit(repo, "docker toolchain only")

    outputs = _classify(repo, base, head)

    assert outputs["docker_toolchain"] == "true"
    assert _required(outputs) == {"changes", "package-artifacts", "bwave-smoke"}


def test_stable_base_input_requests_local_compatibility_build(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    _write(repo, "src/booley/data/docker/Dockerfile.base", "FROM scratch\n")
    head = _commit(repo, "stable base")

    outputs = _classify(repo, base, head)

    assert outputs["stable_base"] == "true"
    assert outputs["build_stable_base"] == "true"
    assert _required(outputs) == {
        "changes",
        "lint",
        "test",
        "package-artifacts",
        "bwave-smoke",
    }


def test_docs_only_requires_only_lightweight_tests_aggregate_inputs(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    _write(repo, "docs/architecture.md")
    head = _commit(repo, "docs only")

    outputs = _classify(repo, base, head)

    assert outputs["docs"] == "true"
    assert _required(outputs) == {"changes", "docs-check"}
    assert _jobs(outputs)["docs-check"] is True
    assert _jobs(outputs)["test"] is False


@pytest.mark.parametrize(
    "path",
    ["tests/bwave/test_contract.py", "src/booley/bwave/cli.py"],
)
def test_native_bwave_changes_require_the_owning_integration_job(
    tmp_path: Path, path: str
) -> None:
    repo, base = _repository(tmp_path)
    _write(repo, path)
    head = _commit(repo, "native B-Wave change")

    outputs = _classify(repo, base, head)

    assert outputs["native_bwave"] == "true"
    assert _jobs(outputs)["bwave-integration"] is True
    assert "bwave-integration" in _required(outputs)


def test_rename_classifies_both_old_and_new_paths(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    _write(repo, "docs/example.py")
    old_head = _commit(repo, "add old path")
    (repo / "src/booley").mkdir(parents=True)
    _git(repo, "mv", "docs/example.py", "src/booley/example.py")
    head = _commit(repo, "rename into source")

    outputs = _classify(repo, old_head, head)

    assert outputs["docs"] == "true"
    assert outputs["python_source"] == "true"
    assert {"test", "package-artifacts", "bwave-smoke"} <= _required(outputs)
    assert base != head


def test_deleted_python_test_still_requires_matrix_but_not_image(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    _write(repo, "tests/unit/test_example.py")
    base = _commit(repo, "add test")
    (repo / "tests/unit/test_example.py").unlink()
    head = _commit(repo, "delete test")

    outputs = _classify(repo, base, head)

    assert outputs["python_tests"] == "true"
    assert outputs["image_tests"] == "false"
    assert "test" in _required(outputs)
    assert "bwave-smoke" not in _required(outputs)


def test_image_related_test_requires_image_smoke(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    _write(repo, "tests/docker/test_image.py")
    head = _commit(repo, "add image test")

    outputs = _classify(repo, base, head)

    assert outputs["python_tests"] == "true"
    assert outputs["image_tests"] == "true"
    assert {"test", "package-artifacts", "bwave-smoke"} <= _required(outputs)


def test_readme_and_version_are_packaging_and_release_inputs(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    _write(repo, "README.md")
    _write(repo, "VERSION", "9.9.9\n")
    head = _commit(repo, "package metadata")

    outputs = _classify(repo, base, head)

    assert outputs["packaging"] == "true"
    assert outputs["release"] == "true"
    assert _required(outputs) >= {
        "lint",
        "test",
        "rust-test",
        "bwave-integration",
        "package-artifacts",
        "bwave-smoke",
    }


def test_workflow_change_and_force_all_require_every_job(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    _write(repo, ".github/workflows/example.yml")
    head = _commit(repo, "workflow")

    workflow = _classify(repo, base, head)
    forced = _classify(repo, base, head, force_all=True)

    expected = {
        "changes",
        "docs-check",
        "lint",
        "test",
        "rust-test",
        "bwave-integration",
        "package-artifacts",
        "bwave-smoke",
    }
    assert workflow["workflow"] == "true"
    assert _required(workflow) == expected
    assert forced["full"] == "true"
    assert forced["stable_base"] == "false"
    assert _required(forced) == expected


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


def test_unknown_path_fails_safe_to_full_ci(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    _write(repo, "unmapped/config.xyz")
    head = _commit(repo, "unknown")

    outputs = _classify(repo, base, head)

    assert outputs["full"] == "true"
    assert "bwave-smoke" in _required(outputs)


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
