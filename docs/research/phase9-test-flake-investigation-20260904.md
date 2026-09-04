# Phase 9 test-flake investigation — 2026-09-04

## Scope and conclusion

This report investigates only the three pytest failures retained by the
[Phase 9 final evaluation](ci-performance-final-evaluation-20260904.md). It
does not recalculate that cohort or reinterpret the independent stable-base
digest failures.

The two console failures have one shared mechanism: `MainPane` queues
unconditional, after-refresh tail scrolls while follow mode is active. A
queued scroll can run after an upward user action and snap the pane back to
the tail, or remain pending while a resize has already changed the wrapped
content height. Windows scheduling made both orderings observable, but the
headless tests use explicit synthetic terminal sizes and do not depend on a
host terminal emulator.

The SIGINT failure is separate. Its test waits 0.2 seconds rather than waiting
for the child to install its signal handlers. Under load, cancellation reached
the child during Python startup, where `SIGINT` caused `site` import to fail
with exit code 1. The supervisor correctly preserved that non-negative child
exit code, so the test observed terminal cause `cancelled` with exit 1 instead
of the intended forced-cancellation result 130.

No relevant source changed between the frozen Phase 9 SHA
`4ab0e406a0622728af265b8ae98b9390cc156318` and current `origin/main`. The Git
blob identities are equal at both revisions for the two test modules,
`console/widgets.py`, `runtime_attachment.py`, and `execution_supervisor.py`.

## Evidence and reproduction

The preserved JUnit and job logs under the Phase 9 evidence directory contain
the exact failures:

- [run 9](https://github.com/boldaxolotl/booley/actions/runs/33814263929),
  Windows 2025 / Python 3.11.9: after one Up key, `scroll_y` remained at
  `max_scroll_y`, producing `153 < 153` on xdist worker `gw1`.
- [run 12](https://github.com/boldaxolotl/booley/actions/runs/33816683880),
  Ubuntu 24.04 / Python 3.13.15: the child printed `Fatal Python error:
  init_import_site: Failed to import the site module`, ending in
  `KeyboardInterrupt`; the result was exit 1, terminal cause `cancelled`, on
  worker `gw1`.
- [run 14](https://github.com/boldaxolotl/booley/actions/runs/33818245239),
  Windows 2025 / Python 3.11.9: after resizing from 120x30 to 70x24,
  `scroll_y` retained the old-layout tail 102 while the new maximum was 228,
  again on worker `gw1`.

The failing and adjacent passing Windows runs used the same
`windows-2025-vs2026` image version `20260824.214.3`, Textual 8.2.8, pytest
9.1.1, four xdist workers, and `--dist=worksteal`. The run-12 Linux image also
passed the same SIGINT test in adjacent run 11. There is therefore no package
or runner-image transition aligned with the failures. The individual JUnit
durations likewise do not support a generic slow-run explanation: the run-9
console failure took 1.508 seconds while adjacent passes took 0.852 and 2.440
seconds; the run-12 SIGINT failure took 0.378 seconds while adjacent passes
took 0.363 and 0.396 seconds. The run-14 resize failure was unusually early at
0.380 seconds versus adjacent passes at 1.183 and 1.353 seconds, consistent
with an assertion overtaking deferred layout work.

The three exact tests were then run in 100 concurrent local pytest processes,
for 300 checks total. All 300 passed on Linux / Python 3.14.4 with the same
Textual and pytest versions as CI. This rules out a frequent platform-neutral
failure, but not a rare event-ordering fault.

Controlled, deterministic probes against the production paths raised the
reproduction rate to 100%:

1. Hold the real after-refresh closure queued by `MainPane._maybe_scroll`, move
   one row up, and then release one held closure. The Up action initially moves
   away from the tail, but the single stale closure snaps the pane back and
   reproduces `153 < 153`.
2. Hold only the real after-refresh tail closure while resizing the production
   test app from 120x30 to 70x24. After the layout updates, the pane reproduces
   the exact CI state `scroll_y == 102` and `max_scroll_y == 228`.
3. Start the production Runtime Attachment path with a child held inside
   `sitecustomize`, signal as soon as that import is active, then send the
   second SIGINT. This reproduces the CI fatal-startup traceback and exact
   `ExecutionResult(exit_code=1, state="terminal", tree_terminal=True,
   terminal_cause="cancelled")` in 0.83 seconds.
4. Replace elapsed-time signaling with a marker written after the child's
   `SIGINT` and `SIGTERM` handlers are installed. The same two-interrupt path
   returns 130 with a terminal process tree.

The probes were kept outside the repository and are not deliverables.

## Root causes

### Deferred console tail work is not revalidated

Every content append calls `MainPane._maybe_scroll`. While `_auto_scroll` is
true, that method calls Textual's `scroll_end(animate=False)`. In Textual 8.2.8
`scroll_end` deliberately creates an after-refresh closure so it can read the
post-layout `max_scroll_y`; once queued, that closure does not re-check
Booley's `_auto_scroll` state.

This creates two races:

- An Up/PageUp/Home action can begin after a tail closure was queued. If the
  closure runs after the action, it forces the pane back to the tail. The
  existing `watch_scroll_y` logic cannot reject work which was scheduled
  earlier.
- `MainPane.on_resize` correctly requests a tail scroll for followers, but the
  interaction test assumes one `Pilot.pause()` after `resize_terminal()` is
  enough to run both layout and the new after-refresh closure. The run-14
  assertion overtook that closure after layout had raised the maximum from 102
  to 228.

The first race is a product behavior defect as well as a test flake. The second
observed failure is primarily an insufficient test-settling contract, though
the same queued-tail design makes the timing window wider. The shared cause is
deferred scroll ordering, not xdist state sharing: each test creates a fresh
app, and high-concurrency local execution did not create cross-test failures.

### The SIGINT test does not synchronize with child readiness

`test_second_sigint_forces_cleanup` starts a Python child whose first work is
installing ignore handlers, but its interrupter sends SIGINT after a fixed
0.2-second sleep. It does not know whether the supervisor has spawned the
child or whether the child has finished interpreter startup and installed the
handlers.

The preserved traceback proves that the first cancellation signal reached
Python during `site` import. In this state Python exits 1. Because cancellation
began before the child exited, `_owned_tree_exit_code` intentionally preserves
a non-negative child return code. This behavior is covered by the neighboring
test which verifies that a command handling SIGINT and exiting zero retains
zero. Changing the production supervisor to overwrite exit 1 with 130 would
weaken that contract and conceal real child outcomes.

## Smallest reliable fixes

1. Make `MainPane` own one coalesced deferred follow-tail request rather than
   queueing one unconditional Textual closure per append. When that request
   runs after refresh, clear its pending flag, re-check `_auto_scroll`, and
   then scroll immediately to the current maximum.
2. Mark upward user actions (at least Up, PageUp, and Home) as leaving follow
   mode synchronously, before their animated movement begins. This lets a
   queued follow callback reject itself even when it runs before the first
   animation tick. Keep `watch_scroll_y` for pointer scrolling and layout
   clamps.
3. Add a deterministic console regression test that holds one follow callback,
   performs Up, releases the callback, and proves the paused position is
   retained. For resize assertions, use a bounded settling helper which waits
   for follower state and `scroll_y == max_scroll_y` across completed layout
   work instead of assuming a fixed number of zero-delay pauses.
4. Change `test_second_sigint_forces_cleanup` to use the readiness-marker
   pattern already used by the adjacent signal-handler test: write the marker
   only after both ignore handlers are installed, wait for it with the existing
   bounded polling style, then send the two host interrupts. No Runtime
   Attachment production change is indicated.

Keep the console and SIGINT changes in separate commits because their root
causes are independent. Verification should repeat the focused console tests
on Windows 3.11 with the normal `worksteal` configuration and the focused
Runtime Attachment test on Ubuntu / Python 3.13. A full replacement Phase 9
cohort is unnecessary for these targeted fixes.
