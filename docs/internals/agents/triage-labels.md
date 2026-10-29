# Triage labels

| Canonical role    | GitHub label       | Meaning                                |
| ----------------- | ------------------ | -------------------------------------- |
| `needs-triage`    | `needs-triage`     | Maintainer needs to evaluate this issue |
| `needs-info`      | `needs-info`       | Waiting for information from reporter  |
| `ready-for-agent` | `ready-for-agent`  | Fully specified and ready for an agent |
| `ready-for-human` | `ready-for-human`  | Requires human implementation          |
| `wontfix`         | `wontfix`          | Will not be actioned                    |
| `tracker`         | `tracker`          | Parent issue that tracks its sub-issues |

When a skill mentions a canonical triage role, apply the corresponding GitHub
label.

`tracker` is this repository's tracking state for a parent issue whose work
was split into sub-issues. A tracker has no Agent Brief and is never
`ready-for-agent`. Triage applies it once the maintainer accepts a split, in
place of `needs-triage`, so "needs attention" sweeps skip the parent. Close a
tracker when its last sub-issue closes. A split the pipeline proposes stays
`needs-triage` until the maintainer accepts it.
