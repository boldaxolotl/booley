# Changelog fragments

A contributor records a user-visible change's changelog entry as one new file
in this directory. It does not edit `CHANGELOG.md` or its packaged copy, so
unrelated pull requests never touch the same lines. At release time
`python3 .github/scripts/release_changelog.py cut --target X.Y.Z` writes the
dated entry for that release directly below `## Unreleased`, sorted into
sections by kind, deletes the fragments it used, and brings the packaged copy
in line. Only release preparation edits `CHANGELOG.md` directly.

## Name

- `<issue number>.<kind>.md`, for example `3606.fix.md`.
- A short lowercase slug when the change has no issue, for example
  `fix-typo.fix.md`. It starts with a letter and uses only `a-z`, `0-9` and `-`.
- One issue may have several fragments, one per kind.

This README and hidden files (names starting with `.`) are never fragments.
Any other name fails, including one with no kind or an unknown kind.

Within a section, fragments appear in this order: issue numbers ascending,
then slugs alphabetically.

## Kind

The kind selects the section of the release entry. Sections appear in this
order, and a section with no fragment is left out.

| Kind | Section | Use it for |
|---|---|---|
| `major` | Major features | A headline capability of a minor release; held |
| `feature` | Regular features | A new capability that is not headline news |
| `qol` | Quality of life | A smaller improvement to an existing workflow |
| `fix` | Bug fixes | A bug fix |
| `internal` | Internal work | A change users do not see directly |
| `upgrade` | Upgrade notes | What a user must do by hand to upgrade; held |

## Hold for the next minor release

A fragment in the `next-minor/` subdirectory is held: a patch release
(`X.Y.Z` with `Z` above 0) leaves it in place, and the next `X.Y.0` release
uses it together with the fragments outside `next-minor/`. Create the
directory when you add the first held fragment.

Hold a fragment when its note must wait for the next minor release, for
example a change that is not announced yet. A `major` or `upgrade` fragment
must be held: a `major` or `upgrade` fragment outside `next-minor/` fails. A
change that needs manual action from users is an `upgrade` fragment, so it is
held too. Other kinds may be held or not; hold them only when the release
that carries them must wait.

## Cut

`python3 .github/scripts/release_changelog.py cut --target X.Y.Z [--date YYYY-MM-DD]`
runs at release preparation, before `VERSION` is bumped to the target. The
target must be above `VERSION`, `## Unreleased` must be empty, and every
fragment, held ones included, must be well formed; otherwise `cut` exits with
status 2, names the cause, and changes no file. The date defaults to today in
UTC. When no fragment is selected for the target, `cut` exits with status 3
and changes no file. Status 2 is every failure, including a usage error.

`validate --target X.Y.Z` fails while a fragment that `cut` would use for that
target remains, so after the `VERSION` bump it passes for a patch release even
with held fragments present.

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

A malformed fragment makes `cut` exit with status 2, name the file, and change
nothing. The fragment format test in `tests/docs/test_changelog_fragments.py`
fails a pull request that commits one.
