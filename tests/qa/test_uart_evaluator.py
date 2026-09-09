"""Independent literal controls for UART oracle policy."""

import functools
import operator

import pytest


def test_mmio_control_detects_one_defined_response_bit():
    from qa.scenarios.uart.evaluator.oracles import compare_mmio

    assert compare_mmio(0x101, 0x101, 0x1FF)
    assert not compare_mmio(0x100, 0x101, 0x1FF)
    assert compare_mmio(0x101, 0x101, 0x1FF)


def test_serial_control_detects_payload_corruption_and_restoration():
    from qa.scenarios.uart.evaluator.oracles import check_tx

    # Known 0x55, 8N1, exact-a: start/data/stop, 64 source clocks per bit.
    trace = [1] * 8 + functools.reduce(
        operator.iadd, ([bit] * 64 for bit in [0, 1, 0, 1, 0, 1, 0, 1, 0, 1]), []
    )
    assert check_tx(trace, [0x55], 0x4000)["status"] == "pass"
    corrupt = trace.copy()
    corrupt[72:136] = [0] * 64
    assert check_tx(corrupt, [0x55], 0x4000)["status"] == "fail"
    assert check_tx(trace, [0x55], 0x4000)["status"] == "pass"


def test_manifest_keeps_every_byte_and_96_seeded_supplements():
    from qa.scenarios.uart.evaluator.cases import materialize

    manifest = materialize("0123456789abcdef0123456789abcdef")
    cases = manifest["cases"]
    assert len([c for c in cases if ".seed.i" in c["id"]]) == 96
    assert len([c for c in cases if "UART-TXRX.tx.b" in c["id"]]) == 256
    assert len([c for c in cases if "UART-TXRX.rx.b" in c["id"]]) == 256
    assert manifest == materialize("0123456789abcdef0123456789abcdef")
    assert not any("FIFO_STATUS.reset-defined" in c["id"] for c in cases)
    assert not any("RDATA.reset-defined" in c["id"] for c in cases)


def test_serial_oracle_rejects_wrong_fractional_cadence():
    from qa.scenarios.uart.evaluator.oracles import check_tx

    bits = [0, 1, 0, 1, 0, 1, 0, 1, 0, 1]
    trace = [1] * 8 + [bit for i, bit in enumerate(bits) for _ in range([85, 85, 86][i % 3])]
    assert check_tx(trace, [0x55], 0x3000)["status"] == "pass"
    wrong = [1] * 8 + [bit for bit in bits for _ in range(85)]
    assert check_tx(wrong + [1] * 20, [0x55], 0x3000)["status"] == "fail"


def test_seed_stream_has_literal_known_answer():
    import hashlib

    from qa.scenarios.uart.evaluator.cases import seed_bytes

    expected = hashlib.sha256(
        b"booley.qa.uart.seed.v1\n0123456789abcdef0123456789abcdef\nUART-TXRX\n00000000\n00000000\n"
    ).digest()
    assert seed_bytes("0123456789abcdef0123456789abcdef", "UART-TXRX", 0, 32) == expected


def test_serial_oracle_rejects_unrequested_extra_frame():
    from qa.scenarios.uart.evaluator.oracles import check_tx

    frame = [bit for bit in [0, 1, 0, 1, 0, 1, 0, 1, 0, 1] for _ in range(64)]
    assert check_tx([1] * 8 + frame + frame, [0x55], 0x4000)["status"] == "fail"


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_compiler_snapshot_rejects_unhashed_and_modified_includes(tmp_path, newline):
    import hashlib
    import os
    import subprocess

    import pytest
    from qa.scenarios.uart.evaluator.inputs import snapshot

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
        subprocess.run(
            [*git_command, *args], check=True, capture_output=True, timeout=10, env=git_env
        )
    commit = subprocess.run(
        [*git_command, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
        env=git_env,
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
    import pytest
    from qa.scenarios.uart.evaluator.publication import publish_new

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


def test_fractional_tx_keeps_global_phase_across_legal_idle():
    from qa.scenarios.uart.evaluator.oracles import check_tx, serial_bits

    cadence = (85, 85, 86)
    bits = [*serial_bits(0x55), 1, *serial_bits(0xAA)]
    trace = [bit for index, bit in enumerate(bits) for _ in range(cadence[index % 3])]
    assert check_tx(trace + [1] * 200, [0x55, 0xAA], 0x3000)["status"] == "pass"


def test_fractional_tx_rejects_restarted_phase_after_idle():
    from qa.scenarios.uart.evaluator.oracles import check_tx, serial_bits

    cadence = (85, 85, 86)
    first = [bit for i, bit in enumerate(serial_bits(0x55)) for _ in range(cadence[i % 3])]
    # This second frame ignores the elapsed idle bit and repeats the old bug's phase.
    wrong = [bit for i, bit in enumerate(serial_bits(0xAA)) for _ in range(cadence[(i + 10) % 3])]
    assert check_tx(first + [1] * 85 + wrong + [1] * 200, [0x55, 0xAA], 0x3000)["status"] == "fail"
