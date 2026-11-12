"""``booley cheat --metrics``: the live metrics listing, in a Project only."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from booley.core.booley_dir import reset_cache
from booley.harness import booley as cli
from tests.measurement.metrics_project_support import fill_listing, write_listing_project

FALLBACK = "Run booley cheat --metrics in a Project to list its Targets' metrics."  # as rendered


@pytest.fixture(autouse=True)
def sandboxed_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the real entry point as if inside a Sandbox, without an ambient Booley Directory."""
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr(cli, "_host_install_authority_error", lambda *_: None)


def run_cli(monkeypatch: pytest.MonkeyPatch, project: Path, *arguments: str) -> int:
    """``booley -C <project> <arguments>`` through ``_dispatch_main``."""
    monkeypatch.setattr(sys, "argv", ["booley", "-C", str(project), *arguments])
    return cli._dispatch_main()


def listing_lines(output: str) -> list[str]:
    """The raw ``metrics.toml`` lines of the output: the headers, keys and comments."""
    return [
        line for line in output.splitlines() if line.startswith(("[", "# ")) or " =  #" in line
    ]


def test_cheat_metrics_prints_the_listing_in_a_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_listing_project(tmp_path)

    assert run_cli(monkeypatch, project, "cheat", "--metrics") == 0

    out = capsys.readouterr().out
    assert "\nMetrics\n" in out or out.startswith("Metrics\n")
    assert "[sim_core.sim]\ntests_failed_max =  # tests, plain\n" in out
    assert "[synth_core.synth]\n" in out
    assert FALLBACK not in out
    assert "Goal Mode" not in out  # only the requested section


def test_printed_listing_with_numbers_passes_measure_validate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_listing_project(tmp_path)
    assert run_cli(monkeypatch, project, "cheat", "--metrics") == 0
    printed = listing_lines(capsys.readouterr().out)
    headers = [line for line in printed if line.startswith("[")]
    assert len(headers) >= 8

    (project / ".booley" / "metrics.toml").write_bytes(fill_listing("\n".join(printed)).encode())
    assert run_cli(monkeypatch, project, "measure", "--validate") == 0

    out = capsys.readouterr().out
    assert f"metrics.toml is valid: {len(headers)} tables" in out


def test_goals_view_has_no_metrics_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_listing_project(tmp_path)

    assert run_cli(monkeypatch, project, "cheat", "--goals") == 0

    out = capsys.readouterr().out
    assert "[sim_core.sim]" not in out
    assert "\nMetrics\n" not in out


def test_all_without_metrics_does_not_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_listing_project(tmp_path)

    assert run_cli(monkeypatch, project, "cheat", "--all") == 0

    out = capsys.readouterr().out
    assert "[sim_core.sim]" not in out
    assert FALLBACK in " ".join(out.split())


@pytest.mark.parametrize("directory", ["plain", "source"])
def test_cheat_metrics_outside_a_project_fails_like_targets(
    directory: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    if directory == "source":
        (root / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    monkeypatch.chdir(root)
    for variable in ("RTL_PROJECT_ROOT", "BOOLEY_CONTAINER"):
        monkeypatch.delenv(variable, raising=False)

    results: list[tuple[int, str]] = []
    for command in (["targets"], ["cheat", "--metrics"]):
        monkeypatch.setattr(sys, "argv", ["booley", *command])
        code = cli.main()
        results.append((code, capsys.readouterr().err))

    assert results[0][0] == 2
    assert results[1] == results[0]


def test_unbuildable_test_plan_prints_a_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_listing_project(tmp_path)
    (project / ".booley" / "tests.toml").write_bytes(b"[sim_core\n")

    assert run_cli(monkeypatch, project, "cheat", "--metrics") == 0

    out = capsys.readouterr().out
    assert "# cycle_count (cycles, per-test): no test selector: " in out
    assert "[synth_core.synth]" in out  # the other Targets are still listed


def test_a_target_named_defaults_is_printed_by_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_listing_project(tmp_path)
    (project / "defaults.core").write_bytes(
        b"CAPI=2:\nname: ::defaults:0\ntargets:\n  defaults:\n"
        b"    flow_options: {tool: yosys}\n    toplevel: top\n"
    )

    assert run_cli(monkeypatch, project, "cheat", "--metrics") == 0

    out = capsys.readouterr().out
    assert '["::defaults:0#defaults".synth]' in out
    assert "\n[defaults." not in out
    printed = listing_lines(out)
    (project / ".booley" / "metrics.toml").write_bytes(fill_listing("\n".join(printed)).encode())
    assert run_cli(monkeypatch, project, "measure", "--validate") == 0
    assert "[defaults" not in capsys.readouterr().err


def test_catalog_failure_with_metrics_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.targets.domain import FuseSocError

    def refuse(_root: Path) -> str:
        raise FuseSocError("core files changed")

    monkeypatch.setattr("booley.measurement.metrics_listing.render_metrics_listing", refuse)
    project = write_listing_project(tmp_path)

    assert run_cli(monkeypatch, project, "cheat", "--metrics") == 2

    assert "ERROR: cannot list metrics: core files changed" in capsys.readouterr().err
