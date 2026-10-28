# Trust the Project inside the Sandbox

Status: accepted

## Context

A Sandbox without a seeded host agent config, such as a Codespace or any host
with nothing to seed from, starts `claude` and `codex` as first-time installs:
a theme picker, a login picker, and a "do you trust this folder?" dialog
([#2258](https://github.com/boldaxolotl/booley/issues/2258)). The host seed
that does carry trust names host paths, which do not exist in the Sandbox.

## Decision

On every Sandbox start the registrar (`booley.runtime.incontainer_setup`)
merges first-run state into the per-Sandbox agent home, set-if-absent:

- **Workspace trust, always.** The Project Root (the lifecycle hook's working
  directory) and the Booley Directory are trusted in both apps: Claude
  `projects[<path>].hasTrustDialogAccepted`, Codex
  `[projects."<path>"] trust_level = "trusted"`. Each path is written resolved
  and as spelled, so a symlinked workspace or a linked Booley Directory entry
  matches whichever form the app checks. The Booley Directory is a separate
  bind outside the Project Root, so it needs its own entry.
- **Onboarding, only with a credential.** Claude `hasCompletedOnboarding` and
  a default `theme` (`dark`, in the Claude settings file, only when neither
  Claude file sets one) are written only when the registrar's own credential
  resolution finds one (OAuth token, API key, stored token, or login file).
  Without a credential the login flow stays as it was. Codex needs no
  onboarding write; its credential writer already stores the API key.
- **Existing values win.** A present value, including `false` and
  `untrusted`, is never changed, and neither is any other key of an existing
  project entry. Comments and other bytes of the Codex file are preserved.
- **Invalid files are refused.** An unparsable or wrongly shaped Claude JSON
  file or Codex `projects` table stops registration with an error naming the
  file and how to retry, and is left untouched. This reverses the old
  replace-with-`{}` fallback in the Claude writers, which would otherwise
  rewrite an invalid file before the new writer could see it.
- **Repeat registration is a no-op**: nothing is rewritten when nothing changed.

## Rationale

The Sandbox is the isolation boundary: the registrar already launches Claude in
`bypassPermissions` and Codex with full access there (see the permission-mode
comment in `incontainer_setup.py`). Trusting the Project's own directories adds
no authority. Writes stay in the per-Sandbox home and never touch the host's
agent configuration.

## Consequences

- With only `ANTHROPIC_API_KEY`, Claude still shows its "use this custom API
  key?" dialog once. That dialog is the billing consent and is deliberately not
  pre-answered; pre-approval would store a key fragment and conflicts with the
  explicit `[agent] auth` policies. The no-login-prompt guarantee therefore
  covers `CLAUDE_CODE_OAUTH_TOKEN` and the login file, plus Codex with an API
  key.
- A Sandbox whose Claude config is invalid now fails registration loudly
  instead of silently losing the file.
- The Codex edit uses the existing in-place TOML editor; its rewrite is
  tracked in [#1666](https://github.com/boldaxolotl/booley/issues/1666).
