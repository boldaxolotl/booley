from __future__ import annotations

import re
import socket
from pathlib import Path

import pytest

from booley.runtime import incontainer_live_preview as ilp

_FIRST_PORT = 49152  # the IANA dynamic range Live Preview ports are drawn from


def _occupy_a_port() -> socket.socket:
    """A listening loopback socket on the first free port from 50000 up."""
    for port in range(50000, 50200):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            sock.close()
            continue
        sock.listen()
        return sock
    pytest.skip("no free port to occupy")


def test_choose_port_retries_occupied_pair():
    with _occupy_a_port() as occupied:
        port = occupied.getsockname()[1]
        draws = iter([port - _FIRST_PORT, *range(port - _FIRST_PORT + 10, 16000, 10)])

        chosen = ilp.choose_port(lambda _width: next(draws))

    assert chosen is not None
    assert chosen >= port + 10


def test_patch_settings_preserves_surrounding_jsonc(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        '{\n  // retained\n  "livePreview.portNumber": 61000,\n  "other": true\n}\n',
        encoding="utf-8",
    )

    assert ilp.patch_settings(path, 54321) is True
    assert path.read_text(encoding="utf-8") == (
        '{\n  // retained\n  "livePreview.portNumber": 54321,\n  "other": true\n}\n'
    )


def test_patch_settings_rejects_missing_or_duplicate_key(tmp_path):
    path = tmp_path / "settings.json"
    original = '{"other": true}\n'
    path.write_text(original, encoding="utf-8")
    assert ilp.patch_settings(path, 54321) is False
    assert path.read_text(encoding="utf-8") == original

    duplicate = '{"livePreview.portNumber": 1, "livePreview.portNumber": 2}\n'
    path.write_text(duplicate, encoding="utf-8")
    assert ilp.patch_settings(path, 54321) is False
    assert path.read_text(encoding="utf-8") == duplicate


def _seed(home: Path, root_name: str, port: int = 1) -> Path:
    """Write a seeded Machine settings file under ``home/<root_name>``.

    Port 1 is outside the drawn range, so a file left alone shows.
    """
    path = home / root_name / "data" / "Machine" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{{"livePreview.portNumber": {port}}}\n', encoding="utf-8")
    return path


def _port_of(path: Path) -> int:
    match = re.search(r'"livePreview\.portNumber"\s*:\s*(\d+)', path.read_text(encoding="utf-8"))
    assert match
    return int(match.group(1))


@pytest.mark.parametrize(
    "root", [".vscode-server", ".vscode-remote"], ids=["server", "codespaces"]
)
def test_main_updates_seeded_machine_setting(tmp_path, monkeypatch, capsys, root):
    path = _seed(tmp_path, root)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert ilp.main() == 0

    port = _port_of(path)
    assert _FIRST_PORT <= port <= 65534
    assert f"{port}/{port + 1}" in capsys.readouterr().out


def test_main_updates_every_seeded_root(tmp_path, monkeypatch):
    """The wait ends only once every existing root's settings are written."""
    first = _seed(tmp_path, ".vscode-server")
    late = tmp_path / ".vscode-remote" / "data" / "Machine" / "settings.json"
    late.parent.mkdir(parents=True)
    ports = iter((50001, 50002))
    monkeypatch.setenv("HOME", str(tmp_path))
    # Two random draws may collide, and each root must get its own draw.
    monkeypatch.setattr(ilp, "choose_port", lambda: next(ports))
    ticks = {"now": 0.0}

    def sleep(seconds: float) -> None:
        ticks["now"] += seconds
        _seed(tmp_path, ".vscode-remote")  # the second poll's worth of devcontainer output

    assert ilp.main(sleep=sleep, clock=lambda: ticks["now"]) == 0
    # Roots are patched in discovery order, each with its own fresh port.
    assert [_port_of(path) if path.exists() else None for path in (first, late)] == [50001, 50002]


def test_main_falls_back_to_default_root(tmp_path, monkeypatch):
    """With no server root at start, the default root's settings are awaited."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ticks = {"now": 0.0}
    seeded: list[Path] = []

    def sleep(seconds: float) -> None:
        ticks["now"] += seconds
        seeded.append(_seed(tmp_path, ".vscode-server"))

    assert ilp.main(sleep=sleep, clock=lambda: ticks["now"]) == 0
    assert _port_of(seeded[0]) >= _FIRST_PORT


def test_main_missing_setting_is_nonfatal(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    ticks = iter((0.0, 6.0))

    assert ilp.main(sleep=lambda _seconds: None, clock=lambda: next(ticks)) == 0
    assert "not found" in capsys.readouterr().out


def test_module_has_no_orphan_temp_file_after_failed_replace(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{"livePreview.portNumber": 61000}\n', encoding="utf-8")

    original_replace = Path.replace

    def fail_replace(self, target):
        if self.name.startswith(".settings.json.booley-"):
            raise OSError
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    assert ilp.patch_settings(path, 54321) is False
    assert list(Path(tmp_path).glob(".settings.json.booley-*")) == []
