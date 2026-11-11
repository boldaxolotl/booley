# Review commits outside measurement

Status: accepted (2026-11-10; renumbered from 0087 on 2026-11-11, because
[ADR 0087](0087-admit-jobs-against-a-measured-memory-budget.md) already had the
number; amended 2026-11-11: reviews are declared in `reviews.toml`, see
Amendments)

Supersedes ADR 0074's Goal-owned review contract once Goal Mode is removed
(0.5.0). ADR 0074's `--verify` and `--waive` mechanics stay. Decided in the
triage of [#3064](https://github.com/boldaxolotl/booley/issues/3064), which
builds on Metric-Driven Development
([#3045](https://github.com/boldaxolotl/booley/issues/3045),
[ADR 0092](0092-measure-commits-against-target-metrics.md)).

## Context

Metric-Driven Development attaches metrics to Targets in `metrics.toml`, and
`booley measure` turns a commit into a **Measured Commit**. It is purely
numeric, so a local verdict and a CI verdict mean the same thing. Goal Mode,
which supplied the Reviewer's scope and spec from the Goal Record, goes away.
Reviews still need a home that the Developer Agent cannot weaken. An agent
that picks its own review scope and spec can write a lax spec for its own
work.

## Decision

Reviews are declared per Target, in a file of their own beside
`metrics.toml`, and produce a **Reviewed Commit**. They never take part in
`booley measure`.

- **Declared Reviews.** A `[<target>.review]` table in `reviews.toml` lists
  focuses per role (`rtl`, `tb`), a `spec` path, a `resolve` threshold
  (`critical`, `major` or `minor`; default `major`, meaning no open major or
  critical finding) and `exclude` globs. `[[<target>.review.paths]]` entries
  add focuses for matching files. `[defaults.review]` fills in Targets that
  have their own review table and never creates one. A focus that does not
  fit the role, a glob that matches firmware or nothing, and a `spec` focus
  without a `spec` are load errors.
- **`booley review [<sha>]`.** It reviews a commit in the same clean exported
  checkout and against the same Base as `booley measure`. Only Targets
  and roles with changed files are reviewed, and identical reviews are merged
  into one Reviewer call. The Reviewer reads the changed files and the files
  that use them. Only findings the change introduced need resolving;
  pre-existing problems are reported as non-blocking observations. The spec
  and the commit message are the review's context.
- **One bounded loop.** Discovery runs once. The Developer Agent fixes or
  waives every introduced finding at or above `resolve`, commits, and runs
  `booley review --verify`, which re-checks only those findings and adds none.
  What remains open is reported for the human.
- **Reviewed Commit.** The commit on which verify leaves nothing open at or
  above `resolve`. It is a record of what happened on that sha, not a
  reproducible property, and never gates CI. It is independent of Measured
  Commit; the user may ask for either or both.
- **Review Run.** One stored `booley review` run, kept under
  `<booley_dir>/reviews/<sha>/`. A verify run links to the discovery run it
  checked. Its report lists waivers first and every fix made for a finding.
- **Firmware** is read-only context for every Reviewer until a firmware
  Reviewer exists. No Reviewer reports findings in it.
- **Hand-run reviews stay** for mid-work checks. `booley specialist reviewer`
  takes the spec as `--spec <path>` or `--spec-text "<text>"` and exits nonzero
  only with `--fail-on`. It never makes a commit Reviewed.

## Considered Options

- **Reviews as metrics inside `booley measure`.** One mechanism, but an LLM
  review of the same commit can differ between runs, and no bound absorbs that
  noise. It would also bring back the local-versus-CI gap that Metric-Driven
  Development removed.
- **Review defaults applied only when the Reviewer is hand-run.** Nothing runs
  automatically, so the user would still have to ask for each review.
- **Booley capturing the user's request as the spec.** It needs hooks in every
  agent client, and in a long chat it is unclear which prompt is the request.

## Consequences

- A Reviewed Commit is advisory: the Developer Agent drives the loop and may
  waive findings. The report, with waivers first, is the safeguard.
- Goal review verdicts (`clean`, `done`, `no_major`, `no_critical`) are
  replaced by the `resolve` threshold.

## Amendments (2026-11-11)

Decided while finishing the design of
[#3045](https://github.com/boldaxolotl/booley/issues/3045).

- **Reviews are declared in `reviews.toml`**, a file in the Booley Directory
  beside `metrics.toml`, not in `metrics.toml` as first decided. The table
  names are unchanged: `[<target>.review]`, `[[<target>.review.paths]]` and
  `[defaults.review]`. `metrics.toml` then never nests deeper than
  `[<target>.<flow>]`, and no Booley Flow name is reserved for reviews.
- **`reviews.toml` is a protected path in the Measurement Report**: an edit to
  it is listed there and never blocks. Classifying a change to the review
  rules (tightened or loosened) belongs to the Review Report, not to
  `booley measure`.
