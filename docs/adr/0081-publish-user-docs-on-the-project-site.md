# Publish the user docs on the project site

Status: accepted (amended 2026-10-26: site domain; 2026-11-10: agent-facing text and contributor docs; 2026-11-12: rendered doc copies, see Amendments)

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
publishes each `docs/user/<FILE>.md` at `https://booley.dev/docs/<slug>/`
(amended 2026-10-26, see Amendments)
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

## Amendments

### 2026-10-26

The project site moved from `https://boldaxolotl.github.io/booley/` to its own
domain, `https://booley.dev/`. The slugs, anchors, and `docs/assets/` paths are
unchanged; only `SITE_URL` changed. GitHub Pages redirects every old URL to the
same path on `booley.dev` while the repository's Pages custom domain is set, so
links printed by earlier releases keep working. Removing that setting, or letting
the domain lapse, breaks them. A test fails if a tracked file outside its short
allowlist (release history, this record, the Pages workflow) names the old host.

### 2026-11-10

The Decision paragraph's rule that text written for agents keeps repository
paths is superseded. The Sandbox has no repository checkout, so agent-facing
text (packaged skills, the cheatsheet, generated config comments, agent-facing
runtime messages) cites the doc copies the Sandbox image ships at the fixed path
`/booley-docs/<FILE>` and names any other doc only with the marker
`(host doc, not shipped)`. `python -m booley.dev_support.reference_docs`
generates the copies from an allowlist into `src/booley/data/docs/`, rewriting
links between docs so each copy resolves in the flat directory; people still
get site URLs through `docs_url()`. `tests/docs/test_sandbox_doc_citations.py`
fails on a citation that is neither.

The site also publishes a short list of contributor docs, starting with
`docs/internals/ARCHITECTURE.md` at `/docs/architecture/`, in an "Internals"
sidebar group after the user docs. Like the user docs, they are published, not
moved: each stays in `docs/internals/`, where contributors and agents read it.
`booley.runtime.docs_links.CONTRIBUTOR_DOCS` lists them, and `SITE_DOCS` gives
the sidebar order. `docs_url()` still covers user docs only, because Booley
prints no links to contributor docs. A contributor doc's relative links to
published files stay on the site; its other links go to GitHub.

### 2026-11-12

The 2026-11-10 copies are no longer committed. The build step
(`write_build_stamp`, which calls `write_packaged_docs`) renders them from
`SHIPPED_DOCS` into `src/booley/data/docs/`, a Git-ignored directory. Nothing
commits or syncs them: `python -m booley.dev_support.reference_docs` and
`.github/scripts/sync_packaged_copies.py` cover only the packaged `CHANGELOG.md`
mirror. Tests read the rendered output of the sources, so they never compare
against a stale committed copy.

Rejected alternatives:

- **A setuptools build hook or custom build backend.** Build-backend code runs
  in every isolated build, which is out of scope. Every build script and
  CI build step already renders before building the wheel and sdist.
- **Committed copies regenerated only at release.** The copies would drift from
  their sources between releases, and tests would read stale text.
