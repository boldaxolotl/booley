# Persistent compiler cache validation (#879)

Validated on 30 SEP 2026 with a pinned public PicoRV32 CPU, real Booley Simulation
execution, and host-sealed Sandbox specifications rendered through the Docker CLI
translation. [Machine-readable evidence](879-compiler-cache.json) records all
47 successful measured builds, compiler commands, generated C++ identities,
Make wall/child CPU times, total wall times, observed values, and cache statistics.
Timing is evidence, never a normal CI assertion.

## Inputs and method

- Public source: [YosysHQ/picorv32](https://github.com/YosysHQ/picorv32/tree/ef203c2b0a3fb793280f5114941416c425c5b461),
  revision `ef203c2b0a3fb793280f5114941416c425c5b461`, ISC licensed. The RTL is
  supplied separately to the test harness; no third-party RTL is redistributed.
- Baseline Python source matches main `08cad954`; the local source-development
  branch originally started at `471ab35c`. The Sandbox Image is pinned by full
  digest in the JSON evidence: Verilator 5.052 (`ea338be`), ccache 4.9.1,
  Python 3.13.15, GCC 13.3.0, GNU Make 4.3, FuseSoC 2.4.7, Edalize 0.6.8,
  and Cocotb 2.1.0. Candidate source was copied into
  the authorized workspace and imported via `PYTHONPATH` for every measurement.
- CPU program: load operands 6 and 3, execute XOR, store its result at address
  256. A one-file behavioral edit changes the CPU's XOR ALU to include a revision
  mask. The independent expected result is `5 ^ revision`; both HDL and Cocotb
  testbenches assert it, and the host independently checks the printed value.
- Each repetition has a new physical Project cache. Baseline uses the image's
  home cache; candidate uses the fixed issued Project-data cache. Initial cold
  builds, unchanged fresh generations, previously unseen same-Sandbox edits,
  and previously unseen edits after container destruction/recreation are measured.
  The before/after order alternates across three repetitions. No cache counters
  are reset and no shared cache is cleared between writers.
- Authored `-j2` bounds compilation identically before and after. The test-only
  Make wrapper delegates to `/usr/bin/make`, records recursive C++ Make wall time
  and aggregate child CPU time, and preserves job arguments. Individual process
  timeouts bound all builds. Host scheduling was shared with other work; report
  distributions instead of asserting a percentage threshold.

## Ordinary CPU performance

| Build stage | Baseline seconds (three runs) | Candidate seconds (three runs) |
|---|---|---|
| Cold | 5.378, 6.077, 5.623 | 5.931, 6.414, 5.520 |
| Edited, same Sandbox | 0.874, 0.863, 0.838 | 0.900, 0.984, 0.842 |
| Unseen edit, recreated Sandbox | 5.581, 6.627, 5.623 | 0.931, 0.907, 0.855 |

Candidate same-Sandbox and recreated-Sandbox medians are 0.900 and 0.907 seconds.
The recreated baseline median is 5.623 seconds: a 6.2-times median improvement.
Same-Sandbox default ccache was already effective before this change; the new
policy preserves that behavior. Relative to candidate cold median 5.931 seconds,
its same-Sandbox edit is 6.6 times faster. Improvement exceeds observed variation.
Cold and unchanged builds have 0/13 and 13/13 cache hits; previously unseen edits
have 11/13 hits. Recorded generated C++ lives in distinct fresh `g/<id>` paths.
Every run prints the correct edited CPU result; no old image is accepted.

## Variants and ownership

Trace and coverage variants produce fresh artifacts and correct edited results.
Candidate trace improves from 4.580 to 1.080 seconds (15 hits, 2 misses after edit);
coverage from 5.812 to 1.191 seconds (16 hits, 2 misses). Cocotb preserves selected
`check` and its Target environment, with 15 hits and 2 misses after editing the
CPU. Matched pre-change trace, coverage, and Cocotb variants also passed; their
raw paired measurements are included. First variant switches legitimately miss.

Two real Git Ticket-style worktrees compiled overlapping edits in two separately
created issued Sandboxes for the same Project. Both printed their independent
expected results (13 and 12), used distinct generations, and shared the same
physical cache. Their compiler environments applied their own `1G` and `5G`
settings. Shared-counter deltas during overlap are **diagnostic only**, not exact
per-build hit counts. The recorded host intervals prove overlap. Sandbox specs
were issued without any additional cache mount, and recreation preserved entries.

Elaboration-only preparation compiled the CPU successfully using the same policy.
A Target PATH containing the required compiler utilities but no ccache compiled
successfully without any `ccache g++` invocation. Disabled CPU builds likewise
compile successfully and produce zero new compiler-cache hits/misses. Boundary
regressions cover inaccessible/link/special-file storage and preserve entries.

The installed-image Flow acceptance gate passed all **15 real ordinary/coverage
verdict pairs** across generated main, custom C++ main, and Cocotb, including
pass, assertion failure, compiler rejection, timeout, and signal termination.
Existing campaign, Cycle Count, Icarus identity/reuse, preview, issuance refresh,
and legacy snapshot-copy suites pass with the full test suite. Autonomous agent
sessions were not launched for this measurement: their inherited environment
routes were audited and are exercised by existing backend/review tests.

## Debug paths and invalidation

An initial unnormalized `CXXFLAGS=-g` edit had zero compiler hits and took 6.490
seconds. ccache logs/compiler recipes and its
[4.9.1 directory-hashing contract](https://ccache.dev/manual/4.9.1.html#_compiling_in_different_directories)
identify the fresh compiler working directory as a debug-cache key input.
The implemented normalization sets `CCACHE_BASEDIR` to the generated build
root and appends `-fdebug-prefix-map=<that-root>=.` through `USER_CPPFLAGS`.
It preserves user flags and semantic file paths and does not disable hashing.
With this policy, the debug edit hits 11/13 entries and takes 1.054 seconds versus
7.329 seconds cold. A further debug edit in a different Ticket-style worktree after Sandbox recreation
also hit 11/13 entries, compiled in 1.364 seconds, and printed the independently
expected value 14. Debuggers should search the current generated build directory.

An isolated native compiler probe additionally changed an included header, a
compiler define, and the compiler wrapper's content while preserving its size
and mtime. Independent outputs changed from 1 to 2, 5, 9, and 10 as expected.
`CCACHE_COMPILERCHECK=content` invalidated the same-size/same-mtime compiler change.
This probe checks compiler-cache invalidation; simulator image authorization is
separately exercised by the real Simulation runs and existing identity tests.

## Size and cleanup

An isolated `1M` Project ran three real CPU builds and 180 bounded small native
compilations. Compiler environments and ccache configuration confirmed the finite
1 MB target. Automatic eviction occurred **48** times without a forced cleanup
or any shared `ccache.conf` rewrite. The sparse cache still held 22,656 KiB and
283 files: ccache 4.9.1 cleans selected subdirectories and can leave a small cache
above its target. This is measured soft eviction behavior, not a hard quota or
an assertion that a sibling's smallest target governs the whole Project.

Generation disposal between completed lab batches preserved the shared cache;
subsequent builds reused its entries. Snapshot regression tests preserve adjacent
authored/runtime files while excluding only the legacy compiler-cache subtree.

## Reproduce and verification

Fetch the pinned public `picorv32.v`, then run
`tests/docker/compiler_cache_acceptance.py --rtl <source> --project <case>` inside
an issued candidate Sandbox. The `compiler_cache_make_probe.py` helper must sit
beside it. Use `--revision`, `--trace`, `--coverage`, `--harness cocotb`, `--debug`,
`--max-size`, and `--disabled` for the recorded cases. Give each repetition a new
Project; recreate its Sandbox with the same issued Project-data owner. Build the
baseline with the pre-change Booley Python sources in the same pinned image.

Complete Ruff check/format gates pass. The final full test suite
passed **15,260 tests, 73 skipped**, including review regressions for inherited
Make precedence, eval forms, broken selected-config links, and compiler-directory
PATH availability. The real Docker
acceptance gate and measured variants ran without optional-tool skips.
