### Stealth sanitation and attribution

The [published Stealth contract](../../../../docs/user/CONFIG.md) lists `booley` among built-in banned words and requires in-place redaction while preserving rationale, with attribution trailers removed. On a run-owned disposable empty commit, with the accepted Stealth settings and no custom banned-word override, supply exactly:

```text
fix(qa): verify booley message handling

Keep this rationale intact.
Checked the booley configuration.

Co-Authored-By: QA Fixture <qa-fixture@example.invalid>
```

Require `booley` absent from final subject/body; retained `Keep this rationale intact.` and retained rewritten configuration sentence; no `Co-Authored-By` or attribution address. Preserve original and stored commit messages plus hook diagnostics. Do not require a particular substitute token unless the documentation matching the tested build specifies it. Restore/discard the disposable commit after evidence capture.
