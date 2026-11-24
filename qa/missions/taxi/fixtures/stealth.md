### Stealth rejection and attribution

The [published Stealth contract](../../../../docs/user/STEALTH.md) has one rule: the commit-msg hook **rejects** recognized attribution footers and messages with banned words (including `booley`), and never rewrites the message file. Use the accepted Stealth settings with no custom banned-word override. Run each case as a commit attempt on a run-owned disposable empty commit, and save the raw message, the stored message (if any), the hook's stdout/stderr, and the exit status. Discard the disposable commits after capturing evidence.

**Case 1: `Co-Authored-By` is rejected.** Supply exactly:

```text
fix(qa): verify booley message handling

Keep this rationale intact.
Checked the booley configuration.

Co-Authored-By: QA Fixture <qa-fixture@example.invalid>
```

Require: hook exit 1 with the remove-the-footer diagnostic, no new commit, and the raw message byte-identical to the input.

**Case 2: the same message without the footer is still rejected for `booley`.** Supply case 1 without its blank line and `Co-Authored-By` line. Require: hook exit 1, no new commit, the raw message file byte-identical to the input, and stderr that lists `'booley' in the subject` and `'booley' on line N` for each body line that has it, says to reword or remove the term from `[stealth] banned_words` / `banned_substrings`, names the kept message file, and gives the retry `-v -e -F "<path>"`. Then reword the message without `booley` and commit it again: a commit is created and `Keep this rationale intact.` is stored verbatim.

**Case 3: a plain "Generated with …" footer is judged by its payload.** Supply case 2's reworded message (no banned terms) with one extra final line, once per variant:

- `Generated with booley`: rejected like case 1 (exit 1, no commit, raw message unchanged; the error doesn't echo the footer).
- `Generated with care by the whole team`: accepted and stored verbatim, footer included. With no banned term in it the footer is not rejected.
