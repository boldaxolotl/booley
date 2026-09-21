"""Console adapter for the Project-independent Host Bootstrap module."""

from __future__ import annotations

from booley.harness.bootstrap import BootstrapState, reconcile_bootstrap
from booley.harness.colors import accent, bold_chrome, green, red, yellow
from booley.runtime.host_install import HostInstallationError, adopt_host_installation
from booley.runtime.image_lifecycle import Intent
from booley.runtime.lifecycle_lock import host_lifecycle_lock
from booley.runtime.paths import skills_dir


def run_bootstrap(args: object) -> int:
    """Run Host Bootstrap and render its typed findings."""
    adopt = getattr(args, "adopt_installation", False)
    upgrade = getattr(args, "upgrade_installation", False)
    if (adopt or upgrade) and getattr(args, "check_only", False):
        print(red("installation adoption cannot be combined with --check-only"))
        return 2
    intent = (
        Intent.CHECK
        if getattr(args, "check_only", False)
        else Intent.REFRESH
        if getattr(args, "force", False)
        else Intent.ENSURE
    )
    if intent is Intent.CHECK:
        from booley.runtime.session_refresh import shared_recovery_blocks_command

        if shared_recovery_blocks_command(read_only=True):
            print(yellow("Interrupted Sandbox host state requires recovery."))
            return 2
        result = reconcile_bootstrap(intent, verbose=getattr(args, "verbose", False))
    else:
        from booley.runtime.session_refresh import shared_recovery_blocks_command

        with host_lifecycle_lock("host bootstrap"):
            if adopt or upgrade:
                try:
                    identity = adopt_host_installation(skills_dir(), replace=upgrade)
                except HostInstallationError as exc:
                    print(red(f"Cannot adopt host installation: {exc}"))
                    return 2
                print(
                    green(
                        "Adopted canonical Booley host installation "
                        f"{identity.version} ({identity.payload_fingerprint[:12]})."
                    )
                )
            if shared_recovery_blocks_command(read_only=False):
                print(
                    yellow(
                        "Recovered interrupted Sandbox host state; run `booley bootstrap` again."
                    )
                )
                return 2
            result = reconcile_bootstrap(intent, verbose=getattr(args, "verbose", False))
    print(bold_chrome("Host Bootstrap"))
    glyphs = {
        BootstrapState.CURRENT: (accent, "[--]"),
        BootstrapState.PENDING: (yellow, "[!!]"),
        BootstrapState.CHANGED: (green, "[OK]"),
        BootstrapState.ERROR: (red, "[XX]"),
    }
    for finding in result.findings:
        color, glyph = glyphs[finding.state]
        print(f"  {color(glyph)} {finding.resource}: {finding.detail}")
    if result.exit_status == 0:
        print(green("Host Bootstrap is current."))
    elif result.exit_status == 1:
        print(yellow("Host Bootstrap has pending work; run `booley bootstrap`."))
    else:
        print(red("Host Bootstrap is incomplete; fix the errors above and retry."))
    return result.exit_status
