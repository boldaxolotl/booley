"""Tests for Specialist — model resolution, tier floors, argparse."""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from booley.harness.models import AgentCallParams
from booley.mcp.base import EXIT_FAILURE, EXIT_SUCCESS, McpToolResult
from booley.specialists.specialist import _DEFAULT_TIER_MODELS, TIER_RANK, VALID_TIERS, Specialist


def _env_with_state(state_file: Path, slug: str = "test") -> dict[str, str]:
    env = os.environ.copy()
    env["BOOLEY_SLUG"] = slug
    env["BOOLEY_STATE_FILE"] = str(state_file)
    return env


class ReviewSpecialist(Specialist):
    """Concrete Specialist for testing."""

    name = "review_test"
    description = "Test review endpoint"
    min_model = "standard"

    def _add_agent_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--diff-ref", default="HEAD~1")

    def _build_prompt(self) -> str:
        return f"Review changes since {self.args.diff_ref}"

    def _interpret_output(self, output: str, structured: dict | None) -> McpToolResult:
        if structured and structured.get("approved"):
            return McpToolResult(
                exit_code=EXIT_SUCCESS,
                criterion_key="review_rtl_bugs_done",
                criterion_met=True,
            )
        return McpToolResult(exit_code=EXIT_FAILURE, criterion_met=False)


class HeavySpecialist(Specialist):
    """Specialist with heavy floor for testing."""

    name = "heavy_test"
    description = "Test heavy-floor endpoint"
    min_model = "heavy"

    def _add_agent_args(self, parser: argparse.ArgumentParser) -> None:
        pass

    def _build_prompt(self) -> str:
        return "test"

    def _interpret_output(self, output: str, structured: dict | None) -> McpToolResult:
        return McpToolResult(exit_code=EXIT_SUCCESS)


class TestTierRank:
    def test_light_lowest(self):
        assert TIER_RANK["light"] < TIER_RANK["standard"] < TIER_RANK["heavy"]

    def test_valid_tiers_match_rank_keys(self):
        assert set(VALID_TIERS) == set(TIER_RANK.keys())


class TestDefaultTierModels:
    """The standalone fallback table, used when the harness config won't import.

    Asserted structurally rather than by model name — naming a specific model
    here just means every model launch breaks the suite. The invariant that
    matters is that the fallback mirrors the real Claude tier table.
    """

    def test_mirrors_the_claude_provider_tiers(self):
        from booley.config.agent import _PROVIDER_TIER_MODELS

        assert _PROVIDER_TIER_MODELS["claude"] == _DEFAULT_TIER_MODELS

    def test_covers_every_tier_with_a_distinct_model(self):
        assert set(_DEFAULT_TIER_MODELS) == set(VALID_TIERS)
        models = list(_DEFAULT_TIER_MODELS.values())
        assert all(m.strip() for m in models)
        assert len(set(models)) == len(models), "tiers must not collapse onto one model"


class TestSpecialistArgparse:
    def test_common_agent_args(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        env = _env_with_state(state_file)
        endpoint = ReviewSpecialist()
        with patch.dict(os.environ, env):
            args = endpoint.parse_args(
                [
                    "--model",
                    "light",
                    "--instruction",
                    "focus on timing",
                    "--diff-ref",
                    "HEAD~3",
                ]
            )
        assert args.model == "light"
        assert args.instruction == "focus on timing"
        assert args.diff_ref == "HEAD~3"

    def test_defaults(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        env = _env_with_state(state_file)
        endpoint = ReviewSpecialist()
        with patch.dict(os.environ, env):
            args = endpoint.parse_args([])
        assert args.model is None
        assert args.instruction == ""
        assert args.diff_ref == "HEAD~1"


class TestFloorEnforcement:
    def test_no_model_falls_back_to_floor(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        env = _env_with_state(state_file)
        endpoint = ReviewSpecialist()
        with patch.dict(os.environ, env):
            endpoint.parse_args([])
        assert endpoint._resolve_tier(None) == "standard"

    def test_model_above_floor_passes_through(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        env = _env_with_state(state_file)
        endpoint = ReviewSpecialist()
        with patch.dict(os.environ, env):
            endpoint.parse_args(["--model", "heavy"])
        assert endpoint._resolve_tier("heavy") == "heavy"

    def test_model_below_floor_upgrades(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        env = _env_with_state(state_file)
        endpoint = ReviewSpecialist()
        with patch.dict(os.environ, env):
            endpoint.parse_args(["--model", "light"])
        assert endpoint._resolve_tier("light") == "standard"

    def test_heavy_floor_upgrades_standard(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        env = _env_with_state(state_file)
        endpoint = HeavySpecialist()
        with patch.dict(os.environ, env):
            endpoint.parse_args(["--model", "standard"])
        assert endpoint._resolve_tier("standard") == "heavy"


class TestModelResolution:
    # These assert against _DEFAULT_TIER_MODELS rather than a model-family
    # substring: the behavior under test is which *tier* a specialist resolves
    # to, and hardcoding the model of the day made every launch break them.

    def test_standalone_heavy_resolves_heavy_tier(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        env = _env_with_state(state_file)
        endpoint = HeavySpecialist()
        with patch.dict(os.environ, env):
            endpoint.parse_args([])
        with patch.dict("sys.modules", {"booley.config.settings": None}):
            model = endpoint._resolve_model()
        assert model == _DEFAULT_TIER_MODELS["heavy"]

    def test_standard_floor_no_flag_resolves_standard_tier(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        env = _env_with_state(state_file)
        endpoint = ReviewSpecialist()
        with patch.dict(os.environ, env):
            endpoint.parse_args([])
        with patch.dict("sys.modules", {"booley.config.settings": None}):
            model = endpoint._resolve_model()
        assert model == _DEFAULT_TIER_MODELS["standard"]

    def test_explicit_heavy_resolves_heavy_tier(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        env = _env_with_state(state_file)
        endpoint = ReviewSpecialist()
        with patch.dict(os.environ, env):
            endpoint.parse_args(["--model", "heavy"])
        with patch.dict("sys.modules", {"booley.config.settings": None}):
            model = endpoint._resolve_model()
        assert model == _DEFAULT_TIER_MODELS["heavy"]


class TestTimeoutClamping:
    def test_timeout_below_min_is_clamped(self, tmp_path: Path):
        """Developer Agent passing too-low --timeout gets clamped to min_timeout."""
        state_file = tmp_path / "state.json"
        state_file.write_text("{}")
        env = _env_with_state(state_file)

        class StrictSpecialist(Specialist):
            name = "strict"
            description = "test"
            min_timeout = 1200

            def _add_agent_args(self, parser):
                pass

            def _build_prompt(self):
                return "test"

            def _interpret_output(self, output, structured):
                return McpToolResult(exit_code=EXIT_SUCCESS)

        endpoint = StrictSpecialist()
        with patch.dict(os.environ, env):
            endpoint.parse_args(["--timeout", "600"])
        assert endpoint.args.timeout == 600  # not clamped at parse time
        # Clamping happens in _run() — test via mock
        with patch.object(endpoint, "_invoke_agent", side_effect=RuntimeError("skip")):
            endpoint.read_state()
            endpoint._start_time = 0
            with contextlib.suppress(RuntimeError):
                endpoint._run()
        assert endpoint.args.timeout == 1200

    def test_timeout_above_min_unchanged(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        state_file.write_text("{}")
        env = _env_with_state(state_file)
        endpoint = ReviewSpecialist()
        with patch.dict(os.environ, env):
            endpoint.parse_args(["--timeout", "3600"])
        endpoint.read_state()
        # Just verify parse — no clamping needed
        assert endpoint.args.timeout == 3600


class TestTranscriptPath:
    def test_transcript_path_created(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        transcript_dir = tmp_path / "transcripts"
        env = _env_with_state(state_file)
        endpoint = ReviewSpecialist()
        with patch.dict(os.environ, env):
            endpoint.parse_args(["--transcript-dir", str(transcript_dir)])
        path = endpoint._transcript_path()
        assert path is not None
        assert path.name == "review_test.jsonl"
        assert transcript_dir.exists()

    def test_no_transcript_when_no_dir(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        env = _env_with_state(state_file)
        endpoint = ReviewSpecialist()
        with patch.dict(os.environ, env):
            endpoint.parse_args([])
        assert endpoint._transcript_path() is None


# ===========================================================================
# Session persistence round-trip
# ===========================================================================


class TestSessionPersistence:
    def test_roundtrip(self, tmp_path: Path):
        endpoint = ReviewSpecialist()
        endpoint._last_session_id = "sess-abc-123"
        with patch.dict(os.environ, {"BOOLEY_LOGS_DIR": str(tmp_path)}):
            endpoint._persist_session_id("test-key")
            assert endpoint._load_session_id("test-key") == "sess-abc-123"
            endpoint._clear_session_id("test-key")
            assert endpoint._load_session_id("test-key") is None
            assert not (tmp_path / "test-key.session_id").exists()

    def test_standalone_run_persists_under_project_runtime(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """SETUP-F-42: outside Ticket Mode there is no BOOLEY_LOGS_DIR, and
        returning None there silently disabled resume — every retry round
        cold-started the agent. Standalone runs land in the project runtime."""
        monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
        endpoint = ReviewSpecialist()
        endpoint._last_session_id = "sess-xyz"
        endpoint._persist_session_id("k")

        assert endpoint._load_session_id("k") == "sess-xyz"
        # The autouse _set_project_dir fixture pins BOOLEY_PROJECT_DIR=tmp_path.
        assert (tmp_path / ".runtime" / "sessions" / "k.session_id").exists()
        endpoint._clear_session_id("k")
        assert endpoint._load_session_id("k") is None

    def test_persist_noop_when_no_project_discoverable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """No ticket AND no project on disk — nowhere to persist, so None."""
        from booley.runtime.project_dir import reset_cache

        monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
        monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
        empty = tmp_path / "no_project"
        empty.mkdir()
        monkeypatch.chdir(empty)
        reset_cache()

        endpoint = ReviewSpecialist()
        endpoint._last_session_id = "sess-xyz"
        endpoint._persist_session_id("k")
        assert endpoint._session_file_path("k") is None
        assert endpoint._load_session_id("k") is None

    def test_load_returns_none_for_missing_file(self, tmp_path: Path):
        endpoint = ReviewSpecialist()
        with patch.dict(os.environ, {"BOOLEY_LOGS_DIR": str(tmp_path)}):
            assert endpoint._load_session_id("nonexistent") is None

    def test_load_returns_none_for_empty_file(self, tmp_path: Path):
        (tmp_path / "empty.session_id").write_text("", encoding="utf-8")
        endpoint = ReviewSpecialist()
        with patch.dict(os.environ, {"BOOLEY_LOGS_DIR": str(tmp_path)}):
            assert endpoint._load_session_id("empty") is None

    def test_persist_noop_when_no_session_id(self, tmp_path: Path):
        endpoint = ReviewSpecialist()
        endpoint._last_session_id = None
        with patch.dict(os.environ, {"BOOLEY_LOGS_DIR": str(tmp_path)}):
            endpoint._persist_session_id("k")
            assert not (tmp_path / "k.session_id").exists()

    def test_clear_noop_when_no_file(self, tmp_path: Path):
        endpoint = ReviewSpecialist()
        with patch.dict(os.environ, {"BOOLEY_LOGS_DIR": str(tmp_path)}):
            endpoint._clear_session_id("gone")


# ===========================================================================
# _build_resume_params
# ===========================================================================


class TestBuildResumeParams:
    def test_sets_resume_fields_without_mutating_original(self):
        original = AgentCallParams(prompt="p", model="sonnet", cwd="/tmp")
        endpoint = ReviewSpecialist()
        result = endpoint._build_resume_params(original, "sess-123")

        assert result.session_id == "sess-123"
        assert result.resume_session is True
        assert original.session_id is None
        assert original.resume_session is False

    def test_preserves_other_fields(self):
        original = AgentCallParams(
            prompt="check",
            model="opus",
            cwd="/work",
            max_turns=50,
            timeout_seconds=900,
        )
        endpoint = ReviewSpecialist()
        result = endpoint._build_resume_params(original, "sid")

        assert result.prompt == "check"
        assert result.model == "opus"
        assert result.cwd == "/work"
        assert result.max_turns == 50
        assert result.timeout_seconds == 900


# ===========================================================================
# _invoke_agent_with_resume
# ===========================================================================


class TestInvokeAgentWithResume:
    def test_non_resume_calls_directly(self):
        endpoint = ReviewSpecialist()
        mock_result = MagicMock()
        with patch.object(endpoint, "_invoke_agent", return_value=mock_result) as mock_inv:
            params = AgentCallParams(prompt="p", model="sonnet", cwd="/tmp")
            result = endpoint._invoke_agent_with_resume(params)
        assert result is mock_result
        mock_inv.assert_called_once_with(params, on_event=None)

    def test_falls_back_on_runtime_error(self):
        endpoint = ReviewSpecialist()
        endpoint.name = "test_endpoint"
        fresh_result = MagicMock()
        calls: list[AgentCallParams] = []

        def _side(p, on_event=None):
            calls.append(p)
            if len(calls) == 1:
                raise RuntimeError("stale session")
            return fresh_result

        with patch.object(endpoint, "_invoke_agent", side_effect=_side):
            params = AgentCallParams(
                prompt="p",
                model="sonnet",
                cwd="/tmp",
                session_id="old",
                resume_session=True,
            )
            result = endpoint._invoke_agent_with_resume(params)

        assert result is fresh_result
        assert len(calls) == 2
        assert calls[1].session_id is None
        assert calls[1].resume_session is False

    def test_timeout_error_propagates(self):
        endpoint = ReviewSpecialist()
        endpoint.name = "test_endpoint"
        with patch.object(endpoint, "_invoke_agent", side_effect=TimeoutError("boom")):
            params = AgentCallParams(
                prompt="p",
                model="sonnet",
                cwd="/tmp",
                session_id="s",
                resume_session=True,
            )
            with pytest.raises(TimeoutError):
                endpoint._invoke_agent_with_resume(params)


class TestCommitMsgBannedPhraseSalvage:
    """_prepare_commit_message falls back to default on banned-phrase rejection.

    Without this salvage, a multi-minute agent run that produces a stylistically
    forbidden commit subject (e.g. mentioning 'copilot') burns to exit-2 instead
    of recovering with a generic message. See Pattern A4 field report.
    """

    def _make_endpoint(self, tmp_path: Path, default_msg: str = "feat(rtl): apply changes"):
        endpoint = ReviewSpecialist()
        # Bypass argparse for the focused unit test.
        mock_args = MagicMock()
        # Use a linked-worktree-shaped path: .git as a *file*, not a directory,
        # so _prepare_commit_message does not refuse on the main-worktree guard.
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
        mock_args.work_dir = worktree
        endpoint._args = mock_args
        endpoint._default_commit_message = default_msg
        endpoint.modifies_category = "rtl"
        return endpoint

    def test_salvage_replaces_banned_phrase_subject(self, tmp_path: Path):
        endpoint = self._make_endpoint(tmp_path)
        # 'copilot' is on the default banned-phrase list.
        salvaged = endpoint._prepare_commit_message("feat(rtl): copilot wrote this")
        assert "copilot" not in salvaged.lower()
        assert salvaged.startswith("feat(")

    def test_non_banned_failure_still_raises(self, tmp_path: Path):
        endpoint = self._make_endpoint(tmp_path)
        # Mock validate_message: auto-format produces a syntactically valid
        # subject for any input, so the only way to stage a non-banned
        # failure is to inject one. The salvage path must NOT kick in here —
        # the error must surface.
        from booley.dev_support import validate_commit_msg as vcm

        with (
            patch.object(
                vcm,
                "validate_message",
                return_value=[
                    "Subject doesn't match '<type>(<scope>): <summary>'",
                ],
            ),
            pytest.raises(Specialist.GitStatusError) as exc,
        ):
            endpoint._prepare_commit_message("anything")
        assert "validation failed" in str(exc.value)

    def test_clean_message_passes_through(self, tmp_path: Path):
        endpoint = self._make_endpoint(tmp_path)
        # No banned phrases, valid conventional commit — must not be rewritten.
        ok = endpoint._prepare_commit_message("feat(rtl): add pipeline stage")
        assert ok == "feat(rtl): add pipeline stage"


class TestSpecialistGitCommit:
    """Specialist commit helper should share harness git hardening."""

    def _init_repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        (repo / "init.txt").write_text("init\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "init.txt"], cwd=repo, check=True, capture_output=True, text=True
        )
        subprocess.run(
            ["git", "commit", "-m", "feat: init"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        return repo

    def test_stale_index_lock_is_removed_and_commit_retried(self, tmp_path: Path):
        repo = self._init_repo(tmp_path)
        worktree = tmp_path / "wt"
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", str(worktree)],
            check=True,
            capture_output=True,
            text=True,
        )
        (worktree / "rtl.sv").write_text("module rtl; endmodule\n", encoding="utf-8")
        git_dir = subprocess.check_output(
            ["git", "-C", str(worktree), "rev-parse", "--git-dir"],
            text=True,
        ).strip()
        git_dir_path = Path(git_dir)
        if not git_dir_path.is_absolute():
            git_dir_path = worktree / git_dir_path
        lock_path = git_dir_path / "index.lock"
        lock_path.write_text("", encoding="utf-8")

        endpoint = ReviewSpecialist()
        endpoint._git_add_and_commit(worktree, ["rtl.sv"], "feat(rtl): recover stale lock")

        assert not lock_path.exists()
        log = subprocess.check_output(
            ["git", "-C", str(worktree), "log", "--oneline", "-1"],
            text=True,
        )
        assert "feat(rtl): recover stale lock" in log


class TestBannedPhraseNote:
    """commit_msg_banned_phrase_note() exposes the live banned list to agents."""

    def test_includes_known_default_words(self):
        note = Specialist.commit_msg_banned_phrase_note()
        # The default list ships with these — note must surface them so the
        # agent does not emit them in the first place.
        assert note  # non-empty
        assert "copilot" in note.lower()
        assert "claude" in note.lower()

    def test_note_starts_with_header(self):
        note = Specialist.commit_msg_banned_phrase_note()
        assert note.startswith("## Banned Words")
