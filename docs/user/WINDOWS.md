# Windows first-run problems

This page is for running Booley natively on Windows: Booley installed with
Windows Python, your RTL checkout on a Windows drive, and Docker Desktop
running the Linux containers. You don't need Ubuntu or any EDA tool installed
on Windows; every tool runs inside the Sandbox. Read it alongside
[Install](INSTALL.md) and [Setup](SETUP.md): each problem below shows up on
first install or the first `booley init`, in roughly that order.

EDA runs can be slower than on Linux, because the container reads your
checkout through a Windows bind mount, and local image builds take longer.

## `python` or `py` opens the Microsoft Store

A fresh Windows has no real Python, only Store shortcuts with the same names.
Install Python 3.11 or newer first, for example
`winget install Python.Python.3.13`, then open a new terminal.

## Bootstrap rejects your Git version

Booley needs Git 2.37.2 or newer, because earlier Git for Windows releases
fail when Booley prepares a large checkout. Upgrade from
[git-scm.com](https://git-scm.com/downloads/win), open a new terminal, and
rerun `booley bootstrap`, then `booley init`.

## `docker` is not found, but Docker Desktop is installed

The `docker` command joins `PATH` only after Docker Desktop has started.
Launch Docker Desktop, open a new terminal, and rerun the command.
`booley init` stops before changing any Project file when Docker is missing or
not running, so retrying is safe.

## Every file shows as modified inside the container

Git for Windows checks files out with Windows (CRLF) line endings by default.
The Linux container then sees every file as changed. `booley init` fixes this
for you:

- It turns off Git's line-ending conversion for the repository
  (`core.autocrlf=false`) and pins `core.eol=lf`, so a checkout writes LF.
- It converts files already checked out with CRLF back to LF, but only on a
  clean tree.

Booley sets no commit-side policy and never creates or edits a
`.gitattributes` file. If you want Git to normalize CRLF when you commit, add
your own `.gitattributes`, for example with `* text=auto eol=lf`.

If init does not convert the files, it says why. Commit or stash your
uncommitted changes, or clear an `assume-unchanged` or `skip-worktree` flag
(init names the file; use `git update-index --no-assume-unchanged <file>` or
`git update-index --no-skip-worktree <file>`), then run `booley init` again.
The Git settings above still apply. A file that Git cannot re-create with LF
endings, for example one that a Git filter changes on every checkout, stays as
it is, and init lists it.

<details markdown>
<summary>In stealth mode</summary>

`booley doctor` also checks line endings in the Booley Directory's own
repository. See [STEALTH.md](STEALTH.md).

</details>

Don't fix this by hand: `git checkout -- .` keeps the CRLF endings.

`booley doctor` checks line endings on every run and fails with the fix (usually `booley init`) when files are checked out with
CRLF again. It warns `git.autocrlf-risk` when a Git config change would bring
CRLF back on the next checkout.

## Init can't find the Booley executable

The error reads `cannot resolve the trusted host Booley executable`. Booley
looks for `booley.exe` on `PATH` and in Python's Scripts directories, including
the per-user one. If you still get it, reinstall Booley the way you installed
it (check `pipx list` or `uv tool list`), or add the directory that holds
`booley.exe` to `PATH`. Then open a new terminal and rerun `booley init`.

## Host-provisioned Vivado isn't available

Windows uses only the EDA tools inside the Sandbox Image. Vivado isn't one of
them, so the `fpga` Flow doesn't run on Windows. A Vivado installed on
Windows can't run in the Linux container, and Booley rejects
`provisioning = "host"` in `[eda.*]` on Windows. See
[Commercial EDA provisioning](SANDBOX.md#commercial-eda-provisioning).

## Still stuck

See [Troubleshooting](TROUBLESHOOTING.md). To report a Windows problem,
[open an issue on GitHub](https://github.com/boldaxolotl/Booley/issues/new) or
email [boldaxolotl@proton.me](mailto:boldaxolotl@proton.me).
