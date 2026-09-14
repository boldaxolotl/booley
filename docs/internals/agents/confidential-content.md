# Confidential-content vocabulary

The private local TOML is the source of truth for banned terms and allowed
identities. It lives at `.git/booley-leak-guard.toml` by default, shared by
worktrees. `BOOLEY_LEAK_GUARD_CONFIG` or the repo-local Git setting
`booley.leakGuardConfig` can select another private path. Keep the TOML outside
tracked files.

After editing the TOML, run this from a Booley checkout with a GitHub account
allowed to update repository Actions secrets:

```sh
python3 .github/scripts/confidential_content_guard.py --repo . sync-ci-secret
```

The command validates the local TOML, sends its base64 encoding to the
`BOOLEY_LEAK_GUARD_CONFIG_B64` Actions secret through `gh` stdin, and reports
only success or a nonsensitive error. GitHub Actions cannot return a secret's
value, so the CI secret is a deployed copy, never an editable source. Local
hooks and PR text preflight read the TOML; CI reads the deployed copy. Repeat
the sync after each vocabulary change, before relying on CI to enforce it.
