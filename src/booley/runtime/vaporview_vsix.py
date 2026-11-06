"""Apply Booley's VaporView compatibility edits to the bundled VSIX at image build.

Run once in the Sandbox Image build, after the Booley wheel is installed, as
``python -I -m booley.runtime.vaporview_vsix``. It rewrites the pinned VSIX
the generated devcontainer spec installs (``vaporview.BUNDLED_VSIX``) with the
same verified edits the attach hook (:mod:`booley.runtime.incontainer_vaporview`)
otherwise makes after install, so VS Code installs an already-compatible
extension. That removes the race in which VaporView activates before the attach
hook patches it and shows "Error getting user theme" in the first window of a
fresh container (#2701; upstream Lramseyer/vaporview#207).

Unlike the attach hook, this command fails loudly: an unexpected VSIX must
fail the image build rather than ship an unpatched extension. It is idempotent
(an already-patched VSIX is left as is) and replaces the file atomically.

GPL-3.0 notices: the VSIX gains ``extension/BOOLEY-MODIFICATIONS.txt``, so
every installed copy says it was modified; ``MODIFICATIONS.txt`` is written
beside the upstream license; and the base image's ``SOURCE.txt`` claim that the
VSIX is redistributed unmodified is rewritten to name the shipped build.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path

from booley.runtime import incontainer_vaporview, vaporview

# Members of the VSIX archive the compatibility edits touch.
_MANIFEST_MEMBER = "extension/package.json"
_BUNDLE_MEMBER = "extension/dist/extension.js"

# Where the base image keeps VaporView's license and source pointer; the
# modification notice goes beside them. A copy of the notice also ships inside
# the VSIX, so the installed extension carries it too (GPL-3.0 section 5(a)).
_LICENSE_DIR = Path("/usr/local/share/booley/licenses/vaporview")
DEFAULT_NOTICE = _LICENSE_DIR / "MODIFICATIONS.txt"
DEFAULT_SOURCE_NOTICE = _LICENSE_DIR / "SOURCE.txt"
_NOTICE_MEMBER = "extension/BOOLEY-MODIFICATIONS.txt"

# The date the current edit set was authored. GPL-3.0 section 5(a) asks for a
# relevant date in the modification notice; update it when the edits change.
_MODIFIED_ON = "2026-11-06"


class VsixPatchError(RuntimeError):
    """The VSIX could not be patched; the message names the file and reason."""


def _read_members(archive: zipfile.ZipFile, path: Path) -> tuple[str, str]:
    """Read the manifest and bundle text that the edits rewrite."""
    try:
        manifest = archive.read(_MANIFEST_MEMBER).decode("utf-8")
        bundle = archive.read(_BUNDLE_MEMBER).decode("utf-8")
    except (KeyError, UnicodeDecodeError) as exc:
        raise VsixPatchError(f"{path}: not a readable VaporView VSIX ({exc})") from exc
    return manifest, bundle


def _write_archive(source: zipfile.ZipFile, target: Path, replacements: dict[str, str]) -> None:
    """Copy every member into *target*, swapping in the replacement texts.

    Each member keeps its original metadata (name, timestamp, compression,
    permissions), so the output is deterministic for a given input. A
    replacement with no existing member (the in-VSIX notice) is appended with
    the bundle's metadata, which keeps it deterministic too.
    """
    with zipfile.ZipFile(target, "w") as output:
        for info in source.infolist():
            if info.filename in replacements:
                data = replacements[info.filename].encode("utf-8")
            else:
                data = source.read(info)
            output.writestr(info, data)
        existing = set(source.namelist())
        template = source.getinfo(_BUNDLE_MEMBER)
        for name, text in replacements.items():
            if name not in existing:
                info = zipfile.ZipInfo(name, date_time=template.date_time)
                info.compress_type = template.compress_type
                info.external_attr = template.external_attr
                info.create_system = template.create_system
                output.writestr(info, text.encode("utf-8"))


def _carries_notice(archive: zipfile.ZipFile) -> bool:
    """Whether the VSIX already holds the current in-extension notice."""
    try:
        return archive.read(_NOTICE_MEMBER).decode("utf-8") == extension_notice()
    except (KeyError, UnicodeDecodeError):
        return False


def patch_vsix(path: Path) -> bool:
    """Patch the VaporView VSIX at *path* in place.

    Adds the in-extension modification notice alongside the two edits.
    Returns ``True`` when the file was rewritten and ``False`` when it was
    already patched. Raises :class:`VsixPatchError` when the file is missing,
    is not a zip, or is not the verified VaporView build; the original file is
    then left untouched.
    """
    temporary: Path | None = None
    try:
        # The source archive is closed before the swap: Windows refuses to
        # replace a file that is still open.
        with zipfile.ZipFile(path) as archive:
            replacements = _replacements(archive, path)
            if replacements is None:
                return False
            handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.booley-")
            os.close(handle)
            temporary = Path(name)
            _write_archive(archive, temporary, replacements)
        temporary.chmod(path.stat().st_mode)
        temporary.replace(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise VsixPatchError(f"{path}: cannot patch VSIX ({exc})") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


def _replacements(archive: zipfile.ZipFile, path: Path) -> dict[str, str] | None:
    """The member texts to write, or ``None`` when the VSIX is already patched."""
    manifest, bundle = _read_members(archive, path)
    try:
        patched = incontainer_vaporview.patch_extension_texts(manifest, bundle)
    except incontainer_vaporview.ExtensionPatchError as exc:
        raise VsixPatchError(f"{path}: {exc}") from exc
    if patched is None and _carries_notice(archive):
        return None
    replacements = {_NOTICE_MEMBER: extension_notice()}
    if patched is not None:
        patched_manifest, patched_bundle = patched
        replacements |= {_MANIFEST_MEMBER: patched_manifest, _BUNDLE_MEMBER: patched_bundle}
    return replacements


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_CHANGES = (
    "Changes, both in the files VS Code installs from the VSIX:\n"
    "- extension/dist/extension.js: the two calls to getTokenColorsForTheme()\n"
    "  are replaced with `void 0`, so VaporView no longer looks up the desktop\n"
    "  color theme, which fails in a remote window (Lramseyer/vaporview#207).\n"
    "- extension/package.json: vaporview.customColor5..7 are machine-scoped\n"
    "  and default to Booley's waveform palette.\n"
    "\n"
    "The edits are made by booley/runtime/incontainer_vaporview.py and\n"
    "booley/runtime/vaporview_vsix.py in the Booley distribution; the modified\n"
    "work remains under GPL-3.0 (see LICENSE.txt). A VaporView installed from\n"
    "anywhere else receives the same edits from Booley's attach hook.\n"
)

_NOTICE_HEADER = f"Modified VaporView {vaporview.VERIFIED_VERSION} ({vaporview.EXTENSION_ID})\n\n"


def extension_notice() -> str:
    """The modification notice carried inside the VSIX, so every installed copy has it."""
    return f"{_NOTICE_HEADER}Booley modified this extension on {_MODIFIED_ON}.\n\n{_CHANGES}"


def modification_notice(vsix: Path) -> str:
    """The image-level notice beside VaporView's license, naming the shipped file."""
    return (
        f"{_NOTICE_HEADER}"
        f"Booley modified {vsix} on {_MODIFIED_ON}.\n"
        f"The shipped file is the modified build (sha256 {_sha256(vsix)});\n"
        f"SOURCE.txt names the official build and source it was made from.\n"
        f"\n"
        f"{_CHANGES}"
    )


# The redistribution statement the base image writes into SOURCE.txt
# (Dockerfile.base), and the form this module rewrites it to. Tests feed the
# base recipe's exact text through :func:`rewrite_source_notice`.
_UNMODIFIED_STATEMENT = re.compile(
    r"Redistributed unmodified as (?P<path>\S+)\n"
    r"\(Open VSX build, sha256 (?P<sha>[0-9a-f]{64})\)\.\n"
)
_MODIFIED_STATEMENT = re.compile(
    r"Made from the official Open VSX build \(sha256 (?P<sha>[0-9a-f]{64})\)\.\n"
    r"Shipped modified as (?P<path>\S+) \(sha256 [0-9a-f]{64}\);\n"
    r"see MODIFICATIONS\.txt for the changes\.\n"
)
_ATTACH_HOOK_STATEMENT = (
    "Booley's attach hook applies a small compatibility edit to the installed copy;\n"
    "see booley/runtime/incontainer_vaporview.py in the Booley distribution.\n"
)


def rewrite_source_notice(text: str, shipped_sha256: str) -> str:
    """Replace the base image's "redistributed unmodified" claim in SOURCE.txt.

    Keeps the license and corresponding-source lines, states that the shipped
    VSIX is modified, and drops the superseded attach-hook statement. Rewriting
    an already-rewritten notice refreshes the shipped hash. Raises
    :class:`VsixPatchError` when the text has neither form, so a changed base
    recipe fails the build instead of shipping a contradictory notice.
    """
    match = _UNMODIFIED_STATEMENT.search(text) or _MODIFIED_STATEMENT.search(text)
    if match is None:
        raise VsixPatchError("SOURCE.txt has no recognized VaporView redistribution statement")
    statement = (
        f"Made from the official Open VSX build (sha256 {match['sha']}).\n"
        f"Shipped modified as {match['path']} (sha256 {shipped_sha256});\n"
        f"see MODIFICATIONS.txt for the changes.\n"
    )
    rewritten = text[: match.start()] + statement + text[match.end() :]
    return rewritten.replace(_ATTACH_HOOK_STATEMENT, "")


def _write_notices(notice: Path, source_notice: Path, vsix: Path) -> None:
    """Write MODIFICATIONS.txt and bring SOURCE.txt in line with the shipped VSIX."""
    source = source_notice.read_text(encoding="utf-8")
    rewritten = rewrite_source_notice(source, _sha256(vsix))
    notice.parent.mkdir(parents=True, exist_ok=True)
    notice.write_text(modification_notice(vsix), encoding="utf-8")
    notice.chmod(0o644)
    if rewritten != source:
        source_notice.write_text(rewritten, encoding="utf-8")


def main(argv: Sequence[str] = ()) -> int:
    """Patch the bundled VSIX and write its notice; exit non-zero on any failure."""
    parser = argparse.ArgumentParser(
        prog="python -m booley.runtime.vaporview_vsix",
        description="Apply Booley's VaporView compatibility edits to the bundled VSIX "
        "at image build and write the GPL-3.0 modification notice.",
    )
    parser.add_argument("--vsix", type=Path, default=Path(vaporview.BUNDLED_VSIX))
    parser.add_argument("--notice", type=Path, default=DEFAULT_NOTICE)
    parser.add_argument("--source-notice", type=Path, default=DEFAULT_SOURCE_NOTICE)
    options = parser.parse_args(argv)
    try:
        changed = patch_vsix(options.vsix)
        _write_notices(options.notice, options.source_notice, options.vsix)
    except (VsixPatchError, OSError) as exc:
        print(f"vaporview_vsix: {exc}", file=sys.stderr)
        return 1
    state = "patched" if changed else "already patched"
    print(f"vaporview_vsix: {state} {options.vsix}; notice {options.notice}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
