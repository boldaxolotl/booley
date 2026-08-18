"""Host-only ``booley eda`` administration CLI."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import authority


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Add the grouped EDA authority command tree."""
    parser = subparsers.add_parser("eda", help="Manage host commercial-EDA authority")
    groups = parser.add_subparsers(dest="eda_group", required=True)
    _installation_parser(groups)
    _license_parser(groups)
    _grant_parser(groups)


def _installation_parser(groups: argparse._SubParsersAction) -> None:
    parser = groups.add_parser("installation", help="Manage EDA installations")
    actions = parser.add_subparsers(dest="eda_action", required=True)
    register = actions.add_parser("register")
    register.add_argument("name")
    register.add_argument("--kind", choices=("vivado",), required=True)
    register.add_argument("--source", type=Path, required=True)
    remove = actions.add_parser("remove")
    remove.add_argument("name")
    actions.add_parser("list")
    show = actions.add_parser("show")
    show.add_argument("name")


def _license_parser(groups: argparse._SubParsersAction) -> None:
    parser = groups.add_parser("license", help="Manage fixed License Profiles")
    actions = parser.add_subparsers(dest="eda_action", required=True)
    register = actions.add_parser("register")
    register.add_argument("name")
    register.add_argument("--server-ipv4", required=True)
    register.add_argument("--server-hostid", required=True)
    register.add_argument("--lmgrd-port", type=int, required=True)
    register.add_argument("--vendor-port", type=int, required=True)
    remove = actions.add_parser("remove")
    remove.add_argument("name")
    actions.add_parser("list")
    show = actions.add_parser("show")
    show.add_argument("name")


def _grant_parser(groups: argparse._SubParsersAction) -> None:
    parser = groups.add_parser("grant", help="Manage exact Project grants")
    actions = parser.add_subparsers(dest="eda_action", required=True)
    add = actions.add_parser("add")
    add.add_argument("project", type=Path)
    add.add_argument("--kind", choices=("vivado",), required=True)
    add.add_argument("--installation")
    add.add_argument("--license-profile")
    revoke = actions.add_parser("revoke")
    revoke.add_argument("project", type=Path)
    revoke.add_argument("--kind", choices=("vivado",), required=True)
    actions.add_parser("list")


def run(args: argparse.Namespace, _project_root: Path) -> int:
    """Execute one authority operation and render stable JSON."""
    try:
        value = _dispatch(args)
    except authority.AuthorityError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if value is not None:
        print(json.dumps(value, indent=2, sort_keys=True))
    return 0


def _dispatch(args: argparse.Namespace) -> object:
    group = args.eda_group
    action = args.eda_action
    if group == "installation":
        return _installation_action(args, action)
    if group == "license":
        return _license_action(args, action)
    if group == "grant":
        return _grant_action(args, action)
    raise authority.AuthorityError(f"unknown EDA authority group: {group}")


def _installation_action(args: argparse.Namespace, action: str) -> object:
    if action == "register":
        return asdict(authority.register_installation(args.name, args.kind, args.source))
    if action == "remove":
        authority.remove_installation(args.name)
        return {"removed": args.name}
    state = authority.load_state()
    if action == "show":
        record = state.installations.get(args.name)
        if record is None:
            raise authority.AuthorityError(f"EDA installation {args.name!r} is not registered")
        return asdict(record)
    return [asdict(item) for _, item in sorted(state.installations.items())]


def _license_action(args: argparse.Namespace, action: str) -> object:
    if action == "register":
        profile = authority.register_license(
            args.name,
            server_ipv4=args.server_ipv4,
            server_hostid=args.server_hostid,
            lmgrd_port=args.lmgrd_port,
            vendor_port=args.vendor_port,
        )
        return asdict(profile)
    if action == "remove":
        authority.remove_license(args.name)
        return {"removed": args.name}
    state = authority.load_state()
    if action == "show":
        profile = state.licenses.get(args.name)
        if profile is None:
            raise authority.AuthorityError(f"License Profile {args.name!r} is not registered")
        return asdict(profile)
    return [asdict(item) for _, item in sorted(state.licenses.items())]


def _grant_action(args: argparse.Namespace, action: str) -> object:
    if action == "add":
        grant = authority.add_grant(
            args.project,
            args.kind,
            installation=args.installation,
            license_profile=args.license_profile,
        )
        return asdict(grant)
    if action == "revoke":
        grant = authority.revoke_grant(args.project, args.kind)
        residual = _cleanup_project_resources(Path(grant.project_root))
        result = asdict(grant)
        result["residual_resources"] = residual
        if residual:
            raise authority.AuthorityError(
                "grant revoked, but Docker cleanup left residual resource(s): "
                + ", ".join(residual)
            )
        return result
    return [asdict(item) for item in authority.load_state().grants]


def _cleanup_project_resources(project: Path) -> list[str]:
    """Remove labeled containers then networks after authority is revoked."""
    from .flexnet_docker import cleanup_project_resources

    return list(cleanup_project_resources(project))
