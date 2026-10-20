"""Build the Project Git-hook zip application."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

BUNDLE_NAME = "project-git-hooks.pyz"
BUNDLE_SCHEMA_VERSION = 1

_SOURCE_INVENTORY = (
    ("boundary.py", Path("core") / "boundary.py"),
    ("checkout_role.py", Path("core") / "checkout_role.py"),
    ("run_command.py", Path("core") / "run_command.py"),
    ("booley_commit_policy.py", Path("commit_policy") / "policy.py"),
    ("booley_commit_validation.py", Path("commit_policy") / "validation.py"),
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
        with source.open("r", encoding="utf-8", newline="") as handle:
            content = handle.read()
    except OSError as exc:
        raise FileNotFoundError(f"canonical hook source is unavailable: {source}") from exc
    return content.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _launcher() -> bytes:
    """Return the zip application's command dispatcher."""
    return b"""from __future__ import annotations

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
        os.environ.setdefault("BOOLEY_DIR", str(bundle.parent.parent))
    del sys.argv[1]
    module = importlib.import_module(module_name)
    raise SystemExit(module.main())


if __name__ == "__main__":
    main()
"""


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
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, members[name], compresslevel=9)
    return output.getvalue()


def build_project_git_hook_bundle() -> ProjectGitHookBundle:
    """Build deterministic Project Git-hook bytes from canonical package sources."""
    root = _source_package_root()
    source_members = {
        name: _normalized_source(root / relative) for name, relative in _SOURCE_INVENTORY
    }
    members = {**source_members, "__main__.py": _launcher()}
    members["manifest.json"] = _manifest(source_members)
    content = _zip_bytes(members)
    return ProjectGitHookBundle(content, hashlib.sha256(content).hexdigest())
