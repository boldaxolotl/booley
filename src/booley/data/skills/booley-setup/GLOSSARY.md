# Booley terms for hardware engineers

Use each definition verbatim the first time the term appears in the grill or
`SETUP-PLAN.md`. Introduce the plain-English question first, then its definition
and secondary row number/internal key. These explanations describe the shared
concepts in `docs/CONTEXT.md` in hardware-engineer language; setup does not
require the user to read that maintainer glossary.

## Booley Flow
Aliases: Flow, Flows
A Booley Flow runs a complete job, such as simulation, lint, ASIC synthesis, or
FPGA implementation, and reads its results. It is like a Makefile recipe that
also knows how to recognize success and collect evidence.

## Target
Aliases: Targets
A Target is a named build configuration in a FuseSoC `.core` file, like a
Makefile target with its source list, top module, defines, and parameter values.
Choose a different Target when you need a different build configuration.

## Specialist
Aliases: Specialists
A Specialist is an optional AI helper, such as a code reviewer or mutation
tester, given fresh context for one task. It advises or checks work rather than
running the deterministic build job itself.

## Sandbox
A Sandbox is the isolated environment where Booley runs commands, edits files,
and invokes EDA tools for your project. It is usually a Docker container with
your EDA tools, and contains the separate worktrees used for ticket work.

## Doctor
Doctor is Booley's health-check command for the build setup and execution
environment. A healthy setup can still expose a real design failure; Doctor is
not a substitute for verifying the RTL.

## Elaboration Check
An Elaboration Check compiles, elaborates, and links a simulation build without
running its tests. It checks that the design can be built, not that it behaves
correctly.

## Stealth Mode
Aliases: stealth, stealth mode
Stealth mode keeps Booley out of the RTL repo's history by scrubbing protected
names from commit messages and, when Booley authors hidden cores, projecting
ignored copies where FuseSoC can find them. Interactive setup enables it when
you choose to keep Booley out of your git history.

## Hidden-core projection
Booley keeps an authored build description under `.booley_project/cores/` and
makes an ignored copy at the RTL repo root for FuseSoC to discover. The hidden
copy remains authoritative, so you do not edit the projected copy.

## Commit-message scrub
The commit-message scrub removes configured protected words, such as AI and
Booley names, from commit messages through the repo's commit-msg hook. It
changes new commit messages, not the RTL or existing commits.

## Parity Check
Aliases: parity check
A parity check compares Booley's verdict with your existing build script after
setup succeeds. The comparison is meaningful only for a phase where both use
the same EDA tool.

## Tech Cell Replacement
Tech Cell Replacement is your project's shared mapping and design inputs for
realizing technology-dependent RTL with cells from the selected physical
library. Each synthesis build records the part of that mapping it uses and
the evidence that the replacement behaves correctly.

## Flow Cache
Aliases: flow cache, Flow-cache
A flow cache holds reusable build products owned by a Booley Flow, so later
runs can avoid rebuilding unchanged work. Setup preserves it by default;
clearing it means paying the build cost again.

## Vendored-Core Quarantine
Aliases: vendored-core quarantine
Vendored-core quarantine marks third-party `.core` files with the `vendored`
tag so Booley's agents treat them as read-only upstream input. Integrations
belong in your own cores rather than edits to that upstream code.

## `.booley_project/`
Aliases: Project directory
`.booley_project/` is the directory holding Booley's configuration, logs, and
other project state alongside your RTL. Hidden setup keeps it out of the RTL
repo's tracked files; open setup commits its durable configuration.

## `.core`
Aliases: FuseSoC core
A `.core` file is a FuseSoC build description listing source files, dependencies,
and named build configurations. It is comparable to a reusable Makefile
fragment for an IP block.

## VLNV
VLNV means Vendor:Library:Name:Version, the four-part name identifying a FuseSoC
core. Together with a Target name it identifies exactly which build to run.

## Project Grant
Aliases: grant
A Project Grant is host-owned permission for one exact project path to use an
approved commercial EDA installation, license connection, or both. A copy or
move to another path needs its own permission.

## License Profile
A License Profile is a host-owned record of an approved commercial-license
connection, including its server identity and ports. A Project Grant permits
your project to use it; your project configuration does not select it directly.

## Sandbox Image
Aliases: Sandbox image
A Sandbox image is the reusable installed filesystem from which a Sandbox is
created, typically a Docker image containing your EDA tools. It supplies the
programs; the Sandbox is the running environment that uses them.

## Ticket
A Ticket is one self-contained piece of hardware development work with its own
requirements for acceptance and tracked lifecycle. It tells an agent what to
change and what evidence must pass before the work is accepted.
