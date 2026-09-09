# T-08 provider process controls

Use this only in the declared disposable Taxi runtime, after a real selected
Codex invocation passes and its absolute executable path/version are recorded.
Keep authentication in the existing approved store. The fixture does not contact
a provider, simulate a successful response, or read/retain credentials.

Create an operator-owned directory with a `mode` file containing `subscription`,
`transient`, `crash`, or `timeout`. Set `QA_PROVIDER_FIXTURE_ROOT` to that directory
and `QA_REAL_CODEX` to the recorded original executable. In the run-owned PATH
prefix, install a `codex` launcher that executes this Python file with unchanged
arguments (`#!/bin/sh` plus `exec python3 /absolute/fixture/codex_fixture.py "$@"`
in the Linux Session Runtime used on both native hosts). Set this environment in the disposable runtime terminal that launches the same
public Ticket execution used by the baseline; never overwrite the installed binary.

Invoke the normal public Ticket execution command. The shim only intercepts
`codex exec`; help/version calls use the original executable. Each intercepted
attempt creates one exclusive numbered file containing only its mode, providing
an independent retry count. Keep the matching Ticket state/log result:

- `subscription`: provider process exits 1 with the explicit usage-limit diagnostic;
  Ticket returns to waiting/requeue with the provider limit recorded.
- `transient`: process exits 1 with `response stalled mid-stream`; require the
  documented bounded known-transient retry, preserving every attempt.
- `crash`: process exits 1 with an unrelated signature; no known-transient retry.
- `timeout`: configure the public Developer active/wall allowance below 60 seconds;
  require the distinct timeout result and no surviving child. The shim itself exits
  after 60 seconds if supervision fails; that fallback is not a product pass.

After detection, remove the PATH prefix and fixture environment, then repeat the
original real provider invocation and require fresh success. `restored` mode is
available for a bounded 120-second transport sanity check, but remove the wrapper
entirely for normal recovery. Reconcile attempt counts with durable product records;
fixture output alone earns no Ticket-behavior credit. No additional retry or spend
allowance is created. Freeze the shim hash and fault input as operator evidence.
