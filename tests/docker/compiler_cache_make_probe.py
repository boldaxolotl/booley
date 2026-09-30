#!/usr/bin/env python3
"""Test-only GNU Make wall/child-CPU measurement; never used by production."""

import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

started = time.monotonic()
before = resource.getrusage(resource.RUSAGE_CHILDREN)
process = subprocess.run(["/usr/bin/make", *sys.argv[1:]], check=False)
after = resource.getrusage(resource.RUSAGE_CHILDREN)
record = {
    "argv": sys.argv[1:],
    "cache_environment": {
        name: os.environ.get(name)
        for name in ("OBJCACHE", "CCACHE_DIR", "CCACHE_MAXSIZE", "CCACHE_COMPILERCHECK")
    },
    "wall_s": time.monotonic() - started,
    "child_cpu_s": after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime,
    "returncode": process.returncode,
}
with Path(os.environ["CACHE_ACCEPTANCE_MAKE_LOG"]).open("a") as stream:
    stream.write(json.dumps(record) + "\n")
sys.exit(process.returncode)
