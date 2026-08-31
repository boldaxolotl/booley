# Node 24 migration evidence — 2026-08-31

This report covers phase 1 of the runtime migration plan in
[#199](https://github.com/boldaxolotl/booley/pull/199) for
[#156](https://github.com/boldaxolotl/booley/issues/156). The implementation
changes only the Node archive version and checksum; Claude Code, Codex CLI,
Ubuntu, Python, cocotb, EDA tools, sidecars, and the Rust builder remain at the
versions on `main`.

## Decision

**Hold until the implementation PR's fresh CI build passes and the dedicated
authenticated matrix is run.** The offline executable policy boundary passes,
but local disk pressure prevented rebuilding a current-commit control after
`main` advanced, and no dedicated short-lived Anthropic or OpenAI test
credentials were available. The candidate pin exists only on the unmerged
implementation branch while those gates remain open.

## Immutable inputs

| Input | Value |
| --- | --- |
| Implementation base | `main` at `031297c293c4bb43dc9abfd7681465ad892691a8` |
| Platform | `linux/amd64` |
| Ubuntu | `ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517` |
| Control Node | 22.23.2, SHA-256 `d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307` |
| Candidate Node | 24.20.0, SHA-256 `2f2c0da162318f0de47665410c7c8c2ed3d36c8f3105de4bbc61176c70a7cbf2` |
| Candidate bundled npm | 11.19.0 |
| Agent CLIs | Claude Code 2.1.251; Codex CLI 0.151.0 |

The Node 24 checksum was selected from the publisher's v24.20.0
`SHASUMS256.txt`. The production Dockerfile verifies it with `sha256sum -c`
before extraction. `npm ci` retained the committed lockfile and integrity
entries without regeneration.

## Local build and size evidence

The locally completed isolated comparison predates the final rebase and used
the otherwise-identical Node 22 and Node 24 Dockerfiles at `0a92df5`. It is
useful supporting evidence, but not promotion evidence: the cached control did
not record a source revision, and the candidate's revision label contained a
manual transcription error. Neither image is a release artifact.

| Image | Image ID | Created | `.Size` bytes |
| --- | --- | --- | ---: |
| Cached Node 22 control | `sha256:be4efc4d72f3ed181a3ab468560d55c70f008bd374b83ad4315d5e81daae6c2c` | 2026-08-31 00:17 +04 | 1,357,679,720 |
| Node 24 candidate base | `sha256:67504e62a587dc027a6b9db39ee02cbe0d90b334a2c88754c259e22ff1f4eea0` | 2026-08-31 16:18 +04 | 1,358,911,245 |
| Node 24 Session Image | `sha256:64c35abe369913a8f118f23bfc6009d8c5c207bb08ba6c028c9099172cd3f006` | 2026-08-31 16:28 +04 | 1,368,463,035 |

The base delta was **+1,231,525 bytes (+0.0907%)**. The Dockerfile diff and
full histories attribute it to the single Node/npm/agent-CLI installation
layer; no second Node installation or package cache was found. The candidate
used the ordinary BuildKit cache. Its build ran approximately 12:09–12:18 UTC
and rebuilt the pinned Yosys, OpenROAD, Icarus, Verilator, sv2v, Verible, Node,
Python-dependency, and final validation layers successfully. The final Session
Image then installed the branch wheel and passed its image-build validation.

After the rebase, the available Node 22/cocotb 2.1 control was
`sha256:1ccb5355796c2363cd1ff01dfb803a1be94e1795bff1f416a3de97f73328657b`
at 1,364,299,524 bytes. A same-commit rebuild was requested but rejected by the
host safety guard because the Docker host was 99% full. No Docker state was
pruned and no unsafe workaround was attempted. The implementation PR's clean
runner is therefore the authoritative current-commit build.

## Executable policy evidence

`tests/docker/agent_policy_probe.py` runs as the image's unprivileged `agent`
user with a fresh tmpfs home and `--network none`. It checks exact runtime and
CLI versions, CLI diagnostics, the recursive installed npm tree and lockfile
integrities, SDK discovery of the system Claude binary, Booley-generated
configuration, and the existing web-isolation probe.

The real installed CLIs were then exercised separately against every hostile
configuration from the plan:

| Surface | Cases | Result |
| --- | --- | --- |
| Codex | user config, trusted project config, `-c web_search=live`, `--search`, danger-full-access | All five diagnostics fell back to required `Disabled` from `/etc/codex/requirements.toml` |
| Claude | user allow, project allow, CLI allow, bypass-permissions, web-required prompt | All five outbound tool inventories omitted `WebFetch` and `WebSearch`; `Read` remained present and successfully returned a canary |

Claude was connected only to an in-container loopback SSE provider. The
provider captured the actual outbound tool definitions, requested `Read`, and
confirmed the CLI returned the canary tool result. No external model or web
request was made. The placeholder provider key was exported only to the child
process and is not a credential.

The same probe passed on the cached Node 22 control, the Node 24 base, and the
Node 24 Session Image. CI now repeats it against the freshly built production
image and uploads only its allowlisted JSON summary for 14 days. The artifact
contains versions, package names, generated-config outcomes, policy
diagnostics, and tool names; it contains no raw transcript, prompt, provider
payload, account identifier, or secret.

## Native compatibility

The Ubuntu 24.04 candidate reported glibc 2.39. Wrapper paths were resolved to
their native payloads before inspection.

| Payload | Maximum required glibc symbol | Loader result |
| --- | --- | --- |
| Node `/usr/local/bin/node` | `GLIBC_2.28` | All libraries resolved |
| Claude `claude.exe` | `GLIBC_2.26` | All libraries resolved |
| Codex x86_64 musl payload | Static musl executable | No dynamic loader required |

All three payloads executed their real version and diagnostic entry points.
The image does not contain `file`; ELF identity was confirmed with `readelf`
and dynamic linkage with `ldd`.

## Verification and open gates

Completed locally:

- the test-first probe failed on the Node 22 baseline only at the expected
  Node/npm version assertion, then passed with Node 24;
- exact CLI versions, `npm ls`, lockfile integrity, SDK discovery, generated
  configuration, and all ten negative policy cases passed;
- the Node 24 base and final Session Image built successfully;
- `ruff format --check src/ tests/` passed;
- `pytest tests/docker/test_sandbox_dockerfile.py tests/harness/test_web_isolation.py`
  passed (29 tests);
- `pytest tests/ci/test_docker_base_contract.py tests/ci/test_change_classifier.py`
  passed (23 tests);
- `git diff --check` passed.

`ruff check src/ tests/` was run and reported 25 pre-existing `BLE001` findings
in files unchanged by this branch. The changed Python files pass Ruff. The
full command is retained as a known baseline failure rather than weakening or
silencing unrelated assertions.

Still required before promotion:

1. the implementation PR's current-commit production-image build and complete
   smoke matrix must pass;
2. a fresh same-commit Node 22 control must be compared with the Node 24 base,
   Session Image, and RISC-V flavor on a host with sufficient disk;
3. dedicated short-lived credentials must run one minimal authenticated turn
   through each direct CLI and each Booley backend using the plan's mounted
   secret and redaction procedure;
4. the resulting sanitized evidence must pass the confidential-content and
   test-credential fingerprint scans.

Until those items pass, this phase remains a hold and does not close #156.
