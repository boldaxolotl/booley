from __future__ import annotations

import re
from pathlib import Path

from booley import incontainer_live_preview as ilp


def test_choose_port_retries_occupied_pair(monkeypatch):
    candidates = iter((50000, 50002))
    monkeypatch.setattr(ilp, "_MIN_PORT", 0)
    monkeypatch.setattr(ilp, "_MAX_START_PORT", 0)
    monkeypatch.setattr(ilp, "_port_pair_is_free", lambda port: port == 50002)

    assert ilp.choose_port(lambda _width: next(candidates)) == 50002


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


def test_main_updates_seeded_machine_setting(tmp_path, monkeypatch, capsys):
    home = tmp_path / "agent"
    path = home / ilp._SETTINGS_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text('{"livePreview.portNumber": 61000}\n', encoding="utf-8")
    monkeypatch.setattr(ilp, "_agent_home", lambda: home)
    monkeypatch.setattr(ilp, "choose_port", lambda: 54321)

    assert ilp.main() == 0
    assert re.search(r'"livePreview\.portNumber"\s*:\s*54321', path.read_text())
    assert "54321/54322" in capsys.readouterr().out


def test_main_missing_setting_is_nonfatal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ilp, "_agent_home", lambda: tmp_path)
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
