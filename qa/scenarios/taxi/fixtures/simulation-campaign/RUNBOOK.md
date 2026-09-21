# Taxi Simulation Campaign shared Simulator Bundle fixture

This fixture supports `campaign.verilator-single-build`, `campaign.heavy-cap`,
`campaign.attempt-isolation`, `campaign.continue-after-failure`,
`campaign.cocotb-batch-resume`, and `campaign.mcp-structured-pointers`. It is
registered for Public QA but remains pending until an authorized Configured
Scenario Run captures product evidence. Unit and real-tool fixture gates do not
complete the Checks.

Copy this directory into the run-owned Taxi Project, register `campaign.core`,
and merge the `sim_campaign_verilator` table into the active test catalog. Run
both exact tests in one invocation with an explicit report root. Preserve the
Verilator version and exact compile/run argv, the immutable Simulation Campaign
Manifest, the sole Simulator Bundle Build Attempt/Result and Simulator Bundle
manifest, both Simulation Attempt/Result records, compatibility projections,
logs, and SHA-256 digests.

Require exactly one `build-variants/*/attempts/*/build-result.json` in state
`ready`, with `bundle.sharing` equal to `shared_variant`. Both ordered results
must authenticate that same build-result byte digest and Simulator Bundle ID
while using distinct Simulation Attempt IDs. Save the build tree and Simulator
Bundle hashes before the first run and after the second; any mutation fails the
Check. Run
`validate_bundle.py` against retained copies as an independent structural
cross-check, never as a replacement for Scenario evidence.

For the three bounded-parallel Checks, apply `parallel.toml`, configure the
run-owned Project with `max_heavy = 3`, and select exact tests `slow-first`,
`slow-fail`, and `slow-last` in that order. The executable holds each process
for 250 ms and writes its test token to the same relative filename,
`qa-shared-name.txt`, inside its attempt-owned directory. This is the complete
owned timing stimulus; do not slow or mutate upstream Taxi sources.

Sample the real filesystem-backed SlotStore from before queue submission until
every child claim is absent. Preserve timestamped simulator intervals, holder
and waiter identities, the borrowed outer execution identity, child execution
identities, and every acquisition/release transition. Require measured peak
simulator overlap greater than one but no greater than three, and count the
borrowed outer Job as one of the three heavy holders. A sample missing the outer
holder does not qualify the cap claim.

Authenticate each `qa-shared-name.txt` against its test and Simulation Attempt;
require distinct canonical run directories, logs, traces, runtime inputs, and
attempt tokens. Retain all three terminal results even though `slow-fail` fails,
then require the summary to render pass/fail/pass in manifest order with strict
grade `fail`. Run `validate_parallel.py` over retained, independently assembled
timeline evidence as a structural cross-check.

After recording evidence, remove only the run-owned fixture registration and
copied files. Prove the pinned Taxi checkout and all upstream source bytes are
unchanged.

For `campaign.cocotb-batch-resume`, use the Scenario's existing Taxi Cocotb
Target and select its exact multi-test suite in one Simulation Campaign. Record
the Cocotb version, module/package manifest, selector environment without
secrets, and complete XML/JSON transport. Terminate and reap only the owned
producer process group after the batch begins and before terminal publication.
Preserve the interrupted attempt, then resume from the exact immutable Manifest.
Require one new attempt for the same single `cocotb_batch` work item, a complete
whole-batch rerun, and multiple XML-derived observations on that attempt. Do not
infer or synthesize independent Simulation Attempt IDs for individual Cocotb
tests. Run `validate_phase5.py` over retained copies as an independent structural
cross-check.

For `campaign.mcp-structured-pointers`, invoke the Simulation Flow over MCP with
the exact ordered `test` array used above; do not use a comma-delimited scalar or
implicit all-tests selection. Retain the complete request, response bytes, byte
count, displayed text card, exit code, and the pointed-to Manifest, summary,
simulation compatibility report, results, and Cocotb XML/JSON. Require the
bounded structured response to contain nonempty manifest, summary, and simulation
pointers, aggregate counts, and a preview of at most 32 observations. Every
preview entry retains exactly its test, execution, functional, assertions,
assertion count, and bounded detail fields. Require `observation_total` and
`observations_truncated` to distinguish a complete preview from a bounded prefix;
the authoritative Manifest/result pointers remain available when the preview is
truncated. The human-readable text card must preserve the same exit code.
Validate the retained response with `validate_phase5.py` and the product's
declared MCP output bound.
