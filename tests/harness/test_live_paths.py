"""Absolute paths into a live root are found in the spellings tools and configs use."""

import os
from pathlib import Path

import pytest

from booley.measurement.live_paths import LiveRoot, input_findings

# The runner runs only in the POSIX Sandbox; the cases spell POSIX paths.
pytestmark = pytest.mark.skipif(os.name != "posix", reason="live roots are POSIX Sandbox paths")


@pytest.mark.parametrize(
    "text",
    [
        "files: [{live}/rtl/top.v]",
        "-I{live}/include",
        "+incdir+{live}/include",
        'cd "{live}" && make',
        "cd {live}\\nmake",
        "true&&{live}/gen.sh",
        "x @{live}/args.f",
        "vlib ${SIM:-{live}/lib}",
        "vlib ${SIM-{live}/lib}",
        "cd sim\\n{live}/gen.sh",
    ],
)
def test_input_findings_name_an_absolute_live_path_in_each_spelling(
    tmp_path: Path, text: str
) -> None:
    live = (tmp_path / "live").resolve()
    live.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "fusesoc.conf").write_text(text.replace("{live}", str(live)), encoding="utf-8")

    findings = input_findings(checkout, [LiveRoot("live working tree", live)])

    assert len(findings) == 1
    assert findings[0].startswith("fusesoc.conf names ")
    assert f"inside the live working tree {live}" in findings[0]


def test_input_findings_ignore_paths_outside_every_live_root(tmp_path: Path) -> None:
    live = (tmp_path / "live").resolve()
    live.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "ip.core").write_text(
        f"files: [rtl/top.v, {tmp_path}/elsewhere/x.v, make-{live}]\n", encoding="utf-8"
    )

    assert input_findings(checkout, [LiveRoot("live working tree", live)]) == []


def test_input_findings_ignore_a_live_name_after_a_placeholder(tmp_path: Path) -> None:
    live = (tmp_path / "work").resolve()
    live.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    # ``work`` is the usual HDL library directory: after a placeholder it is relative to it,
    # so the live root's spelling following ``${BUILD}`` is not an absolute path.
    spellings = ["${BUILD}", "$(pwd)", "%OUT%", "{root}", "!x!"]
    (checkout / "ip.core").write_text(
        "".join(f"vlib {spelling}{live}\n" for spelling in spellings), encoding="utf-8"
    )

    assert input_findings(checkout, [LiveRoot("live working tree", live)]) == []


def test_input_findings_stop_a_path_at_a_nul(tmp_path: Path) -> None:
    live = (tmp_path / "live").resolve()
    live.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    # A committed binary must not crash the scan: the path ends at the NUL.
    (checkout / "ip.core").write_bytes(b"x " + str(live).encode() + b"\x00junk")

    findings = input_findings(checkout, [LiveRoot("live working tree", live)])

    assert len(findings) == 1 and findings[0].startswith("ip.core names ")
