# Exercise Ticket Mode in the production-image smoke

The required pull-request image smoke will exercise Ticket Mode inside the freshly built production image, using a test-local scripted backend in place of a live Developer Agent. The real Runner, Harness, worktree setup, stdio MCP server, detached Job contract, Booley Flows, Criteria evidence, Run Report, and Ticket Board transitions remain in the path; direct Criterion-state mutation is forbidden. The scripted backend performs the same required `dut_info` identity edit as a real Developer Agent before invoking any Flow. The smoke runs real Verible lint, Icarus elaboration/simulation, and Yosys/OpenROAD synthesis against a checksum-pinned Nangate45 cache mounted read-only at `/opt/pdk`, with strict assertions that OpenROAD did not degrade to OpenSTA.

## Considered options

- A live Claude or Codex Developer Agent was rejected for required CI because it would require credentials, network access, model spend, and nondeterministic behavior.
- Full `booley session up/enter` provisioning was deferred because it tests host-issued Session Runtime lifecycle rather than the production image and Ticket Mode boundary targeted here.
- Mocking Booley Flow results or writing Criterion outcomes directly to `booley_state.json` was rejected because it would bypass the verification boundary the smoke exists to protect.

## Consequences

CI fetches and validates the small pinned Nangate45 payload afresh rather than redistributing or persistently caching it. One successful Ticket covers mandatory lint, elaboration, simulation, and synthesis Criteria, optional-Criterion justification, dependency-aware invalidation, and review handoff; a second Ticket proves that an unmet mandatory Simulation Criterion transitions to blocked. The outer Runner's operating-system child launch is replaced only inside the test so the scripted backend remains injectable without adding a production test provider.
