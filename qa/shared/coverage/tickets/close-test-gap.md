Type: verification
Target: sim_generated
Scope: tb/coverage_tb.sv

Add decoder choice 2 to the gap test while retaining reset, counter wrap, choices 0/1 and assertions. Preserve RTL, core, tests.toml and policy. Mandatory simulation sim_generated; mandatory coverage targets [sim_generated], tests all, metrics {cover_property: {min_pct: 100}}. Explicitly collect before and after. Ask the Coverage Analyst about the exact initial Campaign. Retain initial passing simulation / failing coverage, advice and final full Campaign. No waivers or threshold reduction. Finish with a clean committed TB-only diff and verification report. The dedicated Ticket registry contains only gap; all therefore means exactly [gap]. Other Scenarios retain the separate diagnostic registry.
