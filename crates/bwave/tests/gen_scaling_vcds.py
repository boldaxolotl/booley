#!/usr/bin/env python3
"""Generate the synthetic scaling corpus for the FST-vs-bwave benchmark
(FST migration plan, Phase 6).

Tiers (approximate on-disk VCD size, controlled signal counts + activity):
  scaling_10mb.vcd     ~10 MB,  60 signals, moderate activity
  scaling_100mb.vcd    ~100 MB, 120 signals, moderate activity
  scaling_highact.vcd  ~100 MB, 40 signals, high-activity clocked design —
                       every signal toggles every cycle (worst case for
                       async-only storage; the old sync-sparse encoding's
                       best case)
  scaling_1gb.vcd      ~1 GB, 200 signals (opt-in via --tier 1gb)

Outputs land in tests/fixtures/ (scaling_*.vcd is gitignored).
"""

import argparse
import random
from pathlib import Path

OUT_DIR = Path(__file__).parent / "fixtures"


def _ids(n):
    """n printable single/multi-char VCD ids, skipping whitespace."""
    alphabet = [chr(c) for c in range(33, 127)]
    out = []
    i = 0
    while len(out) < n:
        if i < len(alphabet):
            out.append(alphabet[i])
        else:
            out.append(alphabet[i % len(alphabet)] + alphabet[i // len(alphabet)])
        i += 1
    return out


def gen(path: Path, *, n_signals: int, target_bytes: int, activity: float, seed: int = 42) -> None:
    rng = random.Random(seed)
    widths = [1, 1, 1, 8, 8, 16, 32, 32, 64]
    sigs = []
    for i, sid in enumerate(_ids(n_signals)):
        w = 1 if i == 0 else widths[i % len(widths)]  # sig 0 is the clock
        sigs.append((sid, f"sig_{i:03d}", w))

    with path.open("w", newline="\n") as f:
        f.write("$timescale 1ns $end\n$scope module tb $end\n")
        f.write('$var wire 1 ! clk $end\n$var wire 1 " rstn $end\n')
        f.write("$scope module dut $end\n")
        for sid, name, w in sigs[2:]:
            if w == 1:
                f.write(f"$var wire 1 {sid} {name} $end\n")
            else:
                f.write(f"$var wire {w} {sid} {name} [{w - 1}:0] $end\n")
        f.write("$upscope $end\n$upscope $end\n$enddefinitions $end\n")

        # dumpvars-style init
        f.write('#0\n$dumpvars\n0!\n0"\n')
        for sid, _name, w in sigs[2:]:
            f.write(f"b{'x' * w} {sid}\n" if w > 1 else f"x{sid}\n")
        f.write("$end\n")

        vals = [0] * n_signals
        t = 0
        cycle = 0
        # rough bytes/cycle estimate updated as we go
        while f.tell() < target_bytes:
            # rising edge
            t += 5
            f.write(f"#{t}\n1!\n")
            if cycle == 2:
                f.write('1"\n')
            if cycle >= 3:
                for i in range(2, n_signals):
                    if rng.random() >= activity:
                        continue
                    sid, _name, w = sigs[i]
                    if w == 1:
                        vals[i] ^= 1
                        f.write(f"{vals[i]}{sid}\n")
                    else:
                        vals[i] = rng.getrandbits(w)
                        f.write(f"b{vals[i]:b} {sid}\n")
            # falling edge
            t += 5
            f.write(f"#{t}\n0!\n")
            cycle += 1


TIERS = {
    "10mb": {"n_signals": 60, "target_bytes": 10 * 2**20, "activity": 0.3},
    "100mb": {"n_signals": 120, "target_bytes": 100 * 2**20, "activity": 0.3},
    "highact": {"n_signals": 40, "target_bytes": 100 * 2**20, "activity": 1.0},
    "1gb": {"n_signals": 200, "target_bytes": 1024 * 2**20, "activity": 0.3},
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--tier",
        action="append",
        choices=list(TIERS),
        help="tiers to generate (default: 10mb 100mb highact)",
    )
    args = ap.parse_args()
    tiers = args.tier or ["10mb", "100mb", "highact"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for tier in tiers:
        path = OUT_DIR / f"scaling_{tier}.vcd"
        print(f"generating {path.name} ...", flush=True)
        gen(path, **TIERS[tier])
        print(f"  {path.stat().st_size / 2**20:.1f} MB")


if __name__ == "__main__":
    main()
