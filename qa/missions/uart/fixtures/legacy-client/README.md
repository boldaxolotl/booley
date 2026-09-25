# Legacy Sandbox Attachment fixture procedure

Recipes for the legacy-container part of the uart mission's `runtime-lifecycle` area.

Provision this matrix on the native QA host with a real VS Code Dev Containers
Sandbox Attachment and a disposable Docker daemon. Headless Docker output cannot
stand in for it. Freeze the preceding published Booley release, current declared target build, VS Code
and Dev Containers extension versions, old issued configuration, immutable Docker
container IDs, and named-volume identities in `log.md` before each case. Run each case from a
fresh copy of the same disposable Project and keep all attempts under `evidence/`.

Create the baseline through the preceding release's normal `booley init` and
VS Code **Reopen in Container**. Close the window without deleting its container;
prepare the Project with the target build's normal host initialization. Verify
that the old Sandbox still uses the old project-data bind and is running. A copied
label or invented inspect response is not an authenticated baseline.

The control matrix uses actual resources, not simulated Docker inspection:

| Case | Fixture change before current `booley session prepare` | Required observation |
|---|---|---|
| legacy-replace | One authenticated old VS Code Sandbox Attachment | Only that immutable ID stops/removes; named volumes survive; the current VS Code Sandbox Attachment reopens |
| legacy-refuse-headless | Old Sandbox created through `booley session up`, not VS Code | Refusal; Sandbox unchanged |
| legacy-refuse-foreign | Use the old Sandbox belonging to a second run-owned Project; make the selected VS Code workspace-association label conflict in a separately created disposable container | Refusal; both Projects and original Sandbox unchanged |
| legacy-refuse-ambiguous | Separate disposable container has the selected VS Code workspace-association label but lacks the authentic issued identity/configuration labels | Refusal; every container unchanged |
| legacy-refuse-multiple | Two distinct running containers reproduced from the authenticated old VS Code creation configuration, including its original bind/volume definitions | Refusal; neither stops; preserve both IDs |
| legacy-poststop-failure | Use the wrapper below to remove one declared owned bind after the actual selected stop | Validation fails with the exact `docker start <immutable-id>` command |
| legacy-restart | Restore that bind and execute the displayed command | Same Sandbox usable again, same named volumes |

For the three deliberately inconsistent/duplicate containers, save the original
Dev Containers creation command and the exact changed Docker arguments. Only
VS Code workspace-association, configuration, and identity labels and the run-owned container name may vary.
Do not attach borrowed volumes, credentials or host directories. A host unable to
reproduce the recorded old creation command is missing fixture infrastructure;
hand-authored product state is no substitute, so log the case as skipped and why.

For deterministic post-stop injection, create `fixture.json` in an operator-owned
fixture root with `docker` (absolute original executable), `container_id` (the exact
64-character run-owned ID), and `owned_bind` (relative path to the disposable bind
inside that root). The bind must already be referenced by the real issued current
spec. Place a `docker` launcher in the case-owned PATH prefix that executes
`docker_fixture.py` with all arguments unchanged; set `QA_LEGACY_FIXTURE_ROOT`.
On Windows use a case-owned `docker.cmd` calling Python with `%*`; do not overwrite
the installed Docker executable. Every command executes real Docker. Only after a
successful stop of the selected ID does the wrapper rename the bind to
`<name>.qa-restoration`, before returning control to public preparation.

Remove the PATH wrapper, restore the renamed bind, then run the exact recovery
command reported by Booley. Capture the bind contents and Docker and Sandbox Attachment evidence before
cleanup. Stop/remove only the run-owned IDs listed in `resources.md`, preserve named volumes until
restart/restoration proof is complete, and then release the declared fixture.
