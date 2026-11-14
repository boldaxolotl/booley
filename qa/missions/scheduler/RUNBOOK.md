# Scheduler fixture recipes

This runbook gives the QA agent running the [scheduler mission](MISSION.md)
the staging, configuration fragments, reference numbers and observation
points. The mission says what to exercise; this file says exactly how.
`<asset>` below is `qa/missions/scheduler/` in the Booley checkout under test.

## Staging

1. Follow the booley-prj-caliptra README "Quick start" at the mission's pins
   in a fresh disposable directory under the run directory (add it to
   `resources.md`): clone Caliptra at the RTL pin with submodules, clone the
   Project pin as `.booley`, apply `.booley/patches/*.patch` on a `booley`
   branch, and `booley init`. A step the README gets wrong is a finding.
2. Copy `<asset>/fixtures/mem_hog.py` to `.booley/mcp_tools/mem_hog.py` and
   commit it in the Booley Directory.
3. Run every command through `booley session enter -- ...` from the host.
   For several Jobs at once, start each in its own host shell (backgrounded,
   its output in a file under the run directory) and note the start times.
4. Keep a copy of the staged `booley.toml` before area 2; every fragment below
   is undone by copying it back.

## Fixtures

| Path | Role |
|---|---|
| `<asset>/fixtures/mem_hog.py` | Custom Flow `mem_hog`: allocates and touches `--mb` MiB over `--ramp` seconds, holds it for `--hold` seconds, exits 0. Needs `--target`, as every Custom Flow does; use `--target sim_ecc`, so its history key is `sim_ecc` / `mem_hog` |
| `<asset>/fixtures/history_edit.py` | Shows, rewrites and restores `runtime/resources/history.json` records (`show`, `set --phase P [--target T] [--memory-mb N] [--wall-s S] [--finished-at T]`, `restore`). Run with `python3 -I`, only while no Job runs |

## Reference numbers

Peaks and wall times recorded on the maintainer's host (24 cores, 30 GB,
`[sandbox].memory = "28g"`) before this mission was written. They show
the scale, not an oracle: a large difference is worth a note, not a finding.

| Target | Phase | Peak memory | Longest wall time |
|---|---|---|---|
| `lint_ecc` | `lint` | 0.1 GB | 2 s |
| `lint_caliptra` | `lint` | 2.0 GB | 86 s |
| `sim_ecc` | `sim_build` / `sim_run` | 3.1 GB / 3.2 GB | 19 s / 415 s |
| `sim_caliptra` | `sim_build` / `sim_run` | 7.6 GB / 0.3 GB | 158 s / 208 s |
| `synth_ecc_top` | `yosys` / `openroad` | 2.7 GB / 2.1 GB | 59 s / 666 s |
| `synth_ecc_fau` | `yosys` / `openroad` | 5.9 GB / 3.6 GB | 119 s / 833 s |
| `synth_caliptra` | `yosys` / `openroad` | 17.5 GB / 12.0 GB | 1285 s / 1533 s |
| `fpga_ecc_top` | `fpga_impl` | 15.0 GB | 465 s |

An estimate is the largest recorded peak × 1.25. With `"28g"` the budget is
about 26 GB minus the measured agent and server memory: `sim_caliptra`'s build
(about 9.5 GB) and `synth_ecc_fau` (about 7.4 GB) fit together, and
`synth_caliptra` (about 22 GB) fits only alone. `fpga_ecc_top` (about 19 GB)
leaves room only for small Jobs.

## Record tests

Short `sim_caliptra` tests for area 7, each under three minutes:
`hello_world_iccm`, `smoke_test_sha256`, `smoke_test_sha512`,
`smoke_test_mbox`, `smoke_test_hmac`, `smoke_test_trng`, `smoke_test_kv`,
`iccm_lock`. Pass each with its own `--test`. `sim_ecc` Variants: `keygen`,
`sign`, `verify`, `ecdh`, `otf_reset`, `ECC_normal_test`.

## Forcing a rebuild

A repeat run reuses its Verilator build. To make a step build again, append
one comment line (`// qa-rebuild <n>`, a new `<n>` each time) to the Target's
testbench top in the staged checkout: `src/integration/tb/caliptra_top_tb.sv`
for `sim_caliptra`, `src/ecc/tb/ecc_top_tb.sv` for `sim_ecc`. The result says
why the build was or was not reused; check it rebuilt. Revert with
`git checkout -- <file>` when the area ends.

## Configuration fragments

Add each fragment to the staged `.booley/booley.toml`, and remove it when its
step ends.

| Name | Fragment | Use |
|---|---|---|
| `small-budget` | `[jobs]` `memory_budget = "<size>"`, a size string such as `"12g"` | Fit one Job and not two, or a set number of Record items |
| `int-budget` | `[jobs]` `memory_budget = 12` | Must fail: the value must be a size string |
| `queue-zero` | `[jobs]` `queue_max = 0` | A second blocked Job is refused |
| `retired` | `[jobs]` `max_heavy = 2` and `heavy_memory = "22g"` | Retired keys, refused with a fix hint |
| `seats` | Remove `[jobs.seats]`; then `vivado = 0`, `vivado = "x"`, `unknown_tool = 1` under `[jobs.seats]`, and `[jobs] seats = 1` | Seat Cap values, good and bad |
| `sandbox-oom` | `[sandbox]` `memory = "12g"`; restart the Session as `docs/user/SANDBOX.md` says, and set it back to `"28g"` after area 6 | Real OOM kills without squeezing the host |

## Where to look

| What | Where |
|---|---|
| Resource History | `booley resources` and `booley resources --json`; the file `.booley/runtime/resources/history.json` |
| Memory claims, holders and waiters | `.booley/runtime/jobs/slots/memory/`: `h-*.json` (running) and `w-*.json` (waiting), each with a `memory` object (`budget_mb`, `reservation_mb`, `run_alone`, `seats`, `wall_s`) and `queue_reason`. Copy them while a Job waits or runs; a small loop that copies the directory every few seconds into `evidence/` catches shrinks |
| Queue text on the CLI | stderr of the waiting `booley flow` run: a `[slot]` line about every 30 seconds |
| OOM priority | `cat /proc/<pid>/oom_score_adj` in the Sandbox for each tool process and its coordinator |
| OOM kills | `/sys/fs/cgroup/memory.events` in the Sandbox (`oom_kill` counter); the Flow's `[requeue]` line and its result |
| Verilator parallelism | The build log, or `ps` during the build: the `make` command line's `-j<N>` |
| Cores | `nproc` in the Sandbox, and `/sys/fs/cgroup/cpu.max` |
| Host health | `free -g` and `vmstat 5` on the host during areas 3, 6 and 11 |

Record times in UTC and keep every copied claim file and log under the run
directory's `evidence/`.

## Exact contention with `mem_hog`

- **No history, so a Job runs alone:** two `mem_hog` runs on an empty
  `mem_hog` history never overlap; the second waits to run alone.
- **Wall-clock for jump-ahead:** `history_edit.py set ... --phase mem_hog
  --wall-s <s>` gives `mem_hog` the wall time a step needs.
- **Double OOM:** under `sandbox-oom`, `mem_hog --mb 14000` exceeds the
  container limit alone, so both attempts are killed.
