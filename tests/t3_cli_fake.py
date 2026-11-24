"""A fake ``t3`` server CLI that prints the text T3 Code 0.1.11 prints (#4781).

Tests that run a script against the real command's output share this one fake
instead of each writing its own. POSIX only: the fake is a shebang script.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPT = """\
import json, os, sys, time
from pathlib import Path

here = Path(__file__).parent
cfg = json.loads((here / "t3.json").read_text())
args = sys.argv[1:]
say = lambda text: print(text, flush=True)
if args[:2] == ["policy", "check"]:
    if cfg["check_exit"]:
        say("ManagedPolicyFileError: T3 Code cannot use the managed policy at " + args[2])
    sys.exit(cfg["check_exit"])
if args[:1] == ["serve"]:
    say("Managed policy applies.")
    say("Listening on http://127.0.0.1:3773")
    if isinstance(cfg["serve_exit"], int):
        sys.exit(cfg["serve_exit"])
    (here / "serve.pid").write_text(str(os.getpid()))
    if cfg["serve_exit"] is None:
        say("T3 Code server is ready.\\nstartup token: secret")
    time.sleep(600)
if args[:2] == ["policy", "show"]:
    say(cfg["show"])
if args[:3] == ["auth", "pairing", "create"]:
    base = args[args.index("--base-url") + 1] if "--base-url" in args else ""
    say(json.dumps(cfg["pairing"]).replace("{base}", base))
"""


def write_t3_server_cli(
    directory: Path,
    *,
    show: str,
    pairing: dict[str, str],
    check_exit: int = 0,
    serve: str = "ready",
) -> None:
    """Write an executable ``t3`` into *directory* that answers like T3 Code's server CLI.

    ``policy check`` exits *check_exit*. ``serve`` exits 77 when *serve* is ``"refused"``
    before its ready line, sleeps without one when ``"hang"``, and otherwise prints it
    and sleeps until terminated; it records its PID in ``serve.pid`` beside the fake.
    ``policy show`` prints *show*. ``auth pairing create`` prints *pairing* as JSON with
    ``{base}`` replaced by the ``--base-url`` argument.
    """
    serve_exit = {"ready": None, "refused": 77, "hang": "hang"}[serve]
    config = {"check_exit": check_exit, "serve_exit": serve_exit, "show": show, "pairing": pairing}
    (directory / "t3.json").write_text(json.dumps(config), encoding="utf-8", newline="\n")
    fake = directory / "t3"
    fake.write_text(f"#!{sys.executable}\n{_SCRIPT}", encoding="utf-8", newline="\n")
    fake.chmod(0o755)
