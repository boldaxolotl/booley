"""Round trip ``booley measure`` on the public demo and on a pinned submodule.

The demo leg measures a disposable clone of the demo Project with the Target
Metrics of the demo contract and requires a Measured Commit. The submodule leg
builds a scaffolded Project whose ``rtl/`` directory is a Git submodule and
requires that the pinned commit is measured, not the live submodule tree.
Nothing here changes the caller's checkouts.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import tomlkit

sys.path.insert(0, str(Path(__file__).parents[3] / "src"))

from booley.dev_support.demo_contract_codec import load_contract
from booley.harness.setup.scaffold import ScaffoldChoices, scaffold_files
from booley.measurement.run_document import is_measured
from booley.runtime.atomic_files import atomic_replace_bytes

SCHEMA = 1
_TIMEOUT_S = 1800
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Booley CI",
    "GIT_AUTHOR_EMAIL": "ci@example.invalid",
    "GIT_COMMITTER_NAME": "Booley CI",
    "GIT_COMMITTER_EMAIL": "ci@example.invalid",
}
_SUBMODULE_NAME = "rtl_pin"
# An undeclared net under ``default_nettype none`` is a Verilator error finding in the top module.
_NETTYPE_NONE = "`default_nettype none\n"
_BAD_ASSIGN = "  assign undeclared_net = en;\nendmodule"


class RoundTripError(RuntimeError):
    """The measurement did not behave as the release requires."""


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", *args],
        cwd=cwd,
        env=os.environ | _GIT_IDENTITY,
        capture_output=True,
        text=True,
        check=False,
        timeout=_TIMEOUT_S,
    )
    if result.returncode != 0:
        raise RoundTripError(f"git {' '.join(args)} failed in {cwd}:\n{result.stderr}")
    return result.stdout.strip()


def _measure(project: Path, booley_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``booley measure`` in *project*; the one place the round trip starts Booley."""
    env = os.environ | {"BOOLEY_DIR": str(booley_dir), "NO_COLOR": "1"}
    return subprocess.run(
        [sys.executable, "-m", "booley.harness.booley", "measure", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=_TIMEOUT_S,
    )


def _describe(label: str, result: subprocess.CompletedProcess[str]) -> str:
    return (
        f"{label}: booley measure exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _document(label: str, result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RoundTripError(
            f"{_describe(label, result)}\nunreadable run document: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise RoundTripError(f"{_describe(label, result)}\nthe run document is not an object")
    return {str(key): value for key, value in document.items()}


def render_metrics(target_metrics: Mapping[str, Mapping[str, Mapping[str, object]]]) -> str:
    """The ``metrics.toml`` text of the contract's Target Metrics."""
    return tomlkit.dumps(
        {
            target: {flow: dict(table) for flow, table in flows.items()}
            for target, flows in target_metrics.items()
        }
    )


def _commit_all(repository: Path, message: str) -> str:
    _git(repository, "add", "-A")
    _git(repository, "commit", "--allow-empty", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def demo_leg(
    project: Path,
    project_state: Path,
    target_metrics: Mapping[str, Mapping[str, Mapping[str, object]]],
    workspace: Path,
) -> dict[str, Any]:
    """Measure a clone of the demo with the contract's Target Metrics."""
    clone = workspace / "demo"
    state = clone / ".booley"
    _git(workspace, "clone", "--quiet", str(project), str(clone))
    _git(workspace, "clone", "--quiet", str(project_state), str(state))
    exclude = clone / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("ab") as stream:
        stream.write(b"\n/.booley\n")
    (state / "metrics.toml").write_bytes(render_metrics(target_metrics).encode("utf-8"))
    state_commit = _commit_all(state, "Declare the demo contract's Target Metrics")
    head = _git(clone, "rev-parse", "HEAD")

    first = _measure(clone, state, "--json")
    if first.returncode != 0:
        raise RoundTripError(_describe("demo", first))
    document = _document("demo", first)
    commit = document.get("commit")
    sha = commit.get("sha") if isinstance(commit, dict) else None
    if not is_measured(document):
        raise RoundTripError(f"{_describe('demo', first)}\nthe run is not Measured")
    if sha != head:
        raise RoundTripError(f"demo: the run measured {sha!r}, not the checked-out {head}")

    again = _measure(clone, state)
    if again.returncode != 0:
        raise RoundTripError(_describe("demo (second measure)", again))
    if "is already Measured" not in again.stderr:
        raise RoundTripError(
            f"{_describe('demo (second measure)', again)}\nthe commit is not a Measured Commit"
        )
    lines = again.stdout.splitlines()
    verdict = lines[1].strip() if len(lines) > 1 else ""
    if not verdict:
        raise RoundTripError(f"{_describe('demo (second measure)', again)}\nno verdict line")
    return {
        "commit": head,
        "booley_directory_commit": state_commit,
        "run_id": str(document.get("run_id", "")),
        "verdict": verdict,
        "exit_code": first.returncode,
        "measured": True,
    }


def _write_scaffold(root: Path, files: Mapping[str, str], *, only_rtl: bool) -> None:
    for relative, content in files.items():
        if relative.startswith("rtl/") != only_rtl:
            continue
        destination = root / (relative.removeprefix("rtl/") if only_rtl else relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content.encode("utf-8"))


def _lint_table(document: Mapping[str, Any], target: str) -> Mapping[str, Any] | None:
    for table in document.get("tables", []):
        if table.get("flow") == "lint" and str(table.get("identity", "")).endswith(f"#{target}"):
            return {str(key): value for key, value in table.items()}
    return None


def _bad_pin_errors(document: Mapping[str, Any], target: str) -> tuple[int, str]:
    """The ``errors`` value and verdict of a completed lint table, or raise."""
    table = _lint_table(document, target)
    if document.get("status") != "completed" or table is None:
        raise RoundTripError(f"submodule: the bad pin did not complete a {target} lint table")
    for result in table.get("results", []):
        if result.get("key") == "errors_max":
            value, verdict = result.get("value"), result.get("verdict")
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RoundTripError(f"submodule: the bad pin reported errors={value!r}, not > 0")
            if verdict != "unmet":
                raise RoundTripError(f"submodule: the bad pin's errors verdict is {verdict!r}")
            return value, str(verdict)
    raise RoundTripError(f"submodule: the {target} lint table has no errors_max result")


def submodule_leg(workspace: Path) -> dict[str, Any]:
    """Measure a Project whose ``rtl/`` is a submodule: the pin counts, not the live tree."""
    name = "pin"
    target = f"lint_{name}"
    files = scaffold_files(
        ScaffoldChoices(
            name=name,
            sim_eda_tool="verilator",
            tb_style="sv",
            lint_eda_tool="verilator",
            asic=False,
            fpga_part=None,
        )
    )
    ip = workspace / "ip"
    ip.mkdir()
    _git(ip, "init", "--quiet", "-b", "main")
    _write_scaffold(ip, files, only_rtl=True)
    good = _commit_all(ip, "Add the clean RTL")
    source = ip / f"{name}.sv"
    clean_rtl = source.read_text(encoding="utf-8")
    bad_rtl = _NETTYPE_NONE + clean_rtl.replace("endmodule", _BAD_ASSIGN)
    source.write_bytes(bad_rtl.encode("utf-8"))
    bad = _commit_all(ip, "Add an RTL lint error")

    project = workspace / "project"
    project.mkdir()
    _git(project, "init", "--quiet", "-b", "main")
    _write_scaffold(project, files, only_rtl=False)
    metrics = render_metrics({target: {"lint": {"errors_max": 0}}})
    (project / ".booley" / "metrics.toml").write_bytes(metrics.encode("utf-8"))
    _git(project, "add", "-A")
    _git(project, "submodule", "add", "--quiet", "--name", _SUBMODULE_NAME, str(ip), "rtl")
    _git(project / "rtl", "checkout", "--quiet", good)
    _commit_all(project, "Pin the clean RTL")
    booley_dir = project / ".booley"

    _git(project / "rtl", "checkout", "--quiet", bad)  # live tree differs from the pin
    clean = _measure(project, booley_dir, "--json")
    if clean.returncode != 0 or not is_measured(_document("submodule", clean)):
        raise RoundTripError(_describe("submodule (pin at the clean RTL)", clean))
    pinned = _git(project, "rev-parse", "HEAD:rtl")
    live = _git(project / "rtl", "rev-parse", "HEAD")

    _git(project, "add", "rtl")  # pin the bad commit, then restore the clean live tree
    _git(project, "commit", "-m", "Pin the RTL with a lint error")
    _git(project / "rtl", "checkout", "--quiet", good)
    bad_run = _measure(project, booley_dir, "--json")
    if bad_run.returncode != 1:
        raise RoundTripError(_describe("submodule (pin at the bad RTL)", bad_run))
    errors, verdict = _bad_pin_errors(_document("submodule", bad_run), target)
    return {
        "pinned": pinned,
        "live": live,
        "exit_code": clean.returncode,
        "measured": True,
        "bad_pin": {"exit_code": bad_run.returncode, "errors": errors, "verdict": verdict},
    }


def write_evidence(path: Path, document: Mapping[str, Any]) -> None:
    """Publish JSON atomically so an interrupted write cannot masquerade as proof."""
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace_bytes(path, json.dumps(document, indent=2, sort_keys=True).encode() + b"\n")


def round_trip(
    *, project: Path, project_state: Path, contract: Path, image_digest: str
) -> dict[str, Any]:
    """Run both legs on disposable copies and return the evidence document."""
    metrics = load_contract(contract).target_metrics
    with tempfile.TemporaryDirectory(prefix="booley-measure-round-trip-") as scratch:
        root = Path(scratch)
        (root / "demo-leg").mkdir()
        (root / "submodule-leg").mkdir()
        demo = demo_leg(project.resolve(), project_state.resolve(), metrics, root / "demo-leg")
        submodule = submodule_leg(root / "submodule-leg")
    return {"schema": SCHEMA, "image_digest": image_digest, "demo": demo, "submodule": submodule}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--project-state", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.image_digest.strip():
        print("ERROR: --image-digest must not be blank", file=sys.stderr)
        return 2
    try:
        evidence = round_trip(
            project=args.project,
            project_state=args.project_state,
            contract=args.contract,
            image_digest=args.image_digest,
        )
    except (RoundTripError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    write_evidence(args.evidence, evidence)
    print(f"measurement round trip passed: {args.evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
