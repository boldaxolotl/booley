"""Stand-in ``t3`` commands for tests (POSIX only: each is a shebang script).

``write_t3_cli`` names threads, logs its calls and can be slow. ``write_t3_server_cli``
prints the text the pinned T3 Code prints for its server commands (#4781). Tests that run
code against the real command's output share these instead of each writing its own.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

_SCRIPT = """#!{python}
import json, sys, time
thread = sys.argv[3]
with open({log!r}, "a") as log:
    log.write(thread + "\\n")
time.sleep({stall})
title = {titles!r}.get(thread)
if title is None:
    print("Thread " + thread + " is no longer available.")
    sys.exit(1)
print(json.dumps({{"thread": {{"threadId": thread, "title": title}}}}))
"""


def write_t3_cli(
    directory: Path, titles: dict[str, str], *, delay_s: float = 0
) -> tuple[Path, Path]:
    """Write the CLI, which answers after *delay_s*, and return it with its call log."""
    cli, log = directory / "t3-fake", directory / "t3-calls.log"
    text = _SCRIPT.format(python=sys.executable, log=str(log), titles=titles, stall=delay_s)
    cli.write_bytes(text.encode("utf-8"))
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    return cli, log


_SERVER_SCRIPT = """\
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
    parent = os.getppid()
    while os.getppid() == parent:
        if (here / "die").exists():
            sys.exit(1)
        time.sleep(0.05)
if args[:2] == ["policy", "show"]:
    if cfg["dies"]:
        (here / "die").write_text("")
        time.sleep(1)
    say(cfg["show"])
if args[:3] == ["auth", "pairing", "create"]:
    base = args[args.index("--base-url") + 1] if "--base-url" in args else ""
    reply = cfg["pairing"] if isinstance(cfg["pairing"], str) else json.dumps(cfg["pairing"])
    say(reply.replace("{base}", base))
"""


def write_t3_server_cli(
    directory: Path,
    *,
    show: str,
    pairing: dict[str, object] | str,
    check_exit: int = 0,
    serve: str = "ready",
) -> None:
    """Write an executable ``t3`` into *directory* that answers like T3 Code's server CLI.

    ``policy check`` exits *check_exit*. ``serve`` exits 77 when *serve* is ``"refused"``
    before its ready line, sleeps without one when ``"hang"``, exits once ``policy show``
    ran when ``"dies"``, and otherwise prints its ready line and sleeps; it stops when
    its parent exits and records its PID in ``serve.pid`` beside the fake. ``policy show``
    prints *show*. ``auth pairing create`` prints *pairing* (as JSON unless it is a
    string) with ``{base}`` replaced by the ``--base-url`` argument.
    """
    serve_exit = {"ready": None, "refused": 77, "hang": "hang", "dies": None}[serve]
    config = {
        "check_exit": check_exit,
        "serve_exit": serve_exit,
        "dies": serve == "dies",
        "show": show,
        "pairing": pairing,
    }
    (directory / "t3.json").write_text(json.dumps(config), encoding="utf-8", newline="\n")
    fake = directory / "t3"
    fake.write_text(f"#!{sys.executable}\n{_SERVER_SCRIPT}", encoding="utf-8", newline="\n")
    fake.chmod(0o755)
