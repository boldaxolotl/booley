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


def _match_frame(
    trace: Sequence[int], start: int, bits: list[int], lengths: tuple, phases: set[int]
) -> tuple[set[int], int]:
    matched = set()
    last = start
    for phase in phases:
        position = start
        good = True
        for index, bit in enumerate(bits):
            stop = position + lengths[(phase + index) % len(lengths)]
            if stop > len(trace) or any(value != bit for value in trace[position:stop]):
                good = False
                break
            position = stop
        if good:
            matched.add(phase)
            last = position
    return matched, last


def check_tx(
    trace: Sequence[int], payload: Sequence[int], nco: int, parity: str = "disabled"
) -> dict:
    """Check serial bits and one consistent cadence phase, with bounded launch."""
    if nco not in RATES or not payload or any(value not in (0, 1) for value in trace):
        raise ValueError("Expected nonempty payload, binary source-clock trace and selected NCO")
    lengths = RATES[nco]
    longest = max(lengths)
    cursor = 0
    phases = set(range(len(lengths)))
    for byte in payload:
        limit = min(len(trace), cursor + 2 * longest + 1)
        start = next((index for index in range(cursor, limit) if trace[index] == 0), None)
        if start is None:
            return {
                "status": "fail" if len(trace) >= cursor + 2 * longest else "blocked",
                "reason": "TX first/next START liveness",
                "cycle": cursor,
            }
        bits = serial_bits(byte, parity)
        if len(trace) - start < sum(lengths[i % len(lengths)] for i in range(len(bits))):
            return {"status": "blocked", "reason": "Incomplete serial observation", "cycle": start}
        phases, cursor = _match_frame(trace, start, bits, lengths, phases)
        if not phases:
            return {
                "status": "fail",
                "reason": "Serial payload/parity/stop or cadence mismatch",
                "cycle": start,
            }
        phases = {(phase + len(bits)) % len(lengths) for phase in phases}
    if any(value != 1 for value in trace[cursor:]):
        return {"status": "fail", "reason": "Unrequested serial activity after complete payload"}
    return {
        "status": "pass",
        "reason": "Complete independent serial decode and cadence",
        "cycles": cursor,
        "legal_phases": sorted(phases),
    }


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
