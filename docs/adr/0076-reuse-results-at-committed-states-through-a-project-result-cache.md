# Reuse results at committed states through a Project Result Cache

Status: proposed

## Context

Booley repeats expensive EDA work it has already done.

- Simulation reuses builds but never results.
- The SIM Goal baseline store (#1498) reuses results only for baseline
  Jobs.
- The Vivado metrics cache sits in one checkout's work root, and its hits
  count as Goal evidence although its key names the tool by a version
  string (#2051).
- Synthesis has no reuse.

The most valuable repeats happen **at the same commit**:

- a baseline measured again in every Goal Mode and session
- a `main` measurement wanted as a baseline
- the same commit in another worktree
- Finish at a committed HEAD

A full-authority design with a hermetic view was rejected as too complex and
fragile (see Rejected alternatives). This ADR stores and serves only at clean
commits ([decision](https://github.com/boldaxolotl/booley/issues/1500)).

An **incorrect hit** is a stored result served for a run whose fresh
execution, on the same host at the time of the request, would have produced
a different verdict or different metrics. This design narrows where they
can come from, names what remains, and detects drift with sampled re-runs.
It does not prove that they are impossible.

## Decision

### 1. Scope: clean commits, before and after the run

**Participants.** A run has two participants: the RTL repository, and the
Booley Directory's repository in whatever form it takes (tracked inside the
RTL tree, a paired checkout, or a standalone repository). A `.booley` that is
neither tracked nor its own repository is not cacheable.

**Clean.** A participant is clean when the strict form of Goal Mode's dirty
check (`dirty_paths`, without exemptions) reports nothing: no modified,
staged, deleted or untracked file, and no initialised submodule off its
recorded commit or dirty. A symlink, tracked or ignored, that resolves
outside both participants makes the run not cacheable, with the reason
printed, unless it points into a keyed tool manifest.

**Snapshots.** A storing run recomputes the **full key material** (§2) at
two points:

- before any Booley step reads the tree (core discovery, projection,
  Pre-Sim Commands)
- after the last tool process exits and the payload is captured

The result is stored only if the two are byte-identical. A hash memo keyed
by `(size, mtime_ns, ctime_ns, inode)` under `.runtime/` keeps the
steady-state cost at a `stat` walk.

**No overlap.** A storing run holds a checkout-wide `result-cache-store`
lease. At the post-snapshot it also lists the checkout's Job records whose
start and end overlap its own window. If it doesn't hold the lease
exclusively, or any other record overlaps, it doesn't store, and it prints
why. Flows typed in the Sandbox shell write the same Job record.

**Ignored files.** Ignored files are allowed, because build outputs are
ignored. One constant, tested as one list, defines the **Booley-owned
paths**, which are never keyed:

- the managed `.gitignore` patterns
- the projected-core files Booley writes into the RTL root
- `result-cache/`
- the nested Booley Directory repository, when it sits inside the RTL tree

Every other ignored file is keyed by path and content in the **input
inventory** at the pre-snapshot. The post-snapshot records the ignored files
the run created or changed as **outputs**, with their post-run hashes. A
pre-existing file the run changed is both an input and an output.

**Lookup** is two-stage:

1. Find candidates by the key without the inventory.
2. Match a candidate whose input inventory equals the current one. An extra
   file is allowed only if it is on the candidate's outputs list and
   byte-identical to the recorded post-run hash, or matches the write-only
   set: the Target's `trace_files`, `*.vcd`, `*.fst`, `*.log`, `*.jou`,
   `results.xml`.

**Exclusions and cap.** `[cache].ignored_exclude` (keyed) ships empty. An
exclusion asserts that the run never reads those paths, and the docs use
firmware built into `build/` as the way to get it wrong. The ignored set is
capped at 256 MiB and 20,000 files, counted from `stat` before any hashing.
Above the cap the run is not cacheable, and the ten largest offenders are
printed.

### 2. The Result Key

A **Result Key** is a SHA-256 over a canonical, length-delimited encoding of:

| Part | Identity |
|---|---|
| Tracked files | For each participant, the digest of the working tree's tracked bytes and modes (`git ls-files -z --recurse-submodules`, each file hashed), plus `git submodule status`. Wherever the Booley Directory's files are digested, `booley.toml`, `tests.toml`, Goalsets and `criteria.toml` are excluded; they are covered by the next row. HEAD is recorded for the label only. |
| Configuration | `booley.toml` whole, minus `[agent]`, `[models]`, `[feedback]`, `[jobs]`, `[sandbox]`, `[eda]` and `[cache]` (of which only `ignored_exclude` is keyed); a new section is keyed by default. The **resolved Target Test Plan** from `tests.toml`, after `[test_lists]` expansion, with variants, environment and seed. Goalsets and `criteria.toml` define Goals, not run behavior, and are not keyed. |
| Ignored files | The input inventory, matched in the second lookup stage |
| Request | The Target, its resolved recipe and the **resolved EDAM**: the ordered file list (paths relative to the participant roots or the work root, never absolute per-run paths), file types and logical names, defines, parameters, hooks, generators, and the library-root selection. For sim: Test Run name, seed and declared-inputs fingerprint. The pinned thread count (§3). |
| Environment | The keyed allow-list and its values, including the normalised `PATH` |
| Booley | A content hash of every file of the installed Booley, FuseSoC and Edalize distributions |
| Image and tools | The image ID recorded by Sandbox issuance. Image tools need nothing more: they are root-owned and immutable inside the Sandbox. A mounted install (today only Vivado) adds a content manifest created at provisioning and a per-lookup fingerprint `(st_size, st_mtime_ns, st_ctime_ns, st_ino)` of its key files; a mismatch misses, and Doctor warns "re-provision". |
| Host | A Booley-generated host id, plus the CPU model and flag set, the kernel release, the affinity count and the cgroup `cpu.max`. The host id is a file holding a `uuid4`, the host's hostname and, when present, its machine id. It is created once on the host and mounted read-only into the issued Sandbox as a single file. If it records another host, Doctor reports it and the cache stays unavailable until `booley cache host-id --regenerate`. |

**The cache is unavailable**, and Doctor says why, when any of these holds:

- there is no issued image ID (for example in `booley shell`)
- there is no valid host id
- a `PATH` entry, a resolved tool executable the Flow invokes (symlinks
  followed), or `booley`, `fusesoc`, `edalize` or `cocotb` is not owned by
  root, or has a group or other write bit
- the Target uses `provider:` cores

A per-Flow **enumeration test** classifies every Flow option and
configuration knob as keyed or verdict-neutral; an unclassified knob fails.

### 3. Hygiene for clean-commit runs

Every run of a cacheable Target at a clean commit gets this hygiene,
including `--no-cache` runs and independent of the kill switch, so cached and
fresh verdicts mean the same thing. **Behavior changes at commit time, and
the user docs say so.**

- **Environment.** A Project-configurable, keyed allow-list. A fixed deny
  set can't be allow-listed: `EDALIZE_LAUNCHER`, `MAKEFLAGS`, `MFLAGS`,
  `LD_PRELOAD`, `LD_LIBRARY_PATH`, `PYTHONPATH`, `PYTHONSTARTUP`. Per-run
  Booley variables are passed but not keyed. `PATH` keeps only root-owned,
  non-writable image entries. `PYTHONNOUSERSITE=1`, `PYTHONSAFEPATH=1` and
  `PYTHONHASHSEED=0` are set.
- **`HOME` and `TMPDIR`** are fresh per-run directories, and `HOME` is
  seeded from a declared, keyed file list such as a license rc file. A
  Target or Test Variant that sets `HOME`, `PATH`, `TMPDIR`, `XDG_*` or
  `FUSESOC_*` makes the run decline.
- **Threads.** OpenROAD's `set_thread_count` and Vivado's
  `general.maxThreads` are pinned to a keyed value (default: the smaller of
  4 and the affinity count). Verilator's own thread setting is part of the
  Target, keyed and left alone.
- **Build state.** The synth and fpga work roots are per-run, and a
  tripwire test proves those runs never open the live work root or build
  slot. Sim may reuse builds only through the content-keyed mechanisms
  already trusted for Goal evidence: Verilator build reuse and the compiler
  cache. FuseSoC's isolated core registry is regenerated from keyed inputs
  on every call.
- **Outputs and retention.** Booley's outputs go into the run's **per-run
  directory**: the Flow's numbered invocation directory, or
  `<Booley Directory>/.runtime/flow-reports/...` when no report directory is
  set. Each is identified by a UUID, and `booley` prints its path. Existing
  invocation-directory retention prunes them, extended to synth and fpga and
  allowed to hold payload copies. The working directory is unchanged, and
  tool-written files there are ignored outputs under §1.

### 4. What is stored and served

- **Seeded runs only.** A sim Target that can't apply a seed, or a seed
  value that means "pick a random seed" (Verilator `0` and below), is never
  cached.
- **Passes only.** Only completed, graded passes and their metrics are
  stored. Failures, timeouts, infrastructure errors and interrupted runs
  always run again.
- **No large artifacts.** Waveforms, coverage databases and bitstreams are
  never stored. A request that needs one of them misses.
- **Payload.** The verdict inputs the grader reads (reports, logs, exit
  status, standard output and error), with the producer's UUID, date and
  duration. Each entry carries its full key material, and each read
  verifies it and the entry's digest. The verdict and metrics are re-derived
  from the payload by the current code on every hit, and a different or
  failing re-derivation refuses the hit.
- **Time budget.** A hit is served only if the producer's duration, times a
  small factor, fits the consumer's time budget.
- **Same host only.** The host id is in the key.

### 5. Hits are evidence, labelled

A hit counts as evidence, including for Goal Mode. Status, the Dashboard and
the Review Package label it `cached from <producer run, date>`. The serve API
writes the payload into a new per-run directory with a `run.log` header
naming the producer, and every result path points there. Each entry logs
every run it was served to.

**At Finish,** a verified hit is served. An unverified hit runs fresh, and
that run is its verification.

### 6. Safety net: sampled re-runs

- **Burn-in.** The first 10 hits per Target, counted per combination of the
  Booley, image, tool and host key parts, are re-run in the background.
  Until its re-run agrees, a burn-in hit is `cached (unverified)` and counts
  as absent at Finish.
- **Sampling.** After burn-in, 5% of hits are re-run, plus at least one per
  Target every 30 days. That floor sample runs **cold**: no build reuse and
  no compiler cache. An fpga hit stays unverified while its tool manifest's
  last full verification is older than 30 days.
- **How re-runs run.** Sampled, Finish and duplicate-producer re-runs all
  take the same pre and post full-key snapshots and apply the overlap rule
  (§1). A re-run executes only in a checkout whose full key equals the
  entry's. A changed snapshot, an overlap, or an infrastructure error
  (retried up to twice) discards the comparison and is never a mismatch.
- **One comparison rule.**
  - A fresh run that agrees with the entry (same re-derived verdict and
    metrics) verifies it.
  - A fresh run that disagrees triggers a second fresh run.
  - If the two fresh runs agree with each other against the entry, the
    cache is **disabled for the whole Project**: nothing is served or
    stored, every cached record counts as absent evidence, and Doctor fails,
    naming the Target, the differing fields and every consumer from the
    served-hit log. Re-enabling is host-only, needs a reason, and clears
    every entry.
  - If the two fresh runs disagree with each other, the Target is marked
    **nondeterministic, never cached**, and Doctor lists the hits it served.
- **Detection latency.** After burn-in, a persistently wrong source stays
  undetected after *n* hits with probability 0.95^*n*: 36% after 20 hits and
  8% after 50. Hits already consumed by finished Goals can't be undone;
  Doctor lists them.

### 7. Store and controls

- **Location.** A per-Project `result-cache/` in the Main Booley Directory,
  resolved through it from linked and baseline worktrees.
- **Copies and Git.** Excluded from the worktree snapshot copy and listed in
  the managed `.gitignore` patterns. The store refuses to operate if any of
  its files are tracked by Git.
- **Entries.** Write-once and published atomically, one producer per key,
  with a byte cap and LRU eviction.
- **Controls.**
  - `--no-cache` on each cacheable Flow: no lookup and no store, but the
    hygiene still applies.
  - A Project kill switch in `booley.toml`, shown by Doctor.
  - `booley cache` lists, shows and clears entries and shows the disabled
    state. Re-enabling is host-only.
  - Doctor's `--deep` self-tests run `--no-cache`, and `--deep` re-verifies
    tool manifests in full.

Rollout, the per-Flow serving gate and the knob-discovery sources for the
enumeration test are in the implementation plan.

## Accepted residuals

Each is bounded by the sampled re-runs (§6) and the kill switch.

- **Files outside both participants** that the hygiene doesn't reset: `/tmp`,
  system paths outside the image, and files reached by absolute path.
  Issuance allows only four mounts: the Project data, the state volume, the
  read-only devcontainer mount and Vivado.
- **Non-Booley processes** writing to the checkout during the window, which
  the Job-record overlap rule doesn't see.
- **An edit reverted before the run ends**, which leaves both snapshots
  equal.
- **Network and license changes.** A feature gain can't turn a stored
  failure into a hit, because failures aren't stored.
- **Nondeterminism** that the comparison rule doesn't expose.
- **Host changes below the coarse identity**, such as microcode, sysctls,
  memory pressure or storage errors.
- **Mounted tool trees changed without changing the fingerprint**, for up to
  30 days.
- **A verdict-changing knob** that the enumeration test misses.
- **Transient drift** between samples.
- **Hits consumed by finished Goals** before a mismatch is found. They are
  listed but not undone.
- **Deliberate tampering** in the Sandbox: the same trust boundary as Goal
  evidence.

## Rejected alternatives

- **The full hermetic design.** A view proving every input a run can
  observe, for full authority over any working tree. It went through 27
  review rounds on #1500, which keeps the record, and was rejected as too
  complex and fragile for its hit rate.
- **Diagnostic-only authority.** It loses the main uses: baselines across
  Goal Modes and Finish at a committed HEAD.
- **Keys from declared inputs only.** They miss undeclared includes, data
  files and hooks, the weakness of today's Vivado metrics cache (#2051).
- **A whole-configuration-tree key.** Edits to Goalsets, `criteria.toml` or
  another Target's tests would invalidate every entry.
- **No build reuse at clean commits.** Every commit would trigger a full
  rebuild, which teaches users to avoid clean commits.

## Consequences

- Re-running the same commit is fast on the same host.
- Uncommitted work never hits. Any change to tracked files, to non-Booley
  ignored files, or to a keyed configuration section misses. So does every
  Booley upgrade, image rebuild, tool change or host change, and each of
  those restarts burn-in.
- Development installs and `booley shell` never cache.
- At a clean commit, runs of cacheable Targets change behavior: a clean
  environment and `PATH`, a fresh `HOME` and `TMPDIR`, pinned OpenROAD and
  Vivado threads, per-run synth and fpga work roots, and outputs in a
  per-run directory.
- Overlapping runs in one checkout don't store.
- A nondeterministic Target is never cached. A confirmed mismatch disables
  the cache for the Project until a human re-enables it.
- The Vivado metrics cache is retired, which closes #2051.
- Public material will describe the cache as reuse at clean commits on the
  same host, checked by sampling, and won't claim it is always correct.
