"""The stray-VCD hint in the no-trace error keeps a dump's file name literal."""

import pytest

from booley.bwave import cli as bwave


def test_dollar_in_dump_name_is_single_quoted(tmp_path):
    dump = tmp_path / "sim$corner.vcd"
    dump.write_bytes(b"")

    with pytest.raises(SystemExit) as exc:
        bwave._resolve_gui_target(str(tmp_path))

    message = str(exc.value)
    assert f"  bwave register '{dump}' --as ALIAS --build" in message
    assert f"  bwave gui '{dump}'" in message
