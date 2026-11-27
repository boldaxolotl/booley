"""The shared fake of the wheel build: a ``build`` package on ``PYTHONPATH``.

Production runs ``sys.executable -P -m build``; ``-P`` keeps ``PYTHONPATH``. A regular
package is needed, because it must win over the installed ``build`` distribution.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

_MAIN = """\
import json, pathlib, sys

reply = json.loads(pathlib.Path(__file__).with_name("reply.json").read_text(encoding="utf-8"))
sys.stdout.write(reply["stdout"])
sys.stderr.write(reply["stderr"])
if reply["code"] == 0:
    outdir = pathlib.Path(sys.argv[sys.argv.index("--outdir") + 1])
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "booley_rtl-0.0.0-py3-none-any.whl").write_bytes(b"")
sys.exit(reply["code"])
"""


class WheelBuild:
    """The installed fake; a successful build writes an empty wheel into ``--outdir``."""

    def __init__(self, package: Path) -> None:
        self._reply = package / "reply.json"

    def answer(self, code: int, stdout: str = "", stderr: str = "") -> None:
        reply = {"code": code, "stdout": stdout, "stderr": stderr}
        self._reply.write_bytes(json.dumps(reply).encode("utf-8"))


def install_wheel_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WheelBuild:
    package = tmp_path / "build"
    package.mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"")
    (package / "__main__.py").write_bytes(_MAIN.encode("utf-8"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path), prepend=os.pathsep)
    wheel = WheelBuild(package)
    wheel.answer(0)
    return wheel
