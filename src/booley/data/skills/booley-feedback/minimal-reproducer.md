# Build a safe minimal reproducer

For suspected Booley defects depending on private RTL, testbench, configuration,
logs, or design structure, build a standalone synthetic toy exercising the same
failure. Original material and intermediate reductions remain private and local;
only the accepted capsule may be attached and exported for inspection.

## The acceptance contract

Every gate must pass:

| Gate | Required result |
| --- | --- |
| Standalone | Runs without the original repository, project data, source, generated artifacts, or environment variables. |
| Synthetic | Identifiers, comments, constants, test vectors, hierarchy, and logic are written for the toy; renaming reduced private RTL does not qualify. |
| Equivalent | Same Booley component, source path, and stable diagnostic fingerprint (§3). |
| Repeatable | Fails again in a clean toy workspace; run twice when practical and disclose nondeterminism. |
| Inspectable | All relevant files fit the capsule; no waveform, netlist, database, binary, vendor model, or full raw log is needed. |
| Reviewed | Treat every byte as public; apply the redactor and inspect the complete export. |

If any gate fails, keep the original finding local or report only independently
actionable non-project metadata; the reproducer remains local. State limitations without weakening gates or
inventing observed output to make a report filable.

## 1. Fingerprint the original failure locally

Before changing anything, record a compact fingerprint:

- exact Booley command and Target/config selection;
- Booley, Python, simulator/synthesizer, and relevant plugin versions;
- exit status and the Booley phase/component that failed;
- exception type or stable diagnostic fragments;
- expected artifact or behavior that was missing or wrong;
- whether a clean rerun fails the same way.

Exclude project names, paths, module/signal names, raw source, and arbitrary log
text from the upstream fingerprint. Private text may temporarily occupy local
replaceable evidence fields; sanitize them before export using the parent skill's
field audit. Original logs and source remain unattached.

Identify the Booley source branch producing the failure for the equivalence
check in §3; similar final messages alone do not establish equivalence.

## 2. Work in a private scratch area

Use a private scratch directory outside both the tracked RTL repository and its
project data directory; leave project submodules unchanged. Early reductions may
contain copied material. Only the final capsule passing every gate is attachable.

Prefer constructing the trigger from scratch with generic modules such as
`toy_top`, `toy_dut`, and `toy_tb`. Preserve only the language/EDA-tool property that
appears necessary: for example a parameter shape, generate construct, file-order
condition, configuration omission, timeout boundary, or CLI interaction.

When the trigger is not yet understood, a private copy may be reduced locally:

- slice away files and modules outside the failing dependency path;
- remove ports, processes, assertions, parameters, and statements in chunks;
- replace datapaths and values with small generic equivalents;
- simplify clocks, resets, stimulus, `.core`, `booley.toml`, and `tests.toml`;
- rerun after each accepted reduction and restore any change that loses the
  original fingerprint.

Use reductions to discover the trigger, then rewrite a fresh toy case. The
mechanically reduced copy is not the deliverable.

## 3. Prove equivalence and causality

Validate the candidate in a clean toy workspace that cannot see the original
project. Record this table locally:

| Check | Required result |
| --- | --- |
| Original case | Fails with fingerprint A |
| Synthetic case | Fails through the same source path with fingerprint A |
| Clean synthetic rerun | Fails again with fingerprint A |
| Trigger removed or changed | Passes, or fails in the documented expected way |

The counterfactual final row is required for trigger/root-cause claims to exclude
an unrelated broken toy. If no sensible counterfactual exists, report verified
behavior without claiming causality.

Exact paths, generated filenames, line numbers, and temporary identifiers may
differ. The component, source branch, exit behavior, exception/error class, and
stable diagnostic meaning must agree. For nondeterministic failures, report the
number of failures and attempts for both cases.

## 4. Rewrite and audit the public capsule

Make one compact Markdown file containing only:

- a statement that this is an agent-generated synthetic reproducer, not original
  project source;
- the exact command to run from the toy project root;
- required EDA-tool versions or environment conditions;
- expected and observed behavior plus the stable fingerprint;
- the counterfactual command/change and its result, when applicable;
- every required text file in full, in labelled code fences.

Keep the capsule below 120 lines and 8,000 characters, the attachment inline
limits. If it cannot fit, explain why and keep it local; clipping breaks
self-containment.

Audit the capsule for semantic as well as textual leakage. Remove or replace:

- original filenames, identifiers, comments, paths, remotes, user/organization
  names, and ticket/customer names;
- proprietary protocols, topology, register maps, opcodes, memory layouts,
  timing/area targets, device selections, and unusual parameter values not
  essential to the trigger;
- real vectors, firmware, keys, seeds, payloads, waveforms, netlists, EDA-tool
  databases, and vendor models;
- copied error context that contains project-only names or source excerpts.

Search for every known project term, including `[feedback] redact_extra` and
project-specific `[stealth] banned_words`. Run
`booley feedback redact --file <capsule>` and inspect both the original and complete
redacted output; the parent skill's disclosure audit still applies.

## 5. Attach only the verified capsule

Apply the parent skill's field audit and replacement procedure, including titles
and workaround notes. Update the safe finding with the synthetic evidence and
attach only the final capsule:

```console
booley feedback triage F-N \
  --repro "<exact command in the synthetic project>" \
  --observed "<stable synthetic failure fingerprint>" \
  --expected "<correct behavior>" \
  --attach <synthetic-reproducer.md> \
  --verified-against-source
```

Set `--verified-against-source` only after establishing the matching source path.
Keep scratch trees, original logs, and original-to-synthetic name mappings unattached.

Return the safe ID to the parent skill for batch export, inspection, and manual
GitHub/email handoff. In the export, verify that redaction and attachment limits
preserve every required file and the reproducer's meaning. Call the result
**synthetic, minimized, and sanitized**, never anonymous or guaranteed safe.
