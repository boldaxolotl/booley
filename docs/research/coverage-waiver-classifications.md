# Coverage waiver and exclusion classifications in other systems

Accessed: **2026-08-31**

## Question

How do other hardware and software coverage systems classify waivers or
exclusions, and do they use the same two semantic reasons Booley is considering:
`excluded` for an intentional, Target-specific denominator exclusion and
`unreachable` when formal evidence proves that a point cannot be reached?

## Short answer

**No surveyed system publishes those exact two values as a universal,
closed reason taxonomy.** Most open tools expose exclusion *mechanisms*—source
directives, filters, or ignored regions—without requiring a semantic reason.
Commercial hardware tools publicly describe both deliberate/manual exclusions
and formally proven unreachable points, but their detailed schemas and complete
reason enumerations are generally not public.

The closest and most important precedent is Accellera UCIS 1.0. UCIS represents
ordinary exclusion with an `excluded` Boolean and an unconstrained string
`excludedReason`; separately, it represents a coverage item's formally
unreachable status with respect to a particular formal test and records the
formal environment and assumptions. Thus UCIS contains the **same two
concepts**, but not as two mutually exclusive waiver-reason enum values
([UCIS object attributes, pp. 189–190](https://accellera.org/images/downloads/standards/ucis/UCIS_Version_1.0_Final_June-2012.pdf#page=200),
[formally unreachable API, pp. 174–179](https://accellera.org/images/downloads/standards/ucis/UCIS_Version_1.0_Final_June-2012.pdf#page=185)).

Booley should keep the two-reason policy. It is a deliberately stricter
governance layer over the looser mechanisms found elsewhere, and it maps well
to established hardware practice. The reason names must be defined narrowly,
and `unreachable` must carry machine-verifiable proof provenance rather than an
analyst confidence score.

## Comparison

| System | What the primary source actually represents | Semantic reason taxonomy? | Relationship to Booley's proposal |
|---|---|---|---|
| **Accellera UCIS 1.0** | Coverage objects/bins can have `excluded=true` and a free-form `excludedReason`; exclusion removes the object/count from coverage calculation. A separate API marks an exact item formally unreachable for a particular formal test, whose formal environment can include scope and assumptions ([object/bin attributes](https://accellera.org/images/downloads/standards/ucis/UCIS_Version_1.0_Final_June-2012.pdf#page=197), [formal status and environment](https://accellera.org/images/downloads/standards/ucis/UCIS_Version_1.0_Final_June-2012.pdf#page=185)). UCIS also defines flags for exclusions originating from a pragma, file, instance, or automatic process ([UCIS flags](https://accellera.org/images/downloads/standards/ucis/UCIS_Version_1.0_Final_June-2012.pdf#page=350)). | **No closed reason enum.** Generic exclusion has a string reason; formal unreachability is a separate typed status. The flags classify exclusion origin/mechanism, not semantic justification. | **Closest structural match.** Supports both concepts, but Booley adds a stricter two-reason policy and stronger provenance requirements. |
| **Verilator** | `coverage_off`/`coverage_on` and `coverage_block_off` suppress regions; a control file can suppress a file, line range, module/block, and wildcards. Verilator also automatically suppresses branches containing `$stop`, based on its error-check assumption ([coverage suppression](https://verilator.org/guide/latest/simulating.html#suppressing-coverage), [control-file commands](https://verilator.org/guide/latest/control.html#coverage-off)). | **No.** These are collection/suppression mechanisms and one built-in heuristic, not reason records. | Provides neither Booley reason. A directive or wildcard alone is insufficient evidence for an approved Booley waiver. |
| **Siemens Questa Increase Coverage** | Siemens publicly separates manual waivers used to deliberately exclude unused IP configurations from formal analysis that exhaustively identifies dead code and provides formal proof that it can be ignored ([product page](https://eda.sw.siemens.com/en-US/ic/questa-one/formal-verification/increase-coverage/)). | **Conceptual split, not a published enum.** | Strongly aligns with `excluded` versus `unreachable`. The public page does not establish a detailed waiver schema, approval state model, or proof-provenance format. |
| **Cadence Jasper / vManager** | Jasper Design Coverage can automatically exclude or let users manually waive "irrelevant covers" and warns about over-constraint risk ([Jasper COV page](https://www.cadence.com/ja_JP/home/tools/system-design-and-verification/formal-and-static-verification/jasper-verification-platform/jaspergold-design-coverage-verification-app.html)). The separate UNR flow formally explores uncovered simulation points and produces unreachable points for review; only reviewed and accepted unreachables are merged into simulation coverage ([Jasper UNR page](https://www.cadence.com/en_US/home/tools/system-design-and-verification/formal-and-static-verification/jasper-verification-platform/jaspergold-coverage-unreachability-app.html)). | **No public closed enum found.** Public material exposes manual/irrelevant and formal-unreachable pathways. | Conceptually aligns, while also showing why Booley must guard against over-constrained formal environments. Detailed UNR/exclusion user documentation is login-gated ([Cadence technical brief, p. 3](https://login.cadence.com/content/dam/cadence-www/global/en_US/documents/solutions/aerospace-and-defense/aero-defense-program-confidence-tb.pdf#page=3)). |
| **Synopsys VCS / VC Formal FCA** | Synopsys distinguishes inherently unreachable goals from hard-to-hit goals. FCA performs reachability analysis, produces an unreachability exclusion file for VCS, and the documented flow says an unreachable result should be reviewed: expected points can be removed, while unexpected results can reveal a design bug or over-constrained environment ([official coverage-closure article](https://www.synopsys.com/blogs/chip-design/speed-up-simulation-coverage-closure.html), [VC Formal product page](https://www.synopsys.com/verification/static-and-formal-verification/vc-formal.html)). | **No public general reason enum found.** Formal unreachability is a distinct evidence-producing flow. | Strong support for Booley's rule that "hard to hit" is not waivable and that a formal result still needs human interpretation. It does not publicly establish `excluded` as the only other reason. |
| **Aldec Riviera-PRO / Active-HDL** | Aldec publicly documents coverage metrics, a way to exclude a file from a coverage report, and UCIS-compatible ACDB storage ([coverage feature](https://www.aldec.com/en/products/fpga_simulation/active-hdl/feature/982), [support index](https://www.aldec.com/en/support/resources?category=&page=8&products=2&type=)). | **Not publicly verifiable.** The detailed Riviera-PRO manual requires customer-portal sign-in ([manual access page](https://www.aldec.com/en/support/resources/documentation/manuals/1822)). | Public evidence proves exclusion mechanisms, not a reason taxonomy or formal-proof rule. Do not infer either Booley category from the file-exclusion command. |
| **coverage.py** | `# pragma: no cover` excludes code from missing-code reporting, while `# pragma: no branch` suppresses a known partial branch. Built-in patterns also omit placeholders, type-checking branches, and compile-time constants; execution is still recorded for excluded code ([official exclusion guide](https://coverage.readthedocs.io/en/7.13.4/excluding.html)). | **No.** The two pragmas select reporting behavior/metric type, not governance reasons. | `no branch` is not proof of unreachability; neither pragma supplies an approval rationale. |
| **gcovr** | Markers exclude a line, region, function, or only branches; regex options can exclude matching lines, branches, and functions ([exclusion markers](https://www.gcovr.com/en/stable/guide/exclusion-markers.html), [command reference](https://www.gcovr.com/en/stable/manpage.html#exclusion-options)). | **No.** Classification is by granularity/metric, not semantic reason. | Useful mechanism precedent only. Booley's ban on wildcard/file/range waiver candidates is intentionally stricter. |
| **LLVM `llvm-cov`** | `-ignore-filename-regex` skips matching source files from reports ([official command guide](https://llvm.org/docs/CommandGuide/llvm-cov.html#cmdoption-llvm-cov-report-ignore-filename-regex)). | **No.** It is a report filter. | Does not correspond to either reason and carries no point-level evidence. |
| **JaCoCo** | Agent `includes`/`excludes` control which classes collect execution data; report inclusion is separate, and the report cannot tell an instrumentation exclusion from a class that never executed ([official FAQ](https://www.jacoco.org/jacoco/trunk/doc/faq.html#why-do-i-see-classes-in-the-coverage-report-although-i-excluded-them-in-the-jacoco-agent-configuration), [agent API](https://www.jacoco.org/jacoco/trunk/doc/api/org/jacoco/core/runtime/AgentOptions.html)). | **No.** These are collection and report-scope filters. | Illustrates why Booley must not infer a waiver reason from absent measurements or collection configuration. |

## Findings

### 1. The industry does not use one shared meaning of “exclusion”

Across the surveyed systems, “exclude” can mean at least four different things:

1. do not instrument or collect an item (JaCoCo, some tool compile settings);
2. collect it but omit it from a report or missing-code calculation
   (coverage.py, LLVM filters);
3. remove it from a coverage denominator (UCIS exclusion); or
4. classify an uncovered point as impossible under a stated formal environment
   (UCIS formal status and commercial UNR/FCA flows).

These operations are not interchangeable. JaCoCo explicitly warns that a
report cannot distinguish “excluded from instrumentation” from “not executed,”
which is direct evidence against deriving waiver semantics from missing data
([JaCoCo FAQ](https://www.jacoco.org/jacoco/trunk/doc/faq.html#why-do-i-see-classes-in-the-coverage-report-although-i-excluded-them-in-the-jacoco-agent-configuration)).

### 2. UCIS supports Booley's conceptual split, but leaves policy to the producer

UCIS's generic `excludedReason` is a string, not a standard-controlled set of
values. The standard even illustrates an exclusion reason such as “debug
variable,” reinforcing that deliberate design/scope rationale is expected but
not normalized ([UCIS toggle example, p. 200](https://accellera.org/images/downloads/standards/ucis/UCIS_Version_1.0_Final_June-2012.pdf#page=210)).

UCIS separately carries `UCIS_EXCLUDE_PRAGMA`, `UCIS_EXCLUDE_FILE`,
`UCIS_EXCLUDE_INST`, and `UCIS_EXCLUDE_AUTO` flags. These identify how or where
an exclusion originated, not why excluding that point is legitimate
([UCIS flag definitions](https://accellera.org/images/downloads/standards/ucis/UCIS_Version_1.0_Final_June-2012.pdf#page=350)). Its `default`, `ignored`, and
`illegal` excluded-value coveritem categories likewise describe functional
coverage metric modeling rather than approval reasons. Neither set contradicts
the conclusion that UCIS leaves semantic waiver policy to producers.

Formal unreachability is modeled differently. UCIS defines a Boolean formal
status for an exact `(scope, coverindex)` with respect to a particular test; it
also provides formal-environment objects whose scope and associated assumptions
describe the conditions of that run
([UCIS formally unreachable API, pp. 174–175](https://accellera.org/images/downloads/standards/ucis/UCIS_Version_1.0_Final_June-2012.pdf#page=185),
[formal environment, pp. 175–179](https://accellera.org/images/downloads/standards/ucis/UCIS_Version_1.0_Final_June-2012.pdf#page=186)).
The standard notes that assumptions can make an item unreachable. That is why a
bare “formal says unreachable” flag is not enough for Booley: the exact Target,
design revision, constraints, assumptions, tool identity, and proof result all
affect what was proven.

UCIS also says its API does not itself care whether a supplied unreachable point
is “don't-care” or merely “predicted-unreachable,” although applications may
retain that information for warnings if the point is later covered
([UCIS use case, p. 14](https://accellera.org/images/downloads/standards/ucis/UCIS_Version_1.0_Final_June-2012.pdf#page=25)).
Booley should therefore avoid treating every imported UCIS exclusion or
unreachable-like annotation as an approved waiver without validating its origin.

### 3. Commercial hardware workflows converge on two pathways, not necessarily two labels

The public Siemens, Cadence, and Synopsys material consistently exposes:

- a **human/policy pathway** for deliberately irrelevant or unused configuration
  coverage; and
- a **formal pathway** for proving that remaining holes are unreachable.

This is the strongest evidence in favor of Booley's pair. It is not evidence
that every vendor internally stores exactly two reason codes. Public product
pages and datasheets do not publish complete data schemas, approval-state
models, or migration behavior for their exclusion files. Cadence explicitly
gates the detailed UNR user guide, Aldec gates its simulator manual, and no
public detailed Siemens or Synopsys waiver-schema manual was found. Accordingly,
this report makes no claim about proprietary reason enums beyond the workflows
the vendors themselves describe publicly.

Synopsys supplies another important policy distinction: an uncovered point can
be **hard to hit yet reachable**. Formal can generate a path/test for such a
point, while only conclusively unreachable points enter the unreachability
exclusion flow ([Synopsys coverage-closure article](https://www.synopsys.com/blogs/chip-design/speed-up-simulation-coverage-closure.html)).
“Hard to test,” low frequency, schedule pressure, or analyst confidence are
therefore not substitutes for either Booley reason.

### 4. Open-source and software tools mostly classify mechanisms, not justifications

Verilator, coverage.py, gcovr, LLVM, and JaCoCo provide useful syntax and scope
models, but none of the cited primary documentation requires an exclusion
rationale, approval provenance, or formal proof. Some distinguish line versus
branch, region versus file, or collection versus reporting. Those distinctions
answer **where/how** coverage is suppressed, not **why it is legitimate**.

Booley should import such directives only as evidence or candidate hints. It
should not automatically translate `coverage_off`, `no cover`, a filename
filter, or an absent counter into an approved `excluded` or `unreachable`
waiver.

## Recommendation for Booley

Retain exactly these two approved semantic reasons for V1:

1. **`excluded`** — a human-approved statement that this exact Coverage Point
   is outside the verification denominator for this exact Target. Require a
   Target-specific rationale, approver identity, approval time, point identity,
   and source/configuration fingerprints. Typical evidence can describe an
   unused IP configuration, non-product/debug-only logic, or a verification-plan
   non-requirement, but the category must not absorb collection failures,
   unsupported metrics, incomplete Campaigns, difficult stimulus, or deadlines.
2. **`unreachable`** — a human-approved use of an authenticated formal result
   proving that this exact point is unreachable for this exact Target and design
   revision. Require the formal tool/version, proof status, property/point
   mapping, source fingerprint, formal environment, assumptions/constraints,
   and immutable result reference. A timeout, bounded/incomplete result,
   heuristic prediction, source inspection, or model confidence is not proof.

Keep `waived` as the single denominator disposition and keep the reason separate.
This resembles UCIS's separation between exclusion state/reason and formal
status while presenting a much smaller, deterministic policy surface.

For interoperability, preserve imported vendor data without elevating it:

- map a generic UCIS/vendor exclusion to an **unapproved `excluded` candidate**
  unless Booley can validate local approval provenance;
- map formal-unreachable data to an **unapproved `unreachable` candidate** unless
  all required proof provenance and fingerprints match;
- map suppression directives and report filters to **candidate evidence only**;
  and
- leave ambiguous, predicted-unreachable, hard-to-hit, incomplete, stale, or
  incompatible evidence as `investigate`, with the point still eligible.

This policy is narrower than common tools by design. It prevents the Coverage
Analyst from converting a mechanism, a plausible explanation, or confidence
language into authority to alter the denominator.
