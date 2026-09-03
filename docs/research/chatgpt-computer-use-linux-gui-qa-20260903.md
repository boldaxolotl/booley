# ChatGPT Computer Use on Linux as Booley's GUI QA path — 2026-09-03

## Question

Should [`Map the public Booley QA scenario suite`](https://github.com/boldaxolotl/booley/issues/246)
wait for first-party ChatGPT desktop Computer Use on Linux instead of designing
Windows and Linux GUI capture/control adapters in
[`Specify unattended GUI evidence capture and review`](https://github.com/boldaxolotl/booley/issues/310)?

## Recommendation

**Yes: wait for and qualify first-party ChatGPT desktop Computer Use on Linux
before designing Booley-owned operating-system capture/control adapters.** It is
the right category of product: it can see desktop GUIs, take screenshots, and
operate windows, menus, keyboard input, and clipboard state. OpenAI already
ships that packaged capability on macOS and Windows and explicitly says a
future Linux release will add it. The Linux desktop app itself is currently a
preview, and Computer Use is not yet available in it.

This should be a **pause with acceptance gates**, not an assumption that the
future feature will automatically satisfy the QA protocol. Current official
documentation does not promise that the screenshots used internally by the
desktop feature can be exported as stable, named QA artifacts. It also permits
app and sensitive-action approval prompts. Therefore, the open ticket should
stop short of inventing X11/Wayland/Windows drivers now, retain the already
decided machine-readable semantic oracles, and become a qualification decision
for the Linux feature when it ships.

Do not substitute the Responses API `computer` tool as a way to avoid the wait.
That tool supplies the model's visual reasoning and structured action choices,
but the application still has to provide the desktop, capture every screenshot,
execute every mouse/keyboard action, and return the updated screenshot. Using
it for Booley today would be designing the very host harness the proposal seeks
to avoid.

## Two different products called Computer Use

### Packaged ChatGPT desktop Computer Use

This is the feature relevant to the proposal. In supported regions it is
available in the ChatGPT desktop app on macOS and Windows for ChatGPT Work and
Codex. It is installed as a Computer Use plugin. OpenAI describes it as able to
see and operate graphical interfaces and specifically lists desktop-app testing
and reproducing GUI-only bugs as suitable uses
([Computer Use](https://learn.chatgpt.com/docs/computer-use)).

It does the integration work Booley does not want to own: screen access and
OS-level interaction. It can view screen content, take screenshots, and
interact with target-app windows, menus, keyboard input, and clipboard state.
It cannot automate terminal apps or ChatGPT itself, authenticate as an
administrator, or approve operating-system security/privacy prompts
([Computer Use: safety guidance](https://learn.chatgpt.com/docs/computer-use#safety-guidance)).

Platform behavior differs today:

- On **Windows**, the target must be visible in the active desktop session.
  Computer Use takes over foreground input and cannot work in the background
  while a person uses the same session. For unattended work OpenAI says to keep
  the device unlocked and online, or run the desktop app in a Windows VM
  ([Windows foreground use](https://learn.chatgpt.com/docs/computer-use#windows-foreground-use)).
- On **macOS**, Screen Recording and Accessibility permissions provide viewing
  and control. OpenAI documents background use and an opt-in, narrowly scoped
  locked-use mechanism
  ([setup and suitable uses](https://learn.chatgpt.com/docs/computer-use#when-to-use-computer-use),
  [locked use](https://learn.chatgpt.com/docs/computer-use#locked-use)).
- On **Linux**, the desktop app preview supports selected Ubuntu, Debian, and
  Fedora releases, but Computer Use is explicitly unavailable. OpenAI says a
  future release will add Linux support; it gives no date. Native Wayland is
  also experimental, with possible limitations in focus, window positioning,
  and keyboard shortcuts
  ([Linux compatibility and limitations](https://learn.chatgpt.com/docs/linux/linux-app#compatibility-and-limitations),
  [Wayland support](https://learn.chatgpt.com/docs/linux/linux-app#wayland-support)).

App permissions are separate from operating-system permissions. ChatGPT asks
before using an app, but an app can be put on an `Always allow` list. It may
still ask before sensitive or disruptive actions. Windows allows persistent
app IDs in `$CODEX_HOME/config.toml`
([permissions and approvals](https://learn.chatgpt.com/docs/computer-use#permissions-and-approvals)).
This makes a pre-provisioned low-risk QA VM plausible, but the documentation
does not guarantee that an arbitrary flow will never pause for approval.

### Responses API `computer` tool

The API tool is a model-side component, not a hosted Linux desktop automation
service. OpenAI's documented loop is:

1. send a task with the `computer` tool enabled;
2. inspect the returned structured actions;
3. execute those actions in **your harness**;
4. capture the updated screen in **your harness** and send it back; and
5. repeat until no further computer call is returned.

OpenAI states directly that the harness is the keyboard and mouse, while the
model interprets screenshots and chooses what to do
([Computer use API guide](https://developers.openai.com/api/docs/guides/tools-computer-use#option-1-run-the-built-in-computer-use-loop)).
The guide tells developers to prepare an environment that captures screenshots
and runs actions, and offers Playwright/Selenium or an Ubuntu desktop with Xvfb,
VNC, `xdotool`, and ImageMagick as example harnesses
([prepare a safe environment](https://developers.openai.com/api/docs/guides/tools-computer-use#prepare-a-safe-environment)).

The caller-created screenshot is sent back as `computer_call_output`; the
example captures PNG bytes itself. Consequently the API can be used on Linux
today, and those caller-owned bytes can be retained, but it does not eliminate
Booley's capture/control engineering
([capture and return the updated screenshot](https://developers.openai.com/api/docs/guides/tools-computer-use#4-capture-and-return-the-updated-screenshot)).
OpenAI recommends an isolated browser or VM and a human in the loop for
purchases, authenticated flows, destructive actions, and hard-to-reverse work
([API safety guidance](https://developers.openai.com/api/docs/guides/tools-computer-use#keep-a-human-in-the-loop)).

## Screenshot evidence is still unresolved

Official desktop documentation says Computer Use *takes* screenshots and that
ChatGPT data controls apply to them, but it does not document a stable API,
filesystem path, export operation, retention period, or per-step attachment
contract for those internal screenshots
([Computer Use: safety guidance](https://learn.chatgpt.com/docs/computer-use#safety-guidance)).
That absence matters because the scenario protocol requires durable evidence
with run, step, assertion, timestamp, hash, and window identity—not merely a
model statement that the screen looked correct.

Two adjacent features do not fill the gap:

- **Appshots** are explicit, user-triggered captures of the frontmost macOS
  window. They become locally stored session attachments, but are macOS-only
  and initiated with a hotkey, so they are not a documented unattended
  cross-platform evidence channel
  ([Appshots](https://learn.chatgpt.com/docs/appshots)).
- **Computer History** is macOS-only and explicitly does not include
  screenshots
  ([Computer History](https://learn.chatgpt.com/docs/customization/computer-history)).

Thus first-party Linux Computer Use may solve **observation and control** while
still leaving **evidence publication** to Booley or to a future OpenAI export
facility.

## Qualification gates after Linux support ships

Before adopting it as the GUI path, run one small disposable qualification on
both required QA hosts (Linux and Windows). Accept the dependency only if all
of these are true:

1. **Target-app control:** It can select and operate the exact VS Code window,
   the Codex and Claude Code VS Code surfaces, and VaporView. Verify whether the
   ban on terminal apps affects VS Code's integrated terminal; current docs do
   not answer that.
2. **Unattended execution:** A dedicated, isolated QA desktop/VM can remain
   available overnight, with all safe app and OS permissions provisioned in
   advance, and the prescribed non-sensitive flow completes without an
   unexpected approval prompt. An unexpected prompt must yield `not runnable`
   or `inconclusive`, never pass.
3. **Window isolation:** Capture is demonstrably tied to the intended app and
   window, not simply whatever happened to be foreground. Wrong-window or lost
   focus behavior must fail closed. Test Linux under the actual X11/XWayland or
   native Wayland mode Booley intends to qualify.
4. **Durable raw evidence:** The exact rendered image used for each GUI-only
   assertion can be exported or saved as original PNG bytes into the scenario
   evidence directory. A prose conclusion or inaccessible internal screenshot
   is insufficient.
5. **Evidence identity:** Booley can associate each image with run, scenario,
   step, assertion, timestamp, dimensions, hash, target-window/app identity,
   platform/session mode, and the paired Booley artifact or VaporView WCP/B-Wave
   evidence.
6. **Secret-safe framing:** The target is a dedicated clean desktop with no
   unrelated or sensitive apps visible. The capture scope is predictable, and
   accidental exposure or a permission/capture failure is detectable.
7. **Review contract:** The visual assertion produces a structured automated
   verdict with reason and image reference; human inspection remains later and
   asynchronous. The raw image is preserved for that audit.
8. **Repeatability and failure semantics:** Repeated runs reach the correct
   window and state without coordinate-specific scripting. Unavailable display,
   lock/focus loss, capture failure, unsupported platform mode, and ambiguous
   visual state map to explicit `not runnable`, `inconclusive`, or failure
   outcomes.

If these gates pass, Booley should use ChatGPT desktop Computer Use as the
platform adapter and design only a thin scenario-facing evidence contract. If
control works but raw images cannot be exported, first ask whether OpenAI's
supported plugin/tool surface can expose the screenshots; only then consider a
minimal capture-only companion. If the unattended or window-isolation gates
fail, reopen platform-adapter design rather than weakening the evidence rule.

## What Booley still owns in either outcome

Waiting does not remove the need to define:

- which GUI facts are asserted and which machine artifacts/WCP state are their
  semantic oracle;
- the run/step/assertion identity and artifact layout;
- hashes, timestamps, platform and app/window metadata;
- secret-avoidance and dedicated-desktop policy;
- automated verdict schema and asynchronous human-review workflow; and
- precise failure, `inconclusive`, and `not runnable` semantics.

These are product-independent parts of the Scenario Protocol. What should be
deferred is the custom X11, Wayland, and Windows implementation underneath
them.

## Uncertainties

- OpenAI promises future Linux support but publishes no delivery date.
- Official docs do not establish whether Linux will support background or
  locked operation, which display systems it will qualify, or whether its
  behavior will match Windows or macOS.
- Official docs do not establish durable export of screenshots taken during a
  desktop Computer Use task.
- Official docs do not explicitly establish that Computer Use may operate the
  Codex or Claude Code extension surfaces inside VS Code, nor how the terminal
  prohibition applies to VS Code's integrated terminal.
- Official docs do not guarantee a prompt-free unattended task even after app
  allowlisting; sensitive or disruptive actions may still require approval.
