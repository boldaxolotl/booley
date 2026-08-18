#!/usr/bin/env python3
"""Generate a synthetic Xcelium-dialect VCD fixture.

The formatting below is frozen from a real `xmsim(64) 21.03-s001` runtime dump
(validated against bwave on 2026-07-03). Only the *format* is replicated — the
design hierarchy, signal names, and values are invented, so the fixture is safe
to commit (the real dump came from proprietary RTL and cannot be).

Xcelium dialect features reproduced:
  - `$version` body `TOOL:\txmsim(64)\t21.03-s001`
  - `$timescale` with a space: `1 ns`
  - `$var parameter` entries, including very wide string parameters
  - `$var integer` entries, including array-element names (`cnt_arr[0]`)
  - bit-blasted wide nets: per-bit 1-bit `$var wire` entries named with a
    space before the index (`wide_bus [1343]`), 4-digit indices included
  - single-element blast (`chunk_ofs [0]`)
  - column-aligned `$var` type/width fields, multi-char short IDs
  - `$dumpvars ... $end` initial-value block

Run from tests/:  python3 gen_xcelium_dialect_vcd.py
Writes fixtures/test_xcelium_dialect.vcd
"""

from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parent / "fixtures" / "test_xcelium_dialect.vcd"

WIDE_BITS = 1344  # bit-blasted net; real dumps blast >1000-bit nets per-bit


def make_id(n: int) -> str:
    """Short-ID generator: printable ASCII 33..126, little-endian like VCD tools."""
    chars = []
    n += 1
    while n > 0:
        n -= 1
        chars.append(chr(33 + (n % 94)))
        n //= 94
    return "".join(chars)


def var(kind: str, width: int, vid: str, name: str) -> str:
    # column layout frozen from the real xmsim dump:
    # $var parameter 72 !    current_dir $end
    # $var reg       1 &    clk $end
    # $var wire     32 s\   in [31:0] $end
    return f"$var {kind:<9} {width:>2} {vid:<4} {name} $end\n"


def lfsr_stream(seed: int):
    """Deterministic 32-bit xorshift stream (no RNG imports, stable forever)."""
    state = seed & 0xFFFFFFFF
    while True:
        state ^= (state << 13) & 0xFFFFFFFF
        state ^= state >> 17
        state ^= (state << 5) & 0xFFFFFFFF
        yield state


def main() -> None:
    ids = iter(make_id(i) for i in range(10_000))
    header: list[str] = []
    header.append("$date\n    Jul  3, 2026  14:01:23\n$end\n")
    header.append("$version\n    TOOL:\txmsim(64)\t21.03-s001\n$end\n")
    header.append("$timescale\n    1 ns\n$end\n\n")

    # ── scope tb_dialect_top ────────────────────────────────────────────
    header.append("$scope module tb_dialect_top $end\n")
    id_param_str = next(ids)
    header.append(var("parameter", 72, id_param_str, "run_tag"))
    id_param_wide = next(ids)
    header.append(var("parameter", 992, id_param_wide, "vector_names [23:0]"))
    id_count = next(ids)
    header.append(var("parameter", 32, id_count, "COUNT"))
    id_clk = next(ids)
    header.append(var("reg", 1, id_clk, "clk"))
    id_rstn = next(ids)
    header.append(var("reg", 1, id_rstn, "rstn"))
    id_err = next(ids)
    header.append(var("integer", 32, id_err, "error_cnt"))
    id_arr0 = next(ids)
    header.append(var("integer", 32, id_arr0, "cnt_arr[0]"))
    id_arr1 = next(ids)
    header.append(var("integer", 32, id_arr1, "cnt_arr[1]"))

    # ── scope dut / engine ──────────────────────────────────────────────
    header.append("$scope module dut $end\n")
    id_bus = next(ids)
    header.append(var("wire", 32, id_bus, "data_bus [31:0]"))
    id_state = next(ids)
    header.append(var("reg", 3, id_state, "state [2:0]"))
    id_chunk = next(ids)
    header.append(var("wire", 1, id_chunk, "chunk_ofs [0]"))  # 1-bit blast
    header.append("$scope module engine $end\n")
    wide_ids = [next(ids) for _ in range(WIDE_BITS)]
    # real dumps emit blasted bits high-to-low
    for bit in range(WIDE_BITS - 1, -1, -1):
        header.append(var("wire", 1, wide_ids[bit], f"wide_bus [{bit}]"))
    header.append("$upscope $end\n")  # engine
    header.append("$upscope $end\n")  # dut
    header.append("$upscope $end\n")  # tb_dialect_top
    header.append("$enddefinitions $end\n")

    body: list[str] = []
    # ── $dumpvars initial-value block ───────────────────────────────────
    body.append("$dumpvars\n")
    body.append(f"b{'01000101' * 9} {id_param_str}\n")  # 72-bit string param
    body.append(f"b{'0110' * 248} {id_param_wide}\n")  # 992-bit string param
    body.append(f"b101 {id_count}\n")
    body.append(f"0{id_clk}\n")
    body.append(f"0{id_rstn}\n")
    body.append(f"b0 {id_err}\n")
    body.append(f"bx {id_arr0}\n")  # x-valued integer
    body.append(f"bx {id_arr1}\n")
    body.append(f"bx {id_bus}\n")
    body.append(f"b0 {id_state}\n")
    body.append(f"0{id_chunk}\n")
    for vid in wide_ids:
        body.append(f"x{vid}\n")
    body.append("$end\n")

    # ── value changes: 400 clock half-periods of 5 ns ───────────────────
    rnd = lfsr_stream(0xB00113)
    t = 0
    for half in range(1, 401):
        t += 5
        body.append(f"#{t}\n")
        body.append(f"{half % 2}{id_clk}\n")
        if half == 20:
            body.append(f"1{id_rstn}\n")
        if half % 2 and half > 20:  # posedge activity after reset
            r = next(rnd)
            body.append(f"b{r:b} {id_bus}\n")
            body.append(f"b{(r % 6):b} {id_state}\n")
            body.append(f"{(r >> 3) & 1}{id_chunk}\n")
            body.append(f"b{half // 2:b} {id_err}\n")
            body.append(f"b{half:b} {id_arr0}\n")
            body.append(f"b{half * 3:b} {id_arr1}\n")
            # touch a deterministic spread of blasted bits, incl. bit 1343
            for k in range(8):
                bit = (r >> (k * 4)) % WIDE_BITS if k else WIDE_BITS - 1
                body.append(f"{(r >> k) & 1}{wide_ids[bit]}\n")

    OUT.write_text("".join(header) + "".join(body))
    n_vars = sum(1 for line in header if line.startswith("$var"))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {n_vars} vars, end time {t} ns)")


if __name__ == "__main__":
    main()
