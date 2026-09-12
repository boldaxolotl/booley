"""Command-line adapter for Public QA triage records."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

try:
    from . import triage as core
except ImportError:
    import triage as core


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    add_run_commands(commands)
    add_session_commands(commands)
    add_case_commands(commands)
    return root


def add_run_commands(commands: Any) -> None:
    seal = commands.add_parser("seal-run")
    seal.add_argument("run_root", type=Path)
    validate = commands.add_parser("validate-run")
    validate.add_argument("run_root", type=Path)


def add_session_commands(commands: Any) -> None:
    init = commands.add_parser("init")
    init.add_argument("triage_root", type=Path)
    init.add_argument("--suite-root", type=Path, required=True)
    init.add_argument("--product-revision", required=True)
    init.add_argument("--suite-revision", required=True)
    admit = commands.add_parser("admit")
    admit.add_argument("triage_root", type=Path)
    admit.add_argument("run_root", type=Path)
    admit.add_argument("--suite-equivalence-reason")
    admit.add_argument("--reuse-reason")
    add_key(admit)
    supersede = commands.add_parser("supersede")
    supersede.add_argument("triage_root", type=Path)
    supersede.add_argument("run_id")
    supersede.add_argument("--replacement-run-id", required=True)
    supersede.add_argument(
        "--basis", choices=["complete-rerun", "invalid-evidence"], required=True
    )
    supersede.add_argument("--reason", required=True)
    add_key(supersede)
    status = commands.add_parser("status")
    status.add_argument("triage_root", type=Path)
    finalize = commands.add_parser("finalize")
    finalize.add_argument("triage_root", type=Path)
    add_key(finalize)


def add_case_commands(commands: Any) -> None:
    decide = commands.add_parser("decide")
    decide.add_argument("triage_root", type=Path)
    decide.add_argument("case_id")
    decide.add_argument("disposition", choices=sorted(core.DISPOSITIONS))
    decide.add_argument("--details", type=Path, required=True)
    add_key(decide)
    merge = commands.add_parser("merge")
    merge.add_argument("triage_root", type=Path)
    merge.add_argument("case_ids", nargs="+")
    merge.add_argument("--reason", required=True)
    add_key(merge)
    split = commands.add_parser("split")
    split.add_argument("triage_root", type=Path)
    split.add_argument("case_id")
    split.add_argument("--groups", type=Path, required=True)
    split.add_argument("--reason", required=True)
    add_key(split)
    reopen = commands.add_parser("reopen")
    reopen.add_argument("triage_root", type=Path)
    reopen.add_argument("case_id")
    reopen.add_argument("--reason", required=True)
    add_key(reopen)


def add_key(command: argparse.ArgumentParser) -> None:
    command.add_argument("--idempotency-key", required=True)


def execute(args: argparse.Namespace) -> Any:
    handlers = {
        "seal-run": execute_seal,
        "validate-run": execute_validate,
        "init": execute_init,
        "admit": execute_admit,
        "supersede": execute_supersede,
        "decide": execute_decide,
        "merge": execute_merge,
        "split": execute_split,
        "reopen": execute_reopen,
        "status": lambda value: core.render_session(value.triage_root),
        "finalize": lambda value: core.finalize_session(value.triage_root, value.idempotency_key),
    }
    return handlers[args.command](args)


def execute_seal(args: argparse.Namespace) -> dict[str, Any]:
    return {"run_id": core.seal_run(args.run_root).run_id, "sealed": True}


def execute_validate(args: argparse.Namespace) -> dict[str, Any]:
    run = core.validate_run(args.run_root)
    return {"run_id": run.run_id, "manifest_sha256": run.manifest_hash}


def execute_init(args: argparse.Namespace) -> dict[str, Any]:
    return core.init_session(
        args.triage_root, args.suite_root, args.product_revision, args.suite_revision
    )


def execute_admit(args: argparse.Namespace) -> dict[str, Any]:
    return core.admit_run(
        args.triage_root,
        args.run_root,
        args.suite_equivalence_reason,
        args.idempotency_key,
        args.reuse_reason,
    )


def execute_supersede(args: argparse.Namespace) -> dict[str, Any]:
    return core.supersede_run(
        args.triage_root,
        args.run_id,
        args.replacement_run_id,
        args.basis,
        args.reason,
        args.idempotency_key,
    )


def execute_decide(args: argparse.Namespace) -> dict[str, Any]:
    return core.decide_case(
        args.triage_root,
        args.case_id,
        args.disposition,
        args.details,
        args.idempotency_key,
    )


def execute_merge(args: argparse.Namespace) -> dict[str, Any]:
    return core.merge_case_ids(args.triage_root, args.case_ids, args.reason, args.idempotency_key)


def execute_split(args: argparse.Namespace) -> dict[str, Any]:
    return core.split_case_from_file(
        args.triage_root, args.case_id, args.groups, args.reason, args.idempotency_key
    )


def execute_reopen(args: argparse.Namespace) -> dict[str, Any]:
    return core.reopen_case(args.triage_root, args.case_id, args.reason, args.idempotency_key)


def main() -> int:
    try:
        print(json.dumps(execute(parser().parse_args()), indent=2, sort_keys=True))
    except (OSError, core.TriageError, json.JSONDecodeError, yaml.YAMLError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0
