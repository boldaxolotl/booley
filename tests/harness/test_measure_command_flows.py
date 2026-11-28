"""``booley measure <sha>`` runs real child Flows in an exported checkout and stores the run (#3953).

The Project is a committed repository whose Targets are named for the stub Flow's behaviors
(``tests/harness/test_checkout_flow.py``): a target starting with ``metrics`` reports one typed
metric (``marker`` = 7), ``crash`` raises. The command runs through the real entry point; the
children are real processes. The command's refusals and cancellation, which need no process, are
in ``test_measure_cli.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from booley.harness import booley as cli
from booley.harness.standalone_job import standalone_jobs_dir, standalone_log_root
from booley.measurement import metrics_file
from booley.measurement.run_key import resolve_measurement_key
from booley.measurement.run_store import MeasurementStore
from booley.runtime import job_records
from tests.harness.test_checkout_flow import (
    FLOW,
    World,
    WorldBuilder,
    git,
    linux_only,
    make_world,
    use_fusesoc,
)
from tests.harness.test_measure_children_flows import (
    declare_stub_metrics,
    started_targets,
)
from tests.time_budget import hang_guard_s

__all__ = ["make_world"]

# A Target whose name tells the stub Flow what to do; each is a one-file FuseSoC core.
CORE = """CAPI=2:
name: acme:ip:{name}:0.1
filesets:
  rtl:
    files: [top.v]
    file_type: verilogSource
targets:
  {name}:
    filesets: [rtl]
    toplevel: top
    default_tool: icarus
"""
TARGETS = ("metrics_ok", "metrics_b", "crash", "linger")
# No queue limit: every table starts at once and admission orders them.
ONE_AT_A_TIME = ""


def metrics_project(
    make_world: WorldBuilder,
    metrics: str,
    config: str = ONE_AT_A_TIME,
    symlinks: dict[str, str] | None = None,
) -> World:
    """A committed Project with the Targets above and *metrics* as its ``metrics.toml``."""
    files = {f"{name}.core": CORE.format(name=name) for name in TARGETS}
    files["top.v"] = "module top; endmodule\n"
    files[".booley/metrics.toml"] = metrics
    return make_world(files=files, symlinks=symlinks, config=config)


LINKED_HOOK = {".booley/hooks/post-setup.sh": "{live}/hook.sh"}


@pytest.fixture(autouse=True)
def _stub_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stub Flow gets a metric table, and FuseSoC resolves the cores for its preflight."""
    use_fusesoc(monkeypatch)
    declare_stub_metrics(monkeypatch)
    monkeypatch.setattr(metrics_file, "TABLE_FLOWS", (*metrics_file.TABLE_FLOWS, FLOW))


def measure(monkeypatch: pytest.MonkeyPatch, world: World, *arguments: str) -> int:
    """Run ``booley -C <live Project> measure <arguments>`` in a Sandbox."""
    monkeypatch.setattr(sys, "argv", ["booley", "-C", str(world.live), "measure", *arguments])
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # main() quiets a Ctrl-C's traceback
    return cli.main()


def store_of(world: World) -> MeasurementStore:
    return MeasurementStore(world.main)


def head_key(world: World):
    return resolve_measurement_key(world.live, git(world.live, "rev-parse", "HEAD")).key


def stored_documents(world: World) -> list[dict[str, object]]:
    paths = sorted((world.main / "measurements").glob("*/*.json"))
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths]


def measure_records(world: World) -> list[job_records.JobRecord]:
    records = job_records.list_records(standalone_jobs_dir(world.main), skip_malformed=True)
    return [record for record in records if record.endpoint == "measure"]


@linux_only
def test_a_met_commit_exits_zero_stores_one_run_and_is_measured(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = metrics_project(make_world, "[metrics_ok.checkout_stub]\nmarker_max = 10\n")

    assert measure(monkeypatch, world) == 0

    (record,) = measure_records(world)
    assert (record.status, record.exit_code) == ("done", 0)
    key = head_key(world)
    (stored,) = store_of(world).history(key)
    assert stored.run_id == record.run_id
    assert store_of(world).measured(key) == stored
    assert stored.path.with_suffix(".md").is_file()  # the Markdown report
    captured = capsys.readouterr()
    assert "Measurement Report" in captured.out
    assert "post-setup" not in captured.err  # no hook, nothing runs or is logged
    assert not list(standalone_log_root(world.main).rglob("post-setup.log"))
    # Each child's text reports are stored beside the document.
    (kept,) = [d["reports"] for d in stored_documents(world)]
    assert any(report["name"] == "report.json" for report in kept)  # type: ignore[union-attr]
    for report in kept:  # type: ignore[union-attr]
        assert (stored.path.parent / report["path"]).is_file()


@linux_only
def test_a_hook_built_input_is_built_in_the_checkout_and_measured(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook builds an ignored file in the checkout, in the measurement environment."""
    hook = (
        "! git --version >/dev/null 2>&1 && "
        '[ "$BOOLEY_PROJECT_ROOT" = "$(pwd -P)" ] && printf "module gen; endmodule\\n" > gen.v\n'
    )
    world = make_world(
        files={
            "metrics_gen.core": CORE.format(name="metrics_gen").replace(
                "[top.v]", "[top.v, gen.v]"
            ),
            "top.v": "module top; endmodule\n",
            ".gitignore": "gen.v\n",
            ".booley/hooks/post-setup.sh": hook,
            ".booley/metrics.toml": "[metrics_gen.checkout_stub]\nmarker_max = 10\n",
        }
    )

    assert measure(monkeypatch, world) == 0

    assert len(stored_documents(world)) == 1
    assert not (world.live / "gen.v").exists()


@linux_only
def test_a_hook_built_core_defines_a_target_the_rules_name(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook runs before the rules are read, so a core it writes can hold a Target."""
    core = CORE.format(name="metrics_gen").replace("\n", "\\n")
    hook = f'printf "{core}" > metrics_gen.core\n'
    world = make_world(
        files={
            "top.v": "module top; endmodule\n",
            ".booley/hooks/post-setup.sh": hook,
            ".booley/metrics.toml": "[metrics_gen.checkout_stub]\nmarker_max = 10\n",
        }
    )

    assert measure(monkeypatch, world) == 0

    assert len(stored_documents(world)) == 1


@linux_only
def test_a_post_setup_hook_linked_into_the_live_tree_never_runs(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = metrics_project(
        make_world, "[metrics_ok.checkout_stub]\nmarker_max = 10\n", symlinks=LINKED_HOOK
    )
    (world.live / "hook.sh").write_bytes(f"touch {world.ctl / 'hook-ran'}\n".encode())

    assert measure(monkeypatch, world) == 2

    assert "links to" in capsys.readouterr().err
    assert not (world.ctl / "hook-ran").exists()


@linux_only
def test_a_measured_commit_is_shown_and_again_measures_it(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = metrics_project(make_world, "[metrics_ok.checkout_stub]\nmarker_max = 10\n")
    assert measure(monkeypatch, world) == 0

    assert measure(monkeypatch, world) == 0  # shown: no Flow starts, nothing is stored

    assert len(stored_documents(world)) == 1
    assert len(measure_records(world)) == 2
    assert started_targets(world) == ["metrics_ok"]

    assert measure(monkeypatch, world, "--again") == 0

    assert len(stored_documents(world)) == 2
    assert len(store_of(world).history(head_key(world))) == 2
    assert started_targets(world) == ["metrics_ok", "metrics_ok"]


@linux_only
@pytest.mark.parametrize("all_tables", [False, True], ids=["stop", "all"])
def test_an_unmet_bound_stops_later_tables_unless_all(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, all_tables: bool
) -> None:
    world = metrics_project(
        make_world,
        "[metrics_ok.checkout_stub]\nmarker_max = 5\n"
        '[metrics_b.checkout_stub]\ntrack = ["marker"]\n',
    )

    assert measure(monkeypatch, world, *(["--all"] if all_tables else [])) == 1

    assert len(stored_documents(world)) == 1
    (stored,) = store_of(world).history(head_key(world))
    tables = {t["identity"].split("#")[1]: t for t in stored.document["tables"]}
    assert [r["verdict"] for r in tables["metrics_ok"]["results"]] == ["unmet"]
    assert not stored.measured
    # Both children start at once and race for the one live memory claim, so which runs first is
    # not fixed; the table whose child never ran is the one the unmet bound withdrew.
    ran_b = "metrics_b" in started_targets(world)
    assert "metrics_ok" in started_targets(world)
    if all_tables or ran_b:
        assert [r["verdict"] for r in tables["metrics_b"]["results"]] == ["tracked"]
        assert stored.status == "completed"
    else:
        assert [r["verdict"] for r in tables["metrics_b"]["results"]] == ["not_evaluated"]
        assert stored.status == "stopped"
    if all_tables:
        assert ran_b


@linux_only
def test_a_crashing_flow_gives_error_verdicts_with_an_excerpt_and_exits_two(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = metrics_project(make_world, "[crash.checkout_stub]\nmarker_max = 10\n")

    assert measure(monkeypatch, world) == 2

    assert len(stored_documents(world)) == 1
    (stored,) = store_of(world).history(head_key(world))
    (table,) = stored.document["tables"]
    assert [r["verdict"] for r in table["results"]] == ["error"]
    assert "stub crashed" in table["results"][0]["detail"]
    assert "stub crashed" in capsys.readouterr().out  # the report carries the excerpt
    assert not stored.measured


@linux_only
@pytest.mark.parametrize(
    ("retired", "said"),
    [("max_heavy = 1\n", "max_heavy"), ("\n[goals]\nquiet_after = 60\n", "[goals] section")],
    ids=["scheduler-key", "goals"],
)
def test_retired_config_in_the_commit_does_not_stop_its_measurement(
    make_world: WorldBuilder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    retired: str,
    said: str,
) -> None:
    # The commit's own ``booley.toml`` holds *retired*; the live one does not until the end.
    world = metrics_project(
        make_world, "[metrics_ok.checkout_stub]\nmarker_max = 10\n", config=ONE_AT_A_TIME + retired
    )
    live_settings = world.main / "booley.toml"
    live_settings.write_bytes(b"[jobs]\nmax_tickets = 1\n")

    assert measure(monkeypatch, world) == 0
    assert len(stored_documents(world)) == 1

    live_settings.write_bytes(b"[jobs]\nmax_tickets = 1\n" + retired.encode())
    capsys.readouterr()
    assert measure(monkeypatch, world) == 2
    assert said in capsys.readouterr().err
    assert len(stored_documents(world)) == 1  # nothing new was stored


@linux_only
def test_live_paths_in_files_no_flow_reads_do_not_stop_the_measurement(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Files no checkout run reads; a waiver's subject is copied from the warning ID (DOCTOR.md).
    waiver = 'version = 1\n[[waiver]]\ncheck = "interactive.devcontainer-drift"\nreason = "ok"\n'
    waiver += 'subject = "{live}/.devcontainer/devcontainer.json"\npermanent = true\n'
    names = ("doctor-waivers", "configs", "criteria", "reviews", "pipeline", "notes")
    files = {f".booley/{name}.toml": waiver for name in names}
    files[".booley/metrics.toml"] = "[metrics_ok.checkout_stub]\nmarker_max = 10\n"
    files.update({f"{name}.core": CORE.format(name=name) for name in TARGETS})
    files["top.v"] = "module top; endmodule\n"

    assert measure(monkeypatch, make_world(files=files)) == 0


@linux_only
def test_a_target_run_measures_one_target_and_keeps_its_reports_with_the_job(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = metrics_project(
        make_world,
        "[metrics_ok.checkout_stub]\nmarker_max = 10\n[metrics_b.checkout_stub]\nmarker_max = 10\n",
    )

    assert measure(monkeypatch, world, "--target", "metrics_b") == 0

    assert started_targets(world) == ["metrics_b"]
    assert not (world.main / "measurements").exists()
    assert store_of(world).measured(head_key(world)) is None
    (record,) = measure_records(world)
    job_dir = standalone_log_root(world.main) / record.run_id
    (document_path,) = (job_dir / "measurements").glob(f"*/{record.run_id}.json")
    document = json.loads(document_path.read_text(encoding="utf-8"))
    assert [table["identity"].split("#")[1] for table in document["tables"]] == ["metrics_b"]
    (report,) = [r for r in document["reports"] if r["name"] == "report.json"]
    assert (document_path.parent / report["path"]).is_file()


# The ``booley measure`` process of an MCP Job, in the stub Flow's world: what the in-process
# tests above patch is patched in the process itself, then the real entry point runs.
MCP_MEASURE_PROCESS = """
import sys
import pytest
from booley.harness import booley as cli
from booley.measurement import metrics_file
from tests.harness.test_checkout_flow import FLOW, use_fusesoc
from tests.harness.test_measure_children_flows import declare_stub_metrics
from booley.runtime.runtime_context import RuntimeLocation
from tests.sandbox_venue_fake import place_child

patches = pytest.MonkeyPatch()
use_fusesoc(patches)
declare_stub_metrics(patches)
patches.setattr(metrics_file, "TABLE_FLOWS", (*metrics_file.TABLE_FLOWS, FLOW))
place_child(RuntimeLocation.HAND_WRITTEN_SANDBOX)
sys.argv = ["booley", *sys.argv[1:]]
sys.exit(cli.main())
"""
SOURCE_ROOT = Path(__file__).parents[2] / "src"
REPOSITORY_ROOT = Path(__file__).parents[2]


def processes_under(root: Path) -> list[int]:
    """Processes running in *root* or below it (the children run in the measure's checkout)."""
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "cwd").readlink().resolve().is_relative_to(root.resolve()):
                found.append(int(entry.name))
        except OSError:
            continue
    return found


@linux_only
def test_an_mcp_cancel_stops_the_measurement_and_its_children(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from booley.mcp.call_context import resolve_call_context

    # The MCP server builds its Job manager only inside its own lifetime, and the public
    # JobLifecycle's default repository finds records differently from the server's; the test
    # drives the server's real submit and cancel with no server around it.
    from booley.mcp.server import _JobManager
    from tests.mcp_tools.test_mcp_dispatch import _FakeLifetime

    world = metrics_project(make_world, "[linger.checkout_stub]\nmarker_max = 10\n")
    scratch = tmp_path / "measure-scratch"
    scratch.mkdir()
    runtime = tmp_path / "runtime"
    for name, value in {
        "BOOLEY_LOGS_DIR": str(tmp_path / "logs"),
        "BOOLEY_RUNTIME_DIR": str(runtime),
        "BOOLEY_SLOTS_DIR": str(runtime / "jobs" / "slots"),
        "PYTHONPATH": os.pathsep.join([str(SOURCE_ROOT), str(REPOSITORY_ROOT)]),
        "TMPDIR": str(scratch),  # where the measurement exports its checkout
    }.items():
        monkeypatch.setenv(name, value)
    command = [sys.executable, "-c", MCP_MEASURE_PROCESS, "--project", str(world.live), "measure"]

    async def scenario() -> tuple[str | None, float, str]:
        jobs = _JobManager(_FakeLifetime())
        context = resolve_call_context({})
        run_id = jobs.submit("measure", command, 3 * hang_guard_s(), context=context)
        deadline = time.monotonic() + hang_guard_s()
        while not (world.ctl / "lingering").exists():
            assert await jobs.wait(run_id, 0) is False, "measure ended early"
            assert time.monotonic() < deadline, "the child Flow never started"
            await asyncio.sleep(0.05)
        began = time.monotonic()
        phase = await asyncio.wait_for(jobs.cancel(run_id), timeout=hang_guard_s())
        record = job_records.read_record(run_id, context.jobs_root)
        assert record is not None
        return phase, time.monotonic() - began, record.status

    try:
        phase, took, status = asyncio.run(scenario())

        assert (phase, status) == ("running", "cancelled")
        assert (world.ctl / "stopping").exists()  # the child was interrupted, not killed
        assert took > 5  # the child took longer to stop than the default cancel grace
        assert processes_under(scratch) == []
        children = job_records.list_records(standalone_jobs_dir(world.main), skip_malformed=True)
        assert children and all(r.status != job_records.STATUS_RUNNING for r in children)
    finally:
        for pid in processes_under(scratch):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
