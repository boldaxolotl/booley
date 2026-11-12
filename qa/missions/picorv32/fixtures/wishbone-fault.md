### Seeded Wishbone fault

The fault for the picorv32 mission's `interactive-repair` area. Its Interactive Mode child is the
same kind of child as in `interactive-bwave`: one long-lived session inside the Booley Sandbox with
the Project working directory, PTY, Sandbox identity, and MCP tool access preserved. Launch it with
`booley session enter -- booley`, or as a direct inherited child when those properties demonstrably
hold. It is not a separate Specialist or delegate.

Send [`prompts/interactive.md`](../prompts/interactive.md) first, before injecting the fault.

The operator then injects exactly one defect in `picorv32.v`, inside `picorv32_wb`: replace the OR
reduction that derives `we` from `mem_wstrb[3:0]` with an AND reduction, and commit it on a
run-owned disposable branch. Full-word writes still work; byte and halfword stores break. The
Interactive Mode prompt never reveals the mutation or its location.

Then send [`prompts/repair-interactive.md`](../prompts/repair-interactive.md) to the same child.

The trace must expose `mem_wstrb`, `we`, `wbm_we_o`, `wbm_sel_o`, `wbm_stb_o`, `wbm_cyc_o`,
`wbm_ack_i`, `mem_valid`, `mem_ready`, and `ram_we`. The diagnosis must rest on signal
relationships, not hard-coded timestamps. A useful deterministic signature is the byte-store
`ERROR` path: the clean pinned run completes, while the seeded run fails early and still produces a
non-empty trace. Save the reproduction, trace, B-Wave queries, diagnosis, patch, clean reruns, and
commit under `evidence/`. Block remote push, then restore the clean pre-exercise checkpoint and
discard all Interactive Mode changes.

Any repair that restores an OR reduction of `mem_wstrb[3:0]` is valid, including the exact upstream
bytes. Reject a repair commit that is empty relative to the fault commit, or that edits tests,
stimulus, or waivers.
