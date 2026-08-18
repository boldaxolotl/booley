# Security Policy

## Supported versions

Security fixes are provided for the latest released version of Booley. Users
should upgrade to the newest release before reporting a problem that may
already have been corrected.

## Reporting a vulnerability

Please do not open a public GitHub issue for a suspected vulnerability.

Report it privately by email to **boldaxolotl@proton.me**. Include, where
possible:

- the affected Booley version or commit;
- the operating system and container runtime;
- steps to reproduce the issue;
- the expected and observed impact; and
- any suggested mitigation or patch.

Do not include live credentials, private project data, proprietary RTL, or
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
