"""Simulation records an older Booley wrote are refused with one actionable message.

``tests/flows/fixtures/booley_0_3_0`` holds three invocation directories (``complete``,
``interrupted`` and ``coverage``) that Booley 0.3.0's own ``SimulateFlow`` wrote, with only
the simulator replaced. They are captured data, not test cases: each text file embeds
its capture's temporary root, replaced by ``/booley-0.3.0-capture``; the lock files,
run logs and simulator executables were dropped.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from booley.flows.sim.record import codec
from booley.flows.sim.record.codec import (
    canonical_json_bytes,
    decode_simulation_record_manifest,
    decode_simulator_bundle,
)
from booley.flows.sim.record.store import SimulationRecordStore
from tests.flows.sim.simulation_record_test_support import (
    _retained_invocation,
)
from tests.flows.sim.test_simulation_record_manifest_codec import (
    _manifest,
)

_MANIFEST = "booley.simulation-campaign-manifest/"
_UNSUPPORTED = "UnsupportedSimulationRecordFormatError"
_FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The report root of a Project whose Booley Directory is empty, inside the Sandbox."""
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    root = tmp_path / ".booley" / ".runtime" / "flow-reports"
    root.mkdir(parents=True)
    return root


def copy_older_invocation(version: str, kind: str, reports: Path) -> Path:
    """Put a captured ``complete``, ``interrupted`` or ``coverage`` run of *version* at ``sim/1``."""
    source = _FIXTURES / f"booley_{version.replace('.', '_')}" / kind / "sim" / "1"
    return Path(shutil.copytree(source, reports / "sim" / "1"))


def _assert_refused(text: str, schema: str, invocation: Path | None = None) -> None:
    """The one shape of every refusal: an older Booley, the schema, the directory, nothing corrupt."""
    assert "was written by an older Booley and is no longer supported" in text
    assert schema in text
    if invocation is not None:
        assert (
            f"delete the Simulation invocation directory {invocation} and start a fresh run"
            in text
        )
    for corrupt in (
        "integrity failure",
        "is invalid",
        "corrupt",
        "cannot be read",
        "unreadable",
        "missing",
        "Traceback",
    ):
        assert corrupt not in text


def _assert_unsupported_type(error: BaseException) -> None:
    """Compare by name: the class must not be an integrity error, whatever it inherits."""
    assert type(error).__name__ == _UNSUPPORTED
    assert "SimulationRecordIntegrityError" not in [t.__name__ for t in type(error).__mro__]


def _as_030_manifest(raw: bytes, version: str) -> bytes:
    """Re-encode a current manifest the way Booley 0.3.0 wrote it: no ``test_runs``."""
    document = json.loads(raw)
    document["$schema"] = _MANIFEST + version
    if version == "v2":
        document["workload"]["no_waivers"] = True
    for item in document["work_items"]:
        item.pop("test_runs", None)
    return canonical_json_bytes(document)


def _rewrite(path: Path, raw: bytes) -> None:
    path.chmod(0o600)
    path.write_bytes(raw)


@pytest.mark.parametrize(
    "entry", ["decoder_v1", "decoder_v2", "store_in_report_layout", "store_outside_report_layout"]
)
def test_a_0_3_0_manifest_is_refused_naming_an_older_booley(
    tmp_path: Path, reports: Path, entry: str
) -> None:
    version = entry[-2:] if entry.startswith("decoder") else "v1"
    invocation = None
    if entry.startswith("decoder"):
        raw = _as_030_manifest(canonical_json_bytes(_manifest()), version)

        def load() -> object:
            return decode_simulation_record_manifest(raw)

    elif entry == "store_in_report_layout":
        invocation = copy_older_invocation("0.3.0", "complete", reports)
        load = SimulationRecordStore(invocation / "targets" / "sim" / "campaign").load_manifest
    else:
        store = SimulationRecordStore(tmp_path / "campaign")
        store.root.mkdir()
        store.manifest_path.write_bytes(_as_030_manifest(canonical_json_bytes(_manifest()), "v1"))
        load = store.load_manifest

    with pytest.raises(ValueError) as excinfo:
        load()

    _assert_unsupported_type(excinfo.value)
    _assert_refused(str(excinfo.value), _MANIFEST + version, invocation)
    if entry == "store_outside_report_layout":
        # No report layout, so there is no invocation directory to name.
        assert "delete the Simulation invocation directory" not in str(excinfo.value)


def test_a_schema_less_projection_cannot_authenticate_a_coverage_record() -> None:
    from booley.flows.sim.record.projection import decode_simulation_projection

    schema_less = {
        "flow": "sim",
        "target": "sim",
        "target_identity": "::t:0#sim",
        "complete": True,
    }

    with pytest.raises(ValueError, match="cannot authenticate a Coverage Record"):
        decode_simulation_projection(schema_less, coverage_expected=True)


_SNAPSHOT_V2 = "booley.executable-snapshot/v2"
_PREVIOUS_RESULT = "booley.simulation-result/v4"
_PREVIOUS_BUNDLE = "booley.simulator-bundle/v2"


@pytest.mark.parametrize(
    ("schema", "decode"),
    [
        (_PREVIOUS_BUNDLE, decode_simulator_bundle),
        (_SNAPSHOT_V2, "refuse_retired_document"),
    ],
)
def test_previous_version_documents_are_refused(schema: str, decode) -> None:
    if isinstance(decode, str):
        decode = getattr(codec, decode)
    with pytest.raises(ValueError) as excinfo:
        decode(canonical_json_bytes({"$schema": schema}))

    _assert_unsupported_type(excinfo.value)
    _assert_refused(str(excinfo.value), schema)


@pytest.mark.parametrize("raw", [b"\xff", b"not json", b"[]", b'{"$schema":"booley.x/v1"}'])
def test_a_snapshot_document_that_is_not_a_retired_format_is_corrupt(raw: bytes) -> None:
    with pytest.raises(codec.SimulationRecordIntegrityError):
        codec.refuse_retired_document(raw)


@pytest.mark.parametrize("document", ["result", "snapshot"])
def test_a_previous_version_document_is_refused_by_scan_naming_its_invocation(
    tmp_path, document: str
) -> None:
    reports, _project_data, store = _retained_invocation(tmp_path)
    if document == "result":
        schema = _PREVIOUS_RESULT
        result_path = next(store.root.glob("work-items/*/result.json"))
        result = json.loads(result_path.read_bytes())
        result["$schema"] = schema
        result["executable_snapshot"] = result.pop("bundle_verification")
        _rewrite(result_path, canonical_json_bytes(result))
    else:
        schema = _SNAPSHOT_V2
        (attempt,) = store.root.glob("work-items/*/attempts/*")
        snapshot = attempt / "snapshot" / "snapshot.json"
        snapshot.parent.mkdir()
        snapshot.write_bytes((_FIXTURES / "simulation_record" / "snapshot.json").read_bytes())

    with pytest.raises(ValueError) as excinfo:
        store.scan()

    _assert_unsupported_type(excinfo.value)
    _assert_refused(str(excinfo.value), schema, reports / "sim/1")
