# Publish the user docs on the project site

Status: accepted

## Context

People read Booley's user docs in three places: GitHub's Markdown rendering,
the PyPI project page (which shows the README but cannot resolve its relative
links or images), and the URLs Booley prints in messages and generated config
comments. Those printed links named repository paths such as
`docs/user/SETUP.md`, which mean nothing to someone who installed Booley from
PyPI. The project site at <https://boldaxolotl.github.io/booley/> already
carries the feature tour and videos, but not the reference docs.

The Markdown under `docs/user/` has many readers besides people: a packaged
mirror of `TROUBLESHOOTING.md`, about 45 source references, about 30 test
references, and agents that read the files directly in the repository.

## Decision

The project site is the canonical human-facing URL for the user docs. It
publishes each `docs/user/<FILE>.md` at `https://boldaxolotl.github.io/booley/docs/<slug>/`
and its assets at `docs/assets/`, with heading anchors equal to GitHub's.
`booley.runtime.docs_links` holds the one `SITE_URL` constant and the
file-to-slug table; Booley's printed links and the README call or follow it.
These URLs are a public contract: released binaries print them, so a slug or
heading change breaks links in the field.

The docs are published, not moved. `docs/user/` stays the only authored copy;
the site build reads it in place. Text written for agents (Sandbox
Specialists, packaged skills, agent prompts) keeps repository paths, because
agents read the Markdown, not the site.

The site tracks `main`, not the latest release. Its docs landing page says so.

## Rejected alternatives

**Move the docs into `website/`.** Every source, test, packaged mirror, and
agent reference would change, and GitHub rendering of `docs/user/` would stop
working, for no gain over reading the files in place.

**Commit copies, or symlink, under `website/docs/`.** Copies drift; symlinks
break on Windows checkouts and in some packaging paths.

**Copy the files in a CI step before the site build.** The site would differ
between a local `mkdocs serve` and the deployed build, and the copy step is
one more place for the slug table to diverge.

## Consequences

- Printed links work for PyPI installs and survive repository reorganization
  as long as the slug table and headings stay stable.
- Tests fail when a printed link names an unpublished doc or a missing
  heading, and when a message names a user doc by repository path outside the
  allowlisted agent-facing modules.
- A docs fix reaches the site on merge to `main`, before the next release, so
  site readers can see behavior newer than their installed version.
