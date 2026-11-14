"""QA fixture: a Custom Flow that holds a chosen amount of memory for a while.

Install it as `mcp_tools/mem_hog.py` in the fixture Project's Booley Directory
(see the scheduler RUNBOOK). Memory admission records and admits it like any
other Custom Flow; its Job phase is the Flow name, `mem_hog`.

The child process touches every page it allocates, so its resident memory
really reaches `--mb` MiB. That lets a QA run fill the Memory Budget, provoke a
real kernel out-of-memory kill under a small `[sandbox].memory`, and give jobs
a known wall-clock time, without building any RTL.
"""

import sys

from booley.flows.base import BooleyFlow, SubprocessResult
from booley.mcp.base import EXIT_ERROR, McpToolResult

# Runs in the child: allocate `mb` MiB in 16 MiB chunks over `ramp` seconds,
# writing one byte per 4 KiB page so the kernel commits every page, then hold
# the memory for `hold` seconds.
HOG_SCRIPT = """
import sys, time
mb, ramp, hold = int(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
chunk_mb = 16
chunks = []
steps = max(1, mb // chunk_mb)
for step in range(steps):
    block = bytearray(chunk_mb * 1024 * 1024)
    block[::4096] = b"\\x01" * len(range(0, len(block), 4096))
    chunks.append(block)
    time.sleep(ramp / steps)
print(f"mem_hog: holding {steps * chunk_mb} MiB for {hold:g} s", flush=True)
time.sleep(hold)
print("mem_hog: done", flush=True)
"""


class MemHogFlow(BooleyFlow):
    name = "mem_hog"
    description = "QA fixture: hold N MiB of memory for a while, then exit 0"
    code_modifying = False

    def _add_args(self, parser):
        parser.add_argument("--mb", type=int, default=256, help="MiB to allocate and touch")
        parser.add_argument("--ramp", type=float, default=5.0, help="seconds to reach --mb")
        parser.add_argument("--hold", type=float, default=20.0, help="seconds to hold it")

    def _build_command(self):
        return [
            sys.executable,
            "-c",
            HOG_SCRIPT,
            str(self.args.mb),
            str(self.args.ramp),
            str(self.args.hold),
        ]

    def _interpret_result(self, result: SubprocessResult) -> McpToolResult:
        evidence = "\n".join(part for part in (result.stdout, result.stderr) if part)
        if result.timed_out or result.returncode != 0:
            # A kernel OOM kill lands here (exit 137); Booley's requeue decides what follows.
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(evidence or f"mem_hog exited {result.returncode}")[-2000:],
            )
        return McpToolResult(exit_code=0, report_text=evidence[-2000:])


if __name__ == "__main__":
    MemHogFlow().cli()
