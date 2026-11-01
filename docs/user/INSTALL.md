# Install

This guide installs the Booley CLI on your machine and prepares the host with
Host Bootstrap. Do it once per machine. To connect Booley to an RTL project
afterwards, follow [Setup](SETUP.md). If a step fails, see
[Troubleshooting](TROUBLESHOOTING.md#installation-and-editor-integration).

## Requirements

Booley supports Windows and Linux (Ubuntu 26.04 tested); macOS is not
supported. Designs must be SystemVerilog or Verilog: VHDL and UVM are not
supported. You need:

- Python 3.11+
- [Git 2.37.2+](https://git-scm.com/downloads)
- [Docker](https://www.docker.com/), with about **4 GB** free for the image
  (**6 GB** with the RISC-V toolchain) plus room for build artifacts
- [VS Code](https://code.visualstudio.com/) 1.109 or newer
- An agent CLI on the host PATH with credentials configured for Project Setup:
  [Claude Code](https://code.claude.com/docs/en/setup) (`claude`, the default)
  or [Codex](https://developers.openai.com/codex/cli) (`codex`)

On Windows, also read [Windows](WINDOWS.md) for first-run traps.

## Install Booley

With [pipx installed](https://pipx.pypa.io/latest/how-to/install-pipx.html),
install Booley and run Host Bootstrap:

<!-- booley-smoke:install -->
```bash
pipx install booley-rtl
booley bootstrap
```
<!-- /booley-smoke:install -->

Check that `booley --version` works. Seeing `externally-managed-environment`,
PATH, or other install errors? See
[Troubleshooting](TROUBLESHOOTING.md#installation-fails-with-externally-managed-environment).

## Upgrade

To upgrade an existing install:

<!-- booley-smoke:upgrade -->
```bash
pipx upgrade booley-rtl
booley bootstrap --update
```
<!-- /booley-smoke:upgrade -->

## Host Bootstrap

Host Bootstrap (`booley bootstrap`) prepares this machine once for every Booley
Project. It checks Git, Docker, VS Code, and your agent CLI, installs the Dev
Containers extension, installs Booley's skills for Claude Code and Codex, and
downloads or builds the Sandbox Image. It does not touch any Project; you
connect one later in [Setup](SETUP.md).

Run it again with `--update` after every upgrade. Other useful options:

- `booley bootstrap --check-only` reports pending work without changing
  anything, and exits 1 when work is pending.
- `booley bootstrap --force` refreshes Booley's host resources. It keeps your
  caches and files.

**Disk space.** When Booley builds an image locally, for example a Project's
own Dockerfile or pip requirements, it needs up to 40 GiB of free Docker space
during the build (47 GiB with RISC-V). The finished image takes about 4 GB
(6 GB with RISC-V). Booley checks first and stops with an estimate if Docker is
too full; it never deletes images, caches, or your data. Set
`BOOLEY_SKIP_IMAGE_DISK_PREFLIGHT=1` on one command to skip the check.

**Build time.** A local image build can take over an hour, longer on Windows.
Booley stops a build after 7200 seconds (2 hours); set
`BOOLEY_IMAGE_BUILD_TIMEOUT` to a larger number of seconds to allow more.

**Host settings.** Machine-wide Sandbox settings, such as
`egress = "unproxied"` (no egress proxy), live in
`~/.config/booley/config.toml`. Booley never creates that file; write it before
you run Host Bootstrap, or run Host Bootstrap again after you change it. See
[Host configuration](SANDBOX.md#host-configuration-configtoml).

## Next

- [Try the demo](https://booley.dev/demos/) on a configured project.
- [Set up your own project](SETUP.md) with Project Initialization and the
  `booley-setup` skill.
