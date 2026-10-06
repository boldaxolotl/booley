---
name: booley-goal
description: Enter Booley Goal Mode for a piece of RTL work - pick or create a worktree, choose Goalsets and ad-hoc Goals with the human, translate them into concrete Goals, and call goal_enter.
---

# Enter Goal Mode

Goal Mode binds a piece of work to one linked worktree: Booley records the
Goals, creates the Goal Branch, and judges every Goal by Booley Flow and
Specialist evidence only. This skill walks the human from a fuzzy request to
a `goal_enter` call. It never edits RTL.

## 1. Pick the worktree

Goal Mode never runs in the main checkout. Ask whether the work continues in
an existing linked worktree or needs a new one.

- New: run `booley worktree new <name>` inside the Sandbox (`<name>` is a
  short lowercase path segment). It prints the worktree root, under
  `.booley_project/worktrees/<name>`.
- Existing: use its root. The tree must be clean; ask the human to commit or
  stash first, never do it for them.

Remember the absolute root. It is `work_dir` on every Booley call from now on.

## 2. Read the Goalsets

List the `*.md` files in the Project directory's `goalsets/`. Each is a
Project-owned Markdown file written in the vocabulary of `goal_enter`'s
`goals` argument: a short list of Goals and one JSON block of Goal arguments
with placeholders such as `<target>`.

If `default.md` exists it applies to every entry. Include its Goals and list
`default` in `goalsets_used`, unless the human explicitly says to skip it;
then set `default_skipped: true` and record their reason, in their words, as
`skip_reason`.

## 3. Grill for the work

Ask, one question at a time, until each answer is concrete:

1. A slug for the work: lowercase letters, digits, and single hyphens. It
   names the Goal Branch `goal/<slug>-<date>`.
2. Which Goalsets fit (feature, bugfix, refactor, verification, or the
   Project's own).
3. Which Targets the change touches. Use `booley_targets` to list them; a
   Target that does not exist yet is allowed and stays unmet until it does.
4. Any ad-hoc Goals beyond the Goalsets: thresholds for synth or fpga,
   cycle counts, coverage floors, mutation scores, reviews. A spec review
   names its spec file relative to the worktree.

## 4. Translate

Expand every Goalset placeholder into concrete Goals: one Goal per Target per
per-Target Goal, each naming its Target. Set `origin` to the Goalset name, or
leave it out for ad-hoc Goals. Do not merge duplicates by hand: `goal_enter`
merges Goals for one key to the stricter one and reports each merge. Show the
human the final Goal list before entering.

## 5. Enter

Call `goal_enter` with `work_dir`, `slug`, `goals`, `goalsets_used`, and, when
skipping the default Goalset, `default_skipped` and `skip_reason`.

On a refusal, read the reason aloud and fix the cause with the human (a dirty
tree, a Goal Mode already active in the worktree, a missing baseline Target or
spec file, an existing branch name). Never retry with weakened Goals.

## 6. Echo the rules

`goal_enter` returns the Goal list, any warnings, and the rules for working in
Goal Mode. Repeat the rules and warnings to the human verbatim, then start the
work in the Goal worktree, passing `work_dir` on every Booley call.
