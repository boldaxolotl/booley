"""Generated records are normalized without disturbing numeric or macro tokens."""

from types import SimpleNamespace

import pytest

from booley.flows.parameter_transport import normalize_parameter_records


def target(root, tool, value):
    return SimpleNamespace(
        build_root=root,
        configured_eda_tool=tool,
        toplevel="top",
        parameters={
            "NAME": {"datatype": "str", "paramtype": "vlogparam", "default": value},
        },
    )


@pytest.mark.parametrize(
    "tool,suffix,prefix,quote",
    [("verilator", "vc", "-GNAME=", '\\"'), ("icarus", "scr", "+parameter+top.NAME=", '"')],
)
def test_normalization_is_idempotent_and_preserves_other_records(
    tmp_path, tool, suffix, prefix, quote
):
    value = 'a" b\\c; d'
    path = tmp_path / f"top.{suffix}"
    numeric = "-GWIDTH=12\n+define+MACRO=raw token\nsource.sv\n"
    path.write_bytes((numeric + prefix + quote + value + quote + "\n").encode("utf-8"))
    resolved = target(tmp_path, tool, value)
    normalize_parameter_records(resolved)
    normalized = path.read_bytes()
    assert normalized.startswith(numeric.encode())
    if tool == "icarus":
        assert b"\\040" in normalized
    else:
        assert value.encode().hex().encode() in normalized
    normalize_parameter_records(resolved)
    assert path.read_bytes() == normalized


@pytest.mark.parametrize("tool", ["yosys", "vivado", "verible", "vcs", "xcelium", None])
def test_other_backends_need_no_simulator_records(tmp_path, tool):
    normalize_parameter_records(target(tmp_path, tool, "MAC"))
    assert not list(tmp_path.iterdir())


def test_missing_record_is_not_silent(tmp_path):
    with pytest.raises(ValueError, match="missing or ambiguous"):
        normalize_parameter_records(target(tmp_path, "verilator", "MAC"))


@pytest.mark.parametrize(
    "tool,suffix,prefix,quote",
    [("verilator", "vc", "-GNAME=", '\\"'), ("icarus", "scr", "+parameter+top.NAME=", '"')],
)
@pytest.mark.parametrize("value", ["", "a\r\nb\t", "é漢", '"MAC"'])
def test_exact_utf8_controls_and_empty_idempotence(tmp_path, tool, suffix, prefix, quote, value):
    path = tmp_path / f"top.{suffix}"
    untouched = b"-GWIDTH=12\r\n+define+RAW=token\r\n"
    path.write_bytes(untouched + (prefix + quote + value + quote + "\r\n").encode())
    normalize_parameter_records(target(tmp_path, tool, value))
    after = path.read_bytes()
    assert after.startswith(untouched)
    normalize_parameter_records(target(tmp_path, tool, value))
    assert path.read_bytes() == after
    if tool == "verilator" and not value:
        assert after == untouched + b'-GNAME=\\"\\"\r\n'


def test_only_complete_assignment_records_are_changed(tmp_path):
    path = tmp_path / "top.vc"
    comment = '# unrelated -GNAME=\\"MAC\\" suffix\n'
    path.write_text(comment + '-GNAME=\\"MAC\\"\n')
    normalize_parameter_records(target(tmp_path, "verilator", "MAC"))
    assert path.read_text().startswith(comment)


def test_failed_publication_preserves_original_bytes_and_retry(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / "top.vc"
    original = b'-GNAME=\\"MAC\\"\r\n'
    path.write_bytes(original)
    path.chmod(0o640)
    original_mode = path.stat().st_mode
    resolved = target(tmp_path, "verilator", "MAC")
    replace = Path.replace

    def fail_replace(self, destination):
        raise OSError("interrupted publication")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_replace)
        with pytest.raises(OSError, match="interrupted publication"):
            normalize_parameter_records(resolved)
    assert path.read_bytes() == original
    assert path.stat().st_mode == original_mode
    assert list(tmp_path.iterdir()) == [path]
    assert Path.replace is replace
    normalize_parameter_records(resolved)
    assert b"24'h4d4143" in path.read_bytes()
    assert path.stat().st_mode == original_mode


def test_only_edam_named_generated_file_is_inspected(tmp_path):
    path = tmp_path / "configured.vc"
    path.write_text('-GNAME=\\"MAC\\"\n')
    (tmp_path / "user.vc").write_bytes(b"\xff")
    nested = tmp_path / "staged"
    nested.mkdir()
    (nested / "duplicate.vc").write_bytes(path.read_bytes())
    (tmp_path / "unrelated.vc").symlink_to("/dev/null")
    resolved = target(tmp_path, "verilator", "MAC")
    resolved.configuration_name = "configured"
    normalize_parameter_records(resolved)
    assert b"24'h4d4143" in path.read_bytes()
    assert (tmp_path / "user.vc").read_bytes() == b"\xff"
    assert (nested / "duplicate.vc").read_text() == '-GNAME=\\"MAC\\"\n'
