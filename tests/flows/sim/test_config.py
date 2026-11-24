"""Simulation Project-configuration boundary tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from booley.core.boundary import BoundaryError
from booley.flows.sim import config


def _project(tmp_path: Path, toml: str) -> Path:
    (tmp_path / ".booley").mkdir()
    (tmp_path / ".booley" / "booley.toml").write_text(f"[flows.sim]\n{toml}", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("resolver", "key", "default"),
    [
        (config.resolve_sim_build_timeout_ms, "build_timeout_ms", 3_600_000),
        (config.resolve_sim_trace_convert_timeout_ms, "trace_convert_timeout_ms", 1_800_000),
    ],
)
def test_timeouts_default_accept_positive_integers_and_reject_the_rest(
    tmp_path: Path, resolver: Callable[[Path], int], key: str, default: int
) -> None:
    assert resolver(tmp_path) == default
    project = _project(tmp_path, f"{key} = 1234567\n")
    assert resolver(project) == 1_234_567

    for bad in ("true", "1.5", '"1000"', "0", "-1"):
        (project / ".booley" / "booley.toml").write_text(
            f"[flows.sim]\n{key} = {bad}\n", encoding="utf-8"
        )
        with pytest.raises(BoundaryError, match=rf"\[flows\.sim\]\.{key}"):
            resolver(project)


def test_sim_sentinels_come_from_project_config(tmp_path: Path) -> None:
    project = _project(tmp_path, 'pass_sentinels = ["PASS", 7]\nfail_sentinels = ["FAIL"]\n')
    assert config.resolve_sim_sentinels(project) == (["PASS", "7"], ["FAIL"])

    assert config.resolve_sim_sentinels(tmp_path / "empty") == ([], [])


def test_resolvers_accept_a_string_project_root(tmp_path: Path) -> None:
    """The resolvers' declared ``str`` project root reads the same configuration as a Path."""
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_text(
        '[flows.sim]\ntrace_args = ["--trace-depth=2"]\n', encoding="utf-8"
    )

    assert config.resolve_trace_args(str(tmp_path)) == ["--trace-depth=2"]
