#!/usr/bin/env python3
"""Classify a NUL-delimited Git diff and emit change-aware CI outputs."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from docker_base_contract import stable_base_inputs
from picorv32_ci_inputs import RISCV_IMAGE_FILES, RISCV_IMAGE_PREFIXES

CATEGORIES = (
    "docs",
    "python_source",
    "python_tests",
    "image_tests",
    "release_sensitive",
    "standard_image",
    "riscv_image",
    "sidecar",
    "native_bwave",
    "rust",
    "docker_toolchain",
    "stable_base",
    "packaging",
    "workflow",
    "exhaustive_recovery",
    "release",
    "full",
)
CONDITIONAL_JOBS = (
    "docs-check",
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
)
ALWAYS_JOBS = ("changes", "release-semantic")
ALL_JOBS = (*ALWAYS_JOBS, *CONDITIONAL_JOBS)
WINDOWS_SHARD_COUNTS = (4, 6, 8)
# Explicit RISC-V timing arms. "automatic" keeps path-gated selection; every
# other arm is a controlled measurement sample and must build the RISC-V image
# even when the dispatched commit does not touch its inputs.
RISCV_MEASUREMENT_ARMS = ("automatic", "baseline", "warm", "cold")
DEFAULT_WINDOWS_SHARD_COUNT = 6
_STABLE_BASE_FILES = set(stable_base_inputs(Path(__file__).parents[2]))
_STABLE_BASE_ORCHESTRATION_FILES = {
    "src/booley/data/docker/stable-base-inputs.txt",
}
_PACKAGING_FILES = {
    "LICENSE",
    "MANIFEST.in",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "VERSION",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
}
_IMAGE_TEST_PREFIXES = ("tests/docker/", "tests/smoke/")
_IMAGE_TEST_FILES = {"tests/bwave/test_waveform_store_streaming.py"}
_RELEASE_SENSITIVE_PREFIXES = (
    ".github/actions/",
    ".github/contracts/",
    ".github/scripts/",
    ".github/workflows/",
    "demo/",
    "src/booley/eda/provisioning/",
    "src/booley/flows/sim/",
    "src/booley/flows/synth/",
    "src/booley/harness/",
    "src/booley/mcp/",
    "src/booley/runtime/",
    "src/booley/ticket_board/",
    "tests/eda/provisioning/",
    "tests/docker/",
    "tests/flows/sim/",
    "tests/flows/synth/",
    "tests/fixtures/cocotb_counter/",
    "tests/harness/",
    "tests/mcp_tools/",
    "tests/smoke/",
)
_RELEASE_SENSITIVE_FILES = {
    ".dockerignore",
    "MANIFEST.in",
    "VERSION",
    "pyproject.toml",
}
_STANDARD_IMAGE_PREFIXES = _RELEASE_SENSITIVE_PREFIXES
_STANDARD_IMAGE_FILES = _RELEASE_SENSITIVE_FILES
_NATIVE_BWAVE_PREFIXES = ("src/booley/bwave/", "tests/bwave/")
_NATIVE_BWAVE_FILES = {"tests/flows/sim/test_execution_engine.py"}
_SIDECAR_PREFIXES = (
    "src/booley/docker/",
    "src/booley/eda/provisioning/licensing/",
)
_SIDECAR_FILES = {
    "src/booley/data/docker/Dockerfile.egress-proxy",
    "src/booley/data/docker/Dockerfile.flexnet-relay",
    "src/booley/data/docker/Dockerfile.reaper",
    "tests/docker/test_egress_proxy.py",
    "tests/docker/test_egress_proxy_image_e2e.py",
    "tests/docker/test_flexnet_relay_e2e.py",
    "tests/docker/test_proxy_entry.py",
    "tests/docker/test_reaper.py",
    "tests/docker/test_reaper_image_e2e.py",
    "tests/docker/test_sidecar_image_helpers.py",
}
_EXHAUSTIVE_RECOVERY_PREFIXES = (
    ".github/ci/",
    ".github/scripts/",
    ".github/workflows/",
    "src/booley/ticket_board/",
    "tests/ticket_board/",
)
_EXHAUSTIVE_RECOVERY_FILES = {
    "pyproject.toml",
    "src/booley/flows/baseline_worktree.py",
    "src/booley/runtime/git.py",
    "src/booley/core/scope_matching.py",
    "tests/conftest.py",
}


def _sidecar_categories(path: str) -> tuple[str, ...]:
    if path.startswith(_SIDECAR_PREFIXES) or path in _SIDECAR_FILES:
        return ("sidecar",)
    return ()


def _riscv_image_categories(path: str) -> tuple[str, ...]:
    if path.startswith(RISCV_IMAGE_PREFIXES) or path in RISCV_IMAGE_FILES:
        return ("riscv_image",)
    return ()


def _release_image_categories(path: str) -> tuple[str, ...]:
    categories: list[str] = []
    if path.startswith(_RELEASE_SENSITIVE_PREFIXES) or path in _RELEASE_SENSITIVE_FILES:
        categories.append("release_sensitive")
    if path.startswith(_STANDARD_IMAGE_PREFIXES) or path in _STANDARD_IMAGE_FILES:
        categories.append("standard_image")
    return tuple(categories)


def _recovery_categories(path: str) -> tuple[str, ...]:
    if path.startswith(_EXHAUSTIVE_RECOVERY_PREFIXES) or path in _EXHAUSTIVE_RECOVERY_FILES:
        return ("exhaustive_recovery",)
    return ()


def _boolean(value: str) -> bool:
    normalized = value.casefold()
    if normalized not in {"true", "false"}:
        raise argparse.ArgumentTypeError("expected true or false")
    return normalized == "true"


def _changed_paths(raw: bytes) -> list[str]:
    fields = raw.split(b"\0")
    if fields and not fields[-1]:
        fields.pop()
    paths: list[str] = []
    index = 0
    while index < len(fields):
        status = fields[index].decode("ascii")
        index += 1
        path_count = 2 if status[:1] in {"R", "C"} else 1
        if index + path_count > len(fields):
            raise ValueError("malformed NUL-delimited git diff")
        paths.extend(
            field.decode("utf-8", errors="surrogateescape")
            for field in fields[index : index + path_count]
        )
        index += path_count
    return paths


def _path_categories(path: str) -> set[str]:
    categories: set[str] = set()
    if path.startswith("docs/") or path.endswith(".md"):
        categories.add("docs")
    if path in _PACKAGING_FILES:
        categories.add("packaging")
    if path == "VERSION" or path.startswith("release-assets/"):
        categories.add("release")
    if path.startswith("src/booley/"):
        categories.add("python_source")
    if path.startswith("tests/"):
        categories.add("python_tests")
    if path.startswith(_IMAGE_TEST_PREFIXES) or path in _IMAGE_TEST_FILES:
        categories.add("image_tests")
    categories.update(_release_image_categories(path))
    categories.update(_riscv_image_categories(path))
    categories.update(_recovery_categories(path))
    if path.startswith(_NATIVE_BWAVE_PREFIXES) or path in _NATIVE_BWAVE_FILES:
        categories.add("native_bwave")
    categories.update(_sidecar_categories(path))
    if path.startswith("crates/") or path in {"Cargo.lock", "Cargo.toml"}:
        categories.add("rust")
    if (
        path == ".dockerignore"
        or path.startswith(".devcontainer/")
        or path.startswith("src/booley/data/docker/")
        or path.startswith("src/booley/data/edalize/")
        or Path(path).name.startswith("Dockerfile")
    ):
        categories.add("docker_toolchain")
        categories.add("release_sensitive")
        categories.add("standard_image")
        categories.add("riscv_image")
    if path in _STABLE_BASE_FILES | _STABLE_BASE_ORCHESTRATION_FILES:
        categories.add("stable_base")
    if path.startswith((".github/workflows/", ".github/actions/", ".github/scripts/")):
        categories.add("workflow")
    if path in {
        ".github/workflows/publish.yml",
        ".github/workflows/docker-publish.yml",
        "CHANGELOG.md",
        "src/booley/data/refs/CHANGELOG.md",
    }:
        categories.add("release")
    return categories


def classify(
    paths: Iterable[str], *, force_all: bool = False, riscv_measurement: str = "automatic"
) -> set[str]:
    categories: set[str] = set()
    for path in paths:
        path_categories = _path_categories(path)
        if not path_categories:
            categories.add("full")
        categories.update(path_categories)
    if not categories:
        categories.add("full")
    if "full" in categories:
        categories.add("exhaustive_recovery")
    if force_all:
        # Main/manual runs execute every conditional job, but only rebuild the
        # 57-minute runtime base or its 9-minute RISC-V extension when their
        # actual compatibility inputs changed.
        categories.update(set(CATEGORIES) - {"stable_base", "riscv_image"})
    if riscv_measurement != "automatic":
        categories.add("riscv_image")
    return categories


def required_jobs(categories: set[str]) -> set[str]:
    if categories & {"full", "workflow", "packaging", "release"}:
        return set(ALL_JOBS)
    jobs = set(ALWAYS_JOBS)
    if "docs" in categories:
        jobs.add("docs-check")
    if categories & {"python_source", "python_tests"}:
        jobs.update({"lint", "test", "test-verify", "coverage-shards", "coverage"})
    if "rust" in categories:
        jobs.update({"rust-test", "bwave-integration", "package-artifacts", "bwave-smoke"})
    if "native_bwave" in categories:
        jobs.add("bwave-integration")
    if "python_source" in categories:
        jobs.add("package-artifacts")
    if categories & {"docker_toolchain", "image_tests"}:
        jobs.update({"package-artifacts", "bwave-smoke"})
    if categories & {"standard_image", "riscv_image"}:
        jobs.update({"package-artifacts", "bwave-smoke"})
    if "sidecar" in categories:
        jobs.add("sidecar-smoke")
    return jobs


def build_test_matrix(windows_shard_count: int) -> dict[str, list[dict[str, object]]]:
    """Build the compatibility matrix for one supported Windows shard count."""
    if windows_shard_count not in WINDOWS_SHARD_COUNTS:
        raise ValueError(
            f"Windows shard count must be one of {', '.join(map(str, WINDOWS_SHARD_COUNTS))}"
        )
    include: list[dict[str, object]] = [
        {"name": "ubuntu-3.11-full", "os": "ubuntu-latest", "python": "3.11", "mode": "full"},
        {"name": "ubuntu-3.14-full", "os": "ubuntu-latest", "python": "3.14", "mode": "full"},
        {
            "name": "windows-3.11-compatibility",
            "os": "windows-latest",
            "python": "3.11",
            "mode": "compatibility",
        },
        {
            "name": "windows-3.13-compatibility",
            "os": "windows-latest",
            "python": "3.13",
            "mode": "compatibility",
        },
    ]
    include.extend(
        {
            "name": f"windows-3.14-shard-{index + 1}-of-{windows_shard_count}",
            "os": "windows-latest",
            "python": "3.14",
            "mode": "shard",
            "group": "windows",
            "shard_index": index,
            "shard_count": windows_shard_count,
        }
        for index in range(windows_shard_count)
    )
    return {"include": include}


def _git_diff(repo: Path, base: str, head: str) -> bytes:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            base,
            head,
            "--",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.decode(errors="replace").strip() or "git diff failed")
    return result.stdout


def _pull_request_base(repo: Path, head: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--parents", "--max-count=1", head],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "git rev-list failed"
        raise ValueError(f"cannot resolve pull_request head {head}: {detail}")
    commit_and_parents = result.stdout.split()
    if len(commit_and_parents) != 3:
        raise ValueError("pull_request head must be a two-parent merge commit")
    return commit_and_parents[1]


def _diff_base(repo: Path, base: str, head: str, event_name: str) -> str:
    if event_name == "pull_request":
        return _pull_request_base(repo, head)
    if base and not set(base) <= {"0"}:
        return base
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"{head}^"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else head


def _write_outputs(
    destination: Path, categories: set[str], base: str, windows_shard_count: int
) -> None:
    jobs = required_jobs(categories)
    with destination.open("a", encoding="utf-8") as stream:
        for category in CATEGORIES:
            print(f"{category}={'true' if category in categories else 'false'}", file=stream)
        job_policy = {job: job in jobs for job in CONDITIONAL_JOBS}
        print(f"jobs={json.dumps(job_policy, separators=(',', ':'))}", file=stream)
        print(
            f"test_matrix={json.dumps(build_test_matrix(windows_shard_count), separators=(',', ':'))}",
            file=stream,
        )
        print(
            f"build_stable_base={'true' if 'stable_base' in categories else 'false'}", file=stream
        )
        print(f"required_jobs={','.join(job for job in ALL_JOBS if job in jobs)}", file=stream)
        print(f"diff_base={base}", file=stream)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base", default="")
    parser.add_argument("--head", required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    parser.add_argument("--force-all", type=_boolean, default=False)
    parser.add_argument(
        "--windows-shard-count",
        type=int,
        choices=WINDOWS_SHARD_COUNTS,
        default=DEFAULT_WINDOWS_SHARD_COUNT,
    )
    parser.add_argument("--windows-shard-benchmark", type=_boolean, default=False)
    parser.add_argument(
        "--event-name",
        choices=("", "pull_request", "push", "workflow_call", "workflow_dispatch"),
        default="",
    )
    parser.add_argument("--riscv-measurement", choices=RISCV_MEASUREMENT_ARMS, default="automatic")
    args = parser.parse_args()
    try:
        if args.riscv_measurement != "automatic" and args.event_name != "workflow_dispatch":
            raise ValueError("RISC-V measurement arms require workflow_dispatch")
        base = _diff_base(args.repo, args.base, args.head, args.event_name)
        paths = _changed_paths(_git_diff(args.repo, base, args.head))
        if args.windows_shard_benchmark:
            if args.event_name != "workflow_dispatch":
                raise ValueError("Windows shard benchmarking requires workflow_dispatch")
            categories = {"python_source"}
        else:
            categories = classify(
                paths, force_all=args.force_all, riscv_measurement=args.riscv_measurement
            )
        _write_outputs(args.github_output, categories, base, args.windows_shard_count)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"CI categories: {','.join(sorted(categories))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
