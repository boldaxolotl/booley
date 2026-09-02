# Software-development practices for Booley's next stage

**Research date:** 02 SEP 2026

**Repository snapshot:** `49c4e30e58097091a71d29ace26592c8c297dcd6`
**Decision:** Keep the strong CI foundation. Spend the next 90 days primarily on
architectural fitness, incremental typing, and state-space testing. Add dependency
reproducibility and low-cost security scanning in parallel. Do not rewrite Booley,
split it into services, or indiscriminately add more test matrix legs.

## Executive answer

CI has addressed a real class of risk: changes that violate known, executable
expectations. Booley already does this much better than most young projects. Its next
risks are different:

1. **A change can be locally correct but make the architecture harder to change.**
   Booley's top-level Python packages now form a large cyclic dependency component,
   several composition modules have very high fan-out, and 271 functions exceed the
   repository's own 50-line principle. Tests will not reliably reveal that kind of
   deterioration until later.
2. **The test suite samples examples, while Booley contains many state machines and
   parsers.** Ticket lifecycle, Criteria, Job admission, process recovery, image
   reconciliation, target identity, and persisted records have far more possible
   sequences than hand-written examples can enumerate.
3. **The public and persisted contracts are multiplying faster than their
   compatibility policy.** A green current-version suite does not prove that old
   Projects, records, configuration, or integrations still work after an upgrade.
4. **Releases are well validated but not yet fully reproducible or accompanied by a
   complete supply-chain inventory.** Cargo is locked; the Python development and CI
   environments are resolved afresh from mostly lower-bounded dependencies.
5. **The project has throughput data but not yet a small learning loop for escaped
   defects.** Without consistently marking which bugs escaped which existing check,
   it is difficult to know which technique is paying off next.

The best next move is therefore not “more CI” in the abstract. It is to add a few
new kinds of executable knowledge:

- a declared module dependency design and ratchets against architectural erosion;
- strict types at domain and persistence boundaries, expanded package by package;
- generated sequences and fault schedules for the most stateful code;
- explicit compatibility fixtures for versioned data and user-facing interfaces;
- reproducible dependency inputs plus an intentionally separate latest-dependency lane;
- a lightweight security threat model, dependency review, code scanning, and release
  provenance/SBOM evidence;
- small-change and escaped-defect metrics that decide where to invest next.

## What Booley already does well

This research should not produce duplicate work. The current baseline is already
substantial:

| Practice | Repository evidence | Assessment |
|---|---|---|
| Cross-platform CI | [`.github/workflows/test.yml`](../../.github/workflows/test.yml) runs Python 3.11, 3.13, and 3.14 on Linux and Windows, parallelizes tests, bounds individual tests, records JUnit evidence, and asserts suites actually ran. | Strong; preserve it. |
| Coverage | [`pyproject.toml`](../../pyproject.toml) enables branch coverage; CI enforces 80% global coverage and 90% changed-line coverage. | Strong guard against unexercised additions; do not chase 100%. |
| Static analysis | Ruff checks and formatting are required. Pyright is pinned and its scope is probed so a silent no-op fails. | Strong mechanism, but Pyright's useful scope is still small. |
| Rust quality | Locked Cargo builds run tests, Clippy, rustfmt, proptest, parser fuzzing, differential simulator checks, and Criterion benchmark targets. | Strongest state-space coverage in the repo. |
| Mutation testing | [Scheduled deep tests](../../.github/workflows/deep-tests.yml) run a bounded weekly `mutmut` campaign. | Good experiment; currently limited to `src/booley/harness/setup/*.py` and records evidence rather than enforcing a score. |
| Packaging/release | Wheels and sdists are built and installed in clean environments; tag/version and changelog contracts are checked; the exact tagged source workflow reruns before PyPI publication. | Strong. PyPI's official publisher action also creates publish attestations by default when Trusted Publishing is used ([PyPI documentation](https://docs.pypi.org/attestations/producing-attestations/)). |
| Workflow security | Actions are pinned to full commit SHAs, token permissions are narrow, Dependabot covers pip and Actions, and confidential-content scanning executes trusted base code. | Strong start. |
| Architecture guards | [`tests/test_dependency_direction.py`](../../tests/test_dependency_direction.py) and [`tests/runtime/test_runtime_mechanism_boundaries.py`](../../tests/runtime/test_runtime_mechanism_boundaries.py) encode selected dependency and mechanism ownership rules. | Exactly the right technique, currently narrower than the architecture. |
| Domain discipline | [`docs/CONTEXT.md`](../CONTEXT.md), [`docs/internals/ARCHITECTURE.md`](../internals/ARCHITECTURE.md), coding principles, and ADRs define vocabulary and important decisions. | Strong foundation for deeper module contracts. |
| Realistic QA planning | [Issue #246](https://github.com/boldaxolotl/Booley/issues/246) already plans a public, versioned end-to-end scenario suite, while [issue #249](https://github.com/boldaxolotl/Booley/issues/249) specifies failure, recovery, checkpoint, evidence, and cleanup semantics. | Continue this work; do not create a competing “E2E strategy.” |

The central conclusion is that the CI effort succeeded and should now be treated as
infrastructure on which to build, not replaced.

## Repository signals and caveats

The following measurements were taken from the snapshot above using `rg`, `wc`,
Python's `ast` module, `git log`, and read-only GitHub API queries:

- 370 Python source files and 141,682 physical lines under `src/booley`;
- 393 Python test files and 148,007 physical lines under `tests`;
- 9,106 pytest tests collected before two local-environment MCP import errors; hosted
  CI, not this workstation collection, remains the authoritative test result;
- 24,686 Rust lines under `crates/bwave` including source and tests;
- 966 commits between 18 AUG and 02 SEP 2026;
- 205 pull requests at the measurement point: median 7 changed files and 296 added or
  deleted lines; 49 PRs exceeded 1,000 changed lines;
- 63 issues at the measurement point, including 20 labelled bugs and 5 open bugs;
- the largest Python modules are `harness/doctor.py` (7,084 lines),
  `flows/sim/flow.py` (4,224), and `mcp/server.py` (3,593);
- an AST span count finds 271 Python functions longer than the 50-line rule in
  [`CODING_PRINCIPLES.md`](../internals/CODING_PRINCIPLES.md), including 23 over 100
  lines;
- a conservative static import graph places 18 top-level `booley` packages in one
  strongly connected component. This includes local and conditional imports, so it
  is a diagnostic signal rather than proof that every edge is architecturally wrong;
- `harness/booley.py` imports from approximately 45 internal module prefixes across
  13 top-level packages; `harness/doctor.py` imports from about 35 across 12;
- Pyright uses `basic` mode and includes the complete `core` package plus three
  boundary/result modules; four files are strict. Roughly 1% of Python source files
  are therefore explicitly strict today;
- the Python project has no committed dependency lock or constraints file, while
  Cargo uses `Cargo.lock` and CI invokes Cargo with `--locked`;
- no CodeQL or dependency-review workflow is present in the checkout.

Physical lines, import counts, and PR size do not measure design quality by
themselves. Large modules can be deep modules, local imports can be deliberate, and
large generated or mechanical PRs may be easy to review. These numbers matter here
because several point in the same direction: very rapid growth, repeated churn in
composition modules, broad dependency reach, and explicit repository principles that
are not yet mechanically ratcheted.

## Prioritized recommendations

| Priority | Practice | Why now | First decision gate |
|---|---|---|---|
| P0 | Architecture fitness functions and incremental module deepening | Prevent the current cyclic/fan-out pattern from becoming the permanent shape of the product. | Can an allowed dependency map cover the current architecture with a small, explicit legacy exception set? |
| P0 | Incremental strict typing at seams | Catch invalid state and contract misuse before runtime while interfaces are being extracted. | Can one high-risk package become fully included and selected boundaries strict without blanket ignores? |
| P1 | Property/state-machine testing plus targeted fault injection | Explore sequences hand-written tests miss, especially recovery and lifecycle transitions. | Does a pilot find a defect or materially simplify an invariant within two weeks? |
| P1 | Reproducible dependency lanes | Separate “known-good reproducible” from “compatible with newest dependencies.” | Can one named Python lock cover the canonical Linux CI/release environment without weakening the compatibility matrix? |
| P1 | Compatibility inventory and fixtures | Make persisted and public interfaces deliberately evolvable rather than accidentally stable. | Can every versioned artifact name an owner, schema/version, compatibility promise, and fixture? |
| P1 | Threat modeling and low-cost supply-chain controls | Booley executes untrusted project code, handles credentials, drives containers, and publishes executable artifacts. | Do default CodeQL, dependency review, and an attack-surface review produce actionable findings with tolerable noise? |
| P2 | Small-change review and defect-learning loop | Reduce review blind spots and use real escaped defects to choose the next check. | Can bug/PR metadata answer “which check should have caught this?” without a dashboard project? |
| P2 | Performance regression budgets | B-Wave and CI already collect timings but do not consistently gate regressions. | Are stable-runner measurements repeatable enough to set a useful threshold? |

### 1. Make architecture rules executable and ratcheted (P0)

Parnas's original modularity result is still the right criterion: modules should hide
changeable design decisions, rather than merely divide execution steps
([D. L. Parnas, 1972](https://doi.org/10.1145/361598.361623)). That matches Booley's
own “deep modules, shallow interfaces” and policy/infrastructure separation rules.

**Adopt:**

1. Draw a one-page intended package graph using existing domain terms: domain/value
   packages, shared Runtime mechanisms, Flow implementations, Ticket Board, adapters,
   and composition roots. Mark the few modules allowed to compose across layers.
2. Extend the existing AST dependency tests to enforce that graph. Baseline current
   cycles as named debt; fail only on a new forbidden edge or expansion of a legacy
   strongly connected component. Every exception should state the design reason and
   an owner, not just a filename.
3. Add three cheap ratchets rather than universal hard limits:
   - no new function over 50 lines unless the same documented exception process used
     by Ruff explains why it is one cohesive state machine or dispatch;
   - no increase in the dependency fan-out of the top composition hotspots;
   - no new production module above an agreed review threshold without an interface
     note explaining what complexity it hides.
4. Refactor only along observed change axes. `doctor.py` has been touched in roughly
   48 commits and remains the largest module even after the closed decomposition work
   in [issue #26](https://github.com/boldaxolotl/Booley/issues/26). That does not imply
   “split by size”; it implies inspecting which groups of checks change independently
   and can move behind a smaller audit interface.
5. Require every newly extracted domain module to have an explicit public interface,
   dependency-direction test, strict typing, and contract-level tests. This makes a
   refactor improve navigability rather than merely redistribute lines.

**Measure:** number and size of internal import SCCs; forbidden-edge exceptions;
fan-out of the top ten modules; count of >50-line functions; and change concentration
in the top ten files. Ratchet from the measured baseline—do not fail the entire
existing codebase on day one.

**Expected payoff:** safer parallel work by agents and humans, smaller context needed
to change a feature, and fewer fixes that have to coordinate multiple packages.

### 2. Expand static typing by package and by trust boundary (P0)

Pyright explicitly supports incremental adoption, file/directory `strict` scopes,
and a progression from baseline analysis to strict packages
([Pyright's official adoption guide](https://github.com/microsoft/pyright/blob/main/docs/getting-started.md?plain=1),
[configuration reference](https://github.com/microsoft/pyright/blob/main/docs/configuration.md?plain=1)).
Booley is already using the mechanism correctly; the gap is coverage.

**Adopt:**

1. Expand `include` one coherent package at a time, not file by file indefinitely.
   Start with packages where invalid state crosses a durable or external boundary:
   `runtime` record/identity modules, `targets`, `criteria`, `ticket_board` value and
   lifecycle modules, and configuration decoding.
2. Make all new production modules strict by default. Make newly extracted interfaces
   strict before moving callers.
3. Use typed values for identities that are currently easy to confuse: durable Target
   identity versus callable selector, Project root, immutable image ID, run ID,
   execution ID, and persisted state/version. The open typed-contract work in
   [issues #259](https://github.com/boldaxolotl/Booley/issues/259) through
   [#265](https://github.com/boldaxolotl/Booley/issues/265)
   is an ideal vehicle; this recommendation should strengthen that work, not create a
   parallel abstraction programme.
4. Track strict-source percentage and unknown/`Any` at public interfaces. Do not use a
   global `Any` count as a gate: adapters around untyped SDKs legitimately need it.
5. Keep the existing “scope probe must fail” pattern each time a package is added, so
   type-checking coverage cannot silently shrink.

**Do not:** switch all 142k lines to strict mode in one PR, add blanket ignores, or
wrap every dependency in speculative interfaces. The goal is precise seams, not type
annotation volume.

### 3. Generate state sequences, not only input examples (P1)

Hypothesis can generate values, shrink failures to smaller counterexamples, and run
rule-based state machines with invariants after generated transitions
([Hypothesis project documentation](https://github.com/HypothesisWorks/hypothesis),
[stateful API](https://hypothesis.readthedocs.io/en/latest/reference/api.html#stateful-tests)).
This complements Booley's hand-written regression tests and the Rust crate's existing
proptest/fuzzing; it does not replace them.

**Pilot three narrow properties:**

1. **Pure identity/serialization properties:** parsing then rendering a Target handle,
   configuration fragment, result record, or criteria state either round-trips or
   fails with the documented specific error. Equivalent representations normalize to
   the same durable identity.
2. **Lifecycle state-machine properties:** generated Ticket/Criteria/Job transitions
   never permit an invalid terminal state, never turn terminal evidence nonterminal,
   and make idempotent operations stable under repetition.
3. **Atomic-publication properties:** inject failure before and after each abstract
   write/rename/commit point against a fake filesystem or Docker adapter, then assert
   the old or new complete state is visible, never a mixed successful state.

Run a small deterministic profile on PRs and a longer seeded profile in scheduled
deep tests. Persist counterexamples as ordinary regression tests or in Hypothesis's
example database. Keep OS-process and Docker exploration in bounded integration tests;
randomizing real process timing on every PR would create noise.

This work should explicitly join existing plans:

- [Issue #258](https://github.com/boldaxolotl/Booley/issues/258) already specifies a
  fault matrix for interrupted Session refresh and exact restore/commit invariants.
- [Issue #262](https://github.com/boldaxolotl/Booley/issues/262) already specifies the
  missing real-process supervisor-death integration test.
- [Issues #246](https://github.com/boldaxolotl/Booley/issues/246) and
  [#249](https://github.com/boldaxolotl/Booley/issues/249) already own realistic
  product-level QA scenarios and recovery evidence.

Generated pure/model tests should cover the combinatorial state space below those
real-system checks; they should not duplicate the scenario suite.

**Optional two-day design experiment:** model only the Session-refresh journal from
issue #258 in PlusCal/TLA+ and use TLC to enumerate crashes/retries around its state
transitions. TLC is the official TLA+ model checker
([TLA+ tools](https://github.com/tlaplus/tlaplus)); TLA+ is intended to specify and
check concurrent-system designs
([Lamport, “Specifying Concurrent Systems with TLA+”](https://www.microsoft.com/en-us/research/publication/specifying-concurrent-systems-tla/)).
Keep the model only if it finds a missing transition, proves the invariant clearer
than prose, or can be checked cheaply in CI. A model does not prove the Python code
implements it.

### 4. Split reproducibility from dependency compatibility (P1)

The new standardized `pylock.toml` format exists specifically to describe dependencies
for reproducible Python environments and permits named locks for distinct environments
([PyPA specification](https://packaging.python.org/en/latest/specifications/pylock-toml/)).
Tool support is new as of this research date, so Booley should verify support rather
than adopt a particular resolver on faith.

**Adopt two intentional lanes:**

1. **Known-good lane:** commit a lock or hashed constraints input for the canonical
   Linux/Python CI and release build. Build release artifacts from that resolved set.
   Record the lock digest with release evidence.
2. **Compatibility lane:** retain the OS/Python matrix and add scheduled “latest
   allowed dependencies” resolution. Dependabot updates the known-good set only after
   that lane and the regular suite pass. If minimum supported direct-dependency
   versions are a real promise, add a separate, explicit minimums test rather than
   assuming `>=` metadata proves it.

Keep broad dependency ranges in package metadata for users; a development/release
lock is not the same as pinning every transitive dependency for consumers. Cargo's
existing `--locked` practice is the model for intent, not necessarily the Python tool
choice.

### 5. Inventory and test compatibility contracts (P1)

Semantic Versioning requires a declared public API before version numbers can
communicate compatibility; version `0.y.z` is explicitly initial development
([SemVer 2.0.0](https://semver.org/)). Booley can remain `0.x` while still declaring
what it tries not to break.

Create a small compatibility inventory with one row per externally or durably visible
contract:

- CLI commands, exit codes, and machine-readable output;
- `booley.toml`, Targets, Criteria, Ticket and scenario formats;
- execution records, journals, evidence, acceptance data, and upgrade state;
- MCP tool names and schemas;
- B-Wave JSON schema and command output;
- Python entry points intended for external import;
- Session Image labels and release artifact naming.

Each row should name the owner, version marker/schema, reader/writer, compatibility
window, migration behavior, canonical fixtures, and deprecation rule. For persisted
formats, retain representative fixtures from at least the last supported release and
test “old read by new.” For protocols with independent producers, preserve real
sanitized producer samples—exactly the direction of
[issue #264](https://github.com/boldaxolotl/Booley/issues/264).

Do not snapshot every help string or JSON byte order. Test semantic contracts and
explicitly documented text only. Booley's packaged changelog and upgrade-review flow
already provide the user communication channel; this practice supplies the executable
compatibility evidence behind it.

### 6. Add a small secure-development baseline (P1)

NIST's Secure Software Development Framework recommends threat/attack-surface
modeling, tracking security decisions, verifying release integrity, and retaining
component provenance/SBOM information
([NIST SP 800-218](https://csrc.nist.gov/pubs/sp/800/218/final), especially PW.1 and
PS.2–PS.3). These are proportionate for Booley because the product crosses unusually
powerful boundaries: untrusted Projects, agent execution, credentials, container and
network policy, host-provisioned EDA files, Git operations, and published executables.

**First, low-cost controls:**

1. Enable CodeQL default setup for Python and Rust if repository settings confirm both
   languages are supported; GitHub recommends default setup for eligible repositories
   and automatically updates detected languages
   ([GitHub CodeQL setup documentation](https://docs.github.com/en/code-security/concepts/code-scanning/setup-types)).
2. Add the dependency-review action to PRs that change dependency manifests or locks.
   It can fail when a PR introduces a known-vulnerable dependency
   ([GitHub dependency review documentation](https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/reviewing-dependency-changes-in-a-pull-request)).
3. Write one concise attack-surface map referencing existing Session Runtime and host
   authority documentation. For each trust boundary, list assets, attacker/control,
   allowed data flow, fail-closed behavior, and the test/evidence that verifies it.
   Record accepted exceptions as security ADRs.
4. Verify and document the PyPI attestations already expected from the official
   Trusted Publisher action. Attestations bind an artifact digest to the publishing
   identity but do not prove the code is trustworthy
   ([PyPI security model](https://docs.pypi.org/attestations/security-model/)).
5. Add provenance and SPDX/CycloneDX SBOM attestations for the native B-Wave release
   and published Session Images. GitHub supports verifiable provenance and SBOM
   attestations for binaries and containers
   ([GitHub artifact attestations](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations));
   Docker BuildKit can attach SPDX SBOMs to images
   ([Docker SBOM documentation](https://docs.docker.com/build/metadata/attestations/sbom/)).

SLSA defines provenance as verifiable information about where, when, and how an
artifact was produced; its levels are intentionally incremental
([SLSA 1.2 provenance](https://slsa.dev/spec/v1.2/provenance),
[build levels](https://slsa.dev/spec/v1.2/build-track-basics)). Aim first for useful,
verifiable evidence—not a badge or a costly level claim.

### 7. Make review smaller and defects teach the process (P2)

Google's published engineering practice says code review should improve code health
over time and recommends small, self-contained changes
([review standard](https://google.github.io/eng-practices/review/reviewer/standard.html),
[practice overview](https://google.github.io/eng-practices/)). DORA's current guidance
also recommends reducing batch size and measuring both throughput and instability
([DORA delivery metrics](https://dora.dev/guides/dora-metrics/)).

Booley already says “one concern per PR,” but 49 of the first 205 PRs exceeded 1,000
changed lines. Add a **reviewability budget**, not a hard universal line limit:

- explain why a PR over the budget cannot be split into behavior, refactor, generated
  data, or follow-up changes;
- put mechanical changes in separate commits/files and make generated diffs obvious;
- require a second, independent architecture/security review for changes touching
  authority, process ownership, persisted state, release publication, or multiple
  domain packages;
- make the PR description state the invariant, evidence, failure/rollback mode, and
  compatibility impact for those high-risk changes.

Then add three issue labels or structured fields: `escaped-defect`, `ci-gap`, and
`regression`. For each confirmed escaped bug, record only:

1. the release/commit where it entered;
2. why existing checks did not detect it;
3. the smallest permanent prevention (example test, property, architecture rule,
   type, scenario assertion, or no automation justified);
4. time from report to fixed release.

Review this monthly for 15 minutes. Track change lead time, releases requiring an
urgent corrective release, escaped defects per release, and recovery time. Do not
build a metrics platform until the manual record changes a decision.

### 8. Turn existing benchmarks into selective budgets (P2)

Criterion already stores statistical results across runs and can detect regressions
([Criterion.rs documentation](https://criterion-rs.github.io/book/)). Booley has
micro and throughput benchmarks, but ordinary `cargo test --all-targets` is not a
stable performance gate.

Run B-Wave throughput and peak-memory benchmarks on a controlled scheduled runner,
retain baselines by release, and alert on a deliberately generous regression threshold
before making it blocking. Add only two or three Python product budgets initially:
CLI startup/Doctor latency, target inventory on the demo project, and CI wall-clock
time. Performance thresholds on noisy hosted runners should remain advisory until
repeatability is demonstrated.

## Anti-recommendations

1. **Do not rewrite Booley.** The suite and current users embody more knowledge than a
   replacement design document. Use strangler-style interface extraction inside the
   existing codebase and prove equivalence at stable boundaries.
2. **Do not split into microservices.** There is no independent deployment or scaling
   requirement that compensates for network protocols, distributed failure, and more
   release surfaces. First make the in-process package graph coherent.
3. **Do not target 100% coverage.** The current 80% branch/global and 90% changed-line
   ratchets are useful. Spend the next test budget on properties, failure schedules,
   producer fixtures, and mutation effectiveness in defect-prone modules.
4. **Do not run mutation testing over all 142k Python lines.** Expand it only to a
   bounded high-risk package with an explicit score and time budget after ordinary
   tests are fast and deterministic there.
5. **Do not enable strict typing globally in one migration.** Strict new seams and
   package-by-package expansion produce value without thousands of suppressions.
6. **Do not add every available scanner or CI matrix combination.** CodeQL and
   dependency review are low-cost pilots. Keep tools only when findings are actionable
   and overlap is understood.
7. **Do not require TDD for every change.** Require a failing regression for every bug
   and executable acceptance evidence for behavior; allow exploration or refactoring
   to choose the most stable test boundary afterward.
8. **Do not create a second end-to-end QA programme.** Finish the scenario protocol and
   suite under issues #246/#249 and use lower-level generated tests beneath it.
9. **Do not introduce a heavyweight Scrum/process layer.** The bottleneck is not a
   missing meeting cadence. Small PRs, explicit invariants, and defect feedback are
   sufficient process for the current contributor shape.

## Staged adoption roadmap

### Days 0–30: establish baselines and run cheap pilots

- Publish the intended package dependency graph and add a non-expansion AST ratchet.
- Add “strict for new modules”; select the first complete package for Pyright inclusion.
- Add Hypothesis as a test dependency only if the first two or three candidate
  properties can be stated before implementation.
- Finish the real crash-recovery test in #262 and decide #258's guarantee boundary.
- Enable CodeQL default setup and dependency review as non-blocking pilots; triage all
  initial findings before making them required.
- Add `escaped-defect`/`ci-gap` classification and baseline the previous releases.
- Evaluate `pylock.toml`-capable tooling and record which OS/Python environments one
  lock can actually represent.

**Exit evidence:** no new dependency-cycle expansion; one package newly checked; one
state/property pilot; scanner findings triaged; one reproducibility decision note.

### Days 31–90: convert useful pilots into gates

- Make the architecture ratchet and first expanded Pyright package required.
- Add PR-budget explanation and high-risk review checklist.
- Put fast properties on PRs and longer profiles in `deep-tests.yml`; require a
  mutation score only for one bounded, proven campaign.
- Commit the known-good Python dependency input and add the scheduled newest-deps lane.
- Create the compatibility inventory and old-version fixtures for the three most
  durable formats.
- Complete the attack-surface map; publish/verify B-Wave and image provenance plus SBOM.
- Execute the two-day TLA+/PlusCal spike only if #258 remains transition-complex after
  its prose design review.

**Exit evidence:** each new gate has caught a seeded violation; dependency installs are
reproducible in the canonical lane; at least one old fixture is read by current code;
release evidence is verifiable.

### Months 3–6: deepen only where evidence points

- Break one high-value dependency cycle at a time and shrink composition-module fan-out.
- Expand strict checking to the next packages based on bug/churn data.
- Extend property/mutation testing to the defect cluster most often marked `ci-gap`.
- Finish and run the public QA scenario suite from #246; feed its findings into the same
  escaped-defect analysis.
- Add performance budgets only after scheduled measurements prove stable.
- Review whether the compatibility promise is mature enough for a 1.0 public-API
  decision; do not set a date based on code size alone.

## How to know this worked

After three months, the desired outcome is not “more tools.” It is:

- the package dependency graph cannot get worse silently;
- a growing, reported share of production code is type-checked at meaningful seams;
- generated state sequences or fault schedules have found at least one missing case or
  retired a risky manual matrix;
- the same Python dependency input recreates the canonical build, while a separate lane
  still detects upstream incompatibility;
- old persisted fixtures remain readable by current code;
- releases have verifiable provenance and component inventories;
- every escaped bug can say which prevention category, if any, was added;
- PR size and urgent corrective-release rate trend down without slowing ordinary
  delivery.

If a proposed practice cannot name its signal, trial period, and removal condition, it
should remain a research experiment rather than become permanent process.

## Method note

The repository evidence is a point-in-time static audit, not a longitudinal quality
study. GitHub counts came from the public repository API on 02 SEP 2026; author counts
do not reveal who reviewed a PR. Function and import metrics use Python AST/physical
source analysis and intentionally do not claim runtime coupling. Sources outside the
repository are primary or authoritative: official specifications and documentation,
original research, or the owning project's documentation.
