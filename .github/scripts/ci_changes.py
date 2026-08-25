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

CATEGORIES = (
    "docs",
    "python_source",
    "python_tests",
    "image_tests",
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
    "rust-test",
    "bwave-integration",
    "package-artifacts",
    "bwave-smoke",
)
ALL_JOBS = ("changes", *CONDITIONAL_JOBS)
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
_IMAGE_TEST_FILES = {"tests/sim/test_bwave_fifo_pipeline.py"}
_NATIVE_BWAVE_PREFIXES = ("src/booley/bwave/", "tests/bwave/")


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
    if path.startswith(_NATIVE_BWAVE_PREFIXES):
        categories.add("native_bwave")
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
    if path in _STABLE_BASE_FILES | _STABLE_BASE_ORCHESTRATION_FILES:
        categories.add("stable_base")
    if path.startswith((".github/workflows/", ".github/actions/", ".github/scripts/")):
        categories.add("workflow")
    if path in {
        ".github/workflows/publish.yml",
        ".github/workflows/docker-publish.yml",
        "CHANGELOG.md",
    }:
        categories.add("release")
    return categories


def classify(paths: Iterable[str], *, force_all: bool = False) -> set[str]:
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
        # 57-minute runtime base when its actual compatibility inputs changed.
        categories.update(set(CATEGORIES) - {"stable_base"})
    return categories


def required_jobs(categories: set[str]) -> set[str]:
    if categories & {"full", "workflow", "packaging", "release"}:
        return set(ALL_JOBS)
    jobs = {"changes"}
    if "docs" in categories:
        jobs.add("docs-check")
    if categories & {"python_source", "python_tests"}:
        jobs.update({"lint", "test"})
    if "rust" in categories:
        jobs.update({"rust-test", "bwave-integration", "package-artifacts", "bwave-smoke"})
    if "native_bwave" in categories:
        jobs.add("bwave-integration")
    if "python_source" in categories:
        jobs.update({"package-artifacts", "bwave-smoke"})
    if categories & {"docker_toolchain", "image_tests"}:
        jobs.update({"package-artifacts", "bwave-smoke"})
    return jobs


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


def _diff_base(repo: Path, base: str, head: str) -> str:
    if base and not set(base) <= {"0"}:
        return base
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"{head}^"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else head


def _write_outputs(destination: Path, categories: set[str], base: str) -> None:
    jobs = required_jobs(categories)
    with destination.open("a", encoding="utf-8") as stream:
        for category in CATEGORIES:
            print(f"{category}={'true' if category in categories else 'false'}", file=stream)
        job_policy = {job: job in jobs for job in CONDITIONAL_JOBS}
        print(f"jobs={json.dumps(job_policy, separators=(',', ':'))}", file=stream)
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
    args = parser.parse_args()
    try:
        base = _diff_base(args.repo, args.base, args.head)
        paths = _changed_paths(_git_diff(args.repo, base, args.head))
        categories = classify(paths, force_all=args.force_all)
        _write_outputs(args.github_output, categories, base)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"CI categories: {','.join(sorted(categories))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
