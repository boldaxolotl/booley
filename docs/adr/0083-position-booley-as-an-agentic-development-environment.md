# Position Booley as an agentic development environment

Status: accepted (amended 2026-11-12: the positioning names RTL design and
verification instead of FPGA and ASIC development, see Amendments)

Amended by [ADR 0092](0092-measure-commits-against-target-metrics.md): once
Goal Mode is removed, read "Goal status" and "Review Package" below as the
Measurement Report and the Review Report. The positioning stands.

## Context

Booley described itself as "the open-source agentic RTL IDE". The word IDE
puts a person editing code at the center, and the roadmap followed that word
toward a VS Code fork with richer editor chrome. That is not how Booley is
used. The Developer Agent writes the RTL and testbenches and runs the EDA
tools; the engineer directs it, answers its questions, runs several sessions
at once (Goal Mode, Parallel Sessions), and judges the evidence in a Review
Package.

Software tools built around that split have adopted the name *agentic
development environment* (ADE): Warp for its agent-first terminal, Orca for
running parallel coding agents in separate worktrees.

## Decision

Booley is an **agentic development environment (ADE) for RTL design and
verification** (amended 2026-11-12, see Amendments). The tagline is "The
open-source Agentic Development Environment (ADE) for RTL design and
verification". The term is new and unknown in
hardware, so taglines and first mentions spell it out; bare "ADE" is used
only after that. The product name, the `booley-rtl` package, and the CLI stay as
they are.

The glossary lists "IDE" and "agentic RTL IDE" as terms to avoid for the
product itself. VS Code remains the shell; it is described as the window
that holds agent sessions, the Booley Dashboard, and the editor, not as the
product.

Product direction follows the framing: UI work goes into supervising agents
(sessions, Goal and evidence status, review), not editor features. The
roadmap's end state is a standalone ADE; whether it embeds VS Code, forks
it, or uses a lighter editor stays open.

## Rejected alternatives

**Keep "agentic RTL IDE".** It points product work at the editor, which
VS Code already provides, and away from supervising agents, which nothing
provides for RTL work.

**Rename the product.** The category was wrong, not the name; a rename would
break the package, CLI, repository, and published links for no gain.

## Consequences

- README, website, pyproject metadata, glossary, FEATURES, WHY, ROADMAP,
  ARCHITECTURE, and AGENTS.md use the ADE framing.
- Warp and Orca are named only in WHY.md, to explain the category.
- The CHANGELOG keeps its historical "IDE" wording.
- Internal names such as the site's `ide-figure.html` are left alone: that
  illustration shows a VS Code window.

## Amendments (2026-11-12)

The positioning was "an agentic development environment (ADE) for FPGA and
ASIC development", with the tagline "The open-source Agentic Development
Environment (ADE) for FPGA and ASIC development". Both now end in "for RTL
design and verification". The ADE category and everything else in this ADR
stand.

- "ASIC development" reads as covering the back end: place and route, and
  sign-off. Booley covers the front end, and the product of the front end is
  RTL. Its Flows simulate and lint the RTL, and their synthesis and FPGA
  implementation numbers guide RTL changes; they do not sign off a chip.
- "RTL design and verification" is the usual name for that front-end work.
  Verification here means what Booley does today: simulation, coverage, and
  mutation testing of the RTL under design. The roadmap's Verification
  Engineering section covers workflows for dedicated verification engineers,
  which are planned.
- "For FPGA and ASIC RTL development" was rejected: with "(ADE)" in front, it
  puts four abbreviations in one line.
- "For HDL code" was rejected: the project says RTL everywhere, including the
  `booley-rtl` package name, and "code" undersells measuring area, timing, and
  coverage.
- FPGA and ASIC stay named where a doc says what a Flow supports, such as the
  Flow tables and SUPPORTED-EDA-TOOLS.
- The blog post published before this amendment and the CHANGELOG keep their
  wording.
