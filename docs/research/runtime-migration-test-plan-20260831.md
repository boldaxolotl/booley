# Node 24, Python 3.14, and Ubuntu 26.04 migration test plan

This is the execution plan for
[#156](https://github.com/boldaxolotl/booley/issues/156). It does not change a
production pin. The purpose is to make each runtime change independently
reviewable, prove the agent-policy boundary with the real CLIs, and define when
a candidate is promoted or held.

## Baseline and scope

The baseline is `main` at `a1c11fbf5a93cec758c779be9629cfb23fa3df48`
(`v0.2.9`). Its relevant immutable inputs are:

| Surface | Baseline |
| --- | --- |
| Session Runtime | `ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517` |
| Session Python | CPython 3.13 from deadsnakes |
| Node.js | 22.23.2, tarball SHA-256 `d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307` |
| Agent CLIs | Claude Code 2.1.251; Codex CLI 0.151.0 |
| Debian sidecar | `python:3.13.15-slim-bookworm@sha256:c45a22ea000adfd9cda29364bbe7edd23001ce5cc2ad15857cfbf7766943b9ca` |
| Alpine sidecars | `python:3.13.15-alpine3.24@sha256:540c7d91f98ff6880174c40e99067bf5941eb54d818a7a5e094d188b196a934d` |

The candidates named in the issue are Node 24.20.0, Python 3.14.7, and Ubuntu
26.04. Node publishes exact release artifacts and `SHASUMS256.txt` beside each
release, so the Node candidate must come from the
[official v24.20.0 directory](https://nodejs.org/dist/v24.20.0/). Python 3.14.7
is an upstream release according to the
[Python 3.14.7 release page](https://www.python.org/downloads/release/python-3147/).
Ubuntu publishes the 26.04 release artifacts and their checksums from its
[official release archive](https://releases.ubuntu.com/26.04/).

Do not update Claude Code, Codex CLI, EDA-tool versions, cocotb, or the Rust
builder while testing these runtime variables. An unrelated upgrade would make
a failure impossible to attribute.

## Candidate matrix

Build all candidates for the same `linux/amd64` platform and from the same
Booley commit. `B` is rebuilt rather than represented by an old local image so
that archive drift, cache state, and measurement method are identical.

| ID | Ubuntu | Session Python | Node | Sidecars | Purpose |
| --- | --- | --- | --- | --- | --- |
| `B` | 24.04 | 3.13 | 22.23.2 | 3.13.15 | Fresh control |
| `N` | 24.04 | 3.13 | 24.20.0 | 3.13.15 | Isolate Node and both agent CLIs |
| `S` | 24.04 | 3.14 | 22.23.2 | 3.13.15 | Isolate Session Python and Booley dependencies |
| `P` | 24.04 | 3.13 | 22.23.2 | 3.14.7 | Isolate all three sidecars |
| `U` | 26.04 | distro Python 3.14 | 22.23.2 | 3.13.15 | Isolate the OS/compiler stack by comparison with `S` |
| `F` | 26.04 | distro Python 3.14 | 24.20.0 | 3.14.7 | Detect interactions after `N`, `S`, `P`, and `U` pass |

Use disposable candidate Dockerfiles or build arguments during investigation.
Do not edit all production pins first and then try to infer which change caused
a failure.

For every row, retain:

- the source commit, build command, target platform, start/end time, and whether
  the build used an empty cache;
- `docker image inspect`, `docker history --no-trunc`, installed package lists,
  version output, and test logs;
- the exact Node tarball SHA-256 and every external `FROM` digest;
- uncompressed image bytes from `.Size` for every image surface applicable to
  that row.

The implementation PR should add a dated evidence report beside this plan. A
tag alone is not immutable evidence: resolve each OCI tag to a `sha256:` digest,
put that digest in the Dockerfile, pull it by digest, and record the matching
`RepoDigests` value. Verify the Node tarball against the publisher's
`SHASUMS256.txt` before extracting it. Keep `npm ci` and all lockfile integrity
entries; do not regenerate the lockfile merely because Node changed if its
bytes remain valid.

## Phase 1: Node 24 and agent-policy enforcement

Build `B` and `N` with identical CLI packages. The lockfile currently declares
Claude Code's Node engine as `>=22.0.0` and Codex CLI's as `>=16`, but package
metadata is only a compatibility claim, not a policy test.

### Startup and integration probes

Run every probe as the image's `agent` user, with fresh writable Claude and
Codex homes:

1. Assert exact `node`, `npm`, `claude`, and `codex` versions and run each
   client's help/diagnostic entry point.
2. Retain the build-stage `npm ci` log, run
   `npm ls --prefix /opt/agent-clis --omit=dev --all`, and confirm that the
   installed dependency tree resolves exactly the committed CLI versions and
   platform-package integrity hashes.
3. Import `claude_agent_sdk`, verify its removed bundled CLI is still absent,
   and make the SDK discover the system `claude` executable.
4. Generate Booley's Claude and Codex runtime configuration in clean homes and
   run the existing `booley.harness.web_isolation` probe.
5. Run one minimal authenticated turn through each direct CLI and one through
   each Booley backend. Use an empty synthetic repository, inject credentials
   only at container runtime, and redact credentials and account identifiers
   from retained logs.

### Negative policy probes

Static JSON/TOML parsing is necessary but insufficient. Test the installed
CLIs themselves with hostile user and project configuration.

For Codex, `/etc/codex/requirements.toml` contains
`allowed_web_search_modes = []`. The
[official Codex configuration reference](https://developers.openai.com/codex/config-file/config-reference)
defines `requirements.toml` as admin-enforced and says an empty allowed-mode
list effectively permits only `web_search = "disabled"`.

Run these cases separately:

- user `~/.codex/config.toml` requests `web_search = "live"`;
- trusted project `.codex/config.toml` requests `web_search = "live"`;
- the command line requests `-c 'web_search="live"'`;
- the command line uses `--search`;
- danger-full-access is selected, because Codex otherwise defaults web search
  toward live access in that mode.

Each case passes only if Codex rejects the override before a model turn or the
effective session has web search disabled and exposes no web-search tool. A
successful live search is a hard failure. Capture the machine-readable event
stream or diagnostic output that proves the effective result; an exit code
without the relevant diagnostic is not enough.

For Claude Code, `/etc/claude-code/managed-settings.json` denies `WebFetch` and
`WebSearch`. Anthropic documents managed settings as the highest-precedence
policy layer in
[Claude Code settings](https://code.claude.com/docs/en/settings) and documents
deny rules in
[Claude Code permissions](https://code.claude.com/docs/en/permissions).

Run these cases separately:

- user and project settings explicitly allow `WebFetch` and `WebSearch`;
- the CLI explicitly lists those tools as allowed;
- `bypassPermissions` is active, matching Booley's container policy;
- the prompt requires a fresh web search and a fetch of a known public URL.

Do not use the model's choice to attempt a denied tool as the primary oracle.
For every case, capture a supported machine-readable startup/debug tool
inventory and assert that `WebFetch` and `WebSearch` are absent. If the pinned
CLI exposes no such inventory, run it against a loopback test provider that
captures the outbound tool definitions before returning any model response;
assert that the two web tools are absent while a permitted canary such as
`Read` is present. Invoke the canary successfully. A prompted model turn that
requests web access is useful supplemental evidence, but cannot make the test
pass by itself.

### Credential and evidence handling

Authenticated probes use dedicated, short-lived, least-privilege test
credentials. Never put a secret in a command-line argument, Docker build arg,
image layer, `docker run -e NAME=value` metadata, or traced shell. Mount it as a
read-only runtime secret and have an untraced in-container wrapper export it
only to the CLI child process.

Run with `--rm`, a tmpfs or disposable home, an empty synthetic repository, and
a cleanup trap that removes containers, volumes, networks, homes, and raw
transcripts on success or failure. Revoke the credential after the matrix. The
retained artifact is an allowlisted summary of effective tools and outcomes,
not the raw CLI home or transcript. Run the repository confidential-content
guard plus a test-credential fingerprint scan before uploading any evidence;
any match blocks the upload and the phase.

Run the same matrix on `B` and `N`. Node 24 passes only when the outcomes are
identical. No real project source or user home is mounted into these tests.

## Phase 2: Session Python 3.14 on Ubuntu 24.04

Build `S` before changing Ubuntu. Keep the Ubuntu digest, Node, agent CLIs,
sidecars, EDA pins, and Rust builder identical to `B`; change only the Session
Python packages, `python`/`python3` symlinks, and literal user-site paths. Use
the same deadsnakes channel as `B`, and record the exact resolved package
versions. Select the same upstream `major.minor.patch` that `U` will use and
require `platform.python_version()` to match between `S` and `U`. If the
existing channel cannot supply that candidate, hold this phase rather than
changing both the Python source and Ubuntu at once.

In both `B` and `S`, perform the full dependency install and retain resolver
output, then run `python -m pip check`, direct imports of every curated runtime
package, the full Python test suite, agent-SDK discovery, and the cocotb/Icarus
library lookup from `Dockerfile.base`. Run representative plain and cocotb
simulations so compiled Python extensions and the simulator bridge execute,
not merely import. The phase passes only when `S` matches `B` behavior with no
new skipped compatibility test. Ubuntu evaluation must use `S` as its control
so a later failure is attributable to the OS/compiler change rather than the
Python major version.

## Phase 3: Python 3.14 sidecars

Keep the existing OS variants while changing only CPython:

- egress proxy: `python:3.14.7-slim-bookworm` by exact digest;
- FlexNet relay and reaper: `python:3.14.7-alpine3.24` by exact digest;
- reaper's Docker CLI stage remains independently digest-pinned.

Confirm that those exact variant tags exist in the
[Docker Official Images Python source](https://github.com/docker-library/python)
at execution time. If a same-OS variant does not exist, stop: changing Python
and the sidecar distribution in one step is a different migration and needs a
new matrix row.

Required evidence:

1. Build each `B` and `P` sidecar without cache and assert `python3 --version`.
2. Run the full proxy unit suite and a containerized CONNECT/streaming smoke,
   including graceful shutdown.
3. Run the FlexNet healthcheck, fixed-destination forwarding E2E, read-only
   root filesystem, numeric unprivileged user, dropped-capability, and cleanup
   cases.
4. Add an image-owned reaper E2E and run it against the candidate image:

   ```text
   docker build --pull --no-cache \
     --file src/booley/data/docker/Dockerfile.reaper \
     --tag booley-reaper:py314 src/booley/docker
   BOOLEY_REAPER_IMAGE=booley-reaper:py314 \
     pytest tests/docker/test_reaper_image_e2e.py -q
   ```

   The test must launch that image, not import `booley.docker.reaper` on the
   host. Give it a disposable labeled session container and network through a
   mounted Docker socket, assert the candidate ENTRYPOINT removes the owned
   topology, then point the same image at an unreachable daemon. During a
   bounded observation window, assert it logs the failed pass, remains alive,
   and waits for its configured interval instead of busy-looping; stop it at
   the end of the test. Clean all topology in `finally`.
5. Compare image bytes and history for all three sidecars. Every added layer or
   size increase must be attributed; duplicated interpreters or package caches
   fail the phase.

## Phase 4: Ubuntu 26.04

Start `U` from the production Dockerfile with only these unavoidable base
adaptations:

- pin the official Ubuntu 26.04 image by exact digest;
- use Ubuntu's native Python 3.14 packages and remove the deadsnakes PPA;
- update literal Python 3.13 paths such as the agent user-site directory.

Record the resolved versions of glibc, GCC/G++, binutils, CMake, Python, and
every apt-installed library in `S` and `U`. Compare `U` with `S`, not `B`, so
both candidates use Session Python 3.14. Do not pin a moving Ubuntu tag or leave
individual downloaded archives unchecked.

### OpenROAD gate

The current OpenROAD artifact is the pinned
[Precision Innovations 2024-12-14 Ubuntu 22.04 package](https://github.com/Precision-Innovations/OpenROAD/releases/tag/2024-12-14).
Inspect it with `dpkg-deb --info` and `dpkg-deb --field ... Depends`, retain that
output, and attempt installation without adding obsolete Ubuntu repositories or
unversioned compatibility packages. Its hard `libpython3.10` dependency is a
known migration risk.

The Ubuntu phase passes this gate only if the exact existing `.deb` installs,
`ldd` reports no missing libraries, `openroad -version` succeeds, and the real
physical-synthesis/timing smoke passes. Otherwise Ubuntu 26.04 remains held.
Do not silently change the OpenROAD version, binary channel, or build method;
[#154](https://github.com/boldaxolotl/booley/issues/154) owns that decision and
must land first if a new artifact is required.

### EDA build and flow gate

Perform at least one empty-cache stable-base build. A cached layer is not
evidence that the new compiler can build an EDA tool. Require:

- Yosys plus ABC and `read_slang` build from the pinned commits and run a
  representative SystemVerilog synthesis;
- Icarus builds and runs both a plain SystemVerilog simulation and the pinned
  cocotb VPI smoke;
- Verilator builds and passes the native FST cross-validation and simulator
  ground-truth suite;
- the exact sv2v and Verible archives pass their checksum, version, lint, and
  conversion probes;
- the final sandbox passes Verible E2E, sandbox isolation, FIFO pipeline,
  Ticket Mode image smoke, and an ASIC physical-flow smoke using Nangate45;
- the RISC-V flavor builds and passes the PicoRV32 and Spike differential
  flows. Do not change Spike's pin as part of this issue; its channel is
  [#157](https://github.com/boldaxolotl/booley/issues/157).

Warnings promoted by the newer compiler must be reviewed, not globally
suppressed. A source patch belongs in its own commit with an upstream reference
and a regression test.

### glibc and native-artifact gate

Capture `getconf GNU_LIBC_VERSION`, `ldd --version`, and `readelf --version-info`
for native executables and Python extensions. At minimum inspect Node, both
agent CLIs, B-Wave, Yosys/ABC, OpenROAD, Icarus/vvp, Verilator, sv2v, and
Verible. Resolve wrappers and symlinks with `readlink -f` and `file`; for each
agent CLI, also locate and inspect the native platform payload that its npm
launcher executes. A wrapper script is not glibc evidence.

The evidence report must contain this contract table, filled with the measured
maximum required symbol and loader result:

| Artifact class | Compatibility floor / maximum allowed requirement |
| --- | --- |
| B-Wave copied from the Bookworm builder | No newer than `GLIBC_2.34` |
| Node, Claude/Codex native payloads, and pinned prebuilt EDA archives used across candidates | No newer than the glibc shipped by `B` (record its exact version; expected 2.39) |
| Python extensions and EDA binaries built inside `B`, `N`, `S`, or `P` | No newer than that candidate's recorded runtime glibc |
| Python extensions and EDA binaries built inside `U` or `F` | No newer than `U`'s recorded runtime glibc; these artifacts must never be copied into an older candidate |

Every ELF object must have complete `ldd` resolution and execute its real
smoke. A required symbol above the applicable row's ceiling, a missing loader
or library, failure to identify the native payload behind a CLI wrapper, or a
new-image artifact copied into an older runtime fails the gate. This table
defines “disallowed glibc symbols” for the final acceptance decision.

### Image-size assessment

Size is decision evidence, not a compatibility pass/fail budget: the repository
has no established byte threshold, so this plan does not invent one. Make only
single-variable comparisons with both image `.Size` and
`docker history --no-trunc`:

- `N - B`: stable base, final sandbox, and RISC-V flavor (Node only);
- `S - B`: stable base, final sandbox, and RISC-V flavor (Session Python only);
- `P - B`: egress proxy, FlexNet relay, and reaper only (sidecar Python only);
- `U - S`: stable base, final sandbox, and RISC-V flavor (Ubuntu/compiler only);
- `F - U`: stable base, final sandbox, and RISC-V flavor, compared with the
  `N - B` Node delta; `F` sidecars are compared directly with `P`.

Report absolute and percentage deltas and trace every changed byte range to
named layers. The magnitude alone is informational and must be called out for
review, not labeled green or red. Missing measurements, mixed-variable
comparisons, duplicate Python/Node/Rust toolchains, retained package caches, or
build-only files in a final image are test failures.

## Final combined and release gates

Build `F` only after the four isolated phases have passed. Repeat every Node
policy probe, Session-Python dependency and simulation check, sidecar E2E,
image smoke, EDA/physical flow, RISC-V flow, glibc inspection, and size
comparison. Then run the ordinary required checks:

```text
ruff check src/ tests/
ruff format --check src/ tests/
pytest tests/docker/test_sandbox_dockerfile.py tests/harness/test_web_isolation.py
pytest tests/ci/test_docker_base_contract.py tests/ci/test_change_classifier.py
git diff --check
```

The production migration is accepted only when:

- all external images and downloads use the reviewed digest or checksum;
- all exact version assertions, supported-tool documentation, and Docker
  contracts change together;
- each negative agent-policy case passes on Node 24;
- Session Python 3.14 passes independently on Ubuntu 24.04 before the Ubuntu
  rebase;
- all sidecars retain their security and behavioral contracts on Python 3.14;
- the stable base and RISC-V image build from empty cache on Ubuntu 26.04;
- OpenROAD and representative simulation, synthesis, lint, timing, physical,
  and RISC-V flows pass;
- native artifacts have no unresolved libraries or symbols above their defined
  glibc ceiling;
- every single-variable image-size delta is measured and attributed, with no
  retained build-only payload or duplicate toolchain;
- the final combined image repeats the isolated successes.

Any failed gate produces a **hold**, with the candidate pin left out of
production and the exact failure recorded. Do not weaken an assertion, add a
mutable compatibility repository, or combine a second tool migration merely
to make the candidate green.

## Reviewable delivery order

Deliver implementation as separate pull requests, each based on the then-current
`main`:

1. Node 24 plus the executable policy probes and Node checksum.
2. Session Python 3.14 on Ubuntu 24.04 plus dependency, simulator, and agent-SDK
   evidence.
3. Python 3.14 sidecar digests plus sidecar E2E evidence.
4. Ubuntu 26.04 plus compiler/EDA/OpenROAD/glibc/size
   evidence; this PR may remain blocked on #154.
5. A final combined validation or release PR only if interactions require
   changes beyond the first four.

Each PR should say `Refs #156`; no planning or partial migration PR should close
the issue. Close #156 only after the final evidence report records a promote or
hold decision for every candidate surface and links every resulting PR or
follow-up issue.
