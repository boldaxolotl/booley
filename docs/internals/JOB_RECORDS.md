# Durable Job records

MCP endpoint records live under an explicit runtime root's `jobs/` directory.
Schema 2 adds `work_dir` (string or null), `session_key` (string or null), and
`binding` (the immutable `booley.goal-run-binding/v1` object or null). Readers
accept older records without these fields and tolerate unknown future fields.
The persistence boundary validates positive integer schema versions, nullable
string routing fields, and a nullable mapping binding. Malformed envelopes raise
`JobRecordError`; MCP composition validates the Goal binding schema and contents.
The existing endpoint, argv, PID, timestamps, timeout, lease, status, exit-code,
and display-scope fields retain their meanings. New detached run IDs append eight
hexadecimal characters from UUID4 to the endpoint, timestamp, and counter.

`owner` names the agent that started a background Job (`booley.mcp.job_ownership`):
the submitting server's identity plus, when the client names one, the Codex
thread or Claude Code process key of the calling tab. A stdio server's identity
is its display scope plus its client process (its own process when the parent is
not visible); the Interactive Mode HTTP server's is its display scope, hostname
and port, so it survives restarts. `read_at` is the first time the owner was
handed the Job's final result; it is never moved, and a read by anyone else
does not set it. Argument-free `booley_poll` returns the owner's earliest
finished Job that has no `read_at`. A record without `owner` (written before the
field existed) belongs to the caller whose `session_key` stamped it, for
background-Job endpoints only, and counts as unread only while its on-disk status
is still running or when it ended after this server started.

`mcp.server.job_roots()` enumerates the
container jobs root and the jobs root of every retained Goal Record, including
entering, finishing, finished, abandoned, and failed records. `_locate_job` checks
these roots plus the manager's remembered admission root and returns
`LocatedJob(record, root)`. More than one match is an error, including legacy IDs;
no root silently wins. Poll and cancel remain callable without `work_dir`.

The manager stamps the admission binding into the Job before launching its child,
derives the write destination from that binding for terminal updates, and keeps
the located admission root as the fallback for old unbound Jobs.
Startup reconciliation and adopted cancellation retain the located root. Report
and checkpoint discovery use the bound record's runtime directory (or the located
legacy root), never the current server's Goal selection. A completed old Job
cannot adopt a newly entered Goal Mode in the same worktree.

A duplicate live invocation attaches only when endpoint, normalized argv, and
binding requirements match. Transcript paths and invocation IDs do not distinguish
an otherwise identical invocation; the bound record, specification revisions,
protected start snapshot, eligibility, and Target surfaces do. Finished runs are
never replayed by attach.
