# Taxi Simulation Campaign shared-bundle fixture

This fixture supports only `campaign.verilator-single-build`. It is registered
for Public QA but remains pending until an authorized Configured Scenario Run
captures product evidence. Unit and real-tool fixture gates do not complete the
Check.

Copy this directory into the run-owned Taxi Project, register `campaign.core`,
and merge the `sim_campaign_verilator` table into the active test catalog. Run
both exact tests in one invocation with an explicit report root. Preserve the
Verilator version and exact compile/run argv, the immutable manifest, the sole
Bundle Build Attempt/Result and bundle manifest, both Simulation Attempt/Result
records, compatibility projections, logs, and SHA-256 digests.

Require exactly one `build-variants/*/attempts/*/build-result.json` in state
`ready`, with `bundle.sharing` equal to `shared_variant`. Both ordered results
must authenticate that same build-result byte digest and bundle ID while using
distinct Simulation Attempt IDs. Save the build tree and bundle hashes before
the first run and after the second; any mutation fails the Check. Run
`validate_bundle.py` against retained copies as an independent structural
cross-check, never as a replacement for Scenario evidence.

After recording evidence, remove only the run-owned fixture registration and
copied files. Prove the pinned Taxi checkout and all upstream source bytes are
unchanged.
