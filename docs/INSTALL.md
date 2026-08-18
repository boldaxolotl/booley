# Installation

This guide prepares a machine to run Booley: install the host prerequisites,
install the `booley` CLI, and confirm that the executable is available. It does
not change an RTL project.

Once the CLI works, continue to [SETUP.md](SETUP.md) to bootstrap either an
existing RTL repository or a new scaffolded IP.

## Prerequisites

- **Python 3.11+**
- **[Docker](https://www.docker.com/)**: the sandbox image ships Verilator,
  Icarus Verilog, Yosys, OpenSTA, OpenROAD, sv2v, Rust, and both agent CLIs, so
  you don't install them on the host.
- **[VS Code](https://code.visualstudio.com/)** with the [Dev Containers
  extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers):
  Booley runs inside a devcontainer via **Reopen in Container**.
- **Credentials for one supported agent backend** (checked later by `booley
  init`): **Anthropic / Claude** (the default: a Claude Pro/Max subscription or
  an Anthropic API key) or **OpenAI / Codex** (a [ChatGPT plan with Codex
  access](https://learn.chatgpt.com/docs/pricing)—Plus or higher is recommended
  for sustained Booley workloads—or an OpenAI API key). Each project selects one provider through `booley.toml
  [agent] provider` (`claude` or `codex`).

**Supported platforms:** Windows and Linux (Ubuntu 24.04 tested). macOS is not
supported.

> **Windows notes.** The CLI runs **natively on Windows** (repo on `C:\...`,
> Windows Python). You do not work from inside WSL: Docker Desktop's WSL2
> backend only hosts the containers. First-run has several traps: Microsoft
> Store `python` aliases, Docker Desktop's `PATH`, Git's CRLF line-ending
> default, and a long first sandbox-image build. `booley init` and `booley
> doctor` handle the line-ending case for you. See
> [TROUBLESHOOTING.md → Windows first-run problems](TROUBLESHOOTING.md#windows-first-run-problems) for
> each symptom and its fix.
>
> Host-provisioned commercial EDA installations are Linux x86-64 only. Docker
> Desktop hosts Linux Session Runtime containers; mounting a native Windows EDA
> installation does not make its Windows executables runnable there. Windows
> remains supported with the standard image-provisioned Linux EDA toolchain.

## Host and Session Runtime

Booley splits across two sides. The **host** installs the CLI and bootstraps
each project (`pip install`, `booley init`). The per-folder **Session Runtime**
is the devcontainer entered through **Reopen in Container**; it runs `booley
run`, `booley board`, `bwave`, the EDA tools, the Specialist agents, and the
execution steps of the `booley-setup` skill. `booley doctor` runs on either
side. The full per-command ledger is in
[ARCHITECTURE.md](ARCHITECTURE.md).

The project bootstrap and the CLI's host/container enforcement are covered in
[SETUP.md](SETUP.md#bootstrap-the-project--host).

## Install the CLI

```bash
pipx install booley-rtl        # or: pip install booley-rtl
```

`pipx` is the safer default: it puts the CLI in its own virtualenv and links
the `booley` executable into `~/.local/bin` (already on `PATH` on most systems),
sidestepping the `externally-managed-environment` refusal a plain `pip install`
hits on PEP 668 distributions. See
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#pip-install-booley-rtl-fails-with-externally-managed-environment).

Confirm that your shell resolves the installed executable:

```bash
booley --version
```

If the version is not the one you installed, see
[TROUBLESHOOTING.md → The wrong `booley` runs](TROUBLESHOOTING.md#the-wrong-booley-runs-stale-install-shadowing).

## Development install

For an editable install with test dependencies, use a virtualenv:

```bash
git clone https://github.com/boldaxolotl/Booley.git
cd Booley
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

On Debian and Ubuntu, `python3 -m venv` fails until you install the matching
`python3.X-venv` package (the error mentions `ensurepip`) — see
[TROUBLESHOOTING.md → `python3 -m venv` fails](TROUBLESHOOTING.md#python3--m-venv-fails-with-ensurepip-is-not-available).

The `booley` executable then lives in `.venv/bin` and is on `PATH` only while
the venv is active. If you want it available without activating, link it once:
`ln -s "$PWD/.venv/bin/booley" ~/.local/bin/booley`. Whichever environment you
install into has to be the one your shell resolves `booley` from; if the wrong
one shadows this install (the symptom is `booley --version` and `pip show
booley-rtl` disagreeing), use the troubleshooting link above.

## Next step

Booley is now installed. To connect it to an existing RTL repository or
scaffold a new one, continue to [SETUP.md](SETUP.md).
