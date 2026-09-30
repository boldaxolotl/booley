# Doctor health and deep validation

Doctor diagnoses Booley's build and execution machinery in a Project's environment,
including its integration with Project build configuration. Normal Booley Flows
verify the design. A design failure that the machinery executes and interprets
correctly is compatible with healthy machinery. Doctor's existing verdict rules
still apply; deep currency reporting does not change their classification.

Use plain `booley doctor` for ongoing work. It checks current setup and reports
one informational line about prior deep validation. Run `booley doctor --deep`
inside the Sandbox during initial Project Setup, after a Booley version change,
after replacing the active Sandbox Image, or when investigating suspected
machinery problems.

Deep Doctor is **current** when a qualifying deep run matches the executing
Booley version and active immutable Sandbox Image, and no later completed failed
or incomplete deep attempt supersedes it. The recorded date is informational;
deep evidence has no age expiry. Current means prior qualifying setup validation;
it does not guarantee a new deep run will pass after arbitrary design edits.

The two automatic change triggers are:

- A change to the executing Booley version.
- A change to the active immutable Sandbox Image (Docker local image ID).

Project configuration, waiver, `.core`, RTL, testbench, and self-test fixture
edits do not make deep Doctor due. Plain Doctor and affected normal Flow runs
exercise those changes. Moving a mutable image tag does not change the identity
of a Sandbox already running from that image. Selecting an image in configuration
also does not substitute it for the active image.

A qualifying deep run must have no active health failures or unwaived warnings
and must complete every applicable deep check: the Developer Agent authorization
and memory probe, selected Simulation/Lint/ASIC Synthesis smokes, enabled sim/lint
good-and-bad self-tests, and selected-core live resolution. Disabled capabilities
and intentionally excluded FPGA implementation are legitimate exclusions.
Missing required configuration, unavailable execution, absent probe evidence,
omitted agent checks, and missing self-test execution prevent qualification.
Waiving a warning does not execute the missing check.

Checks must run under one verified, stable artifact identity and matching Booley
version. Core resolution runs in the same Sandbox as the Flow checks. Local
FuseSoC resolution inside that Sandbox does not require Docker on its PATH.
A host-side deep invocation cannot qualify when it skips the in-Sandbox Developer
Agent probe; run deep Doctor inside the Sandbox for full evidence.

A completed failed or incomplete deep attempt retains historical success but
makes deep Doctor due until a later qualifying run succeeds. Cancellation or an
unexpected abort before a completed result leaves the previous evidence intact;
version or image drift still applies independently. Successful and failed plain
runs leave deep evidence unchanged.

The host observes the actual owned, running container. Sandbox startup transports
that observation in a root-owned receipt bound to the live container incarnation.
Headless startup writes it before startup checks. VS Code preparation starts a
bounded host observer for its subsequent container start; identity is unavailable
until that observation arrives. Bare terminals, automatic Doctor, and supervised
commands read the same receipt without Docker access. Missing, untrusted, or
stale receipts make identity unverifiable. Restart or reopen the Sandbox through
Booley's normal startup path to restore the receipt.

Missing, corrupt, unsupported, and legacy-only stamps report no qualifying deep
run recorded. Old plain stamps with `deep: true` lack image and completeness
proof and cannot establish currency. A qualifying new run establishes evidence.
Deep records live at `<project_dir>/runtime/doctor_deep_stamp.json`, independently
of the existing plain stamp's configuration fingerprint and age policy.

Plain Doctor and `booley session up` report **current** with version/date or
**due** with all applicable reasons. Manual and automatic structured reports at
`<project_dir>/runtime/doctor/last.json` include `deep_status` with the same status,
reason codes, historical success, and evidence-storage failure indicator.
Storage failures are informational and do not claim new saved success. A known
unsuccessful result still makes that invocation due when saving fails; later
commands can only observe evidence that was successfully saved.

Deep due is advisory: it does not affect health warnings, exit codes, Ticket
Preflight, or execution, and never schedules or automatically launches deep
checks. Automatic plain Doctor retains its existing scheduling policy.
