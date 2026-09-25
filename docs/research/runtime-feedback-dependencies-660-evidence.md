# Issue #660 implementation evidence

Date: 25 SEP 2026.

## Revisions and reproduction

The exact base is `51a59016673f7c76d56efda8028ebcf932ce62f3` with source digest
`sha256:809035ba50549e99b83064df8dd465e3e13bad27d321ffe8f0ae4a08b71e4d10`.
The reviewed implementation is
`d91b0a94ab0c47e67aa7c46214abb4891d6861c9`, with source digest
`sha256:e66fde9fa97fb5b57d33f886610d2045e50efa1ac6a2268ed80a42676878f447`.

```console
python3 tests/architecture/report.py --source-root src/booley --top 40
python3 tests/architecture/compare_report.py \
  --before-ref 51a59016673f7c76d56efda8028ebcf932ce62f3 \
  --after-ref d91b0a94ab0c47e67aa7c46214abb4891d6861c9 --repo-root .
pytest -q tests/architecture
```

Both sides use analyzer digest
`sha256:d7349b67a2b80a9b77f8a31a143ddac1d95fd8692d4a951984a5b6d8b9522297`.

## Graph result

| Diagnostic | Base | Implementation |
| --- | ---: | ---: |
| Python modules | 548 | 552 |
| Located dependency facts | 2,830 | 2,842 |
| Unique normalized edges | 2,345 | 2,354 |
| Direct mutual package pairs | 8 | 6 |
| Nontrivial SCC sizes | 11 and 2 | 11 and 2 |

The removed mutual pairs are exactly `booley.dev_support <-> booley.runtime` and
`booley.feedback <-> booley.harness`. `booley.commit_policy` is an acyclic
singleton; neither approved nontrivial SCC changes membership.

Added edges:

```text
booley.commit_policy -> booley.commit_policy.policy
booley.commit_policy -> booley.commit_policy.validation
booley.commit_policy.policy -> booley.core.boundary
booley.commit_policy.policy -> booley.core.checkout_role
booley.commit_policy.validation -> booley.commit_policy.policy
booley.dev_support.commit_message_format -> booley.commit_policy
booley.dev_support.commit_msg_utils -> booley.commit_policy.policy
booley.dev_support.scope_precommit_hook -> booley.commit_policy
booley.dev_support.validate_commit_msg -> booley.commit_policy
booley.dev_support.validate_commit_msg -> booley.commit_policy.policy
booley.harness.booley -> booley.feedback.storage
booley.harness.booley -> booley.harness.feedback_environment
booley.harness.feedback_environment -> booley.feedback.render
booley.harness.feedback_environment -> booley.harness.doctor_stamp
booley.harness.init_cmd -> booley.commit_policy
booley.harness.setup.cleanup -> booley.feedback.render
booley.harness.setup.cleanup -> booley.harness.feedback_environment
booley.harness.setup.workspace -> booley.commit_policy
booley.runtime.git -> booley.commit_policy
booley.specialists.specialist -> booley.commit_policy
```

Removed edges:

```text
booley.dev_support.commit_message_format -> booley.dev_support.validate_commit_msg
booley.dev_support.commit_msg_utils -> booley.runtime.checkout_role
booley.dev_support.scope_precommit_hook -> booley.dev_support.commit_msg_utils
booley.dev_support.validate_commit_msg -> booley.dev_support.commit_msg_utils
booley.feedback.render -> booley.harness.doctor_stamp
booley.harness.init_cmd -> booley.dev_support.commit_msg_utils
booley.harness.setup.workspace -> booley.dev_support.commit_msg_utils
booley.harness.setup.workspace -> booley.dev_support.validate_commit_msg
booley.runtime.git -> booley.dev_support.validate_commit_msg
booley.specialists.specialist -> booley.dev_support.commit_msg_utils
booley.specialists.specialist -> booley.dev_support.validate_commit_msg
```

The only named composition-hotspot change is `booley.harness.booley`, 59 to 61,
because the command root now resolves Feedback storage and the Harness-owned Doctor
observation explicitly. Project Setup workspace fan-out falls 22 to 21 and Specialist
fan-out falls 18 to 17.

## Shared commit-policy owner

`booley.commit_policy.policy` owns the immutable `StealthPolicy`, Project TOML
loading, source-checkout exemption through `booley.core.checkout_role`, phrase
matching and redaction. `booley.commit_policy.validation` owns the allowed type tuple
and reusable `validate_message()` implementation. Deleting this package would force
Runtime, Specialists, Project Setup, Project Initialization, commit formatting, the
development command, and standalone hooks to reproduce policy or validation.

The old Dev Support modules are adapters. Identity tests prove packaged adapters,
Runtime, Project Setup, and Specialists bind the canonical objects. Fixture-driven
tests cover configured convention, body cap, banned phrases, merge exemption, CRLF,
and source-checkout exemption.

The Project Git-hook bundle retains schema 1 and deterministic content hashing. Its
inventory adds `booley_commit_policy.py` and `booley_commit_validation.py`; package
imports and `python -I -S` flat imports therefore share the same source. The legacy
cleanup set is frozen to the seven historically retired names, and regression tests
prove loose source and bytecode matching either new bundle-only name remain untouched.
Package, absolute-path, isolated flat-bundle, missing-runner fallback, reconciliation,
rollback, previous-manifest, foreign-hook, and idempotent-install tests all pass.

## Feedback environment injection

Feedback no longer imports Harness. `Environment` remains the value crossing the seam;
direct callers collect process/package facts with Doctor status unrecorded. Harness
resolves a stamp only for `feedback report`, `feedback export`, and Project Setup
cleanup. Only a literal boolean `deep` value crosses the seam. Missing, corrupt,
unreadable, non-object, missing, and non-boolean observations degrade to `not recorded`.

Report and export receive one explicit environment. Attachment materialization reuses
the same instance for its before and after proofs, so a stamp changing mid-operation
cannot cause a false equivalence failure. Tests preserve `yes`, `no`, and
`not recorded` rendering.

## Verification

```text
post-review focused policy/hooks/bundle/Feedback/architecture suite: 429 passed
pytest -q tests/architecture: 275 passed
python -m build: passed; sdist and wheel include booley/commit_policy
pytest -q tests/: 13,257 passed, 67 skipped
ruff check src/ tests/: passed
ruff check .: passed
ruff format --check .: passed (1,443 files)
git diff --check: passed
```

The repository's cached console scripts embedded a retired staging path, so the
successful broad run used a disposable launcher directory backed by the readiness
environment's interpreter and installed packages. That preserves the pinned dependency
set while providing working `python` and `fusesoc` executables to child-process tests.
