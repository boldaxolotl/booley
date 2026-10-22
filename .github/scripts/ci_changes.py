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
WINDOWS_SHARD_COUNTS = (4, 6, 8, 10)
# Explicit RISC-V timing arms. "automatic" keeps path-gated selection. "cold"
# is the forced-local arm of the controlled pairs (ADR 0070): it builds the
# RISC-V tooling stage instead of reusing the published image, so it must build
# the RISC-V image even when the dispatched commit does not touch its inputs.
RISCV_MEASUREMENT_ARMS = ("automatic", "cold")
DEFAULT_WINDOWS_SHARD_COUNT = 10
# Manual Ubuntu experiment: 1 keeps one complete-suite job per Python version.
LINUX_SHARD_COUNTS = (1, 2, 3)
DEFAULT_LINUX_SHARD_COUNT = 1
LINUX_SCHEDULERS = ("loadscope", "worksteal")
DEFAULT_LINUX_SCHEDULER = "loadscope"
_LINUX_PYTHONS = ("3.11", "3.14")
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
    "src/booley/goals/",
    "tests/goals/",
    "src/booley/review/",
    "src/booley/criteria/unbound_ledger.py",
    "src/booley/mcp/unbound_recorder.py",
    "src/booley/mcp/project_flow_loader.py",
    "src/booley/runtime/agent_execution.py",
    "src/booley/specialists/verification_freshness.py",
    "src/booley/flows/sim/waiver_candidates.py",
    "tests/criteria/test_evidence_ledger",
    "tests/criteria/ledger_support.py",
    "tests/golden/test_golden_acceptance_ledger.py",
    "tests/mcp_tools/test_unbound",
    "tests/runtime/test_agent_execution.py",
    "tests/flows/sim/test_waiver_candidates.py",
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


def _documentation_categories(path: str) -> set[str]:
    if path.startswith("website/"):
        # The project site shares pinned facts with the README (install
        # commands, demo revision, Goal notation); tests/test_site_contract.py
        # guards them, so site edits must run the Python tests too.
        return {"docs", "python_tests"}
    if path.startswith("docs/") or path.endswith(".md"):
        return {"docs"}
    return set()


def _path_categories(path: str) -> set[str]:
    categories = _documentation_categories(path)
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


def _linux_entries(linux_shard_count: int) -> list[dict[str, object]]:
    """Return the Ubuntu legs: complete-suite jobs, or shards per Python version."""
    if linux_shard_count == 1:
        return [
            {"name": f"ubuntu-{py}-full", "os": "ubuntu-latest", "python": py, "mode": "full"}
            for py in _LINUX_PYTHONS
        ]
    return [
        {
            "name": f"ubuntu-{py}-shard-{index + 1}-of-{linux_shard_count}",
            "os": "ubuntu-latest",
            "python": py,
            "mode": "shard",
            "group": f"linux-{py}",
            "shard_index": index,
            "shard_count": linux_shard_count,
        }
        for py in _LINUX_PYTHONS
        for index in range(linux_shard_count)
    ]


def build_test_matrix(
    windows_shard_count: int, linux_shard_count: int = DEFAULT_LINUX_SHARD_COUNT
) -> dict[str, list[dict[str, object]]]:
    """Build the compatibility matrix for one supported Windows and Linux shard count."""
    if windows_shard_count not in WINDOWS_SHARD_COUNTS:
        raise ValueError(
            f"Windows shard count must be one of {', '.join(map(str, WINDOWS_SHARD_COUNTS))}"
        )
    if linux_shard_count not in LINUX_SHARD_COUNTS:
        raise ValueError(
            f"Linux shard count must be one of {', '.join(map(str, LINUX_SHARD_COUNTS))}"
        )
    include: list[dict[str, object]] = [
        *_linux_entries(linux_shard_count),
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
    destination: Path,
    categories: set[str],
    base: str,
    windows_shard_count: int,
    linux_shard_count: int,
    linux_scheduler: str,
) -> None:
    jobs = required_jobs(categories)
    with destination.open("a", encoding="utf-8") as stream:
        for category in CATEGORIES:
            print(f"{category}={'true' if category in categories else 'false'}", file=stream)
        job_policy = {job: job in jobs for job in CONDITIONAL_JOBS}
        print(f"jobs={json.dumps(job_policy, separators=(',', ':'))}", file=stream)
        print(
            f"test_matrix={json.dumps(build_test_matrix(windows_shard_count, linux_shard_count), separators=(',', ':'))}",
            file=stream,
        )
        print(f"linux_shard_count={linux_shard_count}", file=stream)
        print(f"linux_scheduler={linux_scheduler}", file=stream)
        print(
            f"build_stable_base={'true' if 'stable_base' in categories else 'false'}", file=stream
        )
        print(f"required_jobs={','.join(job for job in ALL_JOBS if job in jobs)}", file=stream)
        print(f"diff_base={base}", file=stream)


def _validate_dispatch_options(args: argparse.Namespace) -> None:
    """Reject manual-experiment options outside workflow_dispatch or with a RISC-V arm."""
    dispatched = args.event_name == "workflow_dispatch"
    if args.riscv_measurement != "automatic" and not dispatched:
        raise ValueError("RISC-V measurement arms require workflow_dispatch")
    if args.windows_shard_benchmark and args.riscv_measurement != "automatic":
        raise ValueError(
            "Windows shard benchmarking cannot be combined with a RISC-V measurement arm"
        )
    if args.windows_shard_benchmark and not dispatched:
        raise ValueError("Windows shard benchmarking requires workflow_dispatch")
    if not dispatched and (
        args.linux_shard_count != DEFAULT_LINUX_SHARD_COUNT
        or args.linux_scheduler != DEFAULT_LINUX_SCHEDULER
    ):
        raise ValueError("Linux shard count and scheduler overrides require workflow_dispatch")
    if args.linux_shard_benchmark and args.riscv_measurement != "automatic":
        raise ValueError(
            "Linux shard benchmarking cannot be combined with a RISC-V measurement arm"
        )
    if args.linux_shard_benchmark and not dispatched:
        raise ValueError("Linux shard benchmarking requires workflow_dispatch")


def _parse_args() -> argparse.Namespace:
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
        "--linux-shard-count",
        type=int,
        choices=LINUX_SHARD_COUNTS,
        default=DEFAULT_LINUX_SHARD_COUNT,
    )
    parser.add_argument(
        "--linux-scheduler", choices=LINUX_SCHEDULERS, default=DEFAULT_LINUX_SCHEDULER
    )
    parser.add_argument("--linux-shard-benchmark", type=_boolean, default=False)
    parser.add_argument(
        "--event-name",
        choices=("", "pull_request", "push", "workflow_call", "workflow_dispatch"),
        default="",
    )
    parser.add_argument("--riscv-measurement", choices=RISCV_MEASUREMENT_ARMS, default="automatic")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        _validate_dispatch_options(args)
        base = _diff_base(args.repo, args.base, args.head, args.event_name)
        paths = _changed_paths(_git_diff(args.repo, base, args.head))
        if args.windows_shard_benchmark or args.linux_shard_benchmark:
            categories = {"python_source"}
        else:
            categories = classify(
                paths, force_all=args.force_all, riscv_measurement=args.riscv_measurement
            )
        _write_outputs(
            args.github_output,
            categories,
            base,
            args.windows_shard_count,
            args.linux_shard_count,
            args.linux_scheduler,
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"CI categories: {','.join(sorted(categories))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
