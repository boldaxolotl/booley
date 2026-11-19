# Sandbox

The Sandbox is the Docker container where Booley works on a Project. The
Developer Agent, every Booley Flow, and every Specialist run inside it, one
container per Project folder. It runs as a non-root user, without your home
directory, SSH keys, or Docker socket, and by default its network access is
limited to a short allowlist. So an agent mistake stays inside the container
instead of reaching your machine; read the
[known escape](#known-sandbox-escape-through-shared-git-metadata) before you
treat it as a security boundary.

Everyday use needs none of this page: `booley init` and the `booley-setup`
skill configure the Sandbox, and VS Code opens it. Come here to work without
VS Code, add Python packages or EDA tools to the image, use a licensed EDA tool
from the host, change host-wide network or session limits, or apply a settings
change or a Booley upgrade.

## Known Sandbox escape through shared Git metadata

The Project's Git metadata is writable from inside the Sandbox. A Developer
Agent can plant Git configuration or hooks that later run as you when Git runs
on the host, from your shell, your editor, or Booley itself. Stopping the
Sandbox does not remove them. A planted object-store symlink can also redirect
host Git writes outside the Project. This is
[issue #1529](https://github.com/boldaxolotl/Booley/issues/1529); a fix is
scheduled for reconsideration on **31 DEC 2026**, not promised for that date.

Doctor does not detect the tampering, and host-side Doctor can itself trigger
it. A clean Doctor run or a clean diff is not a security clearance: Git
configuration and hooks don't show up in the diff.

Until it is fixed, after untrusted agent work:

- Inspect Git only inside an already-running Sandbox.
- Avoid host Git on that checkout, including your editor's automatic Git
  integration and Booley commands that call Git (`booley init`,
  `booley session refresh`, host-side `booley doctor`). No host Booley command
  is known to be safe there.
- Don't approve host execution of Project files such as workspace tasks or
  `.envrc`.
- For a stronger boundary, use a disposable VM or a separate machine without
  valuable credentials or data.

## Entering the Sandbox without VS Code

Without VS Code (for example over SSH), `booley session` starts the same
container that **Reopen in Container** does. Run these on the host, after
`booley init`:

```bash
booley session up                       # create or start the Sandbox
booley session enter                    # open a shell inside it (starts it if needed)
booley session enter -- booley doctor   # or run one command and exit
booley session status                   # running, stopped, absent, or a pending start/recovery
booley session refresh                  # rebuild the image if needed and recreate the Sandbox
booley session down                     # stop and remove the Sandbox
```

`session enter` works like a terminal inside the container, so every
in-Sandbox command works through it; `Ctrl-C` stops everything the command
started. `up` and `enter` reuse a running Sandbox, including one VS Code opened.
`session down` also stops any `booley flow` or `booley specialist` run typed in
a Sandbox shell.

`session refresh` rebuilds only what changed and is safe to rerun after a
failure. It doesn't touch tracked files; when it reports pending file changes,
run `booley init`. Don't use it on a Project you open in VS Code; see
[Applying Sandbox changes](#applying-sandbox-changes).

## Project settings (`[sandbox]`)

The Project's `booley.toml` `[sandbox]` table shapes that Project's
container:

```toml
[sandbox]
memory = "8g"                                  # container memory limit; unset = no limit
# swap = "2g"                                  # swap beyond memory; default a quarter of memory
# image = "booley-sandbox-riscv"               # Sandbox Image to use, see Custom sandbox image
# pip_requirements = ["sim-requirements.txt"]  # Python packages to bake into the image
# mount_host_skills = true                     # also load your own agent skills
# mask_paths = ["private/notes"]               # directories the agent must not see
```

`memory` sets the [Memory Budget](CONFIG.md#jobs--concurrency-jobs) that
admits Jobs; `booley doctor` warns while it is unset. `[sandbox] dashboard` is
covered in [CONFIG.md](CONFIG.md#dashboard-sandboxdashboard).

`swap` is the swap the Sandbox may use beyond `memory`. Unset, it is a quarter
of `memory`, but no more than the swap your Docker host has: `memory = "12g"`
allows 3 GiB of swap. `swap = "0"` gives a Sandbox no swap, and `swap` has no
effect without `memory`. `booley doctor` warns when the host cannot back the
setting. The debug shell uses the same limits, with 4 GB of memory when
`memory` is unset. Without swap the Memory Budget is smaller; see
[CONFIG.md](CONFIG.md#jobs--concurrency-jobs).

Every `[sandbox]` change takes effect when you
[apply it](#after-changing-sandbox-settings).

### Hiding directories (`[sandbox].mask_paths`)

List directories, relative to the Project Root, that the Developer Agent must
not read: reference answers, a competing implementation, private notes. Each
one appears empty and read-only inside the Sandbox; the files on the host are
untouched. Paths must be relative with no `..`, and you can hide a
subdirectory of `.booley/` but not `.booley` itself. If any entry is invalid,
`booley init` reports it and hides nothing.

Masking hides the working tree only. A file committed to Git is still
readable from Git history, so keep files you want hidden out of the
repository.

### Host skills (`[sandbox].mount_host_skills`)

The Sandbox carries Booley's own `booley-*` skills. Set
`mount_host_skills = true` (default `false`) to also give the agent your
personal skills from `~/.claude/skills` (Claude Code) and `~/.agents/skills`
(Codex). Each is mounted read-only; symlinked skills work. Booley's built-in
skills win a name clash, and so does `~/.claude/skills` over
`~/.agents/skills`. Adding or removing a skill is a change to apply, like any
setting.

### Python dependencies (`[sandbox].pip_requirements`)

The Sandbox has no package index access while Flows run, so a Project's Python
packages (cocotb BFMs, test helpers) are baked into its image:

1. Put the pins in a plain requirements file in the repository, for example
   `sim-requirements.txt`. If the upstream project keeps them in `tox.ini`,
   `setup.py`, or a CI workflow, copy them out.
2. List the file in `booley.toml`. Paths are relative to the Project Root, and
   nothing is discovered automatically; if a listed file is missing, Booley
   stops with an error.

    ```toml
    [sandbox]
    pip_requirements = ["sim-requirements.txt", "python/tb/requirements.txt"]
    ```

3. [Apply the change](#after-changing-sandbox-settings). Booley builds a
   Project image on top of the selected base and switches to it; you don't set
   `image`. The file is read only at build time, so apply again after each
   edit.

<details markdown>
<summary>In stealth mode</summary>

Keep the file in the Booley Directory instead, for example
`.booley/sim-requirements.txt`, but not `.booley/docker/requirements.txt`:
Booley generates that one. A fresh clone won't have the file, so `booley init`
there builds without your pins. See
[Stealth Mode: What changes](STEALTH.md#what-changes).

</details>

**What is left out.** Booley skips these lines and warns about each one:

- Lines that need files outside the requirements: editable installs (`-e`),
  includes (`-r`, `-c`), and local or `file:` paths. Install those at runtime
  instead.
- Pins on `fusesoc`, `edalize`, and Booley itself. Booley manages these, and
  an older pin would break its Flows.

Every other pin wins over the base image, including `cocotb`, and Booley prints
a notice for each version it changes.

<details markdown>
<summary>With a cocotb testbench</summary>

The base image ships cocotb 2.x with `cocotbext-axi` and `cocotbext-uart`.
Testbenches that need cocotb 1.x (for example for `TestFactory` or
`cocotb.utils.get_time_from_sim_steps`) or other BFMs bake their own stack:

- **Copy the project's full pinned test stack**, not just what the testbench
  seems to use. Module-level imports such as `cocotb_test.simulator` or
  `pytest` run when cocotb loads the testbench, so one missing package stops
  it loading at all.
- **Add `setuptools`** if anything fails on a missing `distutils`. The Sandbox
  runs Python 3.14, which has no `distutils`, and cocotb-1.x-era
  packages such as `cocotb-test` 0.2.0 still import it.
- **Check the exact import the testbench uses.** `import cocotb_test` can
  succeed while `import cocotb_test.simulator` fails:

    ```bash
    booley session enter -- python -c "import cocotb_test.simulator, cocotb; print(cocotb.__version__)"
    ```

The base image checks its own cocotb at build time, not yours, so the first
`booley flow sim` after the rebuild is your test of the combination. A pin
can't fix a cocotb version that doesn't work with the image's Verilator: the
imports succeed but simulation hangs. See
[Cocotb Targets](FLOW-SIM.md#cocotb-targets-python-testbenches).

</details>

## Custom sandbox image

To add tools that must exist in every container before anything runs, such
as an extra EDA tool, write a Dockerfile at `.booley/docker/Dockerfile` and
[apply the change](#after-changing-sandbox-settings):

```dockerfile
FROM booley-sandbox
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends my-tool \
    && rm -rf /var/lib/apt/lists/*
USER agent
```

- `FROM` must name the image the Project selects: `booley-sandbox` by
  default, `booley-sandbox-riscv` when `[sandbox] image` selects it. With a
  different `FROM`, `init` reports the mismatch and ignores the file.
- End as the unprivileged `agent` user. Switch to `root` only to install
  packages, then switch back.
- For a multi-stage Dockerfile, or one whose `FROM` uses a build argument, add
  a `# booley:parent=<image>` line naming the Booley image it builds on, so
  Booley knows when to rebuild.

Booley names and selects the built image itself, never edits your Dockerfile,
and rebuilds the image when the Booley base image underneath it changes.

To add tools on top of `pip_requirements`, edit the `.booley/docker/Dockerfile`
that Booley generates for it. Booley stops regenerating a file once you edit
it; to take over a generated file without editing it, add a `# booley:keep`
line.

**An image you manage yourself.** If `[sandbox].image` names an image Booley
doesn't know, Booley neither builds nor updates it; applying changes to it
[takes different steps](#applying-sandbox-changes).

For per-worktree preparation, such as building firmware, use
[worktree setup hooks](CONFIG.md#worktree-setup-hooks-booleyhooks) instead of the image.

<details markdown>
<summary>With the RISC-V image</summary>

RISC-V CPU projects compile firmware before simulating. Select the RISC-V
image, and stack a [custom Dockerfile](#custom-sandbox-image) on it with
`FROM booley-sandbox-riscv` if you need more:

```toml
[sandbox]
image = "booley-sandbox-riscv"
# pip_requirements = ["requirements.txt"]   # still works; builds on the RISC-V image
```

It adds to the base Sandbox:

- a RISC-V GCC (newlib, multilib for rv32 and rv64) under both the
  `riscv64-unknown-elf-` and `riscv32-unknown-elf-` prefixes;
- `srec_cat` (srecord) and `dtc` (device-tree compiler);
- Spike, the upstream reference instruction-set simulator, for comparing
  against your core;
- the ratified RISC-V specifications for offline use in `$BOOLEY_RISCV_DOCS`
  (`/opt/riscv-docs`): the unprivileged and privileged ISA manuals, the debug
  specification, and the ELF psABI, plus `pdftotext` to read them as text.

Booley downloads the image that matches your Booley version; a failed
download is an error. Exact versions are in
[SUPPORTED-EDA-TOOLS](SUPPORTED-EDA-TOOLS.md#versions-in-the-sandbox-image),
disk space in [INSTALL](INSTALL.md#host-bootstrap).

</details>

## Commercial EDA provisioning

A licensed EDA tool can't ship in the Sandbox image, so Booley can mount one
installed on the host instead: read-only, at `/opt/booley-eda/vivado`, and the
tool runs inside the Sandbox. Today this covers only **Vivado 2025.2 on a
Linux x86-64 host**: not on Windows, macOS, or Arm.

A host administrator registers the installation and grants it to one Project
folder; `booley.toml` only asks for it, and never holds license servers,
license file paths, or license environment variables.

Run these commands on the host, outside the Sandbox:

1. Install Vivado 2025.2.
2. Register the installation, pointing at its release root (the directory
    that holds `Vivado/bin/vivado`):

    ```bash
    booley eda installation register vivado-2025.2 --kind vivado --source /tools/Xilinx/2025.2
    ```

3. Grant it to the Project. A Grant is tied to the Project's exact path: a
    moved or copied Project needs a new one.

    ```bash
    booley eda grant add /path/to/project --kind vivado --installation vivado-2025.2
    ```

4. Ask for it in the Project's `booley.toml`:

    ```toml
    [eda.vivado]
    provisioning = "host"   # default "image"
    ```

5. [Apply the change](#after-changing-sandbox-settings).

`booley projects` lists every Project on this machine with its Grants. If
Vivado will not start, run `booley doctor` on the host.
Flow options and optimization profiles are in [FLOW-FPGA.md](FLOW-FPGA.md).

**Floating licenses are experimental.** `booley eda license register` records a
FlexNet license server, and `booley eda grant add --license-profile` gives the
Project access to it. This path has not been tested against real paid-seat
checkouts, so don't rely on it yet.

## Host configuration (`config.toml`)

Settings for every Project on this machine live in
`~/.config/booley/config.toml` (or `$XDG_CONFIG_HOME/booley/config.toml`).
The file is optional; without it you get these defaults:

```toml
[sandbox]
idle_timeout_seconds = 7200   # stop a Sandbox after 2 hours with no use
max_sessions = 4              # running Sandboxes allowed on this Docker daemon
egress_allowlist = []         # extra hostnames Sandboxes may reach
egress = "proxy"              # "proxy" or "unproxied"
```

These keys are host-only: `booley.toml` rejects them, and Booley stops on an
unknown key or an invalid value.

- **`idle_timeout_seconds`**: a Sandbox stops after this long with no Booley
  activity and no open editor or terminal session in it.
- **`max_sessions`**: counts every Booley Sandbox on the Docker daemon, across
  all Projects. At the limit, starting another one fails with a list of the
  running Sandboxes; Booley never stops existing work to make room. Free a
  slot with `booley session down` (headless) or by closing the VS Code window,
  or raise the limit.
- **`egress = "proxy"`** (default): Sandboxes reach the network only through
  Booley's proxy, which allows the Claude and OpenAI API and login hosts plus
  the hostnames in `egress_allowlist` and their subdomains. Each entry is a
  bare hostname such as `pypi.org`: no scheme, path, port, IP address, or
  wildcard. The list is shared: every hostname on it is reachable from every
  Project's Sandbox.
- **`egress = "unproxied"`**: Sandboxes get direct network access, including
  to services on the host's Docker gateway address. It can't be combined with a
  non-empty `egress_allowlist`.

After editing the file, run `booley bootstrap`. A changed `egress` also needs
[each Project's Sandbox recreated](#after-changing-sandbox-settings); until
then a running Sandbox keeps its previous mode. Host-side Doctor warns about
this Project's own Sandbox and notes other Projects' Sandboxes.

To switch `egress` without editing the file, run
`booley bootstrap --egress unproxied` (or `proxy`). It records the mode,
bootstraps, and lists the running Sandboxes to recreate; add `--check-only` to
preview the change.

## Agent CLI logins in the Sandbox

The Sandbox gets a login only for the Agent CLIs listed in
[`[agent] clis`](CONFIG.md#agent-clis-agent). An unlisted CLI gets nothing,
even if you are logged in to it on the host. Each listed CLI receives what its
[`[agent.auth]`](CONFIG.md#pinning-what-bills-agent-auth) policy allows: your
host login, the `booley auth` token, or the API key from your shell. Its
session history is kept across rebuilds.

Booley's tools and skills work in every listed CLI, so a plain `claude` or
`codex` in a Sandbox terminal has them. The Developer Agent's window uses the
`default` CLI; Specialists use whichever listed CLI their model needs.

After changing `clis`, `auth`, or `default`,
[apply the change](#after-changing-sandbox-settings). Until you do, a changed
CLI set or `auth` stops the Sandbox from starting. Adding a CLI hands its login
to the Sandbox, and the agent inside can edit `booley.toml`, so review `[agent]`
edits before you apply them.

## Applying Sandbox changes

A Sandbox keeps the settings and Booley version it was created with until you
recreate it. Recreate it the way you open it: a Project you open in VS Code is
rebuilt from VS Code, even while its window is closed. `booley session refresh`
is only for Projects you enter with `booley session`; on a VS Code Project it
either refuses or starts a second Sandbox that VS Code won't open.

### After changing Sandbox settings

This covers `[sandbox]` and `[agent]` in `booley.toml`, a
[custom Dockerfile](#custom-sandbox-image), `egress` in
[`config.toml`](#host-configuration-configtoml), and
[EDA Grants](#commercial-eda-provisioning). The other `config.toml` keys need
only `booley bootstrap`.

- **In VS Code:** run `booley init` on the host, then
  **Dev Containers: Rebuild Container**.
- **Headless:** run `booley session refresh` on the host.

Until you do, Doctor reports that the Sandbox differs from the settings.

### After upgrading Booley

1. [Upgrade Booley on the host](INSTALL.md#upgrade).
2. In each Project, recreate the Sandbox
   [as for a settings change](#after-changing-sandbox-settings). Until you do,
   Doctor in the Sandbox warns `upgrade.runtime-stale`.
3. In the new Sandbox, type `/booley-heal` in a Claude Code or Codex chat. It
   runs the [upgrade review](DOCTOR.md#upgrade-review) and applies what the
   new release changes for this Project.

<details markdown>
<summary>With your own image</summary>

These steps apply when `[sandbox].image` names an image Booley doesn't build.
`booley session refresh` refuses such an image.

After upgrading Booley, first rebuild your image with the new Booley wheel
installed, and run `python3 -I -m booley.runtime.dashboard_extension` as root
in its recipe. Without that step, Dashboard auto-open and `bwave gui` views are
unavailable.

Then, after an upgrade or a settings change:

- **In VS Code:** run `booley init --seed` on the host, then
  **Dev Containers: Rebuild Container**.
- **Headless:** run `booley session down`, `booley init --seed`, and
  `booley session up` on the host.

`--seed` rewrites the Sandbox's container settings without building any image.
See [Custom sandbox image](#custom-sandbox-image).

</details>

---

Found a problem with the Sandbox?
[Open an issue on GitHub](https://github.com/boldaxolotl/Booley/issues/new) or
email [boldaxolotl@proton.me](mailto:boldaxolotl@proton.me).
