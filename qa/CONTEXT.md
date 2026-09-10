# Public QA glossary

This is the canonical vocabulary for Booley's public qualification suite. Shared
product concepts such as **Booley Flow** and **Trace Artifact** are defined in
the [shared glossary](../docs/CONTEXT.md); **Finding** belongs to the
[Feedback glossary](../src/booley/feedback/CONTEXT.md). Scenario definition rules
belong in the [authoring guide](user/AUTHORING.md); run-record files and execution
behavior belong in [FORMAT.md](agents/FORMAT.md) and
[PROTOCOL.md](agents/PROTOCOL.md), respectively.

## Language

**Public QA Suite**:
The versioned collection of Scenarios, Capability Coverage, and shared execution and reporting rules used to qualify a specific Booley product revision.
_Avoid_: test suite, CI suite, regression tests

**Capability**:
One supported, publicly sourced product behavior inventoried by the Public QA Suite.
_Avoid_: feature flag, Check, Criterion

**Scenario**:
One representative, end-to-end use of Booley, expressed as an ordered, versioned declaration of its inputs, Configured Scenarios, Steps, Checks, evidence requirements, recovery, and cleanup.
_Avoid_: script, test, run

**Step**:
One ordered unit of work in a Scenario. A Step may own multiple Checks and the resources or recovery instructions needed to perform them.
_Avoid_: Criterion, pipeline stage

**Check**:
One independently observable product claim within a Scenario, with declared stimulus, expectation, authority, and evidence requirement.
_Avoid_: Criterion, assertion, test function

**Capability Coverage**:
The mapping between inventoried Capabilities and the Scenario Checks that exercise them. Capability Coverage establishes representation, not behavioral evidence.
_Avoid_: RTL coverage, Coverage Campaign, code coverage

**Configured Scenario**:
A Scenario with one specific choice of execution parameters, pre-run requirements, selected Check sets, and justified exclusions. It also states whether Qualification requires a Scenario Run with that configuration.
_Avoid_: Profile, run, environment matrix

**Structural Validation**:
A static consistency evaluation of Public QA Suite assets, distinct from evidence-producing Qualification.
_Avoid_: Qualification, product test, Scenario Run

**Human Maintainer**:
The person who initiates and authorizes a Scenario Run, selects a Configured Scenario, and supplies its required inputs.
_Avoid_: operator, requester, coordinator

**Scenario Operator**:
The agent responsible for executing one Scenario Run within the authority granted by the Human Maintainer.
_Avoid_: test runner, coordinator agent, autonomous campaign, delegate

**Scenario Run**:
One execution of a Configured Scenario with exact product, suite, input, tool, and environment identities. It produces Check evidence.
_Avoid_: QA Run, Job, Ticket run, test invocation

**Check Result**:
One immutable observation of a Check attempt within a Scenario Run.
_Avoid_: Run Result, Criterion result, verdict, summary

**Scenario Run Outcome**:
The evaluation of one Scenario Run's evidence against its selected Checks. Its value is `passed`, `failed`, or `incomplete`, independently of whether execution completed, reached its deadline, or ended in operator error.
_Avoid_: Qualification, Profile Verdict

**Qualification**:
The aggregate evaluation of Scenario Run Outcomes for one Booley product revision. It is `passed` only when a Scenario Run against every required Configured Scenario passed, `failed` when any required run failed, and otherwise `incomplete`.
_Avoid_: Structural Validation, CI pass, test execution
