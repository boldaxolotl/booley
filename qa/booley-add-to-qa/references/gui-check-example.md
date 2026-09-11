# GUI Check example

Read this example only when a proposed requirement covers the supported VS Code
Runtime Attachment or Waveform Viewer.

The Taxi Scenario's `viewer.visual-capture` Check proves that the Waveform Viewer shows
the expected signals, markers, and cursor. This is distinct from
`viewer.actual-client-open`: the client can open without rendering the correct state.

The Check belongs to the GUI Check set and requires a supported VS Code Runtime
Attachment, WCP access, and a qualified screenshot observer:

```yaml
- id: viewer.visual-capture
  capabilities:
  - WAVEFORM-VIEWER
  stimulus: Capture Viewer only with pre-run qualified observer; verify visible scoped signals/markers/cursor.
  expected: Capture Viewer only with pre-run qualified observer; verify visible scoped signals/markers/cursor.
  authority_ref: https://github.com/boldaxolotl/booley/blob/b163fd1f45b76f3950005678e500e695232832fb/qa/handoff/taxi.md
  evidence: Timestamped screenshot with Waveform Viewer and run identity plus observer capability evidence; unavailable observer means the GUI Scenario Run is incomplete.
  capture: evidence/taxi-10g-mac-port-evolution.viewer.visual-capture
```

Only GUI Configured Scenarios select the GUI Check set. Their pre-run requirements
must name the actual client, WCP access, and observer. A CLI or headless run cannot earn
credit for the visual claim; an unavailable observer makes the GUI Scenario Run
incomplete rather than removing the Check.

Review the screenshot oracle, observer qualification, prerequisite order, Configured
Scenario selection, and budget. This is a behavioral change, so every affected required
GUI Configured Scenario needs a fresh run.
