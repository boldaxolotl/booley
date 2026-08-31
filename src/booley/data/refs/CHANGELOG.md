# Changelog

All notable user-visible changes to Booley are recorded here. Release entries
use stable `MAJOR.MINOR.PATCH` headings so Booley can review an exact upgrade
range from the packaged copy of this file.

Packaged release history starts with the first release that replaces the
Unreleased section with a dated stable-version entry. For older changes, see
[GitHub Releases](https://github.com/boldaxolotl/Booley/releases).

## Unreleased

### New features

- Added durable, version-aware upgrade review state, scriptable status and
  compare-and-swap acknowledgment.

### Quality of life

- Doctor and Session Runtime startup now direct users to the packaged release
  history when a Booley update needs review.

### Upgrade notes

- When Booley reports that its version changed, invoke `/booley-heal`.
