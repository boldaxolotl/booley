# Legacy Sandbox Attachment fixture procedure

Recipes for the legacy-container part of the uart mission's
`runtime-lifecycle` area: Booley must refuse an outdated VS Code Sandbox
Attachment, never migrate or remove it, and print a working repair.

Run this matrix on the native QA host with a real VS Code Dev Containers
Sandbox Attachment and a disposable Docker daemon; headless Docker output
cannot stand in for it. Before each case, record in `log.md` the preceding
published Booley release, the candidate build, the VS Code and Dev Containers
extension versions, the old issued configuration, the immutable Docker
container IDs and the named-volume identities. Run each case from a fresh copy
of the same disposable Project and keep all attempts under `evidence/`.

## Baseline

Create the baseline with the preceding release's normal `booley init` and
VS Code **Reopen in Container**. Close the window without deleting its
container, then prepare the Project with the candidate's normal host
initialization. Verify that the old Sandbox still uses the old Booley Directory
bind and is running. A copied label or invented inspect response is not an
authenticated baseline.

## Cases

The matrix uses actual resources, not simulated Docker inspection. Each case
runs the candidate's `booley session prepare`, the hidden command the
devcontainer runs as its `initializeCommand`.

| Case | Fixture change before `booley session prepare` | Required observation |
|---|---|---|
| legacy-refuse-running | One running old VS Code Sandbox Attachment | Refusal naming the container, its removal and `booley init --seed`; the container is unchanged and still running |
| legacy-refuse-headless | Old Sandbox created through `booley session up`, not VS Code | Refusal; Sandbox unchanged |
| legacy-refuse-foreign | Use the old Sandbox belonging to a second run-owned Project; make the selected VS Code workspace-association label conflict in a separately created disposable container | Refusal; both Projects and original Sandbox unchanged |
| legacy-refuse-ambiguous | Separate disposable container has the selected VS Code workspace-association label but lacks the authentic issued identity/configuration labels | Refusal with the inspect-only repair (no removal commands); every container unchanged |
| legacy-refuse-multiple | Two distinct running containers reproduced from the authenticated old VS Code creation configuration, including its original bind/volume definitions | Refusal; neither stops; preserve both IDs |
| legacy-repair | Follow the repair from legacy-refuse-running: close VS Code, stop and remove that container, run `booley init --seed`, reopen in the container | The current VS Code Sandbox Attachment opens; named volumes survive |

For the three deliberately inconsistent or duplicate containers, save the
original Dev Containers creation command and the exact changed Docker
arguments. Only the VS Code workspace-association, configuration and identity
labels and the run-owned container name may vary. Do not attach borrowed
volumes, credentials or host directories. A host that cannot reproduce the
recorded old creation command is missing fixture infrastructure; hand-authored
product state is no substitute, so log the case as skipped and why.

## Cleanup

After each case, capture the Docker and Sandbox Attachment evidence before
cleanup. Stop and remove only the run-owned IDs listed in `resources.md`, keep
named volumes until the legacy-repair proof is complete, then release the
declared fixture.
