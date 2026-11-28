"""In-Sandbox entry of the host's UI controls: ``python -m booley.harness.incontainer_t3``.

The host runs it through the hook shell, so it sees the hook's environment and working
directory. Verbs: ``start`` (the registrar's step for T3 Code and the web Dashboard),
``state`` (``<T3 Code> <Dashboard>``: ``running``, ``off`` or, for T3 Code, ``port-taken``,
then ``running``, ``off`` or, with no Agent CLI to start it for, ``skipped``), and
``pair <relay port>`` (T3 Code's pairing reply on stdout, or ``port-taken`` and exit 1 when
another process holds T3 Code's port).
"""

from __future__ import annotations

import argparse
import json
import sys

from booley.config import agent
from booley.harness import incontainer_register
from booley.runtime import incontainer_setup


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="incontainer_t3")
    verbs = parser.add_subparsers(dest="verb", required=True)
    verbs.add_parser("start")
    verbs.add_parser("state")
    verbs.add_parser("pair").add_argument("port", type=int)
    return parser


def main(argv: list[str]) -> int:
    """Run one verb; the pairing reply goes to stdout only, never to a log or stderr."""
    args = _parser().parse_args(argv)
    if args.verb == "start":
        print(incontainer_register.start_ui(agent.sandbox_agent_clis()))
    elif args.verb == "state":
        dashboard = incontainer_setup.dashboard_state()
        if dashboard == "off" and not agent.sandbox_agent_clis():
            dashboard = "skipped"  # the start step starts none here
        print(f"{incontainer_setup.t3_state()} {dashboard}")
    else:
        if incontainer_setup.t3_state() == incontainer_setup.T3_PORT_TAKEN:
            print(incontainer_setup.T3_PORT_TAKEN)
            return 1
        pairing = incontainer_setup.create_t3_pairing(f"http://127.0.0.1:{args.port}")
        if pairing.returncode:
            print(f"t3 auth pairing create failed (exit {pairing.returncode})", file=sys.stderr)
            return 1
        print(json.dumps(json.loads(pairing.stdout)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
