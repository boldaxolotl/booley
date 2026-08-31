# Releasing Booley

Pin one `origin/main` commit before inventorying changes. The release PR owns
the target entry in root `CHANGELOG.md`, its byte-identical packaged mirror at
`src/booley/data/refs/CHANGELOG.md`, and every version-bearing Python, BWave,
schema, and third-party surface.

Inventory every issue, pull request, and commit from the previous stable tag
through the pinned base. Draft one dated `## MAJOR.MINOR.PATCH - DD MON YYYY`
entry using only applicable New features, Quality of life, Bug fixes, and
Upgrade notes sections. Upgrade notes name changed CLI/configuration surfaces,
defaults, generated files, runtime or image actions, compatibility limits, and
manual steps. Prose coverage is established by the issue/diff inventory; the
checker validates only objective structure.

Use the repository helper to synchronize and validate the mirror:

```bash
python .github/scripts/release_changelog.py sync
python .github/scripts/release_changelog.py validate --target "$VERSION"
```

If `main` advances before merge, update the release branch, redo the complete
range inventory and changelog entry, and rerun the replacement suite. After
merge, the release PR merge commit is the immutable candidate. Candidate Docker
validation must prove that exact SHA; use a temporary non-publishing branch if
workflow dispatch cannot accept a raw SHA, and verify the workflow `headSha`.
Never use a final `v*` tag for preflight or silently move to a newer `main`.

Tag only the verified merge commit. The publish workflow validates that the tag
matches `VERSION`, requires the target changelog entry in both distributions,
extracts its body verbatim, and publishes that file as the GitHub Release body.
After publication, compare the API-returned release body with the extracted
entry.
