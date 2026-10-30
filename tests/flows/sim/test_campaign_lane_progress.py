"""Terminal progress stays attributed to its own lane when campaign tests run in parallel (#1972).

Each test drives the real two-lane ``BoundedCampaignScheduler`` with a
``TerminalProgress`` installed. ``threading.Event`` objects order the lanes
deterministically, and children are real ``python -c`` processes observed
through the production pipe readers. No EDA tool is needed.
"""

from __future__ import annotations

import io
import os
import re
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.config.jobs import SlotCaps
from booley.flows.sim.campaign.capacity import HeavyCapacity
from booley.flows.sim.campaign.child_protocol import ChildExecutionRegistry
from booley.flows.sim.campaign.scheduler import BoundedCampaignScheduler, ScheduledAttempt
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.sim.execution.pre_sim import _run_pre_sim_process
from booley.flows.terminal_progress import TerminalProgress, announce_unit
from booley.runtime.execution_records import ExecutionId
from booley.runtime.job_slots import CLASS_HEAVY, SlotStore
from tests.flows.sim.test_campaign_phase3 import _two_item_manifest
from tests.flows.sim.test_campaign_phase4 import _managed

# Bound for every cross-lane wait so a regression fails instead of hanging.
WAIT_S = 5


class Screen(io.StringIO):
    """In-memory console with a switchable ``isatty``."""

    def __init__(self, *, tty: bool = False) -> None:
        super().__init__()
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def _wait(event: threading.Event) -> None:
    assert event.wait(WAIT_S), "lane handshake timed out"


def _child(tmp_path: Path, code: str) -> None:
    """Run one observed child through the production Pre-Sim process path."""
    result = _run_pre_sim_process(
        [sys.executable, "-c", code], cwd=tmp_path, env=dict(os.environ), timeout=30
    )
    assert result.returncode == 0, result.stderr


def _print_lines(prefix: str, count: int) -> str:
    return f"for i in range({count}): print('{prefix} %d' % i)"


def _run_two_lanes(
    tmp_path: Path,
    sink: TerminalProgress,
    bodies: dict[str, Callable[[], None]],
    labels: dict[str, str],
    *,
    finish: Callable[[], None] | None = None,
) -> None:
    """Run ``alpha`` (outer lane) and ``beta`` (child lane) concurrently.

    ``bodies`` maps a test name to the callable run inside that lane's execute
    callback; ``labels`` receives each test's ``<name>/<attempt8>`` lane label.
    ``finish`` runs after the scheduler, while the sink is still installed.
    """
    manifest = _two_item_manifest()
    campaign_store = CampaignStore(tmp_path / "campaign")
    campaign_store.publish_manifest(manifest)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    registry = ChildExecutionRegistry(manifest, project)
    slot_store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=2))
    outer = slot_store.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))

    def allocate(item) -> ScheduledAttempt:
        attempt_id = os.urandom(16).hex()
        ordinal, directory = campaign_store.allocate_attempt_directory(
            str(item["work_item_id"]), attempt_id
        )
        # A unique collision key lets both lanes run at once.
        return ScheduledAttempt(item, attempt_id, ordinal, directory, f"key-{attempt_id}")

    def execute(attempt, _child_id) -> None:
        name = str(attempt.item["selection"]["names"][0])  # type: ignore[index]
        labels[name] = f"{name}/{attempt.attempt_id[:8]}"
        bodies[name]()

    capacity = HeavyCapacity(
        _managed(slot_store, outer),
        terminal_proof=registry.is_terminal,
        recover_child=registry.cancel,
    )
    try:
        with sink.installed():
            BoundedCampaignScheduler(capacity, registry, allocate=allocate, execute=execute).run(
                manifest.document["work_items"]  # type: ignore[attr-defined]
            )
            if finish is not None:
                finish()
    finally:
        slot_store.release(outer)


def _records(sink: TerminalProgress) -> list[str]:
    return [line for line in sink.stream.getvalue().splitlines() if "[booley-progress]" in line]  # type: ignore[attr-defined]


def _transcript_segments(sink: TerminalProgress) -> list[tuple[str, str]]:
    """Return ``(header, body)`` for every labelled transcript chunk."""
    assert sink.log_path is not None
    text = sink.log_path.read_text(encoding="utf-8")
    parts = re.split(r"\n(\[sim Target=[^\n]*\])\n", "\n" + text)
    return [(parts[i], parts[i + 1]) for i in range(1, len(parts) - 1, 2)]


def _segments_with(sink: TerminalProgress, line: str) -> list[str]:
    return [header for header, body in _transcript_segments(sink) if line in body.splitlines()]


def test_overlapping_lanes_show_each_pre_sim_line_once_under_its_own_lane(
    tmp_path: Path,
) -> None:
    sink = TerminalProgress("sim", "sim_demo", tmp_path, stream=Screen(), clock=lambda: 0.0)
    labels: dict[str, str] = {}
    alpha_first_done = threading.Event()
    beta_done = threading.Event()

    def alpha() -> None:
        announce_unit("Pre-Sim Commands: alpha")
        _child(tmp_path, "print('pre alpha 1')")
        alpha_first_done.set()
        _wait(beta_done)
        _child(tmp_path, "print('pre alpha 2')")
        announce_unit("simulation: alpha")
        _child(tmp_path, _print_lines("sim alpha", 8))

    def beta() -> None:
        _wait(alpha_first_done)
        announce_unit("Pre-Sim Commands: beta")
        _child(tmp_path, "print('pre beta 1')")
        announce_unit("simulation: beta")
        _child(tmp_path, _print_lines("sim beta", 20))
        beta_done.set()

    # Freeze the writer so every observation is consumed at the final drain,
    # after the lanes have moved on to other stages.
    with patch.object(sink, "_run"):
        _run_two_lanes(
            tmp_path, sink, {"alpha": alpha, "beta": beta}, labels, finish=sink._drain_end
        )

    records = _records(sink)
    for test, line in (
        ("alpha", "pre alpha 1"),
        ("alpha", "pre alpha 2"),
        ("beta", "pre beta 1"),
    ):
        stage = f"stage=Pre-Sim Commands: {test} "
        shown = [record for record in records if record.endswith(f"output: {line}")]
        assert len(shown) == 1, f"{line!r} shown {len(shown)} times"
        assert f"lane={labels[test]} " in shown[0]
        assert stage in shown[0]
        headers = _segments_with(sink, line)
        assert len(headers) == 1, f"{line!r} in {len(headers)} transcript chunks"
        assert f"lane={labels[test]} " in headers[0]
        assert stage in headers[0]


def test_lane_coverage_and_simulation_output_keep_their_own_labels(tmp_path: Path) -> None:
    sink = TerminalProgress("sim", "sim_demo", tmp_path, tool_output=True, stream=Screen())
    labels: dict[str, str] = {}
    both_announced = [threading.Event(), threading.Event()]

    def alpha() -> None:
        announce_unit("coverage: build/elaboration")
        both_announced[0].set()
        _wait(both_announced[1])
        _child(tmp_path, "print('cov alpha')")

    def beta() -> None:
        _wait(both_announced[0])
        announce_unit("simulation: beta attempt=t2")
        both_announced[1].set()
        _child(tmp_path, "print('sim beta')")

    _run_two_lanes(tmp_path, sink, {"alpha": alpha, "beta": beta}, labels)

    records = _records(sink)
    for test, line, stage in (
        ("alpha", "cov alpha", "stage=coverage: build/elaboration "),
        ("beta", "sim beta", "stage=simulation: beta attempt=t2 "),
    ):
        shown = [record for record in records if record.endswith(f": {line}")]
        assert len(shown) == 1, f"{line!r} shown {len(shown)} times"
        assert f"lane={labels[test]} " in shown[0]
        assert stage in shown[0]
        headers = _segments_with(sink, line)
        assert len(headers) == 1
        assert f"lane={labels[test]} " in headers[0]
        assert stage in headers[0]


def test_one_lane_command_start_keeps_other_lane_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TERM", "xterm")
    now = [0.0]
    screen = Screen(tty=True)
    sink = TerminalProgress(
        "sim",
        "sim_demo",
        tmp_path,
        stream=screen,
        clock=lambda: now[0],
        dimensions=lambda: os.terminal_size((120, 40)),
    )
    labels: dict[str, str] = {}
    beta_ready = threading.Event()
    alpha_done = threading.Event()
    frame: list[str] = []

    def alpha() -> None:
        announce_unit("simulation: alpha")
        _wait(beta_ready)
        sink.begin_command()  # alpha starts a command while beta holds a tail
        now[0] += 1
        sink.tick()
        frame.append(screen.getvalue().rsplit("\x1b[J", 1)[-1])
        sink.end_command()
        alpha_done.set()

    def beta() -> None:
        announce_unit("simulation: beta")
        _child(tmp_path, "print('beta tail line')")
        sink._poll()
        beta_ready.set()
        _wait(alpha_done)

    with patch.object(sink, "_run"):
        _run_two_lanes(tmp_path, sink, {"alpha": alpha, "beta": beta}, labels)

    assert "beta tail line" in frame[0], "alpha's command start cleared beta's tail"
    rows = frame[0].splitlines()
    lane_rows = [index for index, row in enumerate(rows) if f"lane={labels['beta']} " in row]
    assert lane_rows, f"no row for beta's lane in frame: {rows}"
    assert rows[lane_rows[0] + 1 : lane_rows[0] + 2] == ["beta tail line"]


def test_closed_lane_output_is_not_dropped_before_the_final_drain(tmp_path: Path) -> None:
    # Imported here so the three regression tests above still collect on the base.
    from booley.flows.terminal_progress import progress_lane

    now = [0.0]
    sink = TerminalProgress("sim", "sim_demo", tmp_path, stream=Screen(), clock=lambda: now[0])
    with sink.installed():
        with progress_lane("alpha/1"):
            # More queued chunks than one poll consumes, plus an unterminated last line.
            for index in range(600):
                sink.observe("stdout:1", f"alpha line {index}\n".encode())
            sink.observe("stdout:1", b"alpha last, no newline")
        with progress_lane("beta/2"):
            now[0] += 6  # the heartbeat fires while alpha's lane is already closed
            sink._poll()
            sink._drain_end()
    text = "\n".join(_records(sink))
    assert "alpha line 599" in text
    assert "alpha last, no newline" in text
