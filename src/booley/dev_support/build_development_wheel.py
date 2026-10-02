"""Build and publish one explicitly stamped contributor development wheel."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

from booley.runtime.build_stamp import (
    BuildProfile,
    _iter_development_context_files,
    development_context_path,
    stamp_path,
    write_build_stamp,
)
from booley.runtime.image_build_contracts import source_image_build_contracts


def _validate_checkout(checkout: Path) -> None:
    for generated in (checkout / "build", checkout / "dist"):
        if generated.is_symlink():
            raise ValueError(f"refusing generated directory symlink: {generated}")
        if generated.exists() and not generated.is_dir():
            raise ValueError(f"generated directory is not a directory: {generated}")
    for source in _iter_development_context_files(checkout):
        for parent in source.parents:
            if parent == checkout:
                break
            if parent.is_symlink():
                raise ValueError(f"checkout source directory is a symlink: {parent}")
    source_image_build_contracts(checkout)


def _built_wheel(staging: Path) -> Path:
    candidates = list(staging.glob("booley_rtl-*.whl"))
    if len(candidates) != 1 or candidates[0].is_symlink() or not candidates[0].is_file():
        raise ValueError("build must produce exactly one regular, nonsymlink booley_rtl wheel")
    return candidates[0]


def build_development_wheel(checkout: Path) -> Path:
    """Publish a fresh wheel, preserving old artifacts if its build fails."""
    checkout = checkout.resolve()
    _validate_checkout(checkout)
    build = checkout / "build"
    if build.exists():
        shutil.rmtree(build)
    dist = checkout / "dist"
    dist.mkdir(exist_ok=True)
    with (
        tempfile.TemporaryDirectory(dir=dist, prefix=".development-wheel-") as temporary,
        ExitStack() as cleanup,
    ):
        cleanup.callback(stamp_path(checkout).unlink, missing_ok=True)
        cleanup.callback(development_context_path(checkout).unlink, missing_ok=True)
        write_build_stamp(checkout, profile=BuildProfile.DEVELOPMENT_WHEEL)
        subprocess.run(
            [sys.executable, "-P", "-m", "build", "--wheel", "--outdir", temporary],
            cwd=checkout,
            check=True,
            timeout=1800,
            stdout=sys.stderr,
        )
        wheel = _built_wheel(Path(temporary))
        published = dist / wheel.name
        wheel.replace(published)
    return published.resolve()


def main(checkout: Path) -> int:
    """Print only the successful absolute artifact path on stdout."""
    try:
        wheel = build_development_wheel(checkout)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(
            f"Cannot build development wheel: {error}. Use a complete source checkout "
            "and its editable development environment with the build frontend installed.",
            file=sys.stderr,
        )
        return 1
    print(wheel)
    return 0
