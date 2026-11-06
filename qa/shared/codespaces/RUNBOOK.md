# Codespaces GUI QA runbook

Shared by the `gui` mission and the `booley-gui-check` skill. It takes a QA
agent from a Booley commit `<sha>` to a readable Booley GUI in a browser
Codespace on the picorv32 demo, checks the GUI against the CLI, and cleans up.

Steps 1-6 say what to do and how to tell it worked; steps 7-9 are rules and
references. Steps 1-5 are setup: if
one fails, stop and report, because nothing after it is valid. From step 6 on,
log a failure as a finding, work around it if you can, and keep going.

| Name | Value |
|---|---|
| `<sha>` | The Booley commit under test (full) |
| `<short>` | The first 7 characters of `<sha>`, as `booley --version` prints them |
| `$DEMO` | `boldaxolotl/booley-prj-picorv32`; shell state does not persist between tool calls, so set `DEMO=...` in each call that uses it |
| QA branch | `qa/<short>` on the demo repo |
| Candidate tag | `candidate-<sha>-<databaseId>-<attempt>` from `docker-publish.yml` (`databaseId` is the run id) |

## Hard rules

- **Never use the clipboard.** No clipboard read or write, no paste, no
  Ctrl+C/Ctrl+V/Ctrl+Shift+V. It is the maintainer's real system clipboard.
  Ctrl+C with no selection, to interrupt a running process, is allowed.
  Ctrl+Shift+C, which the palette lists for "Terminal: Create New Terminal",
  copies in a focused terminal: open terminals with Ctrl+Shift+\` or the
  palette instead.
- **Never type into the Monaco editor.** Edit files from a terminal (heredoc or
  a Python one-liner).
- **Use only your own browser tab.** Tabs belong to the T3 thread, and other
  agents may share it. Open yours with `preview_open` and
  `reuseExistingTab: false`, pass its `tabId` to every `preview_*` call, and
  close only that tab.
- **Never change the maintainer's VS Code settings.** The Settings UI and
  commands such as "Toggle Screen Reader Accessibility Mode" write to the
  default profile, which follows the maintainer into other github.dev
  sessions. QA settings go only into the Codespace's machine settings (step 6).
- **Don't reload the page** unless the area is about reload. A reload loses the
  Dashboard tab, and `booley dashboard` then refuses to reopen it (#2857).
- **The Codespace carries the maintainer's identity.** `/etc/gitconfig` holds
  their name and email, and `booley doctor -v` prints them. Keep that output
  out of screenshots and evidence.

## 1. Preconditions

- A T3 session with the `mcp__t3-code__preview_*` tools. Native subagents get
  them too.
- The `default` browser profile is signed into GitHub as the maintainer.
- `gh auth status` lists the `codespace` scope.
- No leftovers from an earlier run. Both commands print nothing:

  ```sh
  gh codespace list --repo boldaxolotl/booley-prj-picorv32
  gh api repos/boldaxolotl/booley-prj-picorv32/git/matching-refs/heads/qa/ --jq '.[].ref'
  ```

  If either prints something, ask the maintainer before deleting it: it may be
  another run's live repro.

## 2. Candidate image

Find a successful `docker-publish.yml` run for `<sha>`:

```sh
gh run list --repo boldaxolotl/booley --workflow docker-publish.yml \
  --limit 200 --json databaseId,headSha,attempt,status,conclusion \
  --jq '.[] | select(.headSha == "<sha>" and .conclusion == "success")'
```

If there is none, dispatch one on the branch whose head is `<sha>` (`main` or
the PR branch) and wait. A newer dispatch on the same ref cancels an older
one, so dispatch once.

```sh
gh workflow run docker-publish.yml --repo boldaxolotl/booley --ref <branch>
sleep 10   # then find the new run's id by commit, not by position
gh run list --repo boldaxolotl/booley --workflow docker-publish.yml --limit 20 \
  --json databaseId,headSha,event \
  --jq '.[] | select(.headSha == "<sha>" and .event == "workflow_dispatch") | .databaseId' | head -1
gh run watch <run-id> --repo boldaxolotl/booley --exit-status
```

With warm caches the run takes about 15-20 minutes; cold, an hour or more.
The candidate tags appear about 10 minutes in, before the contract and demo
jobs finish: still wait for `success`.

The demo uses the RISC-V image. The tag under test is
`candidate-<sha>-<databaseId>-<attempt>` from that run. Worked when: the run
concluded `success`.

## 3. QA branch on the demo repo

Create `qa/<short>` as one commit on top of the demo's `main` that changes only
the `image` line in `.devcontainer/devcontainer.json`. This run may push and
delete that branch and must not touch any other demo branch. Run this in a
scratch directory, not a checkout:

```sh
DEMO=boldaxolotl/booley-prj-picorv32
TAG=candidate-<sha>-<databaseId>-<attempt>
BRANCH=qa/<short>
base=$(gh api repos/$DEMO/git/ref/heads/main --jq .object.sha)
gh api repos/$DEMO/git/refs -f ref=refs/heads/$BRANCH -f sha=$base
file=$(gh api "repos/$DEMO/contents/.devcontainer/devcontainer.json?ref=$BRANCH")
echo "$file" | jq -r .content | base64 -d \
  | sed -E "s#(\"image\": \"ghcr.io/boldaxolotl/booley-sandbox-riscv:)[^\"]*#\1$TAG#" \
  > devcontainer.json
gh api -X PUT repos/$DEMO/contents/.devcontainer/devcontainer.json \
  -f branch=$BRANCH -f message="QA: pin candidate $TAG" \
  -f sha="$(echo "$file" | jq -r .sha)" \
  -f content="$(base64 -w0 devcontainer.json)" --jq .commit.sha
```

Worked when this prints `1` and only `.devcontainer/devcontainer.json`:

```sh
gh api "repos/$DEMO/compare/main...$BRANCH" --jq '.ahead_by, [.files[].filename]'
```

## 4. Create the Codespace

Open your tab on the creation page for the QA branch (the README's "Open in
Codespaces" flow, pointed at the branch), then set the main viewport:

```text
https://github.com/codespaces/new?repo=<repo-id>&ref=qa/<short>
```

Get `<repo-id>` from `gh api repos/$DEMO --jq .id`. Call `preview_resize`
with `freeform` 1440×900; its result reports the scaled screenshot size
(1280×800), so confirm 1440×900 with `preview_status`. Check that the page shows branch `qa/<short>`,
configuration "Booley PicoRV32 demo" and machine type 4-core, then click
"Create codespace" and note the time. The Codespace's name exists only after
the click: add its `resources.md` row as "name pending" before clicking, and
fill in the name from the listing below.

Wait with `gh codespace list --repo $DEMO --json name,state` until the state is
`Available`, and note its name (cleanup needs it). On 2026-11-06 that took
2.5-3 min, and the trust prompt ("Do you trust the authors of the files in
this folder?") appeared 15-30 s later.

## 5. Trust, Dashboard, version

1. VS Code opens in Restricted Mode every time (by design, #2699), and no
   terminal starts until the folder is trusted. Click "Trust Folder &
   Continue". A selector click on that button can time out; click its
   coordinates instead (see "Clicking" in step 7), which works first time.
2. About 5-20 s later the Dashboard extension opens a "Booley Dashboard"
   terminal running `booley dashboard --on-attach`. It should come to the
   front on its own (#2700). On 2026-11-06 the `postAttachCommand` terminal
   ("Codespaces: python -m booley.runtime.incontainer_live_preview …") stayed
   in front, and a "Booley Dashboard is ready" toast may appear; that's a
   finding. The tab list then holds `bash`, the `Codespaces: python -m …`
   terminal and `Booley Dashboard`, in either order. Click the "Booley
   Dashboard" entry.
3. Open a command terminal: F1, type `Terminal: Create New Terminal`, Enter.
   Run `booley --version`. Before step 6 the terminal is a canvas: read the
   result from a screenshot.

Worked when the output reads `booley <version> (<short>, <date>)`, for
example `booley 0.4.0 (10c8f9c, 2026-11-06)`. Any other
commit means the Codespace runs the wrong image: stop and recheck steps 2-4.

Note the Dashboard's `Doctor:` line now. The automatic Doctor run happens
before the folder is trusted, so this first value can differ from a manual
run (#2702).

## 6. Reading mode

By default the terminal draws on a canvas, so the page holds none of its text;
only screenshots show it. Missions that start with a default-settings pass
(screenshots only) do that pass first and record the switch in `log.md`.

Machine settings stay inside this Codespace, apply to open terminals within
seconds, and survive a page reload.
`~/.vscode-remote/data/Machine/settings.json` already holds the demo's
devcontainer settings, so merge into it; never overwrite it. `gh codespace ssh`
does not work (the image has no sshd), so type this into the command terminal:

```sh
python3 -c "import json,os;p=os.path.expanduser('~/.vscode-remote/data/Machine/settings.json');d=json.load(open(p));d.update({'terminal.integrated.gpuAcceleration':'off','workbench.colorTheme':'Dark 2026'});json.dump(d,open(p,'w'),indent=4)"
```

The same merge pins the theme to `Dark 2026`, so every run looks alike (a
Codespace otherwise opens light or dark, and a reload can flip it). Other
themes by name: `Light 2026`, `Dark High Contrast`, `Light High Contrast`.
Never pick one in the theme picker: that writes the maintainer's settings.

With `terminal.integrated.gpuAcceleration: "off"`, terminal rows are DOM text.
Read the terminal in front with `preview_evaluate`:

```js
document.querySelector('.terminal-wrapper.active .xterm-rows').innerText
```

Then hide the sidebar: F1, `View: Toggle Primary Side Bar Visibility`, Enter.
Ctrl+B doesn't work while a terminal has focus, because the shell takes it.

Worked when that expression returns the terminal's text, including your
`booley --version` line, and
`document.querySelector('.monaco-workbench').className` contains
`2026-dark-json` (within about 5 s, no reload).

Optional: `editor.accessibilitySupport: "on"` (same one-liner) also lists
terminal rows in `preview_snapshot`. But then `preview_act type` into a
terminal is silently dropped: only single-key `press` gets through. To undo it
with key presses alone, press the characters of `^'on'^'off'` and Enter: bash
reruns the previous one-liner with `off`. Snapshots are capped (10-20 KB
depending on the tool version), another reason to prefer `preview_evaluate`.

## 7. Interaction rules

- **Clicking:** screenshots come back scaled (a 1440×900 viewport gives a
  1280×800 image), so never click at coordinates read off a screenshot. Get
  them from the page:

  ```js
  [...document.querySelectorAll('.monaco-button, .terminal-tabs-entry')].map(e => {
    const r = e.getBoundingClientRect();
    // A tab entry's center holds its hover Split and Kill buttons: click near its left edge.
    const x = e.matches('.terminal-tabs-entry') ? r.x + 30 : r.x + r.width / 2;
    return [e.innerText.trim() || e.getAttribute('aria-label'), x, r.y + r.height / 2];
  })
  ```

  An `aria-ref` from `preview_snapshot` also works.
- **First key press:** click somewhere in the page first. Until then `press`
  fails with "focus inside cross-site frame".
- **Terminal tabs:** switch terminals by clicking their entry in the tab list
  on the right of the panel. That click focuses the list, not the terminal:
  before typing, click inside the terminal body (center of
  `.terminal-wrapper.active`). A typed command can still be dropped: check
  that `document.activeElement.getAttribute('aria-label')` starts with
  `Terminal`, and read the command line back before pressing Enter. The
  number in that label doesn't follow the tab list order. After switching,
  wait about 5 s before reading: the new terminal's rows come back blank
  before that.
- **Commands:** `preview_act type` the command, then `press Enter`. Start with
  `clear;` and end with a marker unique to this command, such as
  `; echo __"d07"__`, then wait for `__d07__` with
  `preview_wait_for` (up to 60 s per call) before reading. The quotes keep the
  typed command line from matching; waiting for the prompt matches the old
  prompt line, and a reused marker matches the previous command's output
  before `clear` runs. This needs step 6; before it, poll with screenshots.
- **Read only the terminal in front.** A terminal behind another one keeps
  stale DOM text: on 2026-11-06 the hidden Dashboard still read `1 FAIL` after
  the CLI had published `0 failed`, and showed the new value once brought to
  the front.
- **Dashboard:** click inside it, then press keys. `ArrowRight`/`ArrowLeft`
  switch between Sessions and Jobs, `Enter` opens a detail, `Escape` goes back
  one level. `ArrowUp` on the first row moves into the top bar (filters,
  Help); `ArrowDown` or `Escape` leaves it. The top bar reads
  `Sessions Jobs │ Running Finished All Owner │ Help`: `ArrowLeft`/`ArrowRight`
  choose, `Enter` applies. In Help, after leaving the top bar, `ArrowDown`
  scrolls; the mouse wheel and PageDown don't. `q` quits; `booley dashboard`
  in a terminal reopens it.
- **Long output:** F1, `View: Toggle Maximized Panel` shows output that doesn't
  fit the panel (a layout change, not a setting), and gives the Dashboard more
  than its default ~258 px. Toggle it back afterwards.
- **Resizing:** after the viewport narrows, the terminal DOM keeps old cells
  past the new width (a line can read twice). Trust the screenshot then, or
  cut each row to the new column count.
- **Snapshot timeouts:** `preview_snapshot` can time out several times in a
  row while `preview_evaluate` still works. Wait a minute and retry.
- **Command palette:** F1 opens it even with a terminal focused, and its input
  accepts `type`.
- **Files:** write them from a terminal (heredoc or Python), never through the
  editor.
- **Theme:** before step 6 the theme is whatever the Codespace opened with;
  note it with every screenshot set.

## 8. Oracles

The CLI is the truth; the main bug class is "GUI disagrees with CLI". Run the
oracle right after the screenshot it checks, then bring the GUI back to the
front and allow a refresh (the Dashboard redraws every 2 s) before you call a
mismatch.

| GUI surface | Oracle (command terminal) |
|---|---|
| Dashboard `Doctor:` line (`OK`, or `N FAIL · M WARN`) | `booley doctor` (not `-v`, see Hard rules). It republishes the result the Dashboard reads, so the line must match its summary after the next refresh. |
| Dashboard resource line (CPU, Memory, Free disk) | No Booley command: `booley resources --json` shows historical peaks per Target, not this line. Compare with `nproc`, `cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.max` and `df -h /workspaces`; CPU % only roughly, against `top -bn1 \| head -5`. |
| Notices (upgrade review) | `booley upgrade status --json` |
| Sessions overview, preview pane, session detail | No CLI. Check against what you started: which agent sessions, their branch and work dir (`git worktree list`), and the last Booley call you made in each. Each call shows as two rows (started, completed), and the detail stays on an ended session until `Escape`. Without a signed-in agent, make sessions with the scripted MCP client below. |
| Goal rows and Goal detail | `booley goal status --long`, run in that Goal's worktree |
| Jobs window, Job detail | No CLI (agents see Jobs through the `booley_status` MCP tool). Read the Job records: `for f in /workspaces/picorv32/.booley/{,goals/*/,logs/*/}.runtime/jobs/*.json; do jq -c '{run_id,endpoint,status,exit_code,owner,work_dir}' "$f"; done 2>/dev/null`, plus `ps -eo pid,etime,args` for running ones. A record says `running` until its server notices the end, so check the PID before calling a status mismatch. No output means no Job records: before #2298, shell Flows write none. For Flows you started yourself, their terminal output is the oracle. |
| Proposal detail | No CLI; compare with the proposal text in the agent chat. |
| VaporView scoped view (`bwave gui ... -s ... -g ...`) | The command's own report: it prints the signal and group tree VaporView reports back (`getViewerState`), the viewport and the marker times, and warns on stderr about signals the viewer didn't show. Compare the screenshot with that report, and the report with what you asked for. `--time`/`--cursor` without `-s` re-reports the rows, but it moves the markers. |

**Scripted MCP client.** Sessions without an agent: in a command terminal,
write a Python script (heredoc) that uses the image's `mcp` package against the
Booley MCP server that `~/.codex/config.toml` names
(`http://127.0.0.1:8814/mcp` on 2026-11-06). That `mcp` version's API is
`mcp.client.streamable_http.streamable_http_client`, and results carry
`is_error` (not `streamablehttp_client` and `isError`). Calling `booley_status`
creates an Interactive session. `goal_enter` with `work_dir` set to a fresh
`git worktree add --detach` directory, a `slug`, and
`goals=[{"family": "LINT", "target": "lint_core"}]` creates a Goal session.
Keep the client connected (a long sleep) while you check, and Ctrl+C it to
disconnect.

A surface with no oracle is still checked against what you did. Record the
gap in `log.md` the first time you meet it.

## 9. Evidence

- Every claim gets a screenshot named `<area>-<nn>-<what>.png` (for example
  `dashboard-live-03-jobs-two-flows.png`). Take it with `preview_snapshot`
  (`save: true`, `includeImage: false`, your `tabId`; with the image it also
  returns a 10 KB+ accessibility tree) and copy the returned `screenshotPath`
  into the run's evidence directory under that name. Saved screenshots come
  out either 1280×800 (blurry, terminal text barely legible) or 1251×800
  (sharp, right ~30 px cut off); a viewport no wider than 1280 saves at full
  size and sharp. After step 6, read text from the DOM rather than from the
  image. Save the matching oracle output as
  `<area>-<nn>-<what>.txt` next to it.
- Recordings only to reproduce a finding.
- Screenshots come only from the public demo Project. Before posting any image
  anywhere public, look at it yourself: the confidential-content guard reads
  text, not images. Drop any image that shows a token, an email address, a
  private repo, a notification from outside the Codespace, or anything beyond
  the Codespace tab.

## 10. Cleanup

1. Close your tab (`t3_preview_close` with your `tabId`).
2. Delete the Codespace and the QA branch:

   ```sh
   gh codespace delete --codespace <name> --force
   gh api -X DELETE repos/boldaxolotl/booley-prj-picorv32/git/refs/heads/qa/<short>
   ```

3. Check: `gh codespace list --repo $DEMO` and the `qa/` ref listing from
   step 1 both print nothing.

Exception: if a finding needs a live repro, stop the Codespace instead of
deleting it (`gh codespace stop --codespace <name>`), keep the branch and the
image, and tell the maintainer the Codespace name, the branch, and the
finding. A stopped Codespace still uses storage quota.

The candidate image's lifecycle belongs to the caller (mission or skill): only
a run that dispatched the build deletes its versions, and that needs the
`read:packages` and `delete:packages` scopes
(`gh auth refresh -h github.com -s read:packages,delete:packages`, done by the
maintainer).

### Candidate image versions

One build pushes `candidate-<sha>-<databaseId>-<attempt>` to `booley-sandbox`
and `booley-sandbox-riscv`. Each tag is an image index with an image manifest
and an SBOM attestation manifest, and GHCR lists each of the three as its own
package version. Delete exactly those three per package, and never anything in
`booley-sandbox-base`: its tags are content-addressed and shared by every
build. Run this in a scratch directory; it writes one listing per package.

```sh
TAG=candidate-<sha>-<databaseId>-<attempt>
for PKG in booley-sandbox booley-sandbox-riscv; do
  REF=ghcr.io/boldaxolotl/$PKG:$TAG
  # The index digest plus every manifest digest it lists.
  digests=$( { docker buildx imagetools inspect "$REF" --format '{{json .Manifest}}' | jq -r .digest
               docker buildx imagetools inspect "$REF" --raw | jq -r '.manifests[].digest'; } )
  gh api --paginate "/user/packages/container/$PKG/versions?per_page=100" \
    --jq '.[] | [.id, .name, (.metadata.container.tags | join(","))] | @tsv' > "$PKG-versions.tsv"
  for d in $digests; do grep -P "\t$d\t" "$PKG-versions.tsv"; done
done
```

Check the printed rows before deleting anything: exactly three per package,
the index row carrying exactly `$TAG` and the other two no tag. Fewer rows
(the inspect failed, or the listing missed a version) means stop. Any other tag (a release, `latest`, a
second candidate) means the version is shared: delete nothing in that package
and tell the maintainer. Otherwise delete each printed id:

```sh
gh api -X DELETE "/user/packages/container/<pkg>/versions/<id>"
```

Worked when `docker buildx imagetools inspect ghcr.io/boldaxolotl/<pkg>:$TAG`
fails with "not found" for both packages.
