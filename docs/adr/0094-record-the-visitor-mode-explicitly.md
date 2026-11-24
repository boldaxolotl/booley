# Record the visitor mode explicitly

Status: accepted (2026-11-21)

Decided in [#5264](https://github.com/boldaxolotl/booley/issues/5264).

## Context

`auto` is retired as an `[agent] auth` value. A missing `auth` then meant two
things: the subscription everywhere, except in a Sandbox set up by hand, where
it meant the visitor mode (the credential the person in it signs in with or
exports, else the gateway the spec provides). That implicit rule left a
hand-written Project nothing to record, and Doctor needed a note to excuse it.

## Decision

- **`visitor` is a recordable `[agent] auth` value**, a string or an
  `[agent.auth]` entry. It runs only in a hand-written Sandbox: a container with
  no `BOOLEY_AGENT_CLIS` record whose spec sets `BOOLEY_AGENT_APP` or
  `BOOLEY_PRIMARY_AUTH`. Elsewhere it reads as `subscription`, so a Project
  that records it can still be issued a Sandbox, and the record does not carry
  the mode.
- **A missing `auth` is `subscription` everywhere.** Doctor fails it in a
  hand-written Sandbox as on the host, and fails a `visitor` outside one.
- **`booley init` replaces `visitor` on the host** with the detected policy and
  keeps it in a hand-written Sandbox. `--auth` and the prompt do not offer it.
- **`auto` stays retired**, and the issued record keeps its `auto` wire
  spelling.

## Considered options

- Keep the implicit rule: "missing" keeps two meanings.
- Honor `visitor` everywhere: an issued Sandbox would run a mode its record
  does not carry.
- Un-retire `auto`: it names no billing choice a person can read.

## Consequences

- A hand-written Sandbox must now record `visitor`; one that does not bills the
  subscription.
