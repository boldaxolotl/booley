# UART Simulation Campaign attempt-contract fixtures

These fixtures support Checks 14–17. They remain pending until
an authorized `uart-ubuntu-codex-cli` Scenario Run captures product evidence;
automated fixture gates do not complete Public QA Checks.

Copy this directory to `qa-campaign` beneath the run-owned UART Project,
register `campaign.core`, and merge `tests.toml` into the active catalog. Apply
only the named TOML fragment for each fresh Simulation Campaign and retain its
rendered configuration bytes and digest.

For `campaign.runtime-input-isolation`, use `runtime-inputs.toml` and run exact
tests `alpha`, `beta`. Authenticate both attempt-owned copies and their selected
values from logs, distinct owned `run_directory.resolved` values, cleanup after
publication, and one unchanged shared Simulator Bundle/build-result digest.

For `campaign.presim-immutable`, use `immutable-presim.toml` for one test. The
hook must record that `BOOLEY_BUILD_ROOT` is absent and exit 73. Require an
attributed setup failure, no passing observation or Criteria evidence, and
identical source, Simulator Bundle manifest, executable, and snapshot hashes
before and after process-tree death.

For `campaign.presim-legacy-build`, use `legacy-presim.toml` and run both tests.
Require the manifest and attempts to disclose `legacy-per-test`, two distinct
private Simulator Bundle Build Attempt/Result generations, per-test hook
markers, and no `shared_variant` claim. Preserve exact Pre-Sim environments
with secrets redacted, compile/run argv, logs, Simulation Campaign documents,
compatibility reports, and hashes. Use `validate_campaign.py` only as a
read-only structural cross-check.

For `campaign.literal-cwd-serialization`, use `literal-cwd.toml` and run exact
tests `alpha`, `beta` with `max_heavy > 1`. The owned hook holds the canonical
literal directory for 250 ms and prints a monotonic interval plus owner token.
Start one separate owned templated campaign during that bounded window to prove
the collision lock is scoped to the canonical run directory rather than a
global scheduler lock. Require the two literal-directory intervals to be
nonoverlapping, their canonical directory identities to match, and at least one
unrelated isolated interval to overlap. Preserve the rendered configuration,
hook output, process timeline, SlotStore samples, manifest, attempts, results,
logs, and directory-owner markers. Use
`validate_literal_cwd_serialization()` from `validate_campaign.py` as a
read-only cross-check.

## Recovery and cleanup

Before the first mutation, ledger the run-owned fixture copy, configuration
fragments, report roots, literal/templated directories, private build
generations, process groups, and hook markers. Archive every failed attempt and
its environment before restoration. Restore only the exact owned configuration
or hook input, reap its owned process tree, and run a fresh valid control in the
same Scenario Run; recovery never depends on the negative Check passing.

Restore the original UART configuration, reap owned processes, and remove only
run-owned fixture files, directories, and private generations. Prove all pinned
corpus and candidate source bytes are unchanged and give every ledgered
resource a cleanup disposition.
