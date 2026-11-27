"""Shared prerequisite and fake of the native B-Wave binary for B-Wave tests."""

import argparse
import json
import os
import sys
from pathlib import Path

import pytest

from booley.dev_support.test_profiles import BWAVE_BUILD, native_test_binary
from tests.path_tool_fake import PathToolFake


def require_native_binary() -> Path:
    """Select the current checkout's debug build or explain its absence."""
    binary = native_test_binary(Path(__file__).resolve().parents[2])
    if not binary.is_file():
        pytest.skip(f"native B-Wave debug binary not built; run {' '.join(BWAVE_BUILD)}")
    return binary


def require_exact_binary(binary: Path) -> Path:
    """Fail without resolving another binary or triggering a Cargo build."""
    if not binary.is_file():
        raise FileNotFoundError(f"native B-Wave debug binary is missing: {binary}")
    return binary


def fake_native_binary(directory: Path, monkeypatch: pytest.MonkeyPatch) -> PathToolFake:
    """Answer as the native B-Wave binary that BOOLEY_BWAVE_BIN names, with no patch.

    The resolver skips a file whose first line names Python (that is the bwave
    wrapper), so on POSIX a /bin/sh launcher runs the PathToolFake script.
    """
    directory.mkdir(parents=True, exist_ok=True)
    fake = PathToolFake(directory, "bwave")
    launcher = fake.executable
    if os.name != "nt":
        launcher = directory / "bwave-native"
        script = f'#!/bin/sh\nexec "{sys.executable}" "{fake.executable}" "$@"\n'
        launcher.write_bytes(script.encode("utf-8"))
        launcher.chmod(0o755)
    monkeypatch.setenv("BOOLEY_BWAVE_BIN", str(launcher))
    return fake


_VCD_BUILD = """\
if "$enddefinitions" not in text:
    stderr, returncode, output = "fake bwave: no VCD header\\n", 1, None
else:
    if not text.endswith("\\n"):
        offset = text.rfind("\\n") + 1
        stderr += "WARNING: input does not end with a newline; the final line "
        stderr += f"(byte {offset}) may be a truncated record\\n"
    stderr += f"# wrote {args[args.index('-o') + 1]}\\n"
"""


def answer_vcd_build(fake: PathToolFake) -> PathToolFake:
    """Convert a piped VCD into a minimal store and list it; refuse headerless input."""
    # Imported here: ``main`` runs this module as a child CLI without the test conftest.
    from tests.conftest import MINIMAL_FST_BYTES

    fake.answer("build", reads_input=True, output=MINIMAL_FST_BYTES, script=_VCD_BUILD)
    hierarchy = {"scope_prefix": "tb", "root_scopes": ["tb"], "signal_count": 1, "total_ticks": 1}
    fake.answer("list", stdout=json.dumps({"data": hierarchy}))
    return fake


def main() -> None:
    """Run a child CLI with a strict, test-only native resolver."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("binary", type=Path)
    args, remaining = parser.parse_known_args()
    sys.argv[1:] = remaining

    from booley.bwave import cli
    from booley.runtime import paths

    def resolve() -> Path:
        return require_exact_binary(args.binary)

    paths.native_bwave_binary = resolve
    cli.native_bwave_binary = resolve
    try:
        cli.main()
    except FileNotFoundError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
