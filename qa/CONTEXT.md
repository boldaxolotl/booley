# Public QA glossary

This is the canonical vocabulary for Booley's public qualification suite. Shared
product concepts such as **Booley Flow**, **Finding**, and **Trace Artifact** are
defined in the [shared glossary](../docs/CONTEXT.md). File shapes and execution
behavior belong in [FORMAT.md](FORMAT.md) and [PROTOCOL.md](PROTOCOL.md).

**Public QA Suite**:
The versioned collection of Journeys, Scenarios, Profiles, Capability Coverage, and protocols used to qualify a released Booley product.
_Avoid_: test suite, CI suite, regression tests

**Journey**:
A representative end-to-end use of Booley whose intent is encoded by one production Scenario.
_Avoid_: test case, workflow, demo

**Scenario**:
The ordered, versioned declaration of one Journey's inputs, Steps, Checks, evidence requirements, recovery, and cleanup.
_Avoid_: script, test, run

**Scenario Operator**:
The coordinator responsible for one QA Run's sequencing, delegated work, evidence integration, resource control, and final report. Delegation does not transfer the Scenario Operator's authority.
_Avoid_: test runner, autonomous campaign, delegate

**Step**:
One ordered unit of work in a Scenario. A Step may own multiple Checks and the resources or recovery instructions needed to perform them.
_Avoid_: Criterion, pipeline stage

**Check**:
One independently observable product claim within a Scenario, with declared stimulus, expectation, authority, and evidence requirement.
_Avoid_: Criterion, assertion, test function

**Profile**:
A versioned qualification scope selecting complete QA Runs, platform and provider identities, Checks, and capability prerequisites before execution.
_Avoid_: configuration, environment, filter

**QA Run**:
One evidence-producing execution of selected Profile work against exact product, suite, input, platform, provider, and tool identities.
_Avoid_: Job, Ticket run, test invocation

**Run Result**:
An append-only observation recording one Check's outcome for one attempt in a QA Run.
_Avoid_: Criterion result, verdict, summary

**Capability**:
One supported, publicly sourced product behavior inventoried by the Public QA Suite.
_Avoid_: feature flag, Check, Criterion

**Capability Coverage**:
The mapping between inventoried Capabilities and the Scenario Checks that exercise them. Capability Coverage establishes representation, not behavioral evidence.
_Avoid_: RTL coverage, Coverage Campaign, code coverage

**Structural Validation**:
Offline checking that suite assets satisfy their schemas, references, selections, ordering, digests, reachability, and budgets. Structural Validation executes no Scenario and produces no Qualification evidence.
_Avoid_: Qualification, product test, QA Run

**Qualification**:
The evidence-backed evaluation of a released Booley product against every Check selected by a Profile.
_Avoid_: Structural Validation, CI pass, test execution

**Profile Verdict**:
The `passed`, `failed`, or `incomplete` Qualification outcome for one Profile.
_Avoid_: aggregate QA verdict, green status

**Operational Completion**:
The separate statement of whether a QA Run completed, reached its deadline, or ended in operator error. It is not a Profile Verdict.
_Avoid_: pass, qualification result
