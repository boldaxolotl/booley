---
status: accepted
---

# Remember Project Roots Explicitly

Booley keeps a host-owned Project Inventory of canonical Project-root paths and
joins it with Project Grants at read time. Project Initialization remembers a
root, while migration discovers Projects only beneath explicit search roots;
Booley does not scan the whole machine or duplicate grant records into the
inventory. A remembered path remains the administrative identity when it is
missing or uninitialized, so stale grants stay visible and revocable without
requiring the Project directory to exist.

## Considered Options

- Whole-machine discovery was rejected because Projects may live on arbitrary
  or unavailable filesystems, and an unbounded scan is slow and incomplete.
- Docker resources and EDA grants were rejected as the sole inventory because
  Projects can exist without either one.
- A generated Project identifier was rejected because existing grants and
  runtime resources already define identity by canonical root path.

## Consequences

Discovery is explicit and bounded. Reusing a canonical path also reuses its
path-based administrative identity; the inventory does not claim to identify
repository contents independently of their location.
