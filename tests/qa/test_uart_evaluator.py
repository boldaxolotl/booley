"""Independent literal controls for UART oracle policy."""

import functools
import operator


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
