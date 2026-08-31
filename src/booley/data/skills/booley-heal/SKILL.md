---
name: booley-heal
description: Repair a Booley project's environment, configuration, and build-system health by running Doctor, resolving every actionable FAIL and WARN, reviewing packaged release changes after a version update, and verifying the result with plain and deep checks. Use when `booley doctor` is red or warning, an automatic Doctor notice reports problems, Booley reports a pending upgrade review, setup has drifted, a Booley Flow, Specialist, EDA tool, or Target stopped working, or the user asks to heal, repair, or make Doctor green. Hand host-only or otherwise external actions to the user with exact instructions, and route verified Booley or documentation defects through `booley-feedback`.
---

# Heal a Booley project

Turn Doctor findings into a verified clean project. Doctor remains the source
of truth; this skill supplies the diagnosis, repair loop, external-action hand-offs, and
completion discipline around it.

Do not turn this into a second setup workflow. Repair the project as it exists,
preserve its decisions, and avoid creating `SETUP-PLAN.md` or rerunning
`booley-setup` unless Doctor explicitly proves setup is absent.

## Success contract

Call the project **healed** only when every required final invocation reports:

- zero `FAIL` findings;
- zero active `WARN` findings;
- no unexpected `SKIP` on a check used as evidence; and
- no unresolved external action.

An exit code of zero is insufficient because warnings do not change Doctor's
exit code. `WAIVED` is not active, but report it as an accepted risk. `NOTE` is
informational. A `SKIP` can be legitimate; inspect it rather than treating all
skips as failures or silently counting one as a pass.

Use one of these outcomes when the full contract is not met:

- **Locally clean — user action required**: the Session Runtime is clean, but a host,
  credential, license, service, GUI, or other external action remains.
- **Booley defect — feedback captured**: source inspection confirms Booley or
  its documentation is wrong. State whether a workaround restored health.
- **Project/design issue**: Doctor exposed an RTL, testbench, or intentional
  project-design problem outside build-system repair.
- **Repair blocked**: a safe repair made no progress or needs a project choice.

Never describe one of those partial outcomes as healed.

## Guardrails

- Run from the project repository root and inspect `git status --short` before
  changing anything. Preserve all pre-existing changes and never revert,
  overwrite, stage, or commit them.
- Treat project submodules discovered from `.gitmodules` as read-only.
- Fix environment and build-system integration. Do not change RTL or testbench
  behavior unless the user explicitly expands the task.
- Do not install host packages, change host services, edit host credentials,
  restart license daemons, rebuild external infrastructure, or make another
  out-of-container change. Give the user a hand-off instead.
- Do not create a Doctor waiver merely to make the output green. A waiver is a
  project decision: explain the risk and obtain explicit acceptance first.
- Prefer the smallest repair that addresses the demonstrated root cause. Do
  not perform opportunistic cleanup.
- Read Doctor output unabridged. Do not pipe it through `head`, `tail`, or a
  filter that can hide findings or neighboring skips.

## 1. Establish the baseline

1. Before Doctor can refresh any health evidence, record the runtime location,
   `git status --short`, and `booley upgrade status --json`. Doctor is
   context-aware, so do not assume a host result proves the Session Runtime or
   vice versa.
2. Run plain `booley doctor` and retain its complete output. Manual Doctor is
   the repair entry point for limited housekeeping such as guidance links or
   board orphans; include those changes in the evidence and rerun before
   diagnosing remaining rows.
3. Build the active set from every `FAIL` and unwaived `WARN`. Record, where
   present, the severity, message, `fix:` hint, check ID, and subject.
4. Group only findings that demonstrably share one root cause. Repair parsing
   and schema failures before downstream Target, Flow, Specialist, or EDA-tool failures, because the
   latter may be consequences.

Do not begin with `--deep`. Make plain Doctor clean first so expensive Flow
smokes are not diagnosing an already-invalid configuration.

## 2. Review a pending release range

When upgrade status is `current`, continue to diagnosis. For any other status,
preserve its JSON as evidence.

Proceed with a pending review only when the state is readable, the running
version equals `pending_target`, and the packaged changelog contains that exact
release. A `stale-runtime` status requires a Session Runtime refresh or rebuild;
hand that action to the user and stop before acknowledgment. Corrupt or
unavailable state and a missing target entry also block acknowledgment.

Resolve the changelog with:

```console
python3 -c "from booley.runtime.paths import changelog_path; print(changelog_path())"
```

Use `booley.runtime.changelog.releases_between` to read every available entry
in `(reviewed_through, pending_target]`, oldest first. Report the packaged
history boundary when the range begins before the oldest available entry.
Build a concrete checklist from all applicable CLI, configuration, generated
file, Session Runtime, image, compatibility, and manual migration notes. Keep
each item open until source inspection, a repair, or verification demonstrates
its disposition.

During the repair loop, `upgrade.review-pending` is the expected gate rather
than a defect to waive. Every other active warning and every failure remains
actionable.

## 3. Diagnose one causal group

Use evidence in this order:

1. **Doctor's `fix:` hint.** Treat it as the primary repair instruction, then
   verify rather than assuming it worked.
2. **The packaged troubleshooting guide.** Resolve it with:

   ```console
   python3 -c "from booley.runtime.paths import troubleshooting_path; print(troubleshooting_path())"
   ```

   Search that `TROUBLESHOOTING.md` using exact error fragments and concrete nouns from the
   finding. Read the whole matching section, including its commands and runtime-location
   qualifications. The guide documents common residue and pitfalls Doctor cannot
   fully infer; do not force an unrelated recipe onto a finding just because a
   keyword matched.
3. **Named evidence.** Inspect the report directory, `run.log`, stdout, stderr,
   config, `.core` Target, or generated file Doctor identifies.
4. **Booley source.** Use this only when behavior remains contradictory or a
   Booley defect is plausible. Locate the installed source with:

   ```console
   python3 -c "import booley, pathlib; print(pathlib.Path(booley.__file__).parent)"
   ```

Read the code that emits the finding before blaming Booley. Distinguish:

- project configuration or environment → repair locally;
- documentation disagrees with code → documentation defect;
- code emits an impossible, incorrect, or unrepairable result → Booley defect;
- deep smoke exposes incorrect DUT/TB behavior → project/design issue.

## 4. Repair and rerun

Apply one minimal repair for the causal group, then rerun plain Doctor. Compare
the new active findings with the recorded set:

- removed finding → continue;
- changed finding → diagnose the newly exposed cause;
- new unrelated finding → add it to the queue without undoing the valid fix;
- unchanged finding → inspect the attempted repair and evidence before trying a
  different remedy.

Never repeat the same repair against the same finding identity. Track the full
active-set fingerprint (severity, check ID, subject, and message). Stop
automatic repair if a fingerprint repeats or after 12 remediation passes,
whichever comes first. Report the attempted repairs and the evidence needed to
continue. This bound prevents a fix/revert cycle from running forever.

Continue repairing independent local findings even when one finding needs a
user hand-off. Do not let one host-only action hide other useful progress.

## 5. Handle exceptional findings

### External or host-only action

Do not execute an action outside the current Session Runtime. Give the user a
copyable guide containing:

1. **Where:** host OS, named terminal, GUI, license server, or other external system.
2. **Why:** the exact Doctor finding and what is unavailable here.
3. **Do:** minimal numbered commands or UI actions, with placeholders clearly
   marked.
4. **Expect:** the observable successful result.
5. **Verify:** the exact command to rerun, usually host `booley doctor`, and
   what must disappear or change.
6. **Resume:** tell the user to reinvoke `booley-heal` in the Session Runtime.

Never claim that a command was run or a service changed when it was only handed
to the user.

### Deliberate constraint and waiver

Explain the warning's concrete failure mode and the real repair first. If the
user explicitly accepts the constraint, create the narrowest structured entry
in `.booley_project/doctor-waivers.toml` using Doctor's check ID and subject.
Require a specific reason and exactly one expiry date or `permanent = true`.
Rerun the originating command and confirm the row changes from `WARN` to
`WAIVED`; it must not disappear. Never waive a `FAIL`, transient machine issue,
Doctor bug, or fixable defect.

### Verified Booley or documentation defect

Capture the evidence before applying a workaround, then invoke
`/booley-feedback` yourself. Do not merely tell the user to run it later. Pass
the reproduction, observed behavior, expected behavior, Doctor transcript or
log, affected component, and whether source inspection confirmed the defect.

Follow `booley-feedback`'s redaction and approval rules. Logging is local and
must not block healing; public issue or email submission still requires the
user to see and approve the exact outgoing text. If a safe workaround exists,
apply it after capture and continue the Doctor loop without calling the
workaround a Booley success.

## 6. Deep verification and acknowledgment

When plain Doctor has no active findings other than the expected
`upgrade.review-pending` warning, run `booley doctor --deep`. Deep
checks can take minutes or tens of minutes. Run a long invocation in a managed
background session or detached with output redirected to a file, then poll it;
do not abandon it while waiting. Read the final output unabridged.

Repair deep findings through the same classify → diagnose → repair → rerun
loop. A failed deep simulation, lint, or synthesis smoke can be build-system
configuration or a real design problem; use its log to distinguish them before
editing anything.

After deep repairs, run plain Doctor again because deep-side changes can regress
non-deep checks. Then run the final deep check over the exact delivered files.

Changes affecting project configuration, Targets, dependencies, or the Session
Runtime require all of this evidence:

- Session Runtime: final plain `booley doctor`;
- Session Runtime: final `booley doctor --deep`;
- host: final plain `booley doctor`.

Run the invocations available in the current context. Hand unavailable external actions
to the user using the external-action template and wait for its result; do not
infer it. Before acknowledgment, tolerate exactly one active warning:
`upgrade.review-pending` for the recorded target. Any other warning or failure
blocks completion and leaves the pending state unchanged.

When the initial status was `pending`, acknowledge after every required plain,
deep, and host verification succeeds, using the unchanged target from that
initial status:

```console
booley upgrade acknowledge --expected-target <pending_target>
```

For an initially `current` status, skip acknowledgment. The command is a
compare-and-swap: a newer observation, stale runtime, missing
packaged entry, changed target, or unreadable state must fail without changing
the pending review. On success, run final plain `booley doctor` and confirm the
pending warning disappears. If that final Doctor finds a new race or unrelated
problem, report that the version review completed but the project is not
healed; do not reopen the already-acknowledged changelog review.

## 7. Report the result

State the outcome first. Include:

- final pass/fail/warn/waived/note/skip counts for each invocation actually run;
- every file changed and why;
- each repair and the finding it removed;
- every remaining waiver with check ID, subject, reason, and expiry/permanence;
- every external action still required, using the copyable guide; and
- any feedback finding captured, workaround status, and whether anything was
  submitted; and
- the initial and final upgrade status, reviewed changelog range, history gap,
  migration checklist dispositions, and acknowledgment result.

If no file needed changing, say so. Never commit as part of healing unless the
user separately asks for a commit.
