"""Synchronize canonical public documents into installed package data.

``docs/user/TROUBLESHOOTING.md`` is the only authored source. The packaged copy
exists because installed runtimes cannot rely on a repository checkout.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_SOURCE = _REPOSITORY_ROOT / "docs" / "user" / "TROUBLESHOOTING.md"
_PACKAGED = _REPOSITORY_ROOT / "src" / "booley" / "data" / "refs" / "TROUBLESHOOTING.md"


def _atomic_write(path: Path, content: bytes) -> None:
    """Publish one generated document without exposing a partial file."""
    temporary: Path | None = None
    try:
        mode = path.stat().st_mode if path.exists() else 0o644
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        temporary.replace(path)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def troubleshooting_mirror_is_current() -> bool:
    """Return whether the generated package copy matches its canonical source."""
    try:
        return _PACKAGED.read_bytes() == _SOURCE.read_bytes()
    except OSError:
        return False


def sync_troubleshooting_mirror() -> bool:
    """Regenerate the package copy; return whether it changed."""
    content = _SOURCE.read_bytes()
    if _PACKAGED.exists() and _PACKAGED.read_bytes() == content:
        return False
    _atomic_write(_PACKAGED, content)
    return True


def main(argv: list[str] | None = None) -> int:
    """Synchronize the mirror, or check it without writing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail when the mirror is stale")
    args = parser.parse_args(argv)
    if args.check:
        if troubleshooting_mirror_is_current():
            return 0
        print(
            "packaged TROUBLESHOOTING.md is stale; run python -m booley.dev_support.reference_docs"
        )
        return 1
    changed = sync_troubleshooting_mirror()
    print("updated packaged TROUBLESHOOTING.md" if changed else "packaged guide is current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
