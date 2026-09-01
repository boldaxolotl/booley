"""Tests for the mutation proposal lock's live schema-2 behavior."""

from __future__ import annotations

import json
from pathlib import Path

from booley.dev_support import mutation_lock as lm


class TestScopeHashing:
    def test_round_trip(self, tmp_path: Path):
        source = tmp_path / "a.sv"
        source.write_text("module a; endmodule\n", encoding="utf-8")
        hashes = lm.compute_scope_hashes(["a.sv"], tmp_path)
        assert hashes["a.sv"].startswith("sha256:")

    def test_missing_file_marker(self, tmp_path: Path):
        hashes = lm.compute_scope_hashes(["nope.sv"], tmp_path)
        assert hashes["nope.sv"] == "sha256:MISSING"

    def test_edit_changes_hash(self, tmp_path: Path):
        source = tmp_path / "a.sv"
        source.write_text("v1\n", encoding="utf-8")
        first = lm.compute_scope_hashes(["a.sv"], tmp_path)["a.sv"]
        source.write_text("v2\n", encoding="utf-8")
        second = lm.compute_scope_hashes(["a.sv"], tmp_path)["a.sv"]
        assert first != second


class TestLockPersistence:
    def test_round_trip(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        meta = lm.LockMeta(
            schema_version=lm.LOCK_SCHEMA_VERSION,
            created_at=lm.now_iso(),
            scope=["rtl/a.sv"],
            scope_hashes={"rtl/a.sv": "sha256:abc"},
            count=3,
            mutations=[{"index": 1, "category": "x"}],
        )
        lm.save_lock(meta)

        loaded = lm.load_lock()
        assert loaded is not None
        assert loaded.scope == ["rtl/a.sv"]
        assert loaded.count == 3
        assert loaded.mutations[0]["index"] == 1
        persisted = json.loads(lm.lock_json_path().read_text(encoding="utf-8"))
        assert "host_file" not in persisted
        assert "muxed_files" not in persisted
        assert "pkg_file" not in persisted
        assert "docker_digest" not in persisted

    def test_corrupt_lock_treated_as_missing(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        lm.lock_dir().mkdir(parents=True, exist_ok=True)
        lm.lock_json_path().write_text("not json", encoding="utf-8")
        assert lm.load_lock() is None

    def test_missing_lock_is_none(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        assert lm.load_lock() is None

    def test_malformed_count_treated_as_missing(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        lm.lock_dir().mkdir(parents=True, exist_ok=True)
        lm.lock_json_path().write_text('{"count": "ten"}', encoding="utf-8")
        assert lm.load_lock() is None

    def test_malformed_scope_type_treated_as_missing(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        lm.lock_dir().mkdir(parents=True, exist_ok=True)
        lm.lock_json_path().write_text('{"scope": 5}', encoding="utf-8")
        assert lm.load_lock() is None


class TestLockValidity:
    def _meta(self, scope: list[str], hashes: dict[str, str]) -> lm.LockMeta:
        return lm.LockMeta(
            schema_version=lm.LOCK_SCHEMA_VERSION,
            scope=list(scope),
            scope_hashes=hashes,
            count=1,
        )

    def test_match(self):
        hashes = {"a.sv": "sha256:x"}
        meta = self._meta(["a.sv"], hashes)
        assert lm.is_lock_valid(meta, ["a.sv"], hashes) is True

    def test_scope_mismatch(self):
        hashes = {"a.sv": "sha256:x"}
        meta = self._meta(["a.sv"], hashes)
        assert lm.is_lock_valid(meta, ["b.sv"], {"b.sv": "sha256:x"}) is False

    def test_hash_mismatch(self):
        meta = self._meta(["a.sv"], {"a.sv": "sha256:old"})
        assert lm.is_lock_valid(meta, ["a.sv"], {"a.sv": "sha256:new"}) is False

    def test_tool_version_mismatch(self):
        meta = self._meta(["a.sv"], {"a.sv": "sha256:x"})
        meta.schema_version = "0.0"
        assert lm.is_lock_valid(meta, ["a.sv"], {"a.sv": "sha256:x"}) is False

    def test_runtime_mux_lock_is_invalidated(self):
        meta = self._meta(["a.sv"], {"a.sv": "sha256:x"})
        meta.schema_version = "1.4"
        assert lm.is_lock_valid(meta, ["a.sv"], {"a.sv": "sha256:x"}) is False


def test_wipe_is_idempotent(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
    lm.wipe_lock()
    lm.lock_dir().mkdir(parents=True, exist_ok=True)
    (lm.lock_dir() / "stray.txt").write_text("x", encoding="utf-8")
    lm.wipe_lock()
    assert not lm.lock_dir().exists()
