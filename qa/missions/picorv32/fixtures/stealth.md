### Stealth sanitation and attribution

The [published Stealth contract](../../../../docs/user/CONFIG.md) has two rules. It redacts built-in banned words (including `booley`) in place, keeping subject, body, and rationale. It **rejects** recognized attribution footers and leaves the raw message unchanged. Use the accepted Stealth settings with no custom banned-word override. Run each case as a commit attempt on a run-owned disposable empty commit, and save the raw message, the stored message (if any), the hook's stdout/stderr, and the exit status. Discard the disposable commits after capturing evidence.

**Case 1: `Co-Authored-By` is rejected.** Supply exactly:

```text
fix(qa): verify booley message handling

Keep this rationale intact.
Checked the booley configuration.

Co-Authored-By: QA Fixture <qa-fixture@example.invalid>
```

Require: hook exit 1 with the remove-the-footer diagnostic, no new commit, and the raw message byte-identical to the input.

**Case 2: the same message without the footer is redacted and committed.** Supply case 1 without its blank line and `Co-Authored-By` line. Require: a commit is created, `booley` is absent from the stored subject and body, `Keep this rationale intact.` is kept, and the rewritten configuration sentence is kept. Don't require a particular substitute token unless the documentation of the tested build specifies one.

**Case 3: a plain "Generated with …" footer is judged by its payload.** Supply case 2's message with one extra final line, once per variant:

- `Generated with booley`: rejected like case 1 (exit 1, no commit, raw message unchanged).
- `Generated with care by the whole team`: accepted, and the rest is redacted as in case 2. The footer is not rejected, but banned words inside it are still redacted, so with the default vocabulary `Generated` is rewritten. Once #1081 removes `generated` from the defaults, the footer line must be stored verbatim.
