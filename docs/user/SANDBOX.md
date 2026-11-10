# Sandbox

The Sandbox is the Docker container where Booley works on a Project. The
Developer Agent, every Booley Flow, and every Specialist run inside it, one
container per Project folder. It runs as a non-root user, without your home
directory, SSH keys, or Docker socket, and by default its network access is
limited to a short allowlist. So an agent mistake stays inside the container instead of
reaching your machine; read the known escape below before you treat it as a
security boundary.

Everyday use needs none of this page: `booley init` and the `booley-setup`
skill configure the Sandbox, and VS Code opens it. Come here to work without
VS Code, change host-wide network or session limits, add Python packages or
EDA tools to the image, or use a licensed EDA tool from the host.

## Known Sandbox escape through shared Git metadata

The Project's Git metadata is writable from inside the Sandbox. A Developer
Agent can plant Git configuration or hooks that later run as you when Git runs
on the host, from your shell, your editor, or Booley itself. Stopping the
Sandbox does not remove them. A planted object-store symlink can also redirect
host Git writes outside the Project. This is
[issue #1529](https://github.com/boldaxolotl/booley/issues/1529); a fix is
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

`session enter` behaves like a terminal inside the container, so every
in-Sandbox command works through it. `Ctrl-C` stops everything the command
started inside the container.

`up` and `enter` reuse a running Sandbox, including one VS Code opened.
`refresh`, `down`, and `up --rebuild` refuse to touch a VS Code-owned Sandbox:
close the window, or use **Dev Containers: Rebuild Container**.

`session refresh` rebuilds only what changed and keeps the current Sandbox if
nothing did. If the Sandbox replacement fails or you interrupt it, Booley
restores the previous Sandbox. The Sandbox is unavailable while the new
container starts. Afterwards it re-applies Project Initialization (Git hooks,
Git settings, guidance links, core projections, exclude entries and the
Dashboard task). It never rewrites tracked files: CRLF files and pending
`.booley/.gitignore` or `[agent]` changes are reported, and `booley init`
applies them. If that step fails, you keep the new Sandbox and the command
exits non-zero. A Sandbox
using a [floating license](SUPPORTED-EDA-TOOLS.md#vivado-host-provisioning-policy)
can't be refreshed in place: run `booley session down` first.

## Agent CLI logins in the Sandbox

The Sandbox receives a login only for the Agent CLIs your Project lists in
[`[agent] clis`](CONFIG.md#agent-clis-agent). For each listed CLI it gets:

- the login you made on the host (Claude's or Codex's login file),
- the token stored by `booley auth`,
- the API key from your shell, forwarded for that CLI only, and
- its own volume for session history, kept across rebuilds.

An unlisted CLI gets none of these, even if you are logged in to it on the host.
With `clis = ["claude"]` the Sandbox holds only Claude's login; with
`clis = ["claude", "codex"]` it holds both.

Each CLI follows its own [`[agent.auth]`](CONFIG.md#pinning-what-bills-agent-auth)
policy. For example, with `claude = "subscription"` and `codex = "api_key"`, Claude's
login is copied in and its API key is not forwarded, while Codex gets its API key
and no login. The Developer Agent's window connects to the CLI that was `default`
when the Sandbox was issued (run `booley session refresh` after changing `default`
to move it); Specialists use whichever listed CLI their model needs.

After you change `clis` or `auth`, or change `default` in a Project that sets
no `clis` (the set is then just the default CLI), the Sandbox refuses to start until you run
`booley session refresh` (or `booley init --seed` and rebuild the container).
`booley doctor` reports a Sandbox that was issued for a different set of CLIs.

Adding a CLI to `clis` hands that CLI's login to the next refreshed Sandbox. The
agent inside the Sandbox can edit `booley.toml`, so review changes to `[agent]`
before you refresh.

### Codex through a model gateway

With `codex = "gateway"` under [`[agent.auth]`](CONFIG.md#pinning-what-bills-agent-auth),
Codex bills through a model gateway your operator runs. For Codex only; Claude
Code cannot use it. For the gateway variables and token contract, see
[the gateway guide](../internals/GATEWAY.md).

- Issuing the Sandbox reads `BOOLEY_GATEWAY_URL` and one token source
  (`BOOLEY_GATEWAY_TOKEN` or `BOOLEY_GATEWAY_TOKEN_COMMAND`) from your shell
  and forwards them into the Sandbox. Issuing fails naming a missing variable.
  No Codex login or API key is copied in.
- Interactive Codex (`codex`, `booley chat --cli codex`) uses the gateway
  whichever CLI is `default`; Claude keeps its own login.
- `BOOLEY_GATEWAY_TOKEN_COMMAND` runs inside the Sandbox, so it must name a
  command that exists there.
- Add the gateway host to `egress_allowlist` (or use an Unproxied Sandbox).
- The values come from the launching shell, like an API key. A headless
  `booley session` start refuses, naming the variable, when that shell lacks it,
  and a changed URL or static token behaves like a changed API key. "Reopen in
  Container" in VS Code reads them from VS Code's own environment, so start VS
  Code from a shell that exports them; without them it refuses to open the
  Sandbox. `booley session enter` into a VS Code-opened Sandbox does not carry
  them; into a headless gateway Sandbox from a shell without them, it names the
  missing variable.
- `booley doctor` from a shell without them cannot check the spec for drift and
  warns. While a headless gateway Sandbox runs, it fails with "active Sandbox
  differs from current host issuance", as for API keys.
- Booley removes the gateway settings from the environment of Claude sessions
  and runs it starts. This is not isolation: other processes of the same user,
  Claude's shell tool included, can read the token from `/proc`. A `booley`
  command run from Claude's shell cannot reach the gateway, and Doctor run there
  notes that the gateway is hidden. On the first start with new Claude state (a new
  Sandbox, a changed Claude policy, or a rebuild when Claude is not listed), a
  Claude panel opened before the start hook has written the blanks runs with the
  values once; other starts are covered. Prefer a short-lived token through
  `BOOLEY_GATEWAY_TOKEN_COMMAND`.

## Host configuration (`config.toml`)

Settings that apply to every Project on this machine live in
`~/.config/booley/config.toml` (or `$XDG_CONFIG_HOME/booley/config.toml`).
The file is optional, and you own it. Booley edits it only when you run
`booley bootstrap --egress proxy` or `--egress unproxied`, which creates the
file if needed and changes only the `[sandbox] egress` value (a symlinked
file is refused: edit its target by hand). Without the file
you get these defaults:

```toml
[sandbox]
idle_timeout_seconds = 7200   # stop a Sandbox after 2 hours with no use
max_sessions = 4              # running Sandboxes allowed on this Docker daemon
egress_allowlist = []         # extra hostnames Sandboxes may reach
egress = "proxy"              # "proxy" or "unproxied"
```

Booley commands that read this file (bootstrap, init, `session`, Doctor) stop
on an unknown key or invalid value. These keys belong here only:
`booley.toml` rejects them.

- **`idle_timeout_seconds`**: a Sandbox stops after this long with no Booley
  activity and no open editor or terminal session in it.
- **`max_sessions`**: counts every Booley Sandbox on the Docker daemon, across
  all Projects. At the limit, starting another one fails with a list of the
  running Sandboxes and their ages; Booley never stops existing work to make
  room. Free a slot with `booley session down` (headless) or by closing the VS
  Code window, or raise the limit. An interrupted VS Code start also holds a
  slot; cancel it in VS Code, then run the `session down` command the error
  prints.
- **`egress = "proxy"`** (default): Sandboxes reach the network only through
  Booley's proxy, which allows the Claude and OpenAI API and login hosts plus
  the hostnames in `egress_allowlist` and their subdomains. Each entry is a
  bare hostname such as `pypi.org`: no scheme, path, port, IP address, or
  wildcard.
- **`egress = "unproxied"`**: Sandboxes get direct network access, including
  to services on the host's Docker gateway address. It can't be combined with a
  non-empty `egress_allowlist`.

The policy is shared: every allowlisted hostname is reachable from every
Project's Sandbox, in both Goal Mode and Interactive Mode.

**After editing the file**, run `booley bootstrap` again. To switch `egress` in
one step, run `booley bootstrap --egress unproxied` (or `proxy`): it records
the mode, bootstraps under it, and names each running Sandbox still on the
previous mode with the steps to recreate it. It refuses a result the file can't
hold (for example `unproxied` next to a non-empty `egress_allowlist`) and a
file that still uses the `[interactive]` table, and changes nothing in
either case. `booley bootstrap --check-only --egress unproxied` reports the
pending change without writing it. After switching `egress` by any route, also
run `booley init --seed` in each Project and recreate its Sandbox (**Dev
Containers: Rebuild Container** in VS Code; `session down`,
then `session up` headless). Until then the running Sandbox keeps its previous
mode, and host-side Doctor warns about it; see
[TROUBLESHOOTING](TROUBLESHOOTING.md#doctor-reports-a-sandbox-whose-egress-mode-differs-from-the-host).

## Project settings (`[sandbox]`)

The Project's `booley.toml` `[sandbox]` table shapes that Project's
container:

```toml
[sandbox]
memory = "8g"                                  # container memory limit; unset = no limit
# image = "booley-sandbox-riscv"               # Sandbox Image to use, see below
# pip_requirements = ["sim-requirements.txt"]  # Python packages to bake into the image
# mount_host_skills = true                     # also load your own agent skills
# mask_paths = ["private/notes"]               # directories the agent must not see
```

`booley doctor` warns when `memory` looks too small for the
[`[jobs]`](CONFIG.md#jobs--concurrency-jobs) limits. `[sandbox] dashboard` is
covered in [CONFIG.md](CONFIG.md#dashboard-and-quiet-goal-presence-goals).

Changes to `mount_host_skills` and `mask_paths` take effect after
`booley init --seed` and recreating the Sandbox; changes to `image` and
`pip_requirements` need a rebuild (see [After changing the
image](#after-changing-the-image)).

### Hiding directories (`[sandbox].mask_paths`)

List directories, relative to the Project Root, that the Developer Agent must
not read: reference answers, a competing implementation, private notes. Each
one appears empty and read-only inside the Sandbox; the files on the host are
untouched. Paths must be relative with no `..`, and you can hide a
subdirectory of `.booley/` but not `.booley` itself. If any entry is invalid,
`booley init --seed` reports it and hides nothing.

Masking hides the working tree only. A file committed to Git is still
readable from Git history, so keep files you want hidden out of the
repository.

### Host skills (`[sandbox].mount_host_skills`)

The Sandbox carries Booley's own `booley-*` skills. Set
`mount_host_skills = true` (default `false`) to also give the agent your
personal skills from `~/.claude/skills` (Claude Code) and `~/.agents/skills`
(Codex). Each is mounted read-only; symlinked skills work. Booley's built-in
skills win a name clash, and so does `~/.claude/skills` over
`~/.agents/skills`. Rerun `booley init --seed` after adding or removing a
skill.

### Python dependencies (`[sandbox].pip_requirements`)

The Sandbox has no package index access while Flows run, so a Project's Python
packages (cocotb BFMs, test helpers) must be baked into its image. List the
requirements files to bake; nothing is discovered automatically:

```toml
[sandbox]
pip_requirements = ["sim-requirements.txt", "python/tb/requirements.txt"]
```

Paths are relative to the Project Root. `booley init` then builds a Project
image on top of the selected base and switches the Project to it; you don't
set `image` for this. If a listed file is missing, `init` stops with an error.
Leave the key unset to use the base image unchanged.

`booley init` and `booley session refresh` leave two kinds of lines out of the
image and print a warning naming each one:

- Lines that need files outside the requirements: editable installs (`-e`),
  includes (`-r`, `-c`), and local or `file:` paths. Install those at runtime
  instead.
- Pins on `fusesoc`, `edalize`, and Booley itself. Booley manages these, and
  an older pin would break its Flows.

Every other pin wins over the base image, including `cocotb`. `booley init`
(without `--check`) and `booley session refresh` print a notice for each pin
that changes a version the base image ships. The base image checks its own
cocotb at build time; that check doesn't cover yours, so the first
`booley flow sim` after the rebuild is your test of the combination.

**Where to keep the file.** If the upstream project keeps its test pins in
`tox.ini`, `setup.py`, or a CI workflow, copy them into a plain requirements
file:

- **In the repository** (for example `sim-requirements.txt`): reviewed and
  cloned like any source file. Prefer this when you can add files.
- **In the Booley Directory** (for example `.booley/sim-requirements.txt`):
  keeps the tracked tree free of Booley files under
  [stealth mode](STEALTH.md#stealth-mode), but a fresh clone won't have it, so
  `booley init` there builds without your pins. See
  [what survives a fresh clone](STEALTH.md#what-survives-a-fresh-clone).
  Don't use `.booley/docker/requirements.txt`: Booley generates that file.

The file is read only when the image is built. After changing it, rebuild
(see [After changing the image](#after-changing-the-image)). For cocotb 1.x
and BFM packages, also read [Baking the cocotb stack into the
image](#baking-the-cocotb-stack-into-the-image).

## Custom sandbox image

To add tools that must exist in every container before anything runs, such
as an extra EDA tool, write a Dockerfile at `.booley/docker/Dockerfile` and run
`booley init`:

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
- Booley images end as the unprivileged `agent` user. Switch to `root` only to
  install packages, then switch back.
- Booley names and selects the built image itself; you don't need
  `[sandbox].image`.
- Booley never edits your Dockerfile, but rebuilds the image when the Booley
  base image underneath it changes.
- For a multi-stage Dockerfile, or one whose `FROM` uses a build argument, add
  a `# booley:parent=<image>` line naming the Booley image it builds on, so
  Booley knows when to rebuild.

With `pip_requirements` set and no Dockerfile of your own, `init` generates
`.booley/docker/Dockerfile` and `requirements.txt` and keeps them up to date.
Once you edit either file, Booley stops regenerating it. To take over a
generated file without editing it, add a `# booley:keep` line.

If `[sandbox].image` names an image Booley doesn't know, Booley neither builds
nor updates it: that image is yours to manage.

For per-worktree preparation, such as building firmware, use
[worktree setup hooks](CONFIG.md#worktree-setup-hooks) instead of the image.

### After changing the image

- **Headless Sandbox:** run `booley session refresh`.
- **VS Code owns the Sandbox:** run `booley init --force`, then **Dev
  Containers: Rebuild Container**.
- **An image you manage yourself:** rebuild or pull it, run
  `booley init --seed`, then recreate the Sandbox.

### RISC-V toolchain image (`booley-sandbox-riscv`)

RISC-V CPU projects compile firmware before simulating. Select the RISC-V
image, which adds to the base Sandbox:

- a RISC-V GCC (newlib, multilib for rv32 and rv64) under both the
  `riscv64-unknown-elf-` and `riscv32-unknown-elf-` prefixes;
- `srec_cat` (srecord) and `dtc` (device-tree compiler);
- Spike, the upstream reference instruction-set simulator, for comparing
  against your core;
- the ratified RISC-V specifications for offline use in `$BOOLEY_RISCV_DOCS`
  (`/opt/riscv-docs`): the unprivileged and privileged ISA manuals, the debug
  specification, and the ELF psABI, plus `pdftotext` to read them as text.

```toml
[sandbox]
image = "booley-sandbox-riscv"
# pip_requirements = ["requirements.txt"]   # still works; builds on the RISC-V image
```

Booley downloads the image that matches your Booley version; a failed
download is an error. Exact versions are in
[SUPPORTED-EDA-TOOLS](SUPPORTED-EDA-TOOLS.md#versions-in-the-sandbox-image),
disk space in [INSTALL](INSTALL.md#host-bootstrap). If older firmware won't
assemble with this GCC, see
[TROUBLESHOOTING](TROUBLESHOOTING.md#risc-v-firmware-wont-assemble-against-the-sandbox-gcc).

## Baking the cocotb stack into the image

The base image ships cocotb 2.x with `cocotbext-axi` and `cocotbext-uart`.
Testbenches that need cocotb 1.x (for example for `TestFactory` or
`cocotb.utils.get_time_from_sim_steps`) or other BFMs bake their own stack
through [`[sandbox].pip_requirements`](#python-dependencies-sandboxpip_requirements):

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

A pin can't fix a cocotb version that doesn't work with the image's
Verilator: the imports succeed but simulation hangs. See
[Simulation stalls at time zero](TROUBLESHOOTING.md#simulation-stalls-at-time-zero-without-results).

## Commercial EDA provisioning

A licensed EDA tool can't ship in the Sandbox image, so Booley can mount one
installed on the host instead. Today this covers Vivado 2025.2 on Linux
x86-64. The Project asks for it:

```toml
[eda.vivado]
provisioning = "host"   # default "image"
```

The request alone grants nothing. The host administrator registers the
installation and grants it to this exact Project folder with `booley eda`, so
a Project can't pick a host path or license server itself. License server
addresses, license file paths, and license environment variables don't belong
in `booley.toml`. The full procedure is in [Vivado host-provisioning
policy](SUPPORTED-EDA-TOOLS.md#vivado-host-provisioning-policy).

## Hand-written devcontainer specs (Codespaces)

`booley init` writes the devcontainer spec, so you normally set neither
variable below. You need them only when you write the spec yourself, for
example for a GitHub Codespace. Put them in the spec's `containerEnv`, so
every process in the container sees them, including a plain `docker exec`.

### Sandbox egress mode (`BOOLEY_EGRESS`)

Inside a Sandbox, Booley routes agents and tools through its egress proxy. A
container that can't reach that proxy, such as a Codespace, must declare
itself an **Unproxied Sandbox**:

| `BOOLEY_EGRESS` | Effect |
|---|---|
| unset or `proxy` | Default. Booley sets the `HTTP_PROXY` variables unless the environment already has them. |
| `unproxied` | Booley sets no proxy variables; agents and tools use the network the platform gives them. |
| anything else | `booley` and `booley-mcp` refuse to start (exit 2). `booley doctor` still runs and FAILs. |

Booley never guesses this from the platform (for example from `CODESPACES`).
In an Unproxied Sandbox Doctor WARNs `sandbox.unproxied`; to accept that,
[waive the check](DOCTOR.md#doctor-waivers-doctor-waiverstoml).

### Agent app in a hand-written spec (`BOOLEY_AGENT_APP`)

`BOOLEY_AGENT_APP` names the agent app Booley connects to: `claude`, `codex`,
`none`, or `auto`. With `auto`, Booley picks at every Sandbox start from the
credentials present:

1. Claude, if there is a Claude credential (`ANTHROPIC_API_KEY`,
   `CLAUDE_CODE_OAUTH_TOKEN`, a `booley auth` token, or a Claude login), even
   when a Codex one is also present.
2. Otherwise Codex, if there is a Codex credential (`OPENAI_API_KEY`, a
   `booley auth` token, or a Codex login).
3. Otherwise Codex through a configured model gateway.
4. Otherwise Claude, which then asks you to log in.

Booley makes the choice in its start-up step, so the spec's
`postStartCommand` (and `postCreateCommand`, if it has one) must run the
registrar module `booley.harness.incontainer_register` with `python -m`.

`booley doctor` in the Sandbox shows which app `auto` picked and why. If
[`[agent] default`](CONFIG.md#agent-clis-agent) (or the older `provider`) pins an app, set
`BOOLEY_AGENT_APP` to that app or remove the pin. `auto` works only inside a
container.

---

Found a problem with the Sandbox?
[Open an issue on GitHub](https://github.com/boldaxolotl/Booley/issues/new) or
email [boldaxolotl@proton.me](mailto:boldaxolotl@proton.me).
