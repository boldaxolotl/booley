"""Independent literal controls for UART oracle policy."""

import functools
import operator

import pytest

_FRAME_0X55 = [0, 1, 0, 1, 0, 1, 0, 1, 0, 1]  # start, 0x55 LSB first, stop
_CADENCE = (85, 85, 86)  # fractional bit lengths in source clocks


def _exact_trace() -> list[int]:
    """Known 0x55, 8N1, exact-a: start/data/stop, 64 source clocks per bit."""
    return [1] * 8 + functools.reduce(operator.iadd, ([bit] * 64 for bit in _FRAME_0X55), [])


def _corrupted_trace() -> list[int]:
    trace = _exact_trace()
    trace[72:136] = [0] * 64
    return trace


def _fractional_trace() -> list[int]:
    bits = _FRAME_0X55
    return [1] * 8 + [bit for i, bit in enumerate(bits) for _ in range(_CADENCE[i % 3])]


def _wrong_cadence_trace() -> list[int]:
    return [1] * 8 + [bit for bit in _FRAME_0X55 for _ in range(85)] + [1] * 20


def _extra_frame_trace() -> list[int]:
    frame = [bit for bit in _FRAME_0X55 for _ in range(64)]
    return [1] * 8 + frame + frame


def _phase_after_idle_trace(*, restart_phase: bool) -> list[int]:
    """Two frames split by one idle bit; ``restart_phase`` repeats the old phase bug."""
    from qa.missions.uart.evaluator.oracles import serial_bits

    first = [bit for i, bit in enumerate(serial_bits(0x55)) for _ in range(_CADENCE[i % 3])]
    shift = 10 if restart_phase else 11  # the idle bit advances the global phase by one
    second = [
        bit for i, bit in enumerate(serial_bits(0xAA)) for _ in range(_CADENCE[(i + shift) % 3])
    ]
    return first + [1] * 85 + second + [1] * 200


@pytest.mark.parametrize(
    ("trace", "payload", "divisor", "status"),
    [
        pytest.param(_exact_trace(), [0x55], 0x4000, "pass", id="exact-frame"),
        pytest.param(_corrupted_trace(), [0x55], 0x4000, "fail", id="payload-corruption"),
        pytest.param(_fractional_trace(), [0x55], 0x3000, "pass", id="fractional-cadence"),
        pytest.param(
            _wrong_cadence_trace(), [0x55], 0x3000, "fail", id="wrong-fractional-cadence"
        ),
        pytest.param(_extra_frame_trace(), [0x55], 0x4000, "fail", id="unrequested-extra-frame"),
        pytest.param(
            _phase_after_idle_trace(restart_phase=False),
            [0x55, 0xAA],
            0x3000,
            "pass",
            id="global-phase-across-idle",
        ),
        pytest.param(
            _phase_after_idle_trace(restart_phase=True),
            [0x55, 0xAA],
            0x3000,
            "fail",
            id="restarted-phase-after-idle",
        ),
    ],
)
def test_serial_oracle_grades_transmitted_frames(trace, payload, divisor, status):
    from qa.missions.uart.evaluator.oracles import check_tx

    assert check_tx(trace, payload, divisor)["status"] == status


def test_mmio_control_detects_one_defined_response_bit():
    from qa.missions.uart.evaluator.oracles import compare_mmio

    assert compare_mmio(0x101, 0x101, 0x1FF)
    assert not compare_mmio(0x100, 0x101, 0x1FF)


def test_manifest_keeps_every_byte_and_96_seeded_supplements():
    from qa.missions.uart.evaluator.cases import materialize

    manifest = materialize("0123456789abcdef0123456789abcdef")
    cases = manifest["cases"]
    assert len([c for c in cases if ".seed.i" in c["id"]]) == 96
    assert len([c for c in cases if "UART-TXRX.tx.b" in c["id"]]) == 256
    assert len([c for c in cases if "UART-TXRX.rx.b" in c["id"]]) == 256
    assert manifest == materialize("0123456789abcdef0123456789abcdef")
    assert not any("FIFO_STATUS.reset-defined" in c["id"] for c in cases)
    assert not any("RDATA.reset-defined" in c["id"] for c in cases)


def test_seed_stream_has_literal_known_answer():
    from qa.missions.uart.evaluator.cases import seed_bytes

    # SHA-256 of the reviewed v1 message for (seed, "UART-TXRX", index 0, block 0).
    expected = "1508a18e2e6c4ce0391198801df46b6df23a30f9fdd665c7a77cffc1d2764a78"
    assert seed_bytes("0123456789abcdef0123456789abcdef", "UART-TXRX", 0, 32).hex() == expected


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_compiler_snapshot_rejects_unhashed_and_modified_includes(tmp_path, newline):
    import hashlib
    import os

    from qa.missions.uart.evaluator.inputs import snapshot
    from tests.subprocess_budget import run_bounded_child

    root = tmp_path / "candidate"
    root.mkdir()
    (root / "uart.sv").write_bytes(
        newline.join([b'`include "constants.svh"', b"module qa_uart; endmodule", b""])
    )
    (root / "constants.svh").write_bytes(b"`define CONSTANT 1" + newline)
    git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    git_env.update(
        {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TEMPLATE_DIR": ""}
    )
    git_command = [
        "git",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "init.templateDir=",
        "-C",
        str(root),
    ]
    for args in [
        ["init"],
        ["add", "."],
        [
            "-c",
            "user.name=QA",
            "-c",
            "user.email=qa@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "accepted",
        ],
    ]:
        run_bounded_child([*git_command, *args], check=True, env=git_env)
    commit = run_bounded_child(
        [*git_command, "rev-parse", "HEAD"], check=True, env=git_env
    ).stdout.strip()
    entries = [
        {"path": name, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()}
        for name in ["uart.sv", "constants.svh"]
    ]
    candidate = {"root": str(root), "commit": commit, "sources": entries[:1]}
    with pytest.raises(ValueError, match="explicitly hashed"):
        snapshot(candidate, tmp_path / "operator", tmp_path / "missing")
    candidate["include_files"] = entries[1:]
    snapshot(candidate, tmp_path / "operator", tmp_path / "frozen")
    (root / "constants.svh").write_text("`define CONSTANT 2\n")
    assert (tmp_path / "frozen/constants.svh").read_bytes() == b"`define CONSTANT 1" + newline
    with pytest.raises(ValueError, match="committed digest"):
        snapshot(candidate, tmp_path / "operator", tmp_path / "changed")


def test_manifest_publication_retains_existing_and_never_leaves_partial_json(tmp_path):
    from qa.missions.uart.evaluator.publication import publish_new

    path = tmp_path / "manifest.json"
    with pytest.raises(TypeError):
        publish_new(path, {"not_serializable": object()})
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []
    publish_new(path, {"seed": "first"})
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        publish_new(path, {"seed": "second"})
    assert path.read_bytes() == original
