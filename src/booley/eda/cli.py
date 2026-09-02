"""Host-only ``booley eda`` administration CLI."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import cast

from .provisioning import authority

_Record = dict[str, object]
_Records = list[_Record]
_Result = _Record | _Records


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
    _add_json_option(register)
    remove = actions.add_parser("remove")
    remove.add_argument("name")
    _add_json_option(remove)
    _add_json_option(actions.add_parser("list"))
    show = actions.add_parser("show")
    show.add_argument("name")
    _add_json_option(show)


def _license_parser(groups: argparse._SubParsersAction) -> None:
    parser = groups.add_parser("license", help="Manage fixed License Profiles")
    actions = parser.add_subparsers(dest="eda_action", required=True)
    register = actions.add_parser("register")
    register.add_argument("name")
    register.add_argument("--server-ipv4", required=True)
    register.add_argument("--server-hostid", required=True)
    register.add_argument("--lmgrd-port", type=int, required=True)
    register.add_argument("--vendor-port", type=int, required=True)
    _add_json_option(register)
    remove = actions.add_parser("remove")
    remove.add_argument("name")
    _add_json_option(remove)
    _add_json_option(actions.add_parser("list"))
    show = actions.add_parser("show")
    show.add_argument("name")
    _add_json_option(show)


def _grant_parser(groups: argparse._SubParsersAction) -> None:
    parser = groups.add_parser("grant", help="Manage exact Project grants")
    actions = parser.add_subparsers(dest="eda_action", required=True, metavar="{add,revoke}")
    add = actions.add_parser("add")
    add.add_argument("project", type=Path)
    add.add_argument("--kind", choices=("vivado",), required=True)
    add.add_argument("--installation")
    add.add_argument("--license-profile")
    _add_json_option(add)
    revoke = actions.add_parser("revoke")
    revoke.add_argument("project", type=Path)
    revoke.add_argument("--kind", choices=("vivado",), required=True)
    _add_json_option(revoke)
    actions.add_parser("list")


def _add_json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit stable JSON")


def run(args: argparse.Namespace, _project_root: Path) -> int:
    """Execute one authority operation with human output or explicit JSON."""
    try:
        value = _dispatch(args)
    except authority.AuthorityError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    legacy_grant_list = args.eda_group == "grant" and args.eda_action == "list"
    if legacy_grant_list:
        print(
            "WARNING: `booley eda grant list` is deprecated; use `booley projects`.",
            file=sys.stderr,
        )
    if value is not None and (legacy_grant_list or getattr(args, "json", False)):
        print(json.dumps(value, indent=2, sort_keys=True))
    elif value is not None:
        _render_human(args, value)
    return 0


def _dispatch(args: argparse.Namespace) -> _Result:
    group = args.eda_group
    action = args.eda_action
    if group == "installation":
        return _installation_action(args, action)
    if group == "license":
        return _license_action(args, action)
    if group == "grant":
        return _grant_action(args, action)
    raise authority.AuthorityError(f"unknown EDA authority group: {group}")


def _installation_action(args: argparse.Namespace, action: str) -> _Result:
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


def _license_action(args: argparse.Namespace, action: str) -> _Result:
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


def _grant_action(args: argparse.Namespace, action: str) -> _Result:
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
        result = asdict(grant)
        result["residual_resources"] = []
        return result
    return [asdict(item) for item in authority.load_state().grants]


def _render_human(args: argparse.Namespace, value: _Result) -> None:
    if args.eda_group == "installation":
        _render_installation(args.eda_action, value)
    elif args.eda_group == "license":
        _render_license(args.eda_action, value)
    else:
        _render_grant(args.eda_action, value)


def _render_installation(action: str, value: _Result) -> None:
    if action == "register":
        item = cast(_Record, value)
        print(
            f"Registered {item['kind']} EDA installation {item['name']!r} from {item['source']}."
        )
    elif action == "remove":
        print(f"Removed EDA installation {cast(_Record, value)['removed']!r}.")
    elif action == "show":
        _print_records("EDA installation", [cast(_Record, value)], "name")
    else:
        _print_records("EDA installations", cast(_Records, value), "name")


def _render_license(action: str, value: _Result) -> None:
    if action == "register":
        item = cast(_Record, value)
        print(
            f"Registered License Profile {item['name']!r} for "
            f"{item['server_ipv4']}:{item['lmgrd_port']}."
        )
    elif action == "remove":
        print(f"Removed License Profile {cast(_Record, value)['removed']!r}.")
    elif action == "show":
        _print_records("License Profile", [cast(_Record, value)], "name")
    else:
        _print_records("License Profiles", cast(_Records, value), "name")


def _render_grant(action: str, value: _Result) -> None:
    item = cast(_Record, value)
    if action == "add":
        print(
            f"Granted {item['kind']} EDA access to {item['project_root']} "
            f"using {_grant_authority_summary(item)}."
        )
    else:
        print(f"Revoked {item['kind']} EDA access for {item['project_root']}.")


def _grant_authority_summary(item: _Record) -> str:
    selected = []
    if item.get("installation"):
        selected.append(f"installation {item['installation']!r}")
    if item.get("license_profile"):
        selected.append(f"License Profile {item['license_profile']!r}")
    return " and ".join(selected) or "no installation or License Profile"


def _print_records(title: str, records: _Records, key: str) -> None:
    print(f"{title}:")
    if not records:
        print("  none")
        return
    for item in records:
        label = item.get(key, "unnamed")
        details = ", ".join(f"{name}={value}" for name, value in item.items() if name != key)
        print(f"  {label}: {details}")
