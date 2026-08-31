# Session Python 3.14 migration evidence — 2026-08-31

This report records the Phase 2 precondition result from the runtime migration
plan in [#199](https://github.com/boldaxolotl/booley/pull/199) for
[#156](https://github.com/boldaxolotl/booley/issues/156). The evaluation used
`main` at `b5ec64bbd17bd9e97fd04525268cb667e7ae74f5`.

## Decision

**Hold. Keep Session Python 3.13 in production.**

The plan requires candidate `S` to install Python 3.14.7 from the same
deadsnakes channel as the control on Ubuntu 24.04. It also requires `S` and the
later Ubuntu 26.04 candidate `U` to report the same
`platform.python_version()`. If the existing channel cannot supply that patch,
the plan explicitly forbids changing both Python source and Ubuntu at once.

Python 3.14.7 is an upstream release dated 2026-08-05, but the current
deadsnakes Noble archive contains Python 3.14.6 packages and no 3.14.7 package:

| Required package | Newest matching Noble archive entry |
| --- | --- |
| `python3.14` | `python3.14_3.14.6-1+noble1_amd64.deb` |
| `python3.14-venv` | `python3.14-venv_3.14.6-1+noble1_amd64.deb` |
| `python3.14-dev` | `python3.14-dev_3.14.6-1+noble1_amd64.deb` |

Sources checked on 2026-08-31:

- [Python 3.14.7 release](https://www.python.org/downloads/release/python-3147/)
- [deadsnakes Python 3.14 package pool](https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu/pool/main/p/python3.14/)

The package pool was last modified on 2026-06-12. Its listed Noble binary,
development, standard-library, and venv packages are all version
`3.14.6-1+noble1`; searching the complete current index for `3.14.7` returns no
entry.

## Actions deliberately not taken

No Dockerfile, Python symlink, user-site path, dependency, or production pin
was changed. In particular, this evaluation did not:

- build CPython from the upstream source tarball;
- switch to the deadsnakes nightly channel or another PPA;
- substitute Python 3.14.6 for the specified 3.14.7 candidate;
- change Ubuntu while changing Python;
- relax the required patch-version equality between `S` and `U`.

Each alternative would add a second migration variable or test a different
candidate from the one specified in #156.

## Resume condition

Re-evaluate Phase 2 after the existing deadsnakes PPA publishes
`python3.14`, `python3.14-venv`, and `python3.14-dev` version 3.14.7 for Noble
amd64, and after the Ubuntu 26.04 candidate can use the same upstream patch.
At that point build `S` from a fresh current-main control and run the dependency,
SDK-discovery, cocotb/Icarus, plain simulation, full test, native-compatibility,
and size gates from the plan.

Until then, the absence of a compatible package is the exact Phase 2 failure;
the hold is intentional and does not close #156.
