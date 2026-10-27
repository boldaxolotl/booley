'use strict';

const vscode = require('vscode');
const { execFile } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const BOOLEY = '/usr/local/bin/booley';
const PROTOCOL = 'booley.dashboard.attachProtocol';
// VS Code remote names whose workspace extension host runs inside the Sandbox;
// GitHub Codespaces reports `codespaces`.
const REMOTES = new Set(['dev-container', 'attached-container', 'codespaces']);

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
        context.subscriptions.push(this.output, this,
            vscode.workspace.onDidGrantWorkspaceTrust(() => { void this.open(); }),
            vscode.workspace.onDidChangeConfiguration(event => {
                if (event.affectsConfiguration(PROTOCOL)) { void this.open(); }
            }),
            vscode.window.onDidCloseTerminal(closed => this.closed(closed)));
    }

    eligibleRoot() {
        if (this.disposed) { return undefined; }
        const remote = vscode.env.remoteName;
        // A local window (no remote) is never a Sandbox; stay quiet there.
        if (remote === undefined || remote === null) { return undefined; }
        if (!REMOTES.has(remote)) {
            this.output.appendLine(`Dashboard auto-open skipped: remote '${remote}' is not a Sandbox remote.`);
            return undefined;
        }
        if (!vscode.workspace.isTrusted) {
            this.output.appendLine('Dashboard auto-open waits for workspace trust; this window is untrusted.');
            return undefined;
        }
        if (vscode.workspace.getConfiguration().get(PROTOCOL) !== 1) {
            this.output.appendLine('Dashboard auto-open skipped: the attach protocol is not enabled.');
            return undefined;
        }
        const permission = vscode.workspace.getConfiguration('task').inspect('allowAutomaticTasks');
        if (permission?.globalValue === 'off') {
            this.output.appendLine('Dashboard attach disabled by your automatic-task preference.');
            return undefined;
        }
        const folders = vscode.workspace.workspaceFolders || [];
        if (folders.length !== 1) {
            this.output.appendLine('Dashboard attach requires one opened Sandbox Project folder.');
            return undefined;
        }
        const root = folders[0].uri;
        if (!fs.existsSync(path.join(root.fsPath, '.git'))) {
            this.output.appendLine('Dashboard auto-open requires a Git Project Root.');
            return undefined;
        }
        return root;
    }

    async open() {
        if (this.attempted) { return; }
        const root = this.eligibleRoot();
        if (!root) { return; }
        this.attempted = true;
        try {
            const attempts = this.context.workspaceState.get('attachAttempts', []);
            await this.context.workspaceState.update('attachAttempts',
                [...attempts.slice(-15), vscode.env.sessionId]);
            if (this.disposed) { return; }
            const result = await policy(root, this.abort.signal);
            if (this.eligibleRoot()?.fsPath !== root.fsPath) { return; }
            if (!result.enabled) {
                this.output.appendLine(result.reason);
                return;
            }
            this.terminal = vscode.window.createTerminal({
                name: 'Booley Dashboard',
                shellPath: BOOLEY,
                shellArgs: ['dashboard', '--on-attach'],
                cwd: root,
                isTransient: true,
            });
            this.terminal.show(true);
        } catch (error) {
            if (!this.disposed) {
                this.output.appendLine(`Dashboard attach failed: ${error.message}`);
                this.output.show(true);
            }
        }
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

exports.activate = context => {
    startBridge(context);
    void new AttachController(context).open();
};
