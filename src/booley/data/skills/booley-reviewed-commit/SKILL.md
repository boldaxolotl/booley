---
name: booley-reviewed-commit
description: Produce a Reviewed Commit, or a reviewed and measured commit, through the booley_review MCP tool. Use only when the user asks for a reviewed commit; never start it on your own.
disable-model-invocation: true
---

# Produce a Reviewed Commit

A Reviewed Commit is a commit on which its latest verify Review Run, or a
discovery Review Run, left no finding open at or above the Declared Reviews'
threshold (each Declared Review's `resolve` severity in `reviews.toml`). This
skill runs the review loop once: discovery, fixes, one verify. Run it only
when the user asks for a reviewed commit or a reviewed and measured commit.

Every review goes through the `booley_review` MCP tool. It returns a `run_id`
at once; poll it with `booley_poll` until the result carries its exit status.
Exit 0: no finding is open. Exit 1: a finding the commit introduced is open.
Exit 2: a Reviewer failed or the options were refused: report the error to
the user and stop. From a linked worktree, pass its root as `work_dir`.

## Steps

1. **Commit the work.** A review never sees uncommitted changes.
2. **Run review discovery.** Call `booley_review` with no other option. On
   exit 0 the commit is Reviewed: go to step 5.
3. **Resolve each introduced finding at or above `resolve`.** Fix it in the
   code. When a finding is wrong, do not change the code for it: prepare a
   waiver with its `finding_id` and a justification that says why it is
   wrong. A finding below `resolve` needs nothing; list it in the summary.
4. **Commit the fixes and run verify once.** Call `booley_review` with
   `verify` set and `waive` listing each waiver.
5. **Measure, when asked.** When the user also asked for a measured commit,
   run `booley measure` on the final commit (the `booley_measure` MCP tool).
6. **Report.** Give the user the Review Report path:
   `reviews/<commit>/<run id>.md` in the Main Booley Directory, where
   `<commit>` and `<run id>` are those of the last Review Run (verify, or
   discovery when it was clean); check that the file exists before you
   cite it. Then summarize: waivers first, each with its justification; then
   every finding still open; then what you fixed, and the measurement
   verdict when you measured.

## Never

- Never run discovery again after verify. Verify runs once; whatever it
  leaves open goes to the user.
- Never edit the Declared Reviews in `reviews.toml` to make a review pass.
- Never call the commit Reviewed while verify left a finding open at or above
  `resolve`; name the findings that remain.
