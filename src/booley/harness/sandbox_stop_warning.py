"""Warn, before ``booley session down`` stops a Sandbox, about Standalone Jobs it will kill.

A Standalone Job (a ``booley flow`` / ``booley specialist`` run typed in a Sandbox shell)
dies with its Sandbox. Only the Sandbox can say which of its Job records are still live: the
PIDs and the PID namespace are the Sandbox's, and the records live under the Booley Directory
the Sandbox itself mounts (``$BOOLEY_DIR``). So one ``docker exec`` runs a stdlib-only probe
that lists the live running records; this module formats them for the host user.

The check never blocks: when it cannot read the records it says so and ``session down``
proceeds.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from time import time as wall_clock

from booley.core.boundary import BoundaryError, as_str_list, require_dict
from booley.harness.standalone_job import standalone_jobs_dir
from booley.runtime import job_records
from booley.runtime.job_records import JobRecord, JobRecordError, target_arg
from booley.runtime.session_admission import Sandbox
from booley.runtime.session_runtime import exec_argv
from booley.runtime.timefmt import format_duration

_PROBE_TIMEOUT_S = 15
_SKIPPED = "warning: skipped the running Standalone Job check: "

# Records directory relative to ``$BOOLEY_DIR`` inside the Sandbox.
_RECORDS_RELPATH = standalone_jobs_dir(Path()).as_posix()

# Runs inside the Sandbox with ``python3 -I``: no ``booley`` import, so a host/image version
# skew cannot break it. Prints ``{"running": [record, ...], "unreadable": [file name, ...]}``.
# A running record is kept unless its process is provably gone; the liveness rule restates
# ``booley.runtime.pid._observe_procfs_process`` (same PID namespace, same start ticks, not a
# zombie). Anything it cannot judge counts as live: warn rather than hide a Job.
_PROBE = r"""
import json, os, sys

def live(identity):
    if not isinstance(identity, dict):
        return True
    pid = identity.get("pid")
    scope = identity.get("identity_scope", identity.get("pid_namespace"))
    token = identity.get("start_token", identity.get("start_ticks"))
    kind = identity.get("identity_kind", "linux-procfs-start")
    if kind != "linux-procfs-start" or isinstance(pid, bool) or not isinstance(pid, int):
        return True
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8") as handle:
            stat = handle.read()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        fields = stat.rsplit(")", 1)[1].split()
        state, ticks = fields[0], int(fields[19])
        namespace = os.readlink("/proc/%d/ns/pid" % pid)
    except (IndexError, ValueError, OSError):
        return True
    if namespace != scope or ticks != token:
        return False
    return state != "Z"

root = os.path.join(os.environ["BOOLEY_DIR"], sys.argv[1])
running, unreadable = [], []
names = sorted(n for n in os.listdir(root) if n.endswith(".json")) if os.path.isdir(root) else []
for name in names:
    try:
        with open(os.path.join(root, name), encoding="utf-8") as handle:
            record = json.load(handle)
        if not isinstance(record, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        unreadable.append(name)
        continue
    if record.get("status") == "running" and live(record.get("process_identity")):
        running.append(record)
print(json.dumps({"running": running, "unreadable": unreadable}))
"""


def _run_probe(sandbox: Sandbox) -> subprocess.CompletedProcess[str]:
    """Run the probe in the Sandbox; the caller's stdin never reaches it."""
    argv = exec_argv(
        sandbox.container_id, ["python3", "-I", "-c", _PROBE, _RECORDS_RELPATH], tty=False
    )
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=_PROBE_TIMEOUT_S,
    )


def _parse_probe(stdout: str) -> tuple[list[JobRecord], list[str]]:
    """Split probe output into readable running records and unreadable file names."""
    data = require_dict(json.loads(stdout), field="probe output")
    raw_records = data.get("running")
    if not isinstance(raw_records, list):
        raise BoundaryError("probe output has no 'running' list")
    unreadable = as_str_list(data.get("unreadable"))
    records: list[JobRecord] = []
    for raw in raw_records:
        try:
            records.append(JobRecord.from_dict(require_dict(raw, field="Job record")))
        except (BoundaryError, JobRecordError, ValueError, TypeError):
            run_id = raw.get("run_id") if isinstance(raw, dict) else None
            unreadable.append(f"{run_id}.json" if isinstance(run_id, str) else "<unnamed record>")
    return records, unreadable


def _warning_line(record: JobRecord, now: float) -> str:
    """One stderr line naming a Job that stops with the Sandbox."""
    kind = record.argv[1] if len(record.argv) > 1 else "job"
    target = target_arg(record.argv)
    target_clause = f"Target {target}, " if target else ""
    started = job_records.parse_stamp(record.started_at)
    elapsed = "unknown" if started is None else format_duration(now - started)
    return (
        f"warning: Standalone Job {kind} {record.endpoint} "
        f"({target_clause}worktree {record.work_dir or 'unknown'}) "
        f"has run for {elapsed} and stops with the Sandbox"
    )


def standalone_job_warnings(sandbox: Sandbox | None) -> list[str]:
    """Stderr lines for each live Standalone Job; ``[]`` when there is nothing to say.

    ``sandbox`` is ``None`` when the Sandbox container exists but is not running.
    """
    if sandbox is None:
        return [f"{_SKIPPED}the Sandbox is not running"]
    try:
        result = _run_probe(sandbox)
        if result.returncode:
            detail = result.stderr.strip().splitlines()[-1:] or [f"exit {result.returncode}"]
            return [f"{_SKIPPED}could not read the Sandbox's Job records: {detail[0]}"]
        records, unreadable = _parse_probe(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, BoundaryError) as exc:
        return [f"{_SKIPPED}could not read the Sandbox's Job records: {exc}"]
    now = wall_clock()
    ordered = sorted(records, key=lambda record: (record.started_at, record.run_id))
    lines = [_warning_line(record, now) for record in ordered]
    if unreadable:
        lines.append(f"{_SKIPPED}unreadable Job records: {', '.join(sorted(unreadable))}")
    return lines
