### Interactive Mode contract

The QA Scenario Operator launches one long-lived Interactive Mode session as its child inside the same Booley Session Runtime, with the Project working directory, PTY, Session Runtime identity, and MCP tool access preserved. A conforming launch is `booley session enter -- booley` when the environment requires it; a direct inherited child is also valid when those invariants are demonstrably preserved. This is not a separate Specialist or coordinator delegate.

First prompt, before fault injection:

> Work interactively in the current pinned PicoRV32 project. Confirm the Booley session identity, repository cleanliness, Doctor state, and available Targets. Run the traced Wishbone simulation, inspect its artifacts with B-Wave, and report the readiness checkpoint without changing project sources.

The operator then injects exactly one defect in `picorv32.v`, inside `picorv32_wb`: replace the OR reduction that derives `we` from `mem_wstrb[3:0]` with an AND reduction. This preserves full-word writes while breaking byte and halfword stores. The mutation and its location are hidden from the Interactive Mode prompt.

Second prompt to the same child:

> The previously passing Wishbone scenario now fails during ordinary store/load behavior. Reproduce the failure, collect and inspect a fresh trace, use B-Wave to identify the violated signal relationship, diagnose and repair the root cause, then rerun the relevant simulation and lint Target. Commit the repair locally. Do not weaken tests, remove stimulus, add waivers, or push any branch.

The trace must expose `mem_wstrb`, `we`, `wbm_we_o`, `wbm_sel_o`, `wbm_stb_o`, `wbm_cyc_o`, `wbm_ack_i`, `mem_valid`, `mem_ready`, and `ram_we`. Diagnosis is based on signal relationships, not hard-coded timestamps. A useful deterministic signature is the byte-store `ERROR` path; the clean pinned run is known to complete successfully while the seeded run fails early and still produces a nonempty trace/VCD. Record reproduction, trace, B-Wave queries, diagnosis, patch, clean reruns, and commit. Block remote push, then restore the clean pre-exercise checkpoint and discard all Interactive Mode changes.
