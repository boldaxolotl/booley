# Changelog fragments

A contributor records a user-visible change's changelog entry as one new file
in this directory. It does not edit `CHANGELOG.md` or its packaged copy, so
unrelated pull requests never touch the same lines. At release time
`python3 .github/scripts/release_changelog.py assemble` appends every fragment
to the end of `## Unreleased`, deletes the fragments, and brings the packaged
copy in line. `validate --target` fails while a fragment remains. Only release
preparation edits `CHANGELOG.md` directly.

## Name

- `<issue number>.md`, for example `3606.md`.
- A short lowercase slug when the change has no issue, for example
  `fix-typo.md`. It starts with a letter and uses only `a-z`, `0-9` and `-`.

This README and hidden files (names starting with `.`) are never fragments.
Any other name fails.

Fragments are added in this order: issue numbers ascending, then slugs
alphabetically.

## Content

Write one or more Markdown bullets, exactly as they should read under
`## Unreleased`.

```markdown
- Fixed `booley doctor` reporting a stale mirror after a sync.
  Continuation lines are indented.
```

- Use `- ` as the bullet marker. `* ` and `+ ` are accepted and kept as written.
- Indent continuation lines. A line at column 0 directly under a bullet line
  also continues it.
- No headings, no section name, no metadata. A Markdown heading (`#` to
  `######` followed by a space or the end of the line) fails, even when indented
  or right after the bullet marker, so a `# comment` inside an indented code
  block fails too.
- No top-level text after a blank line. Every paragraph is a bullet or indented.
- Use absolute URLs for links. The text moves to the root `CHANGELOG.md`, where
  a relative link would break there.
- The file is UTF-8 and not empty.

A malformed fragment makes `assemble` exit nonzero, name the file, and change
nothing. The fragment format test in `tests/docs/test_changelog_fragments.py`
fails a pull request that commits one.
