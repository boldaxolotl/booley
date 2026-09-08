"""Triage lists build outputs without launching their content in a diff viewer."""

from types import SimpleNamespace

import pytest

from booley.review.triage_package import open_package_diffs


@pytest.mark.parametrize("editor_available", [True, False])
def test_triage_only_opens_source_diffs(monkeypatch, editor_available):
    rows = [
        {
            "path": path,
            "status": "M",
            "presentation": presentation,
            "diff_left": f"/base/{path}",
            "diff_right": f"/head/{path}",
        }
        for path, presentation in [
            ("firmware/main.c", "text"),
            ("rtl/core.sv", "text"),
            ("firmware/image.hex", "text"),
            ("rom/init.mem", "text"),
            ("sw/firmware.vmem", "text"),
            ("vendor/fw/image.ELF", "text"),
            ("build/core.o", "text"),
            ("fpga/top.bit", "binary"),
            ("data/opaque", "binary"),
        ]
    ]
    rows.append({**rows[2], "path": "renamed-image", "old_path": "image.hex"})
    launched = []
    monkeypatch.setattr(
        "booley.config.editor.resolve_editor",
        lambda: (
            SimpleNamespace(diff=("viewer", "{left}", "{right}")) if editor_available else None
        ),
    )
    monkeypatch.setattr(
        "booley.review.triage_package.subprocess.run",
        lambda argv, **kw: launched.append(argv) or SimpleNamespace(returncode=0),
    )
    failures = open_package_diffs({"changed_files": rows})
    assert launched == (
        [
            ["viewer", "/base/firmware/main.c", "/head/firmware/main.c"],
            ["viewer", "/base/rtl/core.sv", "/head/rtl/core.sv"],
        ]
        if editor_available
        else []
    )
    assert failures == ([] if editor_available else ["firmware/main.c", "rtl/core.sv"])


def test_briefing_lists_omitted_artifact_without_claiming_diff_opened():
    from booley.review.triage_package import _render_changes

    lines = []
    _render_changes(
        lines,
        {
            "changed_files": [
                {"path": "firmware/image.hex", "status": "M", "diff_left": "/base/image.hex"},
            ]
        },
        set(),
    )
    assert "firmware/image.hex" in "\n".join(lines)
    assert "diff omitted (compiled artifact)" in "\n".join(lines)
    assert "diff opened" not in "\n".join(lines)
