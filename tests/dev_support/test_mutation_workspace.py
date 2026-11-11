"""Tests for the Mutation Tester's scratch paths and scope hashes."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.dev_support import mutation_workspace as ws


class TestScopeHashing:
    def test_round_trip(self, tmp_path: Path):
        source = tmp_path / "a.sv"
        source.write_text("module a; endmodule\n", encoding="utf-8")
        hashes = ws.compute_scope_hashes(["a.sv"], tmp_path)
        assert hashes["a.sv"].startswith("sha256:")

    def test_missing_file_marker(self, tmp_path: Path):
        hashes = ws.compute_scope_hashes(["nope.sv"], tmp_path)
        assert hashes["nope.sv"] == "sha256:MISSING"

    def test_edit_changes_hash(self, tmp_path: Path):
        source = tmp_path / "a.sv"
        source.write_text("v1\n", encoding="utf-8")
        first = ws.compute_scope_hashes(["a.sv"], tmp_path)["a.sv"]
        source.write_text("v2\n", encoding="utf-8")
        second = ws.compute_scope_hashes(["a.sv"], tmp_path)["a.sv"]
        assert first != second


class TestDirectoryResolution:
    def test_runtime_dir_env_wins_over_logs_dir_env(self, tmp_path: Path, monkeypatch):
        runtime = tmp_path / "runtime"
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(runtime))
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
        assert ws.mutation_tester_dir() == runtime / "mutation_tester"
        assert ws.workspace_dir() == runtime / "mutation_tester" / "campaign"

    def test_logs_dir_env_uses_dot_runtime(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("BOOLEY_RUNTIME_DIR", raising=False)
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        assert ws.workspace_dir() == (tmp_path / ".runtime" / "mutation_tester" / "campaign")

    def test_explicit_logs_dir_wins_over_env(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
        explicit = tmp_path / "explicit"
        assert ws.workspace_dir(explicit) == (explicit / "mutation_tester" / "campaign")

    def test_helpers_live_under_workspace(self, tmp_path: Path):
        root = ws.workspace_dir(tmp_path)
        assert ws.baseline_build_dir(tmp_path) == root / "builds" / "baseline"
        assert ws.variant_build_dir(3, tmp_path) == root / "builds" / "mutant_3"
        assert ws.variants_dir(tmp_path) == root / "variants"
        assert ws.mutant_logs_dir(tmp_path) == root / "mutant_logs"
        assert ws.baseline_log_path(tmp_path) == root / "baseline.log"

    def test_legacy_lock_dir(self, tmp_path: Path):
        assert ws.legacy_lock_dir(tmp_path) == (tmp_path / "mutation_tester" / "lock")

    def test_variant_build_dir_rejects_zero_index(self, tmp_path: Path):
        with pytest.raises(ValueError):
            ws.variant_build_dir(0, tmp_path)
