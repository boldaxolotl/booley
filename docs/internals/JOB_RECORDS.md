# Job records

How Booley persists a [Job](../GLOSSARY.md#execution) (one run of a Booley
Flow or Specialist) on disk, how a later call finds it again, and who it
belongs to. It is for contributors changing Job submission, polling,
cancellation, or the record format. How Jobs are admitted against the Memory Budget is in [ADR 0087](../adr/0087-admit-jobs-against-a-measured-memory-budget.md)
and [CONFIG.md](../user/CONFIG.md#jobs--concurrency-jobs); the MCP call
lifecycle around a Job is in [MCP-TOOLS.md](MCP-TOOLS.md#the-common-lifecycle).

## The record

`booley.runtime.job_records.JobRecord` is one JSON file, `<run_id>.json`, in a
`jobs/` directory. MCP calls write under their runtime root's `jobs/`;
Standalone Jobs started from a Sandbox shell or the Booley Dashboard write
their own records and set `report_root` to where their reports live. The record
is written at submit and again once the child's PID is known, so a restarted
server can tell a running Job from a crashed one without in-memory state.

| Field | Meaning |
|---|---|
| `run_id`, `endpoint`, `argv`, `timeout_s` | What ran. |
| `pid`, `process_identity` | The child process, for liveness checks. |
| `started_at`, `run_started_at`, `ended_at` | Submit time, the time the child was first seen holding its claim, and the end time. Deadlines use `run_started_at` because a queued Job can wait a long time between the two. |
| `status`, `exit_code` | `running`, `done` (exit 0 without timeout), `failed`, or `cancelled` (withdrawn by `booley_cancel`). |
| `display_scope` | Which presentation the Job belongs to, so a Specialist's nested Job does not replace the Developer Agent's status line. |
| `schema` | Envelope version, currently `2`; a record without it reads as `1`. |
| `work_dir`, `session_key` | The checkout the call named, and the caller's worktree session key. |
| `owner`, `read_at` | Who started the Job and when that owner first received its final result (see below). |
| `report_root` | Where a Standalone Job's reports live, when not next to the records. |
| `progress` | `{"finished": n, "total": m}`: child Flows ended and planned, for a Job that runs child Flows (`measure`); null otherwise. |

Readers ignore unknown fields so an older Booley can read a newer record, and
missing fields take their defaults. The persistence layer checks only the
envelope (positive `schema`; string or null `work_dir`, `session_key`,
`report_root`) and raises `JobRecordError` on a malformed one.

Run IDs: a background Job uses `make_run_id`, `<endpoint>-<timestamp>-<counter>-<8 hex>`,
where the random suffix keeps IDs unique across servers sharing a directory;
an inline call uses a random UUID.

## Ownership and reading results

`owner` (`booley.mcp.job_ownership`) is the submitting server's identity plus,
when the client names one, the calling tab's Codex thread or Claude Code
process key. One Interactive Mode HTTP server serves every tab in a Sandbox,
so the server identity alone cannot tell two agents apart. A stdio server's
identity is its display scope plus its client process (its own process when
the parent is not visible); the HTTP server's is its display scope, hostname,
and port, so it survives restarts.

Ownership only decides what an argument-free `booley_poll` returns: the
owner's earliest finished Job without `read_at`. Any Job stays reachable by
its `run_id`. `read_at` is set once, the first time the owner receives the
final result; a read by anyone else does not set it.

A record without `owner` belongs to the caller whose `session_key` stamped it,
but only for background-Job endpoints (`ASYNC_JOB_ENDPOINTS`), because inline
and baseline records carry the same key without being that caller's Jobs. It
counts as unread only while its status on disk is still running or when it
ended after this server started, so a server does not hand back results that
another server may already have delivered.

## Finding a Job

`mcp.job_results.job_roots()` lists every place a record can be: the
container jobs root. `_locate_job` searches it plus the root the manager
remembered at admission, and returns
`LocatedJob(record, root)`. A `run_id` found in more than one root is an
error: silently picking one could show or cancel the wrong Job. Poll and
cancel therefore work without `work_dir`.

The manager writes terminal updates to the root it remembered at admission.
Startup reconciliation and cancellation of an adopted Job keep the located
root.

## Reading Jobs for display

`booley.runtime.job_observation.JobObserver` reads Job records, claims,
processes, and report artifacts from the roots its caller passes, and never
polls, repairs, or writes a Job. The Booley Dashboard's
`harness.dashboard.jobs.JobReader` chooses those roots: the current
interactive root, then the
newest 64 older interactive roots (examining at most 4,096 candidates), and
adds display fields such as handles and the Target's EDA tool. Rule D37 in
[SOURCE-DEPENDENCY-CONTRACT.md](SOURCE-DEPENDENCY-CONTRACT.md) keeps the
observer free of Target policy: it reports process and artifact facts and
leaves interpreting Targets to its caller.

## Attaching to a running duplicate

A new call attaches to a live Job instead of starting another only when the
endpoint and the normalized `argv` match. A finished Job is never attached to; a repeat call
runs again.
