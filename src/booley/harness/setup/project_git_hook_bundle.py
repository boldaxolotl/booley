"""Build the standalone Project Git-hook zip application."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path

BUNDLE_NAME = "project-git-hooks.pyz"
BUNDLE_SCHEMA_VERSION = 1

_SOURCE_INVENTORY = (
    ("boundary.py", Path("core") / "boundary.py"),
    ("checkout_role.py", Path("core") / "checkout_role.py"),
    ("run_command.py", Path("core") / "run_command.py"),
    ("commit_msg_utils.py", Path("dev_support") / "commit_msg_utils.py"),
    ("validate_commit_msg.py", Path("dev_support") / "validate_commit_msg.py"),
    ("commit_msg_hook.py", Path("dev_support") / "commit_msg_hook.py"),
    ("pre_push_hook.py", Path("dev_support") / "pre_push_hook.py"),
)


@dataclass(frozen=True, slots=True)
class ProjectGitHookBundle:
    """The complete bundle and its content identity."""

    content: bytes
    sha256: str


def _source_package_root() -> Path:
    """Return the installed or source-checkout ``booley`` package directory."""
    return Path(__file__).resolve().parents[2]


def _normalized_source(source: Path) -> bytes:
    """Read one canonical source file with platform-independent line endings."""
    try:
        mode = source.lstat().st_mode
    except OSError as exc:
        raise FileNotFoundError(f"canonical hook source is unavailable: {source}") from exc
    if not stat.S_ISREG(mode):
        raise ValueError(f"canonical hook source is not a regular file: {source}")
    return source.read_text(encoding="utf-8", newline="").replace("\r\n", "\n").replace(
        "\r", "\n"
    ).encode("utf-8")


def _read_source_members() -> dict[str, bytes]:
    """Read and validate the explicit source inventory."""
    names = [name for name, _relative in _SOURCE_INVENTORY]
    if len(names) != len(set(names)):
        raise ValueError("project Git-hook bundle contains duplicate source members")
    root = _source_package_root()
    return {name: _normalized_source(root / relative) for name, relative in _SOURCE_INVENTORY}


def _launcher() -> bytes:
    """Return the zip application's command dispatcher."""
    return (
        b"""from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

_COMMANDS = {"commit-msg": "commit_msg_hook", "pre-push": "pre_push_hook"}


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "project-git-hooks.pyz: missing command; expected commit-msg or pre-push",
            file=sys.stderr,
        )
        raise SystemExit(2)
    command = sys.argv[1]
    module_name = _COMMANDS.get(command)
    if module_name is None:
        print(
            f"project-git-hooks.pyz: unknown command {command!r}; "
            "expected commit-msg or pre-push",
            file=sys.stderr,
        )
        raise SystemExit(2)

    bundle = Path(sys.argv[0]).resolve()
    if bundle.parent.name == ".managed":
        os.environ.setdefault("BOOLEY_PROJECT_DIR", str(bundle.parent.parent))
    del sys.argv[1]
    module = importlib.import_module(module_name)
    raise SystemExit(module.main())


if __name__ == "__main__":
    main()
"""
    )


def _manifest(source_members: dict[str, bytes]) -> bytes:
    """Return stable provenance metadata for the bundle."""
    members = sorted(("__main__.py", "manifest.json", *source_members))
    document = {
        "schema": BUNDLE_SCHEMA_VERSION,
        "members": members,
        "source_sha256": {
            name: hashlib.sha256(source_members[name]).hexdigest()
            for name in sorted(source_members)
        },
    }
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    """Pack sorted source bytes with fixed metadata."""
    if len(members) != len(set(members)):
        raise ValueError("project Git-hook bundle contains duplicate archive members")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o100644 & 0o777) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, members[name], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return output.getvalue()


def _validate_archive(content: bytes, source_members: dict[str, bytes]) -> None:
    """Validate the archive's importable Python surface before publication."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("project Git-hook bundle contains duplicate archive members")
        expected = {"__main__.py", "manifest.json", *source_members}
        if set(names) != expected:
            raise ValueError("project Git-hook bundle has an incomplete archive surface")
        for name in expected - {"manifest.json"}:
            if name.endswith(".py"):
                compile(archive.read(name), name, "exec")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("schema") != BUNDLE_SCHEMA_VERSION:
            raise ValueError("project Git-hook bundle has an unsupported schema")
        if manifest.get("members") != sorted(expected):
            raise ValueError("project Git-hook bundle manifest does not match its archive")


def build_project_git_hook_bundle() -> ProjectGitHookBundle:
    """Build deterministic Project Git-hook bytes from canonical package sources."""
    source_members = _read_source_members()
    members = {
        **source_members,
        "__main__.py": _launcher(),
    }
    members["manifest.json"] = _manifest(source_members)
    content = _zip_bytes(members)
    _validate_archive(content, source_members)
    return ProjectGitHookBundle(content, hashlib.sha256(content).hexdigest())
