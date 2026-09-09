# H-06 legacy client fixture procedure

Provision this matrix on the native QA host with the real supported VS Code Dev
Containers client and a disposable Docker daemon. It cannot be qualified by
headless Docker output. Freeze the preceding published Booley release, current
release, extension/client versions, old issued configuration, container immutable
IDs and named-volume identities before each case. Run each case from a fresh copy
of the same disposable Project and retain all attempts.

Create the baseline through the preceding release's normal `booley init` and
VS Code **Reopen in Container**. Close the window without deleting its container;
prepare the Project with the current release's normal host initialization. Verify
that the old runtime still uses the old project-data bind and is running. A copied
label or invented inspect response is not an authenticated baseline.

The control matrix uses actual resources, not simulated Docker inspection:

| Case | Fixture change before current `booley session prepare` | Required observation |
|---|---|---|
| legacy-replace | One authenticated old VS Code runtime | Only that immutable ID stops/removes; named volumes survive; current client reopen works |
| legacy-refuse-headless | Old runtime created through `booley session up`, not VS Code | Refusal; runtime unchanged |
| legacy-refuse-foreign | Use the old runtime belonging to a second run-owned Project; make the selected workspace association conflicting in a separately created disposable container | Refusal; both Projects and original runtime unchanged |
| legacy-refuse-ambiguous | Separate disposable container has the selected workspace association but lacks the authentic issued identity/configuration labels | Refusal; every container unchanged |
| legacy-refuse-multiple | Two distinct running containers reproduced from the authenticated old VS Code creation configuration, including its original bind/volume definitions | Refusal; neither stops; preserve both IDs |
| legacy-poststop-failure | Use the wrapper below to remove one declared owned bind after the actual selected stop | Validation fails with the exact `docker start <immutable-id>` command |
| legacy-restart | Restore that bind and execute the displayed command | Same runtime usable again, same named volumes |

For the three deliberately inconsistent/duplicate containers, retain the original
Dev Containers creation command and the exact changed Docker arguments. Only
workspace/configuration/identity labels and the run-owned container name may vary.
Do not attach borrowed volumes, credentials or host directories. A host unable to
reproduce the recorded old creation command is missing fixture infrastructure;
it cannot substitute hand-authored product state or receive qualification credit.

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
command reported by Booley. Capture file hashes and Docker/client evidence before
cleanup. Stop/remove only recorded run-owned IDs, preserve named volumes until
restart/restoration proof is complete, and then release the declared fixture.
