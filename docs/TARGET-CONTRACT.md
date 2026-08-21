# Immutable Target contracts

Status: accepted, 21 AUG 2026

## Context

Ticket Mode previously treated FuseSoC Target recipes as revision-owned. A
developer could therefore add or change a Target while implementing a ticket,
and relative QoR compared the baseline recipe with the final recipe. That made
the thing being measured part of the implementation rather than part of the
ticket's acceptance contract. It also allowed tickets to enter the queue with
missing relative-QoR Targets or caller-supplied baseline SHAs that did not
resolve.

The Target surface can span two repositories. Native `.core` files live in the
RTL repository, while stealth projects keep authored cores and project control
files in a separate `.booley_project` repository.

## Decision

Every new executable ticket carries `target_contract.schema: 1`. Ticket
creation opens isolated worktrees, authors all Target/control-plane changes,
and seals exact outer and project-data commits before enqueue. Top-level
`base_sha` is retained for compatibility and must equal
`target_contract.outer_sha`.

The sealed surface contains all authored `.core` definitions, the project test
registry, Target-selecting Flow configuration, selected constraints, and
referenced generator/hook programs. Its normalized manifest is hashed with
SHA-256. File identity is part of the manifest, so adding, deleting, or
replacing a control file changes the digest even when another file has the same
content.

Developer execution starts at the sealed commits. RTL and testbench contents
may change, but the Target surface may not. The contract is checked before
criteria initialization, at every Flow entry, by the commit guard, and before
review handoff. A mismatch blocks as `target-contract-change-required`; an
implementation agent records the needed revision but does not edit the
contract.

Relative synthesis and FPGA criteria use the sealed outer and project-data
commits for the baseline. Thus baseline and final RTL are evaluated with one
identical normalized recipe. A Target used by a relative criterion must fully
resolve against the sealed baseline. A non-relative future Target may name
missing source files only when every missing path is explicitly declared
`[new]` in ticket Scope.

Contract revision is an author/operator action. It archives the previous
identity, starts again from the destination baseline, reseals, clears execution
evidence, and restarts the ticket. Version 1 does not transplant implementation
commits because doing so could put implementation changes into the QoR
baseline.

## Authoring workflow

After `create-file` writes an approved draft, open the contract checkouts:

```bash
python -m booley.ticket_board contract-open <slug>
```

The command reports the outer worktree and, for a standalone project-data
repository, its paired worktree. Make only Target/control-plane changes there.
Review the complete ticket and both repository diffs, then publish the seal:

```bash
python -m booley.ticket_board contract-seal <slug>
python -m booley.ticket_board enqueue <slug>
```

`enqueue` and fresh execution independently reject a missing, stale, or
unresolvable seal. `create-file` has no public SHA argument; only
`contract-seal` writes `base_sha` and `target_contract` from Git.

For a draft or blocked sealed ticket whose recipe must change, run:

```bash
python -m booley.ticket_board revise-contract <slug>
```

This archives the prior contract commits, removes execution evidence and
implementation worktrees, returns a blocked ticket to drafts, and opens fresh
authoring checkouts. Review, seal, and enqueue it again. An unsealed legacy
blocked ticket must instead run `contract-open` and `contract-seal` before a
normal reset can restart it.

## Consequences

- Target changes are reviewed with ticket intent, before implementation begins.
- A relative QoR comparison cannot silently change its measurement recipe.
- Cross-repository sealing is publish-last: commits may be prepared first, but
  ticket metadata is not executable until all repositories validate and seal.
- Existing running/review tickets may continue in explicit legacy mode. Draft,
  queued, waiting, or reset legacy tickets must be sealed before execution.
- A requested Target change discovered during execution requires a contract
  revision and a fresh execution generation.
