"""``booley auth`` — store the agent's rotation-free credential.

Wraps the dance (mint the credential → export it → re-seed the devcontainer
spec) into one command, and fixes the part of that dance that was quietly broken:
exporting a value in a shell does NOT make it visible to VS Code's "Reopen in
Container", because the Dev Containers CLI resolves ``${localEnv:...}`` against
VS Code's own process environment, not your terminal's. Storing the credential
where Booley can find it (see :mod:`auth_token`) makes every path work with no
export at all: Booley-driven runs inject it themselves, and the re-seeded spec
mounts it read-only for ``incontainer_register`` to apply on every container
start — which is how it reaches VS Code sessions too.

Why it matters: the credential both apps ship with by default REFRESHES, and
refreshing rotates it — so a long headless run can be killed stone dead by an
unrelated agent session on the host revoking the container's copy. The
rotation-free alternative differs per app:

* **Claude** — ``claude setup-token`` mints a one-year token. Booley runs it.
* **Codex** — there is no ``setup-token`` equivalent; ``codex login`` writes the
  *refreshing* credential we are trying not to depend on. Codex's only
  rotation-free credential is an API key, which must be pasted in — and which
  bills per token instead of against a subscription. That is a real trade-off, so
  the command states it rather than quietly swapping your billing.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

from booley.harness.setup.common import banner, err, info, ok, warn
from booley.runtime import auth_token
from booley.runtime.auth_token import APP_CLAUDE, APP_CODEX, AppCredential

# Where each app's rotation-free credential comes from, in words.
_ACQUIRE_HINT = {
    APP_CLAUDE: "Booley will run `claude setup-token` and store the one-year token.",
    APP_CODEX: (
        "Codex has no `setup-token`: paste an OpenAI API key "
        "(platform.openai.com/api-keys). Note it bills per token, not to a subscription."
    ),
}


# What each effective-credential mode means for billing and rotation, in words.
_MODE_DESC = {
    auth_token.MODE_API_KEY: "API key — rotation-free, bills per token",
    auth_token.MODE_OAUTH_TOKEN: "rotation-free, bills against the subscription",
    auth_token.MODE_SUBSCRIPTION: "refreshing subscription login",
}


def _report_status() -> int:
    """Print the credential each detected app's agents would actually run on.

    Resolved with the agent CLI's own precedence (see
    :func:`auth_token.effective_credential`) — an exported API key outranks
    both the stored rotation-free token and the subscription login, so it must
    never be reported as "falls back to a refreshing one".
    """
    banner("Agent auth status")
    at_rotation_risk = 0
    for app, credential in auth_token.CREDENTIALS.items():
        effective = auth_token.effective_credential(app)
        env_token = (os.environ.get(credential.env_var) or "").strip()
        stored = auth_token.read_stored_token(app)

        if effective is None:
            at_rotation_risk += 1
            warn(f"{app}: no credential found — agents cannot authenticate")
            info(f"       fix: run the app's login, export an API key, or booley auth --app {app}")
            continue

        line = f"{app}: agents run on {effective.source} ({_MODE_DESC[effective.mode]})"
        if effective.rotation_free:
            ok(line)
        else:
            at_rotation_risk += 1
            warn(line)
            info("       a host-side login can revoke a running container's copy mid-run")
            info(f"       fix: booley auth --app {app}")
        for loser in effective.overridden:
            info(f"       {loser} is present but NOT used — the line above wins")

        if stored and not auth_token.stored_token_is_private(app):
            warn(f"{app}: stored credential is readable by others — chmod 600 it")
        if env_token and stored and env_token != stored:
            warn(f"{app}: the exported value differs from the stored one; the exported one wins")
    return 1 if at_rotation_risk == len(auth_token.CREDENTIALS) else 0


def _project_is_initialized(project_root: Path) -> bool:
    """Whether *project_root* resolves to Project-owned state."""
    from booley.runtime.project_dir import resolve_checkout_project_dir

    try:
        return resolve_checkout_project_dir(project_root).is_dir()
    except FileNotFoundError:
        return False


def _mint_claude_token(credential: AppCredential, project_root: Path) -> str | None:
    """Run ``claude setup-token`` and collect the token it prints."""
    assert credential.mint_cmd is not None
    command = list(credential.mint_cmd)
    in_project = _project_is_initialized(project_root)
    if not in_project and shutil.which(command[0]) is None:
        err(
            f"{command[0]} CLI not found on PATH — run this from an initialized Booley "
            "Project to use its Session Runtime, or install the CLI on the host"
        )
        return None

    location = " in the Project's Session Runtime" if in_project else ""
    info(f"Running `{' '.join(command)}`{location} — authorize in the browser when prompted.")
    print()
    # stdio is inherited on purpose: the flow prints a URL, waits, then prints the
    # token. Capturing stdout would hide the URL and leave the user staring at a
    # frozen terminal.
    if in_project:
        from booley.harness import session_runtime

        try:
            returncode = session_runtime.run_project_command(
                project_root,
                command,
                tty=sys.stdin.isatty() and sys.stdout.isatty(),
            )
        except session_runtime.SessionError as exc:
            err(f"could not run `{' '.join(command)}` in the Session Runtime: {exc}")
            return None
    else:
        returncode = subprocess.run(credential.mint_cmd, check=False).returncode
    print()
    if returncode != 0:
        err(f"`{' '.join(command)}` failed (exit {returncode})")
        return None
    info("Paste the token printed above (input is hidden):")
    return _prompt_secret()


def _prompt_secret() -> str | None:
    try:
        value = getpass.getpass("  credential: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        err("aborted — nothing stored")
        return None
    return value or None


def _read_token_from_stdin(credential: AppCredential) -> str | None:
    """Read a credential piped in, e.g. ``claude setup-token | booley auth --token-stdin``."""
    data = sys.stdin.read()
    # A piped mint command prints chatter around the value; take the shaped line.
    for line in data.splitlines():
        candidate = line.strip()
        if candidate and candidate.startswith(credential.prefix):
            return candidate
    lines = [ln.strip() for ln in data.splitlines() if ln.strip()]
    return lines[0] if len(lines) == 1 else None


def _reseed_spec(project_root: Path) -> bool:
    """Refresh this folder's devcontainer spec so it references the credential.

    The devcontainer paths (VS Code, ``booley session``, and headless drivers) read
    the credential through the spec's ``remoteEnv``, so a freshly stored one does
    not reach them until the spec carries the reference.
    """
    from booley.harness.init_cmd import run_init

    if not _project_is_initialized(project_root):
        info(f"no Booley project at {project_root} — skipping devcontainer re-seed")
        info("run `booley auth` (or `booley init --seed`) inside a project to wire it in")
        return True
    return run_init(argparse.Namespace(seed=True), project_root) == 0


def _clear(app: str, project_root: Path) -> int:
    """Drop *app*'s stored credential, falling back to the refreshing one."""
    banner("Agent auth")
    if auth_token.clear_stored_token(app):
        ok(f"removed {auth_token.token_path(app)}")
        info(f"{app} falls back to its refreshing subscription credential")
        # Drop the token-seed mount from the spec. Deleting the file breaks the
        # bind (docker mounts the inode), so existing containers must rebuild.
        if not _reseed_spec(project_root):
            err(
                "credential was removed, but the Project Session Runtime spec could not be "
                "re-seeded; run `booley init --seed` and retry"
            )
            return 1
        info("rebuild any existing container once so it drops the stale credential")
    else:
        info(f"no stored {app} credential to remove")
    return 0


def _acquire(credential: AppCredential, from_stdin: bool, project_root: Path) -> str | None:
    """Get the credential: piped in, minted, or pasted."""
    if from_stdin:
        token = _read_token_from_stdin(credential)
        if token is None:
            err("could not find a credential on stdin")
        return token
    if credential.mint_cmd is not None:
        return _mint_claude_token(credential, project_root)
    # No mint command (Codex): the user supplies the key.
    info(_ACQUIRE_HINT[credential.app])
    return _prompt_secret()


def _resolve_app(args: argparse.Namespace) -> str | None:
    """The app to act on; ``--app`` or the default."""
    app = getattr(args, "app", None) or APP_CLAUDE
    if app not in auth_token.CREDENTIALS:
        err(f"unknown app '{app}' — expected one of: {', '.join(auth_token.CREDENTIALS)}")
        return None
    return app


def _store_and_reseed(token: str, credential: AppCredential, project_root: Path) -> Path | None:
    """Store one credential and refresh the Project's runtime specification."""
    try:
        path = auth_token.store_token(token, credential.app)
    except ValueError as exc:
        err(str(exc))
        return None

    ok(f"stored {credential.label} at {path} (mode 0600)")
    info("kept outside every repo; Session Runtimes receive it only through a read-only mount")
    if _reseed_spec(project_root):
        return path
    err(
        "credential was stored, but the Project Session Runtime spec could not be re-seeded; "
        "run `booley init --seed` and retry"
    )
    return None


def run_auth(args: argparse.Namespace, project_root: Path) -> int:
    """Store and wire up an agent's rotation-free credential."""
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

    if getattr(args, "status", False):
        return _report_status()

    app = _resolve_app(args)
    if app is None:
        return 1
    if getattr(args, "clear", False):
        return _clear(app, project_root)

    credential = auth_token.CREDENTIALS[app]
    banner(f"Agent auth — {app} ({credential.label})")

    token = _acquire(credential, getattr(args, "token_stdin", False), project_root)
    if token is None:
        return 1

    if not token.startswith(credential.prefix):
        # Warn, don't reject: a prefix could change, and locking someone out of
        # their own credential over a cosmetic check is worse than a bad paste.
        warn(f"that does not look like a {credential.label} — storing it anyway")

    if _store_and_reseed(token, credential, project_root) is None:
        return 1

    print()
    ok(
        f"Done. Every {app} container picks this up automatically — "
        "VS Code's 'Reopen in Container' included, no export needed."
    )
    info("It does not refresh, so a host-side login can no longer revoke a running agent.")
    info("Existing containers: rebuild once to add the mount; after that a restart suffices.")
    info(f"Escape hatch: an exported {credential.env_var} still overrides the stored value.")
    return 0
