"""Absolute paths into a live root are found in the spellings tools and configs use."""

from pathlib import Path

import pytest

from booley.measurement.live_paths import LiveRoot, input_findings


@pytest.mark.parametrize(
    "text",
    [
        "files: [{live}/rtl/top.v]",
        "-I{live}/include",
        "+incdir+{live}/include",
        "--opt={live}/x",
        "url = 'file://{live}/x'",
        '"{live}"',
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
