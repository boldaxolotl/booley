"""Console adapter for the Project-independent Host Bootstrap module."""

from __future__ import annotations

from booley.harness.bootstrap import BootstrapState, reconcile_bootstrap
from booley.harness.colors import accent, bold_chrome, green, red, yellow
from booley.harness.image_lifecycle import Intent


def run_bootstrap(args: object) -> int:
    """Run Host Bootstrap and render its typed findings."""
    intent = (
        Intent.CHECK
        if getattr(args, "check_only", False)
        else Intent.REFRESH
        if getattr(args, "force", False)
        else Intent.ENSURE
    )
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
