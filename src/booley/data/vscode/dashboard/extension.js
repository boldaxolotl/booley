'use strict';

const vscode = require('vscode');
const { execFile } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const BOOLEY = '/usr/local/bin/booley';
const PROTOCOL = 'booley.dashboard.attachProtocol';
const OPEN_COMMAND = 'booley.dashboard.open';
// VS Code remote names whose workspace extension host runs inside the Sandbox;
// GitHub Codespaces reports `codespaces`.
const REMOTES = new Set(['dev-container', 'attached-container', 'codespaces']);
// GitHub Codespaces runs devcontainer lifecycle commands (postAttachCommand
// and friends) in terminals it names `Codespaces: <command>`.
const LIFECYCLE_PREFIX = 'Codespaces: ';
// A reload leaves the earlier Dashboard process answering for a few seconds, so an
// automatic open straight after it can find the Dashboard still counted as seen.
// Look once more when it will have gone unseen (`UNSEEN_AFTER`, view_probe.py: 15 s).
const RECHECK_MS = 16000;
const ALREADY_OPEN_REASON = 'The Dashboard is already open in this Sandbox.';

// A Codespaces lifecycle-command terminal: Codespaces may send its command as
// text, which VS Code also counts as interaction, so it never means the user
// is using the panel. Check both names: Codespaces may set the prefix at
// creation or only as the terminal's title. A user terminal renamed to
// `Codespaces: ...` also matches; that rare case is accepted.
function isLifecycleTerminal(terminal) {
    return vscode.env.remoteName === 'codespaces' &&
        [terminal.name, terminal.creationOptions.name]
            .some(name => name?.startsWith(LIFECYCLE_PREFIX));
}

function policy(root, signal) {
    return new Promise((resolve, reject) => {
        execFile(BOOLEY, ['dashboard', '--attach-policy'], {
            cwd: root.fsPath,
            timeout: 35000,
            maxBuffer: 65536,
            signal,
        }, (error, stdout, stderr) => {
            if (error) {
                reject(new Error(stderr.trim() || error.message));
                return;
            }
            try {
                const value = JSON.parse(stdout);
                if (value.protocol !== 1 || typeof value.enabled !== 'boolean' ||
                    typeof value.reason !== 'string') {
                    throw new Error('Unsupported Dashboard attach policy; refresh the Sandbox.');
                }
                resolve(value);
            } catch (error) {
                reject(error);
            }
        });
    });
}

class AttachController {
    constructor(context) {
        this.context = context;
        this.output = vscode.window.createOutputChannel('Booley Dashboard');
        this.abort = new AbortController();
        this.attempted = context.workspaceState.get('attachAttempts', []).includes(vscode.env.sessionId);
        this.disposed = false;
        // The automatic open in flight, so the command never races it into a second terminal.
        this.opening = undefined;
        this.recheck = undefined;
        context.subscriptions.push(this.output, this,
            vscode.commands.registerCommand(OPEN_COMMAND, () => this.openByCommand()),
            vscode.workspace.onDidGrantWorkspaceTrust(() => { void this.open(); }),
            vscode.workspace.onDidChangeConfiguration(event => {
                if (event.affectsConfiguration(PROTOCOL)) { void this.open(); }
            }),
            vscode.window.onDidCloseTerminal(closed => this.closed(closed)));
    }

    // The Sandbox Project root this window may show the Dashboard for, or the
    // reason it may not. An explicit request (the command) is not automatic, so
    // the attach protocol, the automatic-task preference and the Project's
    // auto-open opt-outs do not apply, like a manual `booley dashboard`.
    rootOrReason(explicit) {
        if (this.disposed) { return { quiet: true }; }
        const remote = vscode.env.remoteName;
        // A local window (no remote) is never a Sandbox; stay quiet there.
        if (remote === undefined || remote === null) {
            return { quiet: true, reason: 'This window is not a Booley Sandbox.' };
        }
        if (!REMOTES.has(remote)) {
            return { reason: explicit
                ? `Booley: Open Dashboard needs a Booley Sandbox window, not remote '${remote}'.`
                : `Dashboard auto-open skipped: remote '${remote}' is not a Sandbox remote.` };
        }
        if (!vscode.workspace.isTrusted) {
            return { reason: explicit
                ? 'Booley: Open Dashboard needs a trusted window; trust this workspace first.'
                : 'Dashboard auto-open waits for workspace trust; this window is untrusted.' };
        }
        if (!explicit) {
            if (vscode.workspace.getConfiguration().get(PROTOCOL) !== 1) {
                return { reason: 'Dashboard auto-open skipped: the attach protocol is not enabled.' };
            }
            const permission = vscode.workspace.getConfiguration('task').inspect('allowAutomaticTasks');
            if (permission?.globalValue === 'off') {
                return { reason: 'Dashboard attach disabled by your automatic-task preference.' };
            }
        }
        const folders = vscode.workspace.workspaceFolders || [];
        if (folders.length !== 1) {
            return { reason: 'Dashboard attach requires one opened Sandbox Project folder.' };
        }
        const root = folders[0].uri;
        if (!fs.existsSync(path.join(root.fsPath, '.git'))) {
            return { reason: explicit ? 'The Dashboard requires a Git Project Root.'
                : 'Dashboard auto-open requires a Git Project Root.' };
        }
        return { root };
    }

    eligibleRoot() {
        const found = this.rootOrReason(false);
        if (found.reason && !found.quiet) { this.output.appendLine(found.reason); }
        return found.root;
    }

    async open() {
        if (this.attempted) { return; }
        const root = this.eligibleRoot();
        if (!root) { return; }
        this.attempted = true;
        const attaching = this.attach(root);
        this.opening = attaching;
        try {
            await attaching;
        } finally {
            if (this.opening === attaching) { this.opening = undefined; }
        }
    }

    // The automatic open after the window passed `eligibleRoot`; it never rejects.
    async attach(root, rechecking = false) {
        try {
            const attempts = this.context.workspaceState.get('attachAttempts', []);
            await this.context.workspaceState.update('attachAttempts',
                [...attempts.slice(-15), vscode.env.sessionId]);
            if (this.disposed) { return; }
            const result = await policy(root, this.abort.signal);
            if (this.eligibleRoot()?.fsPath !== root.fsPath) { return; }
            if (!result.enabled) {
                this.output.appendLine(result.reason);
                if (result.already_open && !rechecking) { this.recheckLater(root); }
                return;
            }
            // Sample before creating ours: a terminal that has received input since
            // this window loaded (for example a shell the user typed in) is in use.
            // A terminal nobody has used yet, such as the Codespace shell that
            // starts with the Trust click, does not hold the Dashboard back, so it
            // can end up behind the Dashboard. A Codespaces lifecycle-command
            // terminal is never in use, even though VS Code may flag it interacted.
            const busy = this.terminalInUse();
            const terminal = this.createDashboardTerminal(root, ['dashboard', '--on-attach']);
            if (busy) {
                this.offer(terminal);
            } else {
                terminal.show(true);
            }
        } catch (error) {
            if (!this.disposed) {
                this.output.appendLine(`Dashboard attach failed: ${error.message}`);
                this.output.show(true);
            }
        }
    }

    // One more automatic attempt after `RECHECK_MS`, unless a Dashboard shows by then.
    recheckLater(root) {
        this.recheck = setTimeout(() => {
            this.recheck = undefined;
            if (!this.disposed && !this.shownDashboard()) { void this.attach(root, true); }
        }, RECHECK_MS);
    }

    // The one fixed launch of the Dashboard terminal; the view survives an
    // extension-host restart, never a reload (hence transient).
    createDashboardTerminal(root, args) {
        const terminal = vscode.window.createTerminal({
            name: 'Booley Dashboard',
            shellPath: BOOLEY,
            shellArgs: args,
            cwd: root,
            isTransient: true,
        });
        this.terminal = terminal;
        return terminal;
    }

    // The Dashboard terminal this window shows, if any. After an extension-host
    // restart the terminal lives on but `this.terminal` is lost, so adopt one
    // created by the fixed launch above (matched by launch, not by name).
    shownDashboard() {
        const live = vscode.window.terminals.filter(t => !t.exitStatus);
        if (this.terminal && live.includes(this.terminal)) { return this.terminal; }
        const adopted = live.find(t => t.creationOptions?.shellPath === BOOLEY &&
            t.creationOptions.shellArgs?.[0] === 'dashboard');
        if (adopted) { this.terminal = adopted; }
        return adopted;
    }

    // Booley: Open Dashboard. Reveal the Dashboard this window shows, or open
    // one. It is explicit, so the automatic-open opt-outs do not apply.
    async openByCommand() {
        if (this.opening) { await this.opening; }
        const found = this.rootOrReason(true);
        if (!found.root) {
            this.explain(found.reason ?? 'Booley: Open Dashboard is unavailable.');
            return;
        }
        const shown = this.shownDashboard();
        if (shown) {
            shown.show();
            return;
        }
        try {
            const result = await policy(found.root, this.abort.signal);
            if (this.disposed) { return; }
            if (result.already_open) {
                this.explain(ALREADY_OPEN_REASON);
                return;
            }
            this.attempted = true;
            this.createDashboardTerminal(found.root, ['dashboard']).show();
        } catch (error) {
            if (!this.disposed) {
                this.output.appendLine(`Dashboard open failed: ${error.message}`);
                this.output.show(true);
            }
        }
    }

    // A command has no log to read: tell the person why nothing opened.
    explain(reason) {
        this.output.appendLine(reason);
        this.output.show(true);
        void Promise.resolve(vscode.window.showInformationMessage(reason)).then(() => {}, () => {});
    }

    // Whether a terminal the user has used is open. In a Codespace, log every
    // terminal considered so a live check can see which one decided.
    terminalInUse() {
        const terminals = vscode.window.terminals;
        const counted = terminals.map(t => !isLifecycleTerminal(t) && t.state.isInteractedWith);
        if (vscode.env.remoteName === 'codespaces' && terminals.length > 0) {
            const seen = terminals.map((t, i) => `'${t.name}' (created as ` +
                `'${t.creationOptions.name ?? ''}', interacted: ${t.state.isInteractedWith}, ` +
                `${counted[i] ? 'in use' : 'not in use'})`);
            this.output.appendLine(`Dashboard terminal check: ${seen.join('; ')}.`);
        }
        return counted.includes(true);
    }

    // A panel terminal created without show() becomes the visible tab only
    // when it is the panel's first terminal, so a terminal the user has used
    // and any prompt waiting in it stay in front. Offer the Dashboard instead.
    offer(terminal) {
        void Promise.resolve(vscode.window.showInformationMessage(
            'Booley Dashboard is ready in the terminal panel.', 'Open')).then(choice => {
            // Skip the reveal once the Dashboard has exited or been closed.
            if (choice === 'Open' && this.terminal === terminal) { terminal.show(); }
        }, () => {});
    }

    closed(closed) {
        if (closed !== this.terminal) { return; }
        if ([vscode.TerminalExitReason.User, vscode.TerminalExitReason.Shutdown,
            vscode.TerminalExitReason.Extension].includes(closed.exitStatus?.reason)) {
            this.terminal = undefined;
            return;
        }
        if ([0, 3, 4].includes(closed.exitStatus?.code)) {
            if (closed.exitStatus.code === 3) {
                this.output.appendLine('The Dashboard is already open in this Sandbox.');
            }
            if (closed.exitStatus.code === 4) {
                this.output.appendLine('Dashboard attach was disabled before startup; check Project policy.');
            }
            closed.dispose();
        } else if (closed.exitStatus?.code !== undefined ||
            closed.exitStatus?.reason === vscode.TerminalExitReason.Process) {
            this.output.appendLine(`Dashboard process failed (exit ${closed.exitStatus.code ?? 'unknown'}); run booley dashboard for details.`);
            this.output.show(true);
        }
        this.terminal = undefined;
    }

    dispose() {
        this.disposed = true;
        this.abort.abort();
        clearTimeout(this.recheck);
        // VS Code owns terminal lifetime across extension-host restarts.
    }
}

// The `bwave gui` Viewer Bridge runs in every window, independent of the
// Dashboard attach policy; a failure is logged and never blocks the Dashboard.
function startBridge(context) {
    let bridge;
    try {
        bridge = Promise.resolve(require('./vaporview_bridge').start(context));
    } catch (error) {
        bridge = Promise.reject(error);
    }
    bridge.catch(error => {
        const output = vscode.window.createOutputChannel('Booley VaporView Bridge');
        context.subscriptions.push(output);
        output.appendLine(`Viewer Bridge failed to start: ${error.message}`);
    });
}

// Recent VS Code opens its Chat panel in the secondary side bar the first time a
// workspace opens. Booley's agent runs as a terminal CLI, so in a Sandbox the
// panel only takes space. A devcontainer setting cannot hide it: the browser
// lays out the window before the remote settings arrive. Close it once per
// workspace instead, so a user who reopens it keeps it open.
const CHAT_CLOSED_KEY = 'chatSideBarClosed';

async function closeChatSideBarOnce(context) {
    if (!REMOTES.has(vscode.env.remoteName)) { return; }
    if (context.workspaceState.get(CHAT_CLOSED_KEY, false)) { return; }
    await context.workspaceState.update(CHAT_CLOSED_KEY, true);
    try {
        await vscode.commands.executeCommand('workbench.action.closeAuxiliaryBar');
    } catch {
        // Layout is cosmetic: an editor without the command keeps its default.
    }
}

// GitHub Codespaces opens `customizations.codespaces.openFiles` before the
// remote settings arrive, so a `workbench.editorAssociations` entry (such as a
// getting-started Markdown file that should open rendered) is skipped for
// those first tabs; reopening the file later honors it. Once the settings are
// here, reopen each such text tab with its associated editor. This runs once
// per workspace and only for tabs opened in the first minute, so a file the
// user later opens as plain text on purpose stays that way.
const ASSOCIATIONS_APPLIED_KEY = 'startupEditorAssociationsApplied';
const ASSOCIATION_WINDOW_MS = 60000;

// The association view type for *uri*, or undefined. A pattern without a slash
// matches the file name, as VS Code's own editor resolver does.
async function associatedViewType(uri, entries) {
    let document;
    try {
        document = await vscode.workspace.openTextDocument(uri);
    } catch {
        return undefined;
    }
    for (const [pattern, viewType] of entries) {
        const glob = pattern.includes('/') ? pattern : `**/${pattern}`;
        if (vscode.languages.match({ pattern: glob }, document) > 0) { return viewType; }
    }
    return undefined;
}

async function applyStartupEditorAssociations(context) {
    if (vscode.env.remoteName !== 'codespaces') { return; }
    if (context.workspaceState.get(ASSOCIATIONS_APPLIED_KEY, false)) { return; }
    await context.workspaceState.update(ASSOCIATIONS_APPLIED_KEY, true);
    const deadline = Date.now() + ASSOCIATION_WINDOW_MS;
    const handled = new Set();
    const sweep = async () => {
        const associations = vscode.workspace.getConfiguration('workbench').get('editorAssociations') || {};
        const entries = Object.entries(associations)
            .filter(([, viewType]) => typeof viewType === 'string' && viewType !== 'default');
        if (entries.length === 0) { return; }
        for (const group of vscode.window.tabGroups.all) {
            for (const tab of group.tabs) {
                if (!(tab.input instanceof vscode.TabInputText)) { continue; }
                const uri = tab.input.uri;
                if (handled.has(uri.toString())) { continue; }
                handled.add(uri.toString());
                const viewType = await associatedViewType(uri, entries);
                if (viewType === undefined) { continue; }
                try {
                    await vscode.commands.executeCommand('vscode.openWith', uri, viewType,
                        { viewColumn: group.viewColumn, preserveFocus: !tab.isActive });
                    await vscode.window.tabGroups.close(tab);
                } catch {
                    // Cosmetic: the text editor stays open if the view cannot.
                }
            }
        }
    };
    await sweep();
    // openFiles may land after activation; watch briefly for those tabs.
    const watcher = vscode.window.tabGroups.onDidChangeTabs(() => {
        if (Date.now() > deadline) { watcher.dispose(); return; }
        void sweep();
    });
    context.subscriptions.push(watcher);
}

exports.activate = context => {
    startBridge(context);
    void closeChatSideBarOnce(context);
    void applyStartupEditorAssociations(context);
    void new AttachController(context).open();
};
