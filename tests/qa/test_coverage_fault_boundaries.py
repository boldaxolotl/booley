"""Exercise the real Linux IO shim against disposable ordinary processes."""

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "qa/shared/coverage/faults"
SPEC = importlib.util.spec_from_file_location("qa_filesystem", ROOT / "filesystem.py")
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    if sys.platform != "linux":
        pytest.skip("Linux preload fixture; Windows QA uses the issued Linux Sandbox")
    target = tmp_path_factory.mktemp("coverage-shim") / "boundary.so"
    result = subprocess.run(
        [
            "gcc",
            "-shared",
            "-fPIC",
            "-O2",
            "-Wall",
            "-Wextra",
            "-o",
            str(target),
            str(ROOT / "boundary.c"),
            "-ldl",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return target


def test_link_failure_is_one_shot_and_preserves_other_paths(tmp_path, library):
    source = tmp_path / "source"
    source.write_text("unchanged")
    script = """import os,sys
from pathlib import Path
p=Path(sys.argv[1])
os.link(p/'source',p/'unrelated')
try: os.link(p/'source',p/'coverage.json')
except OSError as e: assert e.errno==5
else: raise AssertionError('expected injected failure')
assert not (p/'coverage.json').exists()
os.link(p/'source',p/'coverage.json')
"""
    code = controller.run(
        [sys.executable, "-c", script, str(tmp_path)],
        tmp_path,
        tmp_path / "control",
        library,
        "link",
        "/coverage.json",
        "any",
        set(),
        20,
    )
    assert code == 0
    assert (tmp_path / "coverage.json").read_text() == "unchanged"
    assert (tmp_path / "unrelated").read_text() == "unchanged"


def test_progress_gate_ignores_nonterminal_writes(tmp_path, library):
    script = """import json,os,sys
from pathlib import Path
p=Path(sys.argv[1])
for complete in [False,True]:
 (p/'temp').write_text(json.dumps({'complete':complete}))
 try: os.replace(p/'temp',p/'progress.json')
 except OSError as e: assert complete and e.errno==5
 else: assert not complete
"""
    assert (
        controller.run(
            [sys.executable, "-c", script, str(tmp_path)],
            tmp_path,
            tmp_path / "control",
            library,
            "rename",
            "/progress.json",
            "complete",
            set(),
            20,
        )
        == 0
    )
    assert json.loads((tmp_path / "progress.json").read_text()) == {"complete": False}


def test_progress_gate_reply_is_never_observed_partially_written(tmp_path, library, monkeypatch):
    """A slow controller write must not expose an empty reply that the shim reads as F."""
    original_write_text = Path.write_text

    def slow_write_text(self, data, *args, **kwargs):
        self.touch()
        time.sleep(0.1)
        return original_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", slow_write_text)
    test_progress_gate_ignores_nonterminal_writes(tmp_path, library)


def test_progress_gate_uses_source_bytes_from_event_time(tmp_path, library, monkeypatch):
    script = """import json,os,sys
from pathlib import Path
p=Path(sys.argv[1])
for complete in [False,True]:
 (p/'temp').write_text(json.dumps({'complete':complete}))
 try: os.replace(p/'temp',p/'progress.json')
 except OSError as e: assert complete and e.errno==5
 else: assert not complete
"""
    original_service = controller.service
    mutated = False

    def mutate_original_after_event(*args, **kwargs):
        nonlocal mutated
        control = args[0]
        published = any(
            "." not in event.name and event.read_text().endswith("\n")
            for event in control.glob("event-*")
        )
        if not mutated and published:
            (tmp_path / "temp").write_text(json.dumps({"complete": True}))
            mutated = True
        return original_service(*args, **kwargs)

    monkeypatch.setattr(controller, "service", mutate_original_after_event)

    assert (
        controller.run(
            [sys.executable, "-c", script, str(tmp_path)],
            tmp_path,
            tmp_path / "control",
            library,
            "rename",
            "/progress.json",
            "complete",
            set(),
            20,
        )
        == 0
    )


def test_source_mutation_waits_for_published_event_after_snapshot_creation(tmp_path, monkeypatch):
    """Snapshot sidecars and partial records must not trigger the event-time mutation."""

    def run_with_snapshot_before_event(command, owned, control, *args):
        control.mkdir()
        source = owned / "temp"
        original = json.dumps({"complete": False})
        source.write_text(original)
        event = control / "event-123-0"
        snapshot = event.with_suffix(".snapshot")
        snapshot.touch()  # The shim creates this before copying source bytes.
        seen = set()
        controller.service(control, "complete", set(), seen, os.getpid())
        assert source.read_text() == original
        assert not event.with_suffix(".reply").exists()
        snapshot.write_text(original)
        event.touch()  # Event publication can itself be observed before the newline.
        controller.service(control, "complete", set(), seen, os.getpid())
        assert source.read_text() == original
        assert not event.with_suffix(".reply").exists()
        event.write_text(f"rename\t{snapshot}\t{owned / 'progress.json'}\n")
        controller.service(control, "complete", set(), seen, os.getpid())
        assert json.loads(source.read_text()) == {"complete": True}
        assert snapshot.read_text() == original
        assert event.with_suffix(".reply").read_text() == "P"
        return 0

    monkeypatch.setattr(controller, "run", run_with_snapshot_before_event)
    test_progress_gate_uses_source_bytes_from_event_time(
        tmp_path, tmp_path / "unused.so", monkeypatch
    )


def test_unlinkat_directory_fd_is_intercepted(tmp_path, library):
    (tmp_path / "payload").write_text("retained")
    script = """import os,sys
fd=os.open(sys.argv[1],os.O_RDONLY)
try:
 try: os.unlink('payload',dir_fd=fd)
 except OSError as e: assert e.errno==5
 else: raise AssertionError('missing injection')
finally: os.close(fd)
"""
    assert (
        controller.run(
            [sys.executable, "-c", script, str(tmp_path)],
            tmp_path,
            tmp_path / "control",
            library,
            "unlink",
            "/payload",
            "any",
            set(),
            20,
        )
        == 0
    )
    assert (tmp_path / "payload").read_text() == "retained"
    # Restoration is a new ordinary operation without any injected environment.
    subprocess.run(
        [sys.executable, "-c", "import os,sys;os.unlink(sys.argv[1])", str(tmp_path / "payload")],
        check=True,
        timeout=10,
    )
    assert not (tmp_path / "payload").exists()


def test_missing_interception_cannot_report_fault_success(tmp_path, library):
    with pytest.raises(ValueError, match="not injected"):
        controller.run(
            [sys.executable, "-c", "pass"],
            tmp_path,
            tmp_path / "control",
            library,
            "link",
            "/coverage.json",
            "any",
            set(),
            10,
        )


def test_acceptance_gate_requires_new_transaction(tmp_path):
    source = tmp_path / "state.tmp"
    source.write_text(json.dumps({"acceptance_transactions": ["old"]}))
    assert not controller.should_fail(source, "acceptance", {"old"})
    source.write_text(json.dumps({"acceptance_transactions": ["old", "new"]}))
    assert controller.should_fail(source, "acceptance", {"old"})


def test_controller_does_not_change_parent_environment(tmp_path, library):
    before = dict(os.environ)
    with pytest.raises(ValueError, match="not injected"):
        controller.run(
            [sys.executable, "-c", "pass"],
            tmp_path,
            tmp_path / "control",
            library,
            "link",
            "/absent",
            "any",
            set(),
            10,
        )
    assert dict(os.environ) == before


def test_interrupt_reaps_producer_before_publication(tmp_path, library):
    (tmp_path / "source").write_text('{"complete":true}')
    script = "import os,sys;os.link(sys.argv[1]+'/source',sys.argv[1]+'/coverage.json')"
    code = controller.run(
        [sys.executable, "-c", script, str(tmp_path)],
        tmp_path,
        tmp_path / "control",
        library,
        "link",
        "/coverage.json",
        "interrupt",
        set(),
        20,
    )
    assert code == -15
    assert not (tmp_path / "coverage.json").exists()
    assert (tmp_path / "control/interrupted").is_file()
    assert not (tmp_path / "control/consumed").exists()
    assert list((tmp_path / "control").glob("*.source.json"))


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process-group controller")
def test_shutdown_kills_descendant_after_group_leader_exits(tmp_path):
    marker = tmp_path / "ready"
    grandchild = f"import signal,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path({str(marker)!r}).write_text('ready');time.sleep(60)"
    leader_script = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',sys.argv[1]]);time.sleep(60)"
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_script, grandchild], start_new_session=True
    )
    try:
        deadline = time.monotonic() + 5
        while not marker.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        controller.shutdown(leader, grace=0.1)
        assert leader.poll() is not None
        assert not controller.group_exists(leader.pid)
    finally:
        controller.shutdown(leader, grace=0.1)


def test_any_gate_preserves_attempted_publication_bytes(tmp_path, library):
    (tmp_path / "source").write_text('{"complete":true}')
    script = "import os,sys;from pathlib import Path;p=Path(sys.argv[1]);\ntry:os.link(p/'source',p/'coverage.json')\nexcept OSError:pass\n(p/'source').unlink()"
    assert (
        controller.run(
            [sys.executable, "-c", script, str(tmp_path)],
            tmp_path,
            tmp_path / "control",
            library,
            "link",
            "/coverage.json",
            "any",
            set(),
            20,
        )
        == 0
    )
    captures = list((tmp_path / "control").glob("*.source.json"))
    assert len(captures) == 1
    assert json.loads(captures[0].read_text()) == {"complete": True}
