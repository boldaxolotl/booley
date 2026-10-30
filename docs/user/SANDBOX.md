# Sandbox

Every Booley Flow, Specialist, and Developer Agent runs inside one Docker
container per Project folder: the Sandbox. This page covers its settings:
host-wide Sandbox Policy, the Project's `[sandbox]` table, network egress,
custom images, and commercial EDA provisioning. Read the known limitation
below before you rely on the Sandbox as a security boundary.

## Known Sandbox escape through shared Git metadata

[Issue #1529](https://github.com/boldaxolotl/booley/issues/1529) tracks a known,
unresolved escape hatch from the Sandbox. Shared Git metadata is writable from
the container. A Developer Agent can plant executable configuration or hooks
that run as the host user when Git later runs on the host, including through
an editor or Booley. Stopping the Sandbox does not remove the planted metadata.
The investigation also demonstrated that a substituted Git object-store symlink
can redirect native host Git writes outside the Project. The exposure includes
filesystem writes as well as execution of planted commands.

Doctor does not currently detect this tampering, and host-side Doctor can
itself trigger it. A successful Doctor run or a review of tracked changes is
not a security clearance: Git configuration and hooks need not appear in the diff.

Until this is fixed, keep Git inspection inside an already-running Sandbox after untrusted
agent work, and avoid host Git operations on the affected checkout, including
automatic editor Git integrations and Booley commands that invoke Git. Do not
use host-side Doctor as a cleanup or validation step. For work requiring a
stronger boundary, use a disposable VM or a separate machine without valuable
host credentials or data; these precautions are not a fix for the vulnerability.

Host-side `booley init` and `booley session refresh` also invoke Git. There is
currently no audited list of safe host Booley commands for an affected checkout;
these precautions do not establish that opening or refreshing a Sandbox is safe.

Workspace tasks and `.envrc` are additional ways to execute Project-controlled
code on the host. Do not approve host execution of untrusted Project files.
Remediation of #1529 is deferred for reconsideration on **31 DEC 2026**, not
promised for that date.

## Host configuration (`config.toml`)

Optional global policy lives at `~/.config/booley/config.toml` (or beneath
`$XDG_CONFIG_HOME/booley`). An absent file selects these defaults and is never
created automatically:

```toml
[sandbox]
idle_timeout_seconds = 7200
max_sessions = 4
egress_allowlist = []
egress = "proxy"
```

`idle_timeout_seconds` applies when no client Docker exec is running and the later of the last valid Booley heartbeat and container start time is older than the timeout (start time alone when the heartbeat is absent). Booley MCP calls and supervised commands update the heartbeat. Running execs used by Sandbox Attachments, such as VS Code and terminal shells, or other clients prevent reaping for their entire lifetime; the reaper's heartbeat reads do not. An inspection failure keeps the Sandbox for that pass. Closing an attachment does not reset the heartbeat timestamp.

The file is user-owned: Booley reads it but never rewrites it. Parsing is
strict. Unknown tables or keys, malformed TOML, wrong types, non-positive
limits, and invalid egress entries stop Host Bootstrap before mutation. Egress
entries are hostnames only; schemes, paths, ports, IP literals, and wildcards
are rejected.

`egress` chooses how issued Sandboxes reach the network: `"proxy"` (the
default when absent) or `"unproxied"`. Any other value, or `"unproxied"` with a
non-empty `egress_allowlist`, stops Host Bootstrap before mutation. Under
`"unproxied"`, Booley issues **Unproxied Sandboxes**: no proxy variables,
`BOOLEY_EGRESS=unproxied` in the spec's `containerEnv`, and the Booley-managed
routed network `booley-unproxied-v1` instead of the egress network. The first
session start creates that network; a licensed Sandbox joins it next to its
private license network. After you switch modes, the next session start
refuses the Sandbox spec issued for the other mode and asks you to run
`booley init --seed` (then reopen the folder in the container, or use Dev
Containers: Rebuild Container if VS Code is running it). A running Sandbox
keeps its mode until it is recreated; until then, headless `booley session
enter` and `booley auth` refuse it with the same remedy. Host `booley doctor`
prints the host egress mode and warns about each running Sandbox still in the
other mode; see
[Doctor reports a Sandbox whose egress mode differs from the host](TROUBLESHOOTING.md#doctor-reports-a-sandbox-whose-egress-mode-differs-from-the-host).
Like Docker's default `bridge`, `booley-unproxied-v1` also lets Sandboxes reach
services listening on the host's Docker gateway address.

Host `config.toml [sandbox]` and Project `booley.toml [sandbox]` have different
keys: host limits and egress belong only in the host file; Project image and
memory settings belong only in the Project file. Init and Doctor reject
host-only keys placed in Project `[sandbox]`.

This Sandbox Policy applies to the whole Docker daemon, across all Projects
and both Goal Mode and Interactive Mode. The
timeout and admission cap cover all Booley Sandboxes, every extra
egress hostname becomes reachable from every Project, and `egress` applies to
every Project's issued Sandboxes.

`max_sessions` is checked before a start would add a live Sandbox. At the limit,
the command refuses, lists live Projects and ages, and explains how to free capacity
or raise the limit; it never evicts existing work. Use `session down` for a
headless Sandbox; close the VS Code window or stop its Sandbox through Dev
Containers for an editor-owned Sandbox. A pending VS Code start also
holds a slot and can be cleared with the reported `session down` command after
canceling the editor operation.

## Sandbox (`[sandbox]`)

This is the Project table in `booley.toml`. Host-wide `idle_timeout_seconds`,
`max_sessions`, `egress_allowlist`, and `egress` belong in
[`config.toml [sandbox]`](#host-configuration-configtoml).

One container, one memory limit:

```toml
[sandbox]
# image = "my-project-booley-sandbox:latest"  # optional project image, see
#                                             # "Custom sandbox image" below
memory = "8g"   # single container memory limit, fed into the generated
                # devcontainer; unset means no explicit limit
# pip_requirements = ["sim-requirements.txt"]  # Python deps to bake, see below
```

`booley doctor` warns when the limit looks too small for the configured
`[jobs]` caps.

### Commercial-license policy

A supported License Profile is host-owned, separately granted to the exact Project Root, and emits only the
fixed Sandbox pointer required by the built-in commercial policy. Do not put
license-server addresses, license-file paths, or environment forwarding in
`booley.toml`.

### Host skills (`[sandbox].mount_host_skills`)

By default the sandbox carries only Booley's built-in `booley-*` skills (baked
into the image). Flip this on to also use your **own** global Claude Code or Codex skills
inside the container:

```toml
[sandbox]
mount_host_skills = true   # default false
```

`booley init --seed` scans your host skill dirs — `~/.claude/skills` (Claude)
and `~/.agents/skills` (Codex) — resolves each entry to its **real** directory
(these dirs are usually full of symlinks, which would otherwise dangle inside
the container), and mounts each one **read-only**. Booley's own built-ins are
excluded (they are already in the image and always win a name clash), and each
skill is de-duplicated by name. The Developer Agent discovers them alongside the built-ins
on every container start. Nothing on the host is ever written. Rerun
`init --seed` after adding or removing host skills.

### Python dependencies (`[sandbox].pip_requirements`)

The sandbox has no network while a Flow runs, so a project's Python deps must be
**baked into its image**, not installed on demand. `booley init` bakes exactly
the requirements files you list here, and **nothing is auto-discovered**, then
builds and automatically selects `<slug>-booley-sandbox`. Leave the key unset
(or empty) and the project runs the base image with no extra deps. Set
`[sandbox].image` only when selecting a genuinely custom image.

```toml
[sandbox]
pip_requirements = ["sim-requirements.txt", "python/tb/requirements.txt"]
```

The value is a **list of paths to requirements files** (a bare string is
ignored). Paths are relative to the Project Root; any name and any depth works.
`init` warns on an entry it can't find and skips it — and if every entry is
missing, the project quietly falls back to the base image, so check the `init`
output rather than assuming the pins landed. Two content rules still apply to
the files it bakes:

- Non-bakeable lines are **skipped with a warning**: editable installs (`-e`),
  nested includes (`-r`/`-c`), and local/`file:` paths. The isolated build
  can't reach those paths, so install them at runtime instead.
- Pins on the packages the base image manages (`fusesoc`, `edalize`, and Booley
  itself) are **dropped with a warning**, not baked: a design repo pinning an
  older `fusesoc` would otherwise silently downgrade the driver out from under
  the flows.

**Everything else you pin wins.** That list of managed packages is short on
purpose: your project owns its verification stack. Pinning `cocotb==1.5.1`
replaces the base image's curated `cocotb==2.1.0`, and `init` says so:

```
pin 'cocotb==1.5.1' overrides the base image's cocotb==2.1.0 (project wins)
```

That line is a note, not a warning — nothing is blocked. But understand what
you took on: the base image validates *its* cocotb (imports, VPI library
present) at build time, and that check does not re-run on your layer. A project
pin is a commitment to test the resulting combination yourself, and the first
`booley flow sim` after the rebuild is that test.

**Where the file lives when upstream has no requirements.txt.** Plenty of
projects keep their test pins in `tox.ini`, `setup.py`, or a CI workflow, none
of which this knob reads. Transcribe them into a real file. Two homes work, and
the choice is about whether the pins should survive a fresh clone:

- **Tracked, in the repo** (e.g. `sim-requirements.txt` at the root, or next to
  the testbenches) — the file is a normal source file, reviewed and cloned like
  any other. Preferred when you can add a file to the repo.
- **Inside the Booley Directory** (e.g. `.booley/sim-requirements.txt`) —
  legal, and the right call under [stealth mode](STEALTH.md#stealth-mode) when you
  do not want a Booley-shaped file in the tracked tree. The catch is durability:
  `.booley/` is excluded from the host repo, so a fresh clone has no
  such file and `booley init` there produces a base-image project with none of
  your pins. See [what survives a fresh clone](STEALTH.md#what-survives-a-fresh-clone).
  Do not name `.booley/docker/requirements.txt`: that path is the
  *generated* output of this knob.

Either way the file is an **image-build input**, consumed only by `booley init`
(Step 9b). It must exist on disk before the build, and changing it does nothing
until you re-run `booley init` and recreate the container.

This is also the supported way to pin a **cocotb 1.x** stack, or to add BFM
packages (`cocotbext-*`) the base image doesn't carry — with rules of its own:
see [Baking the cocotb stack into the
image](#baking-the-cocotb-stack-into-the-image).

## Sandbox egress mode (`BOOLEY_EGRESS`)

Inside a Sandbox, Booley points every agent and tool at its egress proxy
(`booley-proxy`) by setting the `HTTP_PROXY` family of variables. A Sandbox
that cannot use that proxy, such as a Codespace you wrote the devcontainer
spec for, declares an **Unproxied Sandbox** instead. Booley-issued Sandboxes
are proxied unless the host sets `[sandbox] egress = "unproxied"` in
[`config.toml`](#host-configuration-configtoml), which issues
`BOOLEY_EGRESS=unproxied` for you; set the variable yourself only in
hand-written specs.

| `BOOLEY_EGRESS` | Effect |
|---|---|
| unset or `proxy` | Default. Booley sets the proxy variables when the environment did not deliver them. |
| `unproxied` | Booley sets no proxy variable; agents and nested MCP servers inherit none. |
| anything else | `booley` and `booley-mcp` refuse to start (exit 2) and name `proxy` and `unproxied`. `booley doctor` still runs and FAILs. |

Set it in the spec's `containerEnv`, not `remoteEnv`: a bare `docker exec`
drops `remoteEnv`, and the Sandbox would silently fall back to `proxy`. The
variable is read only inside a Sandbox (`BOOLEY_CONTAINER=1`) and is never
inferred from platform signals such as `CODESPACES`.

It is a declaration, not a security boundary. Any process in a Sandbox can set
it for itself; in a Sandbox Booley issued under host `egress = "proxy"` the
internal network still denies direct egress, and Booley never removes a proxy
variable the Sandbox delivered, so doing so only stops Booley adding its own
proxy variables in that process and turns Doctor's unresolved-proxy FAIL into
the waivable WARN for that run.

Doctor reports the mode in the Sandbox checks:

- `proxy` and the proxy host resolves: PASS.
- `proxy` and the proxy host does not resolve: FAIL. Start the Sandbox from a
  Booley-issued devcontainer spec, or declare `BOOLEY_EGRESS=unproxied`.
- `unproxied`: WARN `sandbox.unproxied`, because Booley does not mediate
  egress. Accept it by waiving that check ID in
  [`doctor-waivers.toml`](DOCTOR.md#doctor-waivers-doctor-waiverstoml).
- Any other value: FAIL naming the two valid values.

## Agent app in a hand-written spec (`BOOLEY_AGENT_APP`)

`BOOLEY_AGENT_APP` names the agent app whose user config gets the Booley MCP
entry. `booley init` writes `claude` or `codex` for you; set it yourself only
in a hand-written spec, such as a Codespace.

| `BOOLEY_AGENT_APP` | Effect |
|---|---|
| `claude` or `codex` | Register that app. |
| `none` | Register nothing. |
| `auto` | Pick the app from the credentials the user brought, at every start. |

`auto` takes the first match in this order:

1. A Claude credential (`ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, a
   stored `booley auth` token, or the Claude login file). Claude wins when a
   Codex credential is also present.
2. A Codex credential (`OPENAI_API_KEY`, a stored token, or the Codex login
   file).
3. A complete [gateway](../internals/GATEWAY.md) configuration, when
   `BOOLEY_PRIMARY_AUTH` is unset or `auto`: Codex through the gateway. A
   user's own credential always wins over the gateway.
4. Otherwise Claude, with its login offered.

The spec's `postStartCommand` (and `postCreateCommand`, when the spec has one)
must run Booley's registrar, the `booley.harness.incontainer_register` module.
It makes the choice at every start, so a changed credential changes the app at
the next start, and Booley's agents, `booley chat` and `booley doctor` all
follow it. In a Sandbox `booley doctor` shows the app and why it was chosen,
and WARNs when `[agent] provider` pins an app that `auto` might not pick;
remove the pin or set `BOOLEY_AGENT_APP` to it.

`auto` outside a Sandbox is refused, and the message names the variable.

## Custom sandbox image

To add project-specific EDA tools to every sandbox container, hand-author a
Dockerfile at `.booley/docker/Dockerfile` that extends `booley-sandbox`,
then run plain `booley init`:

```dockerfile
FROM booley-sandbox
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends my-tool \
    && rm -rf /var/lib/apt/lists/*
USER agent
```

The shipped base and flavor images finish as the unprivileged `agent` user.
Switch to `root` only for OS-package installation, then restore `agent` before
the image is used as a Sandbox.

```console
$ booley init
[--] manual edits detected in docker/{Dockerfile} and image
     'myproj-booley-sandbox-<root-digest>' is not built — using your files as the build
     input (leaving them untouched)
[OK] built myproj-booley-sandbox-<root-digest>
```

That is one pass, not a recipe: init builds the image **from your file
verbatim** and re-seeds the devcontainer spec later in the same run. The image
name is derived automatically from the Project slug and canonical-root digest.
A matching manual `.booley/docker/Dockerfile` is built above the selected
Booley wheel overlay, so no matching `[sandbox].image` entry is written or needed. Set `[sandbox].image` only to select a genuinely
custom name or a Booley flavor image.

What init decides, and how to steer it:

| Configured selection and recipe | Initialization behavior |
|---|---|
| Standard or Booley flavor with matching manual `FROM` | Build the selected wheel overlay, then the exact manual recipe under this Project's private image name. |
| Manual `FROM` mismatches the selection or is ambiguous | Report the selection and parsed parent, preserve the recipe, and leave it unused. |

| Your `.booley/docker/` state | What `booley init` does |
|---|---|
| No Dockerfile, `[sandbox].pip_requirements` set | Generates `Dockerfile` + `requirements.txt` and builds the derived image |
| Hand-authored Dockerfile, image **not** built | Builds the derived image from your file untouched |
| Hand-authored Dockerfile, image already built | Leaves it alone while current; rebuilds it verbatim when inherited Booley provenance is stale |
| `[sandbox].image` naming an image Booley doesn't recognise | Skips the step entirely — you manage that image |

"Hand-authored" means: the file does not carry init's generated header, *or* it
carries one but its contents do not match the stamped self-hash (you edited
it), *or* it contains the line `# booley:keep`. Add `# booley:keep` to a file
that started life generated and that you now want to own — it is the explicit
"hands off" flag, and it works for `requirements.txt` too:

```dockerfile
# booley:keep
FROM booley-sandbox-riscv
RUN pip install --no-cache-dir -r /tmp/reqs.txt
```

Hand-authored recipes remain conservative: Booley preserves their bytes and
exact ancestry and cannot move their Project dependencies below its final wheel
overlay. The same inherited-provenance check rebuilds a Project image left
behind by a parent update. Manual ownership prevents file rewriting; it does
not freeze stale parent layers into the Sandbox.

Booley infers ancestry only from a Dockerfile with one unambiguous `FROM`. For
a multi-stage or variable-based recipe, declare the managed direct parent
without changing Docker semantics:

```dockerfile
# booley:parent=booley-sandbox-riscv
FROM booley-sandbox-riscv AS tools
FROM tools AS runtime
```

The directive is lifecycle metadata. Booley verifies the resulting image label
against that parent's current immutable Docker image ID; it never treats a tag
or matching Booley version as proof of ancestry.

Published Booley flavors use the registry equivalent of that rule: their label
records the base's digest-qualified GHCR reference, and init verifies it against
the pulled base's repository digests. It does not compare Docker image IDs from
the publisher and consumer, because those local identities are not registry
provenance.

Use the [post-setup hook](CONFIG.md#worktree-setup-hooks) for per-worktree
preparation. Use a custom image for EDA tools that must exist in every container
before commands run.

**Changing an already-built image is a lifecycle operation, not a config
edit.** For a headless Booley-managed Sandbox, `booley session refresh` plans
the minimal invalid image closure, builds and verifies transaction candidates
while the prior Sandbox remains available, then pins the new immutable image ID,
recreates the Sandbox, and probes its installed Booley wheel before discarding
the predecessor. For generated recipes, a Python-only change rebuilds only the
final wheel overlay; compatible EDA, RISC-V, and Project-dependency substrates
retain their exact immutable IDs. A matching manual recipe remains above the
wheel overlay and its descendants rebuild when that wheel changes. A failed recreation or probe restores the prior Sandbox. If VS
Code owns the running Sandbox, refresh the image with
`booley init --force` and use **Dev Containers: Rebuild Container**. Explicit
external images remain your responsibility: rebuild or pull them, run
`booley init --seed`, then recreate and probe the Sandbox.

### RISC-V toolchain image (`booley-sandbox-riscv`)

RISC-V CPU-core projects (ibex, picorv32, biriscv, …) compile firmware/tests
with a RISC-V cross-compiler before simulating, so Booley ships a prebuilt
**`booley-sandbox-riscv`** image: the base sandbox plus:

- a **multilib RISC-V GNU toolchain** (xPack prebuilt, newlib) exposing both
  `riscv64-unknown-elf-` and `riscv32-unknown-elf-` prefixes and covering
  rv32i / rv32im / rv32imc (ilp32) plus the rv64 ABIs;
- **`srec_cat`** (srecord), which ibex's `.vmem` generation hard-depends on, and
  **`dtc`** (device-tree-compiler);
- **Spike** (`riscv-isa-sim`), the official upstream reference ISS for
  differential testing (not lowRISC's `ibex-cosim` fork that Ibex's UVM
  co-simulation needs);
- the ratified **RISC-V International spec set** baked in for offline use at
  `$BOOLEY_RISCV_DOCS` (`/opt/riscv-docs`): the unprivileged and privileged ISA
  manuals (`riscv-isa-unprivileged.{pdf,html}` and
  `riscv-isa-privileged.{pdf,html}`), the external debug spec, and the ELF psABI;
  the ISA manuals follow the newest date-named upstream release, checked at
  each Booley release currency audit
  ([ADR 0071](../adr/0071-follow-date-named-riscv-isa-manual-releases.md));
  **`pdftotext`** is included for shell and Developer Agent text extraction.

Reserve about 6 GB of Docker storage for the complete RISC-V demo stack, plus
Project artifacts and temporary upgrade/build data. The current Linux/AMD64
image occupies 2.02 GB on the measured containerd store and exposes a 4.48 GB
filesystem; its standard-image layers are shared rather than additive.

Point a project at it with the normal image selector:

```toml
[sandbox]
image = "booley-sandbox-riscv"
```

**CSR instructions require an explicit `zicsr`.** With GCC 15, a Project
defaulting to `-march=rv32imc` fails to assemble every
`csrr` / `csrw`. Override the ISA at the build invocation, not in the sources:

```bash
make -C sw/... ARCH=rv32imc_zicsr     # simple-system style firmware
make -C sw/... RV_ISA=rv32im_zicsr    # CoreMark's variable name
```

The right variable name is the project's own; grep its makefiles for `rv32i`.
Keep the override at the call site (the `post-setup` hook).

Project Initialization owns its private selected image; shared flavor and runtime-base
artifacts remain immutable inputs. Internally, both user-facing
images are assembled from reusable substrates in
`ghcr.io/boldaxolotl/booley-sandbox-base`; those substrate tags are an
implementation detail, not additional `[sandbox].image` choices.

When Booley is running from a source checkout, it reuses compatible local
standard and RISC-V substrates and installs the requested checkout's wheel in a
final overlay. An installed official distribution pulls and verifies only the
selected complete standard or RISC-V image.

To build or refresh the host flavor by hand (Bootstrap acquires or reuses the
contract-compatible runtime base; add `--refresh-base` to rebuild it explicitly):

```bash
./src/booley/data/docker/build-riscv.sh
```

Or pull the pre-built image from the GitHub Container Registry:

```bash
docker pull ghcr.io/boldaxolotl/booley-sandbox-riscv:latest
docker tag  ghcr.io/boldaxolotl/booley-sandbox-riscv:latest booley-sandbox-riscv
```

When the Project also declares Python dependencies, keep the RISC-V selector
and declare the requirements normally:

```toml
[sandbox]
image = "booley-sandbox-riscv"
pip_requirements = ["requirements.txt"]
```

Booley automatically selects the generated Project image, installs those
dependencies on the RISC-V substrate, then applies the same final wheel
overlay. All selection surfaces—Project Initialization, Session start,
refresh, and Doctor—resolve that generated image name consistently.

## Baking the cocotb stack into the image

The base sandbox pins **cocotb 2.x** with a curated BFM set (`cocotbext-axi`,
`cocotbext-uart`). A project that needs different pins, most often **cocotb
1.x**, which `TestFactory` and `cocotb.utils.get_time_from_sim_steps` still
require, bakes its own via
[`[sandbox].pip_requirements`](#python-dependencies-sandboxpip_requirements).

**Bake the project's full pinned test stack, not the subset the testbench looks
like it needs.** Module-scope imports like `cocotb_test.simulator` and `pytest`
(often only for an unused pytest entry point) run at import time, so dropping
them makes cocotb fail to import the testbench at all. Copy the project's own
pin set (`tox.ini` / `requirements.txt`) wholesale.

**Check the pinned stack against Python 3.14**, which the Sandbox runs:

- **`distutils` is gone** (removed in Python 3.12). Packages of the cocotb-1.x
  era still do `from distutils.spawn import find_executable` — `cocotb-test`
  0.2.0 does. Nobody lists `setuptools` as a dependency, so add it explicitly. If a pinned stack
  dies on a missing `distutils`, put **`setuptools`** in the requirements file
  and rebuild.
- **Probe the import path the testbench actually uses.** `import cocotb_test`
  succeeds while `import cocotb_test.simulator` — the line the testbench runs —
  raises, because the submodule is where the dead import lives. Verify with the
  real thing:

    ```bash
    booley session enter -- python -c "import cocotb_test.simulator, cocotb; print(cocotb.__version__)"
    ```

**What a pin cannot fix: the simulator pairing.** cocotb's run loop is compiled
against the simulator, so a cocotb build incompatible with the image's current Verilator can
fail even after its Python dependencies import successfully. If simulation
stalls at time zero, follow the diagnosis and recovery in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#simulation-stalls-at-time-zero-without-results).

## Commercial EDA provisioning

The initial policy supports Vivado 2025.2 on Linux x86-64 only. A Project can
request host provisioning as follows:

```toml
[eda.vivado]
provisioning = "host"
```

`provisioning` is `image` (the default) or `host`. For `host`, the exact
Project Root must have a host-issued Grant selecting one Installation
Registration before a Sandbox can be created. That Grant is the sole source of
the installation name and host path; Project configuration cannot select
either. The host administrator manages registrations and grants using
`booley eda installation register` and `booley eda grant add`.

Successful ordinary `booley init` runs remember their canonical Project Root
in the host-owned Project Inventory. `booley projects` joins those roots with
their Grants and reports each root as `present`, `missing`, or `uninitialized`.
Use `booley projects discover <root>...` to import existing initialized
Projects; discovery scans only the roots named on the command line and does not
follow directory symlinks. Discovery stops descending at each initialized
Project to avoid scanning its RTL, vendor, and build trees, so nested Projects
are not imported by that scan. To import a nested Project, run
`booley projects discover <nested path>` on it directly.
`booley projects forget <project>` removes an
obsolete remembered root only after its live Grants have been revoked.
Forget then releases that root's issued-image keeper tag when no container uses
its immutable image; an in-use keeper is retained while the root is forgotten.
Docker inspection or removal failure preserves the inventory entry for retry.
If a present root's keeper was released, run `booley init` before using it again.
Forget then lists what the Project still holds in Docker, with sizes: images
named by its own tags or recorded as built for it, its agent state volumes, and
containers or networks labelled for it. Each entry says whether a container
uses it or another tag shares it. Forget removes none of the listed resources. A state volume
holds a copy of the agent's login and the agent's history.
`booley projects forget --purge <project>` also removes the unused ones; it
accepts a root that is already forgotten or deleted, unless a live Grant holds it.
For keepers left by earlier forgotten Projects, preview with
`booley projects prune-keepers`, then apply that exact preview with
`booley projects prune-keepers --confirm <digest>`.

EDA administration prints human-readable confirmations by default. Add
`--json` to a public leaf operation when a script needs the stable structured
record.

License Profiles are host-owned and never appear in Project configuration. A
licensed Sandbox receives only a fixed FlexNet relay pointer; it cannot choose
an upstream server or arbitrary license environment. Invalid, missing, drifted,
or revoked authority fails closed before Flow execution.
