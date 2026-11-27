"""In-Sandbox entry of the host's T3 Code controls: ``python -m booley.harness.incontainer_t3``.

The host runs it through the hook shell, so it sees the hook's environment and working
directory. Verbs: ``start`` (the registrar's T3 Code step), ``state`` (``running``, ``off`` or
``port-taken``), and ``pair <relay port>`` (T3 Code's pairing reply on stdout, or ``port-taken``
and exit 1 when another process holds T3 Code's port).
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
        print(incontainer_register.start_t3(agent.sandbox_agent_clis()))
    elif args.verb == "state":
        print(incontainer_setup.t3_state(incontainer_register.hook_start_directory()))
    else:
        root = incontainer_register.hook_start_directory()
        if incontainer_setup.t3_state(root) == incontainer_setup.T3_PORT_TAKEN:
            print(incontainer_setup.T3_PORT_TAKEN)
            return 1
        pairing = incontainer_setup.create_t3_pairing(root, f"http://127.0.0.1:{args.port}")
        if pairing.returncode:
            print(f"t3 auth pairing create failed (exit {pairing.returncode})", file=sys.stderr)
            return 1
        print(json.dumps(json.loads(pairing.stdout)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
