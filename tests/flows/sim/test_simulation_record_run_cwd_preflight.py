"""A missing literal ``[flows.sim].run_cwd`` must fail before any build (issue #881)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.record import serial_execution
from booley.flows.sim.record.codec import (
    SimulationRecordIntegrityError,
    encode_simulation_record_manifest,
)
from booley.flows.sim.record.coordinator import (
    NewSimulationRecordRunRequest,
    ResumeSimulationRecordPreviewRequest,
    SimulationRecord,
    SimulationRecordPolicy,
)
from booley.flows.sim.record.model import create_simulation_record_plan
from booley.flows.sim.record.planning import finalize_manifest, manifest_digest
from booley.flows.sim.record.run_directory import RunDirectory, claimed_run_directory
from booley.flows.sim.record.store import SimulationRecordStore
from booley.flows.sim.request import SimRequest
from booley.flows.sim.verilator_coverage import SimulationBuildResult
from tests.flows.sim.leased_session import canned_build_session
from tests.flows.sim.simulation_record_boundary import PASS_PROCESS, run_simulation_record_sim
from tests.flows.sim.simulation_record_test_support import (
    _admission,
    _executor,
    _handle,
    _legacy_disclosures,
    _manifest_for,
    _run_coverage_simulation_record,
)
from tests.flows.sim.test_coverage_flow import _coverage, _coverage_project, _pin_revision
from tests.flows.sim.test_coverage_transaction import NativeExecution
from tests.flows.sim.test_simulation_record_manifest_codec import (
    _baseline_manifest,
    _distinct_root_resume,
    _manifest,
    _selection_test_runs,
    _sha,
)
from tests.flows.sim.test_simulation_record_resume_diagnostics import (
    _interrupted_simulation_record,
)

_MISSING = "literal run directory must already exist"
_IDENTITY = {"simulation_record_id": "c", "work_item_id": "w", "attempt_id": "a"}


def literal_manifest(access: str, *, configured: str = "run"):
    """One-item ordinary HDL manifest whose run_cwd is the literal ``configured``."""
    return finalize_manifest(literal_document(access, configured=configured))


def literal_document(access: str, *, configured: str = "run") -> dict:
    """Unfinalized document behind :func:`literal_manifest`."""
    document = _manifest()
    document.pop("fingerprints")
    document["workload"]["pre_sim_build_access"] = access
    document["workload"]["run_cwd"] = {
        "configured": configured,
        "kind": "literal",
        "placeholders": [],
    }
    name = "alpha"
    suite_raw = f"[test.{name}]\n".encode()
    document["required_suite"] = {
        "names": [name],
        "default_invocation": False,
        "source_path": "tests.toml",
        "source_bytes": len(suite_raw),
        "source_sha256": "sha256:" + hashlib.sha256(suite_raw).hexdigest(),
    }
    variant = document["build_variants"][0]
    identity = {
        "ordinal": 0,
        "kind": "ordinary_hdl",
        "role": "candidate",
        "revision": "abc123",
        "target": document["target"],
        "selection": {"kind": "named", "names": [name]},
        "test_runs": _selection_test_runs((name,)),
        "arguments": [name],
        "build_variant_id": variant["build_variant_id"],
        "run_directory": {
            "configured": configured,
            "kind": "literal",
            "collision_template": configured,
        },
    }
    fingerprint = _sha(identity)
    document["work_items"] = [
        {
            "work_item_id": "item:0000:" + fingerprint.removeprefix("sha256:")[:16],
            **identity,
            "fingerprint_sha256": fingerprint,
        }
    ]
    if access == "legacy-per-test":
        document["planning_disclosures"] = [_legacy_disclosures()[name]]
    return document


class _Harness:
    """One record run against a counting fake execution."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, access: str) -> None:
        self.tmp_path = tmp_path
        self.access = access
        self.project = tmp_path / "project"
        self.project.mkdir()
        build_root = tmp_path / "engine-build"
        build_root.mkdir()
        (build_root / "simv").write_bytes(b"image")
        self.counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
        self.hooks: list[tuple[str, ...]] = []
        self.executor = _executor(build_root, self.counters, disclosures=_legacy_disclosures())
        # Private seam: the legacy per-test hook runs inside the serial executor
        # and no public result reports whether it was invoked.
        monkeypatch.setattr(
            serial_execution,
            "_run_hook",
            lambda _handle, _root, names, *_args, **_kwargs: self.hooks.append(names),
        )
        monkeypatch.setattr(
            serial_execution.TargetCatalog,
            "build",
            lambda _root: SimpleNamespace(select=lambda *_a, **_k: _handle(self.project)),
        )
        self.invocation = tmp_path / "reports" / "000001"
        self.invocation.mkdir(parents=True)

    def run(self, manifest=None):
        plan = create_simulation_record_plan(manifest or literal_manifest(self.access))
        return SimulationRecord(self.executor).run(
            NewSimulationRecordRunRequest(
                plan,
                self.project,
                self.invocation.parent,
                SimulationRecordPolicy(),
                self.invocation,
                _admission(),
            )
        )

    def assert_nothing_built_or_published(self) -> None:
        assert self.counters == {"compile": 0, "durable_reuse": 0, "launch": 0}
        assert self.hooks == []
        root = self.invocation / "targets/sim/record"
        assert not tuple(root.glob("work-items/*/attempts/*"))
        assert not tuple(root.glob("build-variants/*/attempts/*"))


@pytest.fixture(params=["immutable", "legacy-per-test"])
def harness(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Harness:
    return _Harness(tmp_path, monkeypatch, request.param)


def _make_missing(project: Path) -> None:
    """Leave the literal ``run`` directory absent."""


def _make_link(project: Path) -> None:
    """Make ``run`` a symlink, or skip where links are unavailable."""
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    real = project / "real"
    real.mkdir()
    try:
        (project / "run").symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")


def _make_file(project: Path) -> None:
    """Make ``run`` an ordinary file."""
    (project / "run").write_text("not a directory", encoding="utf-8")


@pytest.mark.parametrize(
    ("make_run_cwd", "expected"),
    [
        pytest.param(_make_missing, _MISSING, id="missing"),
        pytest.param(_make_link, "not a link", id="linked"),
        pytest.param(_make_file, "exists but is not a directory", id="non-directory"),
    ],
)
def test_unusable_literal_run_cwd_fails_before_any_build(
    harness: _Harness, make_run_cwd, expected: str
) -> None:
    make_run_cwd(harness.project)

    with pytest.raises(SimulationRecordIntegrityError, match=expected) as caught:
        harness.run()

    if make_run_cwd is _make_missing:
        assert ".gitkeep" in str(caught.value)
        assert "{attempt}" in str(caught.value)
    harness.assert_nothing_built_or_published()


def test_existing_literal_run_cwd_still_runs(harness: _Harness) -> None:
    (harness.project / "run").mkdir()

    outcome = harness.run()

    assert outcome.complete is True
    assert harness.counters["compile"] == 1
    assert harness.counters["launch"] == 1


def test_templated_run_cwd_is_not_preflighted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch, "immutable")

    outcome = harness.run(_manifest_for(("alpha",)))

    assert outcome.complete is True
    assert harness.counters["compile"] == 1


def test_complete_simulation_record_rerun_does_not_require_literal_run_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch, "immutable")
    run_dir = harness.project / "run"
    run_dir.mkdir()
    assert harness.run().complete is True
    run_dir.rmdir()

    assert harness.run().complete is True
    assert harness.counters["compile"] == 1


def test_claim_rejects_literal_removed_after_preflight(tmp_path: Path) -> None:
    missing = tmp_path / "gone"
    run = RunDirectory(missing, str(missing), False, tmp_path / "locks" / "gone.lock", None)

    with (
        pytest.raises(SimulationRecordIntegrityError, match=_MISSING) as caught,
        claimed_run_directory(run, identity=_IDENTITY),
    ):
        pass

    assert ".gitkeep" in str(caught.value)


def test_missing_literal_run_cwd_blocks_every_publication_across_the_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A candidate's missing directory is found before anything builds or publishes."""
    harness = _Harness(tmp_path, monkeypatch, "immutable")

    def request(manifest, root: Path) -> NewSimulationRecordRunRequest:
        return NewSimulationRecordRunRequest(
            create_simulation_record_plan(manifest),
            root,
            harness.invocation.parent,
            SimulationRecordPolicy(),
            harness.invocation,
            _admission(),
        )

    candidate = request(literal_manifest("immutable"), harness.project)

    # Private seam: the all-requests-before-first-publication order is held by
    # this method, and no public flow fixture builds two requests with a missing
    # literal run_cwd.
    with pytest.raises(SimulationRecordIntegrityError, match=_MISSING):
        SimulateFlow()._publish_and_run_simulation_record_requests(
            SimulationRecord(harness.executor), [candidate]
        )

    assert harness.counters["compile"] == 0
    assert not tuple(harness.invocation.glob("targets/*/record/*manifest*"))


def test_owner_missing_run_cwd_fails_before_prerequisite_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The baseline's own directory exists, so only the owner check can stop its build."""
    harness = _Harness(tmp_path, monkeypatch, "immutable")
    (harness.project / "run").mkdir()  # the baseline's literal "run"
    owner_document = literal_document("immutable", configured="owner-run")
    owner_document["prerequisites"] = [
        _prerequisite_entry(harness, _baseline_manifest(), "base-revision")
    ]

    with pytest.raises(SimulationRecordIntegrityError, match=_MISSING) as caught:
        harness.run(finalize_manifest(owner_document))

    assert "owner-run" in str(caught.value)
    assert harness.counters["compile"] == 0


def _prerequisite_entry(harness, manifest, slug: str) -> dict:
    """Publish ``manifest`` under the invocation and return the entry that references it."""
    store = SimulationRecordStore(harness.invocation / "targets" / slug / "record")
    store.publish_manifest(manifest)
    raw = encode_simulation_record_manifest(manifest)
    document = json.loads(raw)
    return {
        "role": "cycle_count_baseline",
        "manifest": {
            "path_base": "origin_invocation",
            "path": store.manifest_path.relative_to(harness.invocation).as_posix(),
            "bytes": len(raw),
            "sha256": manifest_digest(manifest),
            "kind": "simulation_record_manifest",
            "owner": document["simulation_record_id"],
        },
        "simulation_record_id": document["simulation_record_id"],
        "target": document["target"],
        "required_observation": "cycle_count",
        "work_item_id": document["work_items"][0]["work_item_id"],
    }


def test_nested_prerequisite_missing_run_cwd_fails_before_deeper_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A -> B -> C: B's missing directory must stop C's build, not only A's or B's own run."""
    harness = _Harness(tmp_path, monkeypatch, "immutable")
    (harness.project / "run").mkdir()  # owner A and leaf C use the literal "run"
    leaf = _prerequisite_entry(harness, _baseline_manifest(), "leaf-revision")
    middle_document = json.loads(encode_simulation_record_manifest(_baseline_manifest()))
    middle_document.pop("fingerprints")
    middle_document["simulation_record_id"] = "550e8400-e29b-41d4-a716-446655440001"
    middle_document["workload"]["run_cwd"]["configured"] = "middle-run"
    middle_document["prerequisites"] = [leaf]
    middle = _prerequisite_entry(harness, finalize_manifest(middle_document), "middle-revision")
    owner_document = literal_document("immutable")
    owner_document["prerequisites"] = [middle]

    with pytest.raises(SimulationRecordIntegrityError, match=_MISSING) as caught:
        harness.run(finalize_manifest(owner_document))

    assert "middle-run" in str(caught.value)
    assert harness.counters["compile"] == 0


def test_missing_literal_run_cwd_fails_coverage_before_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "booley.flows.sim.record.flow_planning.resolve_run_cwd", lambda _root: "missing-run"
    )
    builds: list[object] = []

    class CountingNative(NativeExecution):
        def build(self, request):
            builds.append(request)
            return SimulationBuildResult(True)

    with pytest.raises(SimulationRecordIntegrityError, match=_MISSING):
        _run_coverage_simulation_record(tmp_path, CountingNative())

    store = tmp_path / "reports/1/targets/sim_0/record"
    assert builds == []
    assert not tuple(store.glob("work-items/*/attempts/*"))


def _toml(run_cwd: str) -> str:
    return f'[flows.sim]\nrun_cwd = "{run_cwd}"\n'


def _ordinary_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cwd: str):
    def run(dry_run: bool):
        with canned_build_session():
            return run_simulation_record_sim(
                tmp_path,
                monkeypatch,
                {"sim_0": ["reset"]},
                PASS_PROCESS,
                files={".booley/booley.toml": _toml(run_cwd)},
                dry_run=dry_run,
            ).result

    return run


def _coverage_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cwd: str):
    data = _coverage_project(tmp_path, monkeypatch)
    (data / "booley.toml").write_text(_toml(run_cwd), newline="\n")
    return lambda dry_run: _coverage(tmp_path, dry_run=dry_run)


def _resume_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cwd: str):
    (tmp_path / "run").mkdir()
    reports, manifest, _executions = _interrupted_simulation_record(
        tmp_path, monkeypatch, booley_toml=_toml(run_cwd)
    )
    (tmp_path / "run").rmdir()
    return lambda dry_run: SimulateFlow().execute(
        SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports, dry_run=dry_run)
    )


_ARRANGERS = {"ordinary": _ordinary_runs, "coverage": _coverage_runs, "resume": _resume_runs}


@pytest.mark.parametrize("mode", ["ordinary", "coverage", "resume"])
def test_dry_run_refuses_missing_literal_run_cwd_like_the_real_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    run = _ARRANGERS[mode](tmp_path, monkeypatch, "run")

    dry = run(True)
    real = run(False)

    assert dry.exit_code == real.exit_code == 2
    assert _MISSING in dry.outcome.report_text
    assert dry.outcome.report_text == real.outcome.report_text.splitlines()[0]


@pytest.mark.parametrize("mode", ["existing-literal", "templated", "resume-without-pending-work"])
def test_dry_run_and_run_accept_usable_run_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    if mode == "resume-without-pending-work":
        _pin_revision(monkeypatch, "a" * 40)
        (tmp_path / "run").mkdir()
        done = _ordinary_runs(tmp_path, monkeypatch, "run")(False)
        assert done.exit_code == 0
        manifest = tmp_path / "reports/sim/1/targets/sim_0/record/manifest.json"
        (tmp_path / "run").rmdir()

        def run(dry_run: bool):
            return SimulateFlow().execute(
                SimRequest(
                    resume_from=manifest,
                    work_dir=tmp_path,
                    report_dir=tmp_path / "reports",
                    dry_run=dry_run,
                )
            )

    else:
        (tmp_path / "run").mkdir()
        run = _ordinary_runs(
            tmp_path, monkeypatch, "run" if mode == "existing-literal" else "runs/{attempt}"
        )

    assert run(True).exit_code == 0
    assert run(False).exit_code == 0


@pytest.mark.parametrize("baseline_run", [False, True], ids=["baseline-missing", "both-present"])
def test_resume_preflight_checks_each_record_in_its_own_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, baseline_run: bool
) -> None:
    validated, _candidate, _store, candidate_root, baseline_root = _distinct_root_resume(
        tmp_path, monkeypatch
    )
    (candidate_root / "run").mkdir()
    if baseline_run:
        (baseline_root / "run").mkdir()
    request = ResumeSimulationRecordPreviewRequest(
        validated,
        create_simulation_record_plan(validated.manifest),
        candidate_root,
        None,
        SimulationRecordPolicy(),
    )

    if baseline_run:
        SimulationRecord().preflight(request)
        return
    with pytest.raises(SimulationRecordIntegrityError, match=_MISSING) as caught:
        SimulationRecord().preflight(request)
    assert str(baseline_root / "run") in str(caught.value)
