# UART Simulation Campaign attempt-contract fixtures

These fixtures support Checks 14, 16, and 17 only. They remain pending until
an authorized `uart-ubuntu-codex-cli` Scenario Run captures product evidence;
automated fixture gates do not complete Public QA Checks.

Copy this directory to `qa-campaign` beneath the run-owned UART Project,
register `campaign.core`, and merge `tests.toml` into the active catalog. Apply
only the named TOML fragment for each fresh campaign and retain its rendered
configuration bytes and digest.

For `campaign.runtime-input-isolation`, use `runtime-inputs.toml` and run exact
tests `alpha`, `beta`. Authenticate both attempt-owned copies and their selected
values from logs, distinct owned `run_directory.resolved` values, cleanup after
publication, and one unchanged shared Bundle/build-result digest.

For `campaign.presim-immutable`, use `immutable-presim.toml` for one test. The
hook must record that `BOOLEY_BUILD_ROOT` is absent and exit 73. Require an
attributed setup failure, no passing observation or Criteria evidence, and
identical source, bundle manifest, executable, and snapshot hashes before and
after process-tree death.

For `campaign.presim-legacy-build`, use `legacy-presim.toml` and run both tests.
Require the manifest and attempts to disclose `legacy-per-test`, two distinct
private Bundle Build Attempt/Result generations, per-test hook markers, and no
`shared_variant` claim. Preserve exact Pre-Sim environments with secrets
redacted, compile/run argv, logs, campaign documents, compatibility reports,
and hashes. Use `validate_campaign.py` only as a read-only structural
cross-check.

Restore the original UART configuration and remove only run-owned fixture
files. Prove all pinned corpus and candidate source bytes are unchanged.
