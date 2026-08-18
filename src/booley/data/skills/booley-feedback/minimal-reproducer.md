# Build a safe minimal reproducer

Use this guide only for a suspected Booley defect whose evidence depends on
private RTL, testbench code, project configuration, logs, or design structure.
The goal is not to disguise the original project. The goal is to produce a new,
standalone toy project that exercises the same Booley failure without containing
the original project.

The original material and every intermediate reduction remain private and
local. Only the final synthetic capsule may be attached to a finding, and it is
still shown byte-for-byte in `booley feedback preview` before submission.

## The acceptance contract

A reproducer is ready only when all of these are true:

1. **Standalone:** it runs without the original repository, project data
   directory, source files, generated artifacts, or environment variables.
2. **Synthetic:** its identifiers, comments, constants, test vectors, hierarchy,
   and logic were written for the toy case. Renaming a reduced copy of private
   RTL does not make it synthetic.
3. **Equivalent:** it reaches the same Booley component and failure path, with
   the same stable diagnostic fingerprint. A different failure with similar
   wording is not equivalent.
4. **Repeatable:** the final command fails again from a clean toy workspace. Run
   it twice when practical; disclose nondeterminism rather than hiding it.
5. **Minimal enough to inspect:** every relevant file fits in the capsule. No
   waveform, netlist, database, binary, vendor model, or full raw log is needed.
6. **Reviewed for disclosure:** assume every byte in the capsule will become
   public. The normal feedback redactor and user preview remain mandatory.

If any gate fails, there is no publishable reproducer. Keep the original finding
local or file only non-project metadata that is independently actionable. Never
weaken a gate or invent observed output to make the report filable.

## 1. Fingerprint the original failure locally

Before changing anything, record a compact fingerprint:

- exact Booley command and Target/config selection;
- Booley, Python, simulator/synthesizer, and relevant plugin versions;
- exit status and the Booley phase/component that failed;
- exception type or stable diagnostic fragments;
- expected artifact or behavior that was missing or wrong;
- whether a clean rerun fails the same way.

Keep project names, paths, module/signal names, raw source, and arbitrary log text
out of the fingerprint used upstream. The initially logged finding may retain
project-specific text temporarily in its replaceable evidence fields while the
reproducer is built; replace those fields before preview. Do not attach the
original log or source to the finding because `triage` cannot remove an
attachment later.

Read the Booley source responsible for the failure and identify the branch that
emits the diagnostic or incorrect result. That code path is part of the
equivalence test. If the toy reaches another branch, reject it even if its final
message looks similar.

## 2. Work in a private scratch area

Create a scratch directory outside the tracked RTL repository and outside its
project data directory. Do not modify project submodules. Treat the scratch area
as private because early reductions may still contain copied project material.
Do not attach anything from it until a final synthetic capsule has passed every
gate below.

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

Reduction is a discovery technique, not the deliverable. Once the trigger is
known, rewrite it as a fresh toy case. Do not submit the mechanically reduced
copy.

## 3. Prove equivalence and causality

Validate the candidate in a clean toy workspace that cannot see the original
project. Record this table locally:

| Check | Required result |
| --- | --- |
| Original case | Fails with fingerprint A |
| Synthetic case | Fails through the same source path with fingerprint A |
| Clean synthetic rerun | Fails again with fingerprint A |
| Trigger removed or changed | Passes, or fails in the documented expected way |

The counterfactual final row is required whenever the report names a trigger or
root cause. It distinguishes a causal reproducer from an unrelated broken toy.
If a counterfactual cannot sensibly exist, do not claim causality; describe only
the behavior that was verified.

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

Keep it below 120 lines and 8,000 characters because feedback attachments inline
only that much; a clipped reproducer is not self-contained. If the necessary case
does not fit, describe the limitation and keep it local instead of silently
submitting a partial example.

Audit the capsule for semantic as well as textual leakage. Remove or replace:

- original filenames, identifiers, comments, paths, remotes, user/organization
  names, and ticket/customer names;
- proprietary protocols, topology, register maps, opcodes, memory layouts,
  timing/area targets, device selections, and unusual parameter values not
  essential to the trigger;
- real vectors, firmware, keys, seeds, payloads, waveforms, netlists, EDA-tool
  databases, and vendor models;
- copied error context that contains project-only names or source excerpts.

Search explicitly for every known project term, including `[feedback]
redact_extra` and project-specific `[stealth] banned_words`. Then run the
capsule through `booley feedback redact --file <capsule>` as an additional check.
That redactor is a denylist, not proof of anonymity; inspect its complete output
and the original capsule yourself.

## 5. Attach only the verified capsule

Update the finding so its `--repro`, `--observed`, and `--expected` describe the
synthetic case, and add only the final capsule:

```console
booley feedback triage F-N \
  --repro "<exact command in the synthetic project>" \
  --observed "<stable synthetic failure fingerprint>" \
  --expected "<correct behavior>" \
  --attach <synthetic-reproducer.md> \
  --verified-against-source
```

Use `--verified-against-source` only when source inspection actually established
the matching Booley path. Do not attach the scratch tree, an original log, or a
mapping between original and synthetic names.

Finally run `booley feedback preview F-N`. Show the user its entire output
verbatim as required by the parent skill. Call the result **synthetic, minimized,
and sanitized**, never anonymous or guaranteed safe. Submission still requires
the user's explicit approval for that exact preview.
