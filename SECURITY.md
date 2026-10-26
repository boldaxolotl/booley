# Security Policy

## Supported versions

Security fixes are provided for the latest released version of Booley. Users
should upgrade to the newest release before reporting a problem that may
already have been corrected.

## Known unresolved vulnerability

[Issue #1529](https://github.com/boldaxolotl/booley/issues/1529) tracks a known
Sandbox escape hatch: a Developer Agent can modify shared Git configuration or
hooks so that a later host Git command executes planted code as the host user.
This affects human and editor Git commands as well as Booley's host-side Git
calls. Doctor does not currently detect this tampering and can itself trigger it.

Remediation is deferred for reconsideration on **31 DEC 2026**; that is a review
date, not a promised fix date. See the [current precautions](docs/user/SANDBOX.md#known-sandbox-escape-through-shared-git-metadata).

## Reporting a vulnerability

For newly discovered vulnerabilities, use the private reporting process below
rather than opening a public GitHub issue. The known issue above is already public.

Report it privately by email to **boldaxolotl@proton.me**. Include, where
possible:

- the affected Booley version or commit;
- the operating system and container runtime;
- steps to reproduce the issue;
- the expected and observed impact; and
- any suggested mitigation or patch.

Do not include live credentials, private contents of the Booley Directory, proprietary RTL, or
other secrets. Replace them with minimal synthetic examples.

We aim to acknowledge reports within three business days and provide an
initial assessment within seven business days. Confirmed vulnerabilities will
be handled through coordinated disclosure: we will agree on a disclosure date,
prepare a fix and release notes, and credit the reporter unless anonymity is
requested.

## Scope

Reports involving command execution, container isolation, credential or secret
exposure, network-egress controls, dependency or release-pipeline compromise,
and unsafe handling of untrusted project files are especially welcome.

General bugs and feature requests should use the public GitHub issue tracker.
