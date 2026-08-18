#!/usr/bin/env python3
"""Generate synthetic VCD files for cache comparison testing.

Creates 3 VCDs with different characteristics:
  1. small_clocked.vcd   — 50 cycles, 10 signals, clock+reset, clean edges
  2. medium_async.vcd    — 5000 transitions, 20 signals, no clock, async
  3. large_multiwidth.vcd — 200 cycles, 30 signals (1/8/16/32/64/256-bit),
                            clock+reset, x/z values, stuck signals
"""

import random
from pathlib import Path

OUT_DIR = Path(__file__).parent / "fixtures"
OUT_DIR.mkdir(parents=True, exist_ok=True)

random.seed(42)  # reproducible


def write_header(f, signals, timescale="1ns"):
    f.write(f"$timescale {timescale} $end\n")
    f.write("$scope module tb $end\n")
    f.write("$scope module dut $end\n")
    for sig_id, name, width, var_type in signals:
        if width == 1:
            f.write(f"$var {var_type} {width} {sig_id} {name} $end\n")
        else:
            f.write(f"$var {var_type} {width} {sig_id} {name} [{width - 1}:0] $end\n")
    f.write("$upscope $end\n")
    f.write("$upscope $end\n")
    f.write("$enddefinitions $end\n")


def write_scalar(f, sig_id, val):
    f.write(f"{val}{sig_id}\n")


def write_vector(f, sig_id, width, val):
    """val is an int or 'x'/'z'"""
    if isinstance(val, str):
        f.write(f"b{''.join([val] * width)} {sig_id}\n")
    else:
        f.write(f"b{val:0{width}b} {sig_id}\n")


# ── VCD 1: small_clocked ──────────────────────────────────────────────


def _write_small_clocked_signals(f, cycle, state):
    """Write signal updates for one cycle of the small_clocked VCD."""
    # Deassert reset at cycle 3
    if cycle == 2:
        write_scalar(f, '"', "1")

    # Signal activity after reset
    if cycle < 3:
        return

    # counter increments every cycle
    state["counter"] = (state["counter"] + 1) & 0xFFFF
    write_vector(f, "&", 16, state["counter"])

    # data_a changes every 3 cycles
    if cycle % 3 == 0:
        write_vector(f, "#", 8, random.randint(0, 255))

    # data_b changes every 5 cycles
    if cycle % 5 == 0:
        write_vector(f, "$", 8, random.randint(0, 255))

    # flag toggles every 7 cycles
    if cycle % 7 == 0:
        state["flag"] = 1 - state["flag"]
        write_scalar(f, "%", str(state["flag"]))

    # state: cycles through 0-7
    write_vector(f, "'", 4, (cycle - 3) % 8)

    # enable goes high at cycle 5, low at cycle 40
    if cycle == 5:
        write_scalar(f, "(", "1")
    elif cycle == 40:
        write_scalar(f, "(", "0")

    # addr changes every 2 cycles
    if cycle % 2 == 0:
        state["addr"] = (state["addr"] + 4) & 0xFF
        write_vector(f, ")", 8, state["addr"])

    # done pulses high at cycle 45
    if cycle == 45:
        write_scalar(f, "*", "1")
    elif cycle == 46:
        write_scalar(f, "*", "0")


def gen_small_clocked():
    path = OUT_DIR / "small_clocked.vcd"
    signals = [
        ("!", "clk", 1, "wire"),
        ('"', "rstn", 1, "wire"),
        ("#", "data_a", 8, "wire"),
        ("$", "data_b", 8, "wire"),
        ("%", "flag", 1, "wire"),
        ("&", "counter", 16, "reg"),
        ("'", "state", 4, "reg"),
        ("(", "enable", 1, "wire"),
        (")", "addr", 8, "wire"),
        ("*", "done", 1, "wire"),
    ]
    with path.open("w") as f:
        write_header(f, signals)

        # Initial values at #0
        f.write("#0\n")
        write_scalar(f, "!", "0")
        write_scalar(f, '"', "0")  # reset asserted (active-low)
        write_vector(f, "#", 8, 0)
        write_vector(f, "$", 8, 0)
        write_scalar(f, "%", "0")
        write_vector(f, "&", 16, 0)
        write_vector(f, "'", 4, 0)
        write_scalar(f, "(", "0")
        write_vector(f, ")", 8, 0)
        write_scalar(f, "*", "0")

        tick = 0
        state = {"counter": 0, "flag": 0, "addr": 0}

        for cycle in range(50):
            # Rising edge
            tick += 5
            f.write(f"#{tick}\n")
            write_scalar(f, "!", "1")

            _write_small_clocked_signals(f, cycle, state)

            # Falling edge
            tick += 5
            f.write(f"#{tick}\n")
            write_scalar(f, "!", "0")

    print(f"  {path} ({path.stat().st_size} bytes)")
    return path


# ── VCD 2: medium_async ───────────────────────────────────────────────


def gen_medium_async():
    path = OUT_DIR / "medium_async.vcd"
    signals = []
    for i in range(20):
        width = [1, 1, 8, 8, 16, 1, 8, 4, 1, 32][i % 10]
        var_type = "wire" if i % 3 != 0 else "reg"
        sig_id = chr(33 + i)  # !, ", #, ...
        name = f"sig_{i:02d}"
        signals.append((sig_id, name, width, var_type))

    with path.open("w") as f:
        write_header(f, signals, timescale="10ns")

        f.write("#0\n")
        vals = {}
        for sig_id, _name, width, _ in signals:
            if width == 1:
                write_scalar(f, sig_id, "0")
                vals[sig_id] = 0
            else:
                write_vector(f, sig_id, width, 0)
                vals[sig_id] = 0

        tick = 0
        for _t in range(5000):
            tick += random.randint(1, 20)
            f.write(f"#{tick}\n")

            # Each timestamp changes 1-4 signals
            n_changes = random.randint(1, 4)
            changed = random.sample(range(len(signals)), min(n_changes, len(signals)))
            for idx in changed:
                sig_id, _name, width, _ = signals[idx]
                if width == 1:
                    new_val = 1 - vals[sig_id]
                    write_scalar(f, sig_id, str(new_val))
                    vals[sig_id] = new_val
                else:
                    new_val = random.randint(0, (1 << width) - 1)
                    write_vector(f, sig_id, width, new_val)
                    vals[sig_id] = new_val

    print(f"  {path} ({path.stat().st_size} bytes)")
    return path


# ── VCD 3: large_multiwidth ───────────────────────────────────────────


def _write_large_multiwidth_signals(f, cycle, state):
    """Write signal updates for one cycle of the large_multiwidth VCD.

    Handles all 30 signals' per-cycle behavior after reset (cycle >= 5).
    """
    # Reset deasserts at cycle 5
    if cycle == 4:
        write_scalar(f, "A", "1")

    if cycle < 5:
        return

    # counter
    state["counter"] = (state["counter"] + 1) & 0xFFFF
    write_vector(f, "M", 16, state["counter"])

    # bit_sig toggles every 3 cycles
    if cycle % 3 == 0:
        write_scalar(f, "B", str(cycle % 2))

    # byte_data
    if cycle % 4 == 0:
        write_vector(f, "C", 8, random.randint(0, 255))

    # half_word
    if cycle % 6 == 0:
        write_vector(f, "D", 16, random.randint(0, 0xFFFF))

    # word_data
    if cycle % 8 == 0:
        write_vector(f, "E", 32, random.randint(0, 0xFFFFFFFF))

    # wide_bus
    if cycle % 10 == 0:
        write_vector(f, "F", 64, random.randint(0, (1 << 64) - 1))

    # huge_val (256-bit)
    if cycle % 20 == 0:
        write_vector(f, "G", 256, random.randint(0, (1 << 256) - 1))

    _write_large_flags(f, cycle)
    _write_large_events(f, cycle)
    _write_large_bus_and_control(f, cycle, state)


def _write_large_flags(f, cycle):
    """Write flag and status signal updates for large_multiwidth."""
    # flag_a: rises at specific cycles (for --find testing)
    if cycle in (10, 30, 50, 80, 120, 160):
        write_scalar(f, "K", "1")
    elif cycle in (11, 31, 51, 81, 121, 161):
        write_scalar(f, "K", "0")

    # flag_b: rises every 15 cycles
    if cycle % 15 == 0:
        write_scalar(f, "L", "1")
    elif cycle % 15 == 1:
        write_scalar(f, "L", "0")

    # status: specific values for --find
    if cycle % 10 == 0:
        write_vector(f, "N", 8, 0xFF)
    elif cycle % 10 == 5:
        write_vector(f, "N", 8, 0xAA)

    # xz_signal: some x/z values
    if cycle == 20:
        write_vector(f, "Q", 8, "x")
    elif cycle == 25:
        write_vector(f, "Q", 8, "z")
    elif cycle == 30:
        write_vector(f, "Q", 8, 0xAB)


def _write_large_events(f, cycle):
    """Write interrupt, error, and overflow signal updates for large_multiwidth."""
    # irq: pulse at specific cycles
    if cycle in (50, 100, 150):
        write_scalar(f, "S", "1")
    elif cycle in (51, 101, 151):
        write_scalar(f, "S", "0")

    # ack follows irq with 1 cycle delay
    if cycle in (51, 101, 151):
        write_scalar(f, "T", "1")
    elif cycle in (52, 102, 152):
        write_scalar(f, "T", "0")

    # error: rare
    if cycle == 99:
        write_scalar(f, "W", "1")
    elif cycle == 100:
        write_scalar(f, "W", "0")

    # overflow at cycle 180
    if cycle == 180:
        write_scalar(f, "b", "1")
    elif cycle == 181:
        write_scalar(f, "b", "0")


def _write_large_bus_and_control(f, cycle, state):
    """Write bus, handshake, and misc signal updates for large_multiwidth."""
    # addr_bus: sequential
    if cycle % 2 == 0:
        state["addr"] = (state["addr"] + 4) & 0xFFFFFFFF
        write_vector(f, "O", 32, state["addr"])

    # data_out: depends on addr
    if cycle % 2 == 0:
        write_vector(f, "P", 32, random.randint(0, 0xFFFFFFFF))

    # state_machine: 0->1->2->3->4->5->0
    write_vector(f, "R", 4, (cycle - 5) % 6)

    # valid/ready handshake
    if cycle % 12 == 0:
        write_scalar(f, "U", "1")
        write_scalar(f, "V", "0")
    elif cycle % 12 == 2:
        write_scalar(f, "V", "1")
    elif cycle % 12 == 3:
        write_scalar(f, "U", "0")
        write_scalar(f, "V", "0")

    # mode
    if cycle % 25 == 0:
        write_vector(f, "X", 2, random.randint(0, 3))

    # phase
    if cycle % 5 == 0:
        write_vector(f, "Y", 3, cycle % 8)

    # level
    if cycle % 3 == 0:
        write_vector(f, "Z", 8, min(cycle, 255))

    # result
    if cycle % 7 == 0:
        write_vector(f, "a", 32, random.randint(0, 0xFFFFFFFF))


def gen_large_multiwidth():
    path = OUT_DIR / "large_multiwidth.vcd"
    signals = [
        ("!", "clk", 1, "wire"),
        ("A", "rstn", 1, "wire"),
        ("B", "bit_sig", 1, "wire"),
        ("C", "byte_data", 8, "wire"),
        ("D", "half_word", 16, "reg"),
        ("E", "word_data", 32, "reg"),
        ("F", "wide_bus", 64, "wire"),
        ("G", "huge_val", 256, "wire"),
        ("H", "stuck_zero", 8, "wire"),  # never changes
        ("I", "stuck_one", 1, "wire"),  # never changes after init
        ("J", "stuck_x", 4, "wire"),  # stays x
        ("K", "flag_a", 1, "wire"),
        ("L", "flag_b", 1, "wire"),
        ("M", "counter", 16, "reg"),
        ("N", "status", 8, "reg"),
        ("O", "addr_bus", 32, "wire"),
        ("P", "data_out", 32, "wire"),
        ("Q", "xz_signal", 8, "wire"),  # has x/z transitions
        ("R", "state_machine", 4, "reg"),
        ("S", "irq", 1, "wire"),
        ("T", "ack", 1, "wire"),
        ("U", "valid", 1, "wire"),
        ("V", "ready", 1, "wire"),
        ("W", "error", 1, "wire"),
        ("X", "mode", 2, "reg"),
        ("Y", "phase", 3, "reg"),
        ("Z", "level", 8, "wire"),
        ("a", "result", 32, "reg"),
        ("b", "overflow", 1, "wire"),
        ("c", "underflow", 1, "wire"),
    ]

    with path.open("w") as f:
        write_header(f, signals)

        # $dumpvars section
        f.write("$dumpvars\n")
        write_scalar(f, "!", "0")
        write_scalar(f, "A", "0")  # reset asserted
        for sig_id, _, width, _ in signals[2:]:
            if sig_id == "I":
                write_scalar(f, sig_id, "1")
            elif sig_id == "J":
                write_vector(f, sig_id, width, "x")
            elif width == 1:
                write_scalar(f, sig_id, "0")
            else:
                write_vector(f, sig_id, width, 0)
        f.write("$end\n")

        tick = 0
        f.write(f"#{tick}\n")

        state = {"counter": 0, "addr": 0}

        for cycle in range(200):
            # Rising edge
            tick += 5
            f.write(f"#{tick}\n")
            write_scalar(f, "!", "1")

            _write_large_multiwidth_signals(f, cycle, state)

            # Falling edge
            tick += 5
            f.write(f"#{tick}\n")
            write_scalar(f, "!", "0")

    print(f"  {path} ({path.stat().st_size} bytes)")
    return path


if __name__ == "__main__":
    print("Generating test VCD files:")
    gen_small_clocked()
    gen_medium_async()
    gen_large_multiwidth()
    print("Done.")
