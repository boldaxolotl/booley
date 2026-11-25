"""In-Sandbox entry of the host's T3 Code controls: ``python -m booley.harness.incontainer_t3``.

The host runs it through the hook shell, so it sees the hook's environment and working
directory. Verbs: ``start`` (the registrar's T3 Code step), ``state`` (``running`` or ``off``),
and ``pair <relay port>`` (T3 Code's pairing reply on stdout).
"""

from __future__ import annotations

import json
import sys

from booley.config import agent
from booley.harness import incontainer_register
from booley.runtime import incontainer_setup


def main(argv: list[str]) -> int:
    """Run one verb; the pairing reply goes to stdout only, never to a log or stderr."""
    verb, *rest = argv
    if verb == "start":
        print(incontainer_register.start_t3(agent.sandbox_agent_clis()))
    elif verb == "state":
        print("running" if incontainer_setup.t3_serving() else "off")
    else:
        port = int(rest[0])
        pairing = incontainer_setup.create_t3_pairing(
            incontainer_register.hook_start_directory(), f"http://127.0.0.1:{port}"
        )
        if pairing.returncode:
            print(f"t3 auth pairing create failed (exit {pairing.returncode})", file=sys.stderr)
            return 1
        print(json.dumps(json.loads(pairing.stdout)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
