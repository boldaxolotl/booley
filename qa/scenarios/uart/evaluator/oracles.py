"""Independent observations derived only from the frozen public UART contract."""

import math
from collections.abc import Sequence

RATES = {0x4000: (64,), 0x2000: (128,), 0x3000: (85, 85, 86)}


def compare_mmio(observed: int, expected: int, mask: int = 0xFFFFFFFF) -> bool:
    """Compare defined response bits; unspecified reset bits are never scored."""
    return observed & mask == expected & mask


def serial_bits(byte: int, parity: str = "disabled") -> list[int]:
    """Return the specified LSB-first start/data/parity/stop frame."""
    if type(byte) is not int or not 0 <= byte <= 255 or parity not in {"disabled", "even", "odd"}:
        raise ValueError("Invalid public serial stimulus")
    bits = [0, *[(byte >> bit) & 1 for bit in range(8)]]
    if parity != "disabled":
        bits.append((byte.bit_count() & 1) ^ (parity == "odd"))
    return [*bits, 1]


def _frame_end(trace: Sequence[int], start: int, bits: list[int], nco: int, residue: int) -> int:
    position = start
    for index, bit in enumerate(bits):
        stop = start + math.ceil(((index + 1) * 1048576 - residue) / nco)
        if stop > len(trace) or any(value != bit for value in trace[position:stop]):
            return -1
        position = stop
    return position


def _tx_phase(
    trace: Sequence[int], payload: Sequence[int], nco: int, parity: str, initial: int
) -> dict:
    cursor, origin = 0, None
    longest = max(RATES[nco])
    for byte in payload:
        start = next(
            (i for i in range(cursor, min(len(trace), cursor + 2 * longest + 1)) if trace[i] == 0),
            None,
        )
        if start is None:
            return {
                "status": "fail" if len(trace) >= cursor + 2 * longest else "blocked",
                "reason": "TX first/next START liveness",
                "cycle": cursor,
            }
        if origin is None:
            origin = start
        residue = (initial + (start - origin) * nco) % 65536
        if residue >= nco:
            return {
                "status": "fail",
                "reason": "START is inconsistent with the shared NCO phase",
                "cycle": start,
            }
        bits = serial_bits(byte, parity)
        needed = math.ceil((len(bits) * 1048576 - residue) / nco)
        if len(trace) - start < needed:
            return {"status": "blocked", "reason": "Incomplete serial observation", "cycle": start}
        cursor = _frame_end(trace, start, bits, nco, residue)
        if cursor < 0:
            return {
                "status": "fail",
                "reason": "Serial payload/parity/stop or cadence mismatch",
                "cycle": start,
            }
    if any(value != 1 for value in trace[cursor:]):
        return {"status": "fail", "reason": "Unrequested serial activity after complete payload"}
    return {
        "status": "pass",
        "reason": "Complete independent serial decode and cadence",
        "cycles": cursor,
        "initial_accumulator_residue": initial,
    }


def check_tx(
    trace: Sequence[int], payload: Sequence[int], nco: int, parity: str = "disabled"
) -> dict:
    """Keep one accumulator phase over frame bits and all elapsed idle clocks."""
    if nco not in RATES or not payload or any(value not in (0, 1) for value in trace):
        raise ValueError("Expected nonempty payload, binary source-clock trace and selected NCO")
    outcomes = [
        _tx_phase(trace, payload, nco, parity, residue)
        for residue in range(0, nco, math.gcd(nco, 65536))
    ]
    for status in ["pass", "blocked", "fail"]:
        for outcome in outcomes:
            if outcome["status"] == status:
                return outcome
    raise AssertionError("Selected rates must have accumulator phases")


def check_val(rx: Sequence[int], reads: Sequence[tuple[int, int]], nco: int) -> bool:
    """Fit a single legal NCO phase and synchronization delay to all VAL reads."""
    if nco not in RATES or not reads:
        raise ValueError("VAL requires selected NCO and nonempty observation")
    if any(cycle < 0 or cycle >= len(rx) or not 0 <= value <= 65535 for cycle, value in reads):
        raise ValueError("Invalid VAL observation")
    max_delay = math.ceil(max(RATES[nco]) / 4)
    # Distinct phases are equivalent between NCO multiples; enumerate their residues.
    for phase in range(0, 65536, math.gcd(nco, 65536)):
        for delay in range(max_delay + 1):
            if _val_alignment(rx, reads, nco, phase, delay):
                return True
    return False


def _val_alignment(
    rx: Sequence[int], reads: Sequence[tuple[int, int]], nco: int, phase: int, delay: int
) -> bool:
    observed = dict(reads)
    history, samples, accumulator = 0, 0, phase
    for cycle, _ in enumerate(rx):
        accumulator += nco
        if accumulator >= 65536:
            accumulator -= 65536
            if cycle >= delay:
                history = ((history << 1) | rx[cycle - delay]) & 65535
                samples += 1
        if cycle in observed and (samples < 16 or history != observed[cycle]):
            return False
    return True
