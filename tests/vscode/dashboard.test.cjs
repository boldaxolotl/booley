'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname,
    '../../src/booley/data/vscode/dashboard/extension.js'), 'utf8');
const LIFECYCLE_NAME = 'Codespaces: python -m booley.runtime.incontainer_live_preview && ' +
    'python -m booley.runtime.incontainer_vaporview';
const tick = () => new Promise(resolve => setImmediate(resolve));

function fakeEditor(options, records, settings) {
    const { trusted = true } = options;
    // An explicit `remote: undefined` models a local window, as VS Code reports it.
    const remote = 'remote' in options ? options.remote : 'dev-container';
    const { log, terminals, listeners, commands, messages } = records;
    // Terminals already open; the Dashboard joins this list. An idle terminal
    // (like the Codespace default shell) comes first; a user terminal is one
    // that has received input.
    const open = [];
    if (options.idleTerminal) {
        open.push({ name: 'bash', creationOptions: {}, state: { isInteractedWith: false } });
    }
    if (options.userTerminal) {
        open.push({ name: 'bash', creationOptions: {}, state: { isInteractedWith: true } });
    }
    // Codespaces runs devcontainer lifecycle commands (postAttachCommand) in a
    // terminal named `Codespaces: <command>`, which the editor may flag
    // interacted when the command is sent as text.
    if (options.lifecycleTerminal) {
        open.push({ name: LIFECYCLE_NAME, creationOptions: { name: LIFECYCLE_NAME },
            state: { isInteractedWith: true } });
    }
    // The prefix only on the current name (a process title or a rename).
    // The prefix only on the creation name (the shell title replaced the name).
    if (options.createdLifecycleTerminal) {
        open.push({ name: 'bash', creationOptions: { name: LIFECYCLE_NAME },
            state: { isInteractedWith: true } });
    }
    if (options.titledLifecycleTerminal) {
        open.push({ name: 'Codespaces: python -m booley.runtime.incontainer_vaporview',
            creationOptions: {}, state: { isInteractedWith: true } });
    }
    const root = { fsPath: '/work' };
    const output = { appendLine: line => log.push(line), show() {}, dispose() {} };
    const listen = name => fn => { listeners[name] = fn; return { dispose() {} }; };
    class TabInputText { constructor(uri) { this.uri = uri; } }
    // Editor tabs the window has open; `records.tabs` lets a test add one later.
    const group = { viewColumn: 1, tabs: records.tabs };
    for (const file of options.tabs || []) {
        group.tabs.push({ input: new TabInputText(fileUri(file)), isActive: true });
    }
    return {
        TerminalExitReason: {Shutdown: 1, Process: 2, User: 3, Extension: 4},
        TabInputText,
        env: { remoteName: remote, sessionId: 'fixture-window' },
        commands: {
            executeCommand: async (id, ...args) => {
                commands.push(args.length ? [id, args[0].fsPath, args[1]] : id);
                if (options.commandFails) { throw new Error('unknown command'); }
            },
            // The latest registration wins, as after an extension-host restart.
            registerCommand: (id, handler) => {
                records.registered[id] = handler;
                return { dispose() {} };
            },
        },
        languages: {
            // Enough glob for the fixtures: `**/` prefix and `*` within a name.
            match: (selector, document) => {
                const body = selector.pattern.replace(/^\*\*\//, '')
                    .replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '[^/]*');
                return new RegExp(`(^|/)${body}$`).test(document.uri.fsPath) ? 10 : 0;
            },
        },
        workspace: {
            isTrusted: trusted,
            workspaceFolders: [{ uri: root }],
            getConfiguration: name => ({
                get: () => name === 'workbench' ? settings.associations : settings.protocol,
                inspect: () => ({ defaultValue: 'off', globalValue: settings.permission }),
            }),
            openTextDocument: async uri => ({ uri }),
            onDidGrantWorkspaceTrust: listen('trust'),
            onDidChangeConfiguration: listen('config'),
        },
        window: {
            createOutputChannel: () => output,
            createTerminal: options => {
                const terminal = { options, creationOptions: options, shown: false, disposed: false,
                    state: { isInteractedWith: false },
                    show(preserveFocus) { this.shown = true; this.preserveFocus = preserveFocus; },
                    dispose() { this.disposed = true; } };
                terminals.push(terminal);
                open.push(terminal);
                return terminal;
            },
            get terminals() { return open.slice(); },
            showInformationMessage: (message, ...items) => {
                const record = { message, items, resolve: undefined };
                messages.push(record);
                return new Promise(resolve => { record.resolve = resolve; });
            },
            onDidCloseTerminal: listen('close'),
            tabGroups: {
                get all() { return [group]; },
                close: async tab => {
                    records.closedTabs.push(tab.input.uri.fsPath);
                    group.tabs.splice(group.tabs.indexOf(tab), 1);
                },
                onDidChangeTabs: listen('tabs'),
            },
        },
    };
}

function fileUri(fsPath) {
    return { fsPath, toString: () => `file://${fsPath}` };
}

function fixture(options = {}) {
    const {enabled = true, error, raw, delayed = false} = options;
    const records = {log: [], terminals: [], calls: [], listeners: {}, commands: [], messages: [],
        tabs: [], closedTabs: [], registered: {}};
    const {log, terminals, calls, listeners} = records;
    const settings = {protocol: options.protocol ?? 1, permission: options.permission,
        associations: options.associations};
    const vscode = fakeEditor(options, records, settings);
    let complete;
    function execFile(command, args, options, callback) {
        calls.push({ command, args, options });
        complete = () => callback(error, raw ?? JSON.stringify({
            protocol: 1, enabled, reason: enabled ? '' : 'disabled by Project configuration',
        }), error ? 'policy unavailable' : '');
        if (!delayed) { queueMicrotask(complete); }
    }
    const module = { exports: {} };
    vm.runInNewContext(source, { exports: module.exports, AbortController,
        require: name => {
            if (name === 'vscode') { return vscode; }
            if (name === 'node:child_process') { return {execFile}; }
            if (name === 'node:fs') { return {existsSync: () => options.git !== false}; }
            if (name === './vaporview_bridge') { return {start: async () => ({})}; }
            return require(name);
        } });
    const state = new Map();
    const context = { subscriptions: [], workspaceState: {
        get: (key, fallback) => state.has(key) ? state.get(key) : fallback,
        update: async (key, value) => { state.set(key, value); },
    } };
    module.exports.activate(context);
    return { calls, commands: records.commands, messages: records.messages, context,
        registered: records.registered,
        // Run the command the way the Command Palette does.
        runCommand: () => records.registered['booley.dashboard.open'](),
        tabs: records.tabs, closedTabs: records.closedTabs, log, terminals, listeners, vscode, settings, complete: () => complete(),
        reactivate: () => module.exports.activate({subscriptions: [], workspaceState: context.workspaceState}) };
}

// The command comes first so that its failure is the head of the suite's output.
test('the open-Dashboard command is registered and contributed', async () => {
    const f = fixture();
    await tick();
    assert.equal(typeof f.registered['booley.dashboard.open'], 'function');
    const manifest = JSON.parse(fs.readFileSync(path.join(__dirname,
        '../../src/booley/data/vscode/dashboard/package.json'), 'utf8'));
    assert.deepEqual(manifest.contributes.commands, [{ command: 'booley.dashboard.open',
        title: 'Open Dashboard', category: 'Booley' }]);
});

test('default-off application permission still opens one fixed remote process', async () => {
    const f = fixture();
    await tick();
    assert.equal(f.terminals.length, 1);
    assert.equal(f.terminals[0].shown, true);
    assert.equal(f.terminals[0].options.shellPath, '/usr/local/bin/booley');
    assert.deepEqual(Array.from(f.terminals[0].options.shellArgs), ['dashboard', '--on-attach']);
    assert.equal(f.terminals[0].options.cwd.fsPath, '/work');
    assert.equal(f.terminals[0].options.isTransient, true);
    assert.equal(f.messages.length, 0, 'an idle panel needs no notification');
    assert.equal(f.calls[0].options.cwd, '/work');
    assert.equal(f.calls[0].options.timeout, 35000);
    assert.deepEqual(Array.from(f.calls[0].args), ['dashboard', '--attach-policy']);
    f.listeners.config({ affectsConfiguration: () => true });
    f.listeners.trust();
    await tick();
    assert.equal(f.terminals.length, 1);
});

test('a GitHub Codespace opens one fixed remote process like a Dev Container', async () => {
    const f = fixture({ remote: 'codespaces' });
    await tick();
    assert.equal(f.calls.length, 1);
    assert.deepEqual(Array.from(f.calls[0].args), ['dashboard', '--attach-policy']);
    assert.equal(f.terminals.length, 1);
    assert.deepEqual(Array.from(f.terminals[0].options.shellArgs), ['dashboard', '--on-attach']);
    assert.equal(f.log.length, 0);
});

test('an ineligible remote, untrusted window and disabled protocol log one reason', async () => {
    const cases = [
        [{ remote: 'ssh-remote' }, /remote 'ssh-remote'/],
        [{ trusted: false }, /untrusted/],
        [{ protocol: 0 }, /attach protocol/],
    ];
    for (const [options, reason] of cases) {
        const f = fixture(options);
        await tick();
        assert.equal(f.calls.length, 0);
        assert.equal(f.terminals.length, 0);
        assert.equal(f.log.length, 1);
        assert.match(f.log[0], reason);
    }
});

test('a local window starts nothing and stays quiet', async () => {
    for (const remote of [null, undefined]) {
        const f = fixture({ remote });
        await tick();
        assert.equal(f.calls.length, 0);
        assert.equal(f.terminals.length, 0);
        assert.equal(f.log.length, 0);
    }
});

test('explicit off, host folder, unrelated remote and disabled protocol are inert', async () => {
    for (const options of [{ permission: 'off' }, { remote: null },
        { remote: 'ssh-remote' }, { protocol: 0 }, { git: false }]) {
        const f = fixture(options);
        await tick();
        assert.equal(f.calls.length, 0);
        assert.equal(f.terminals.length, 0);
    }
});

test('trust grant starts once; untrusted startup does no policy reads', async () => {
    const f = fixture({ trusted: false });
    await tick();
    assert.equal(f.calls.length, 0);
    f.vscode.workspace.isTrusted = true;
    f.listeners.trust();
    await tick();
    assert.equal(f.terminals.length, 1);
});

test('a late generated machine setting can start the opener once', async () => {
    const f = fixture({ protocol: 0 });
    await tick();
    assert.equal(f.calls.length, 0);
    f.settings.protocol = 1;
    f.listeners.config({ affectsConfiguration: () => true });
    await tick();
    assert.equal(f.terminals.length, 1);
});

test('Project opt-outs and invalid policy never create a terminal', async () => {
    for (const options of [{ enabled: false }, { error: new Error('failed') },
        { raw: 'garbage' }, { raw: '{"protocol":2,"enabled":true,"reason":""}' }]) {
        const f = fixture(options);
        await tick();
        assert.equal(f.terminals.length, 0);
        assert.equal(f.log.length, 1);
    }
});

test('late policy completion after deactivation cannot open a terminal', async () => {
    const f = fixture({ delayed: true });
    await tick();
    for (const disposable of f.context.subscriptions) { disposable.dispose(); }
    assert.equal(f.calls[0].options.signal.aborted, true);
    f.complete();
    await tick();
    assert.equal(f.terminals.length, 0);
});

test('an opt-out arriving while policy is pending suppresses terminal creation', async () => {
    const f = fixture({ delayed: true });
    await tick();
    f.vscode.workspace.isTrusted = false;
    f.complete();
    await tick();
    assert.equal(f.terminals.length, 0);
});

test('an extension-host restart respects the attempt, while a new window can open', async () => {
    const f = fixture();
    await tick();
    for (const disposable of f.context.subscriptions) { disposable.dispose(); }
    assert.equal(f.terminals[0].disposed, false, "extension-host shutdown must preserve its view");
    f.reactivate();
    await tick();
    assert.equal(f.calls.length, 1);
    f.vscode.env.sessionId = 'new-window';
    f.reactivate();
    await tick();
    assert.equal(f.calls.length, 2);
});

test('only owned terminals are cleaned up, and closing never causes a restart', async () => {
    const f = fixture();
    await tick();
    const owned = f.terminals[0];
    const other = { name: 'Booley Dashboard', exitStatus: { code: 0 },
        dispose() { assert.fail('must not close another terminal'); } };
    f.listeners.close(other);
    owned.exitStatus = { code: 3 };
    f.listeners.close(owned);
    assert.equal(owned.disposed, true);
    assert.match(f.log[0], /already open/);
    f.listeners.config({ affectsConfiguration: () => true });
    await tick();
    assert.equal(f.calls.length, 1);
});

test('startup failures and late policy opt-outs remain visible in Output', async () => {
    for (const code of [1, 2, 4]) {
        const f = fixture();
        await tick();
        const terminal = f.terminals[0];
        terminal.exitStatus = {code};
        f.listeners.close(terminal);
        assert.equal(f.log.length, 1);
        assert.match(f.log[0], code === 4 ? /disabled/ : /failed/);
    }
});


test('user closure, editor shutdown and extension disposal stay quiet', async () => {
    for (const name of ['User', 'Shutdown', 'Extension']) {
        const f = fixture();
        await tick();
        const terminal = f.terminals[0];
        terminal.exitStatus = {code: 130, reason: f.vscode.TerminalExitReason[name]};
        f.listeners.close(terminal);
        assert.equal(f.log.length, 0);
    }
});

test('a Sandbox window closes the Chat side bar once per workspace', async () => {
    for (const remote of ['dev-container', 'attached-container', 'codespaces']) {
        const f = fixture({ remote });
        await tick();
        assert.deepEqual(f.commands, ['workbench.action.closeAuxiliaryBar']);
        // A reload or a later window of the same workspace leaves the user's layout alone.
        f.reactivate();
        await tick();
        assert.deepEqual(f.commands, ['workbench.action.closeAuxiliaryBar']);
    }
});

test('a local or unrelated window keeps its Chat side bar', async () => {
    for (const remote of [undefined, 'ssh-remote']) {
        const f = fixture({ remote });
        await tick();
        assert.deepEqual(f.commands, []);
    }
});

test('a missing close command never blocks the Dashboard', async () => {
    const f = fixture({ commandFails: true });
    await tick();
    assert.deepEqual(f.commands, ['workbench.action.closeAuxiliaryBar']);
    assert.equal(f.terminals.length, 1);
    assert.equal(f.log.length, 0);
});

test('a terminal nobody has used does not hold the Dashboard back', async () => {
    // A Codespace starts its default shell with the Trust click; a restored
    // terminal in an already-trusted window looks the same.
    for (const trusted of [false, true]) {
        const f = fixture({ idleTerminal: true, trusted });
        await tick();
        if (!trusted) {
            f.vscode.workspace.isTrusted = true;
            f.listeners.trust();
            await tick();
        }
        assert.equal(f.terminals.length, 1);
        assert.equal(f.terminals[0].shown, true, 'the Dashboard comes to the front');
        assert.equal(f.terminals[0].preserveFocus, true, 'keyboard focus stays put');
        assert.equal(f.messages.length, 0, 'no notification when nothing is in use');
    }
});

test('a terminal the user has used keeps the panel; the Dashboard waits behind a notification', async () => {
    // The unused terminal comes first: any used terminal counts, not just the first.
    const f = fixture({ idleTerminal: true, userTerminal: true });
    await tick();
    assert.equal(f.terminals.length, 1, 'the Dashboard still starts');
    assert.equal(f.terminals[0].shown, false, 'the user terminal must stay in front');
    assert.equal(f.messages.length, 1);
    assert.deepEqual(Array.from(f.messages[0].items), ['Open']);
    f.messages[0].resolve('Open');
    await tick();
    assert.equal(f.terminals[0].shown, true);
});

test('a Codespaces lifecycle terminal does not hold the Dashboard back', async () => {
    const f = fixture({ remote: 'codespaces', lifecycleTerminal: true, idleTerminal: true });
    await tick();
    assert.equal(f.terminals.length, 1);
    assert.equal(f.terminals[0].shown, true, 'the Dashboard comes to the front');
    assert.equal(f.terminals[0].preserveFocus, true, 'keyboard focus stays put');
    assert.equal(f.messages.length, 0, 'no notification for a lifecycle terminal');
    const lines = f.log.filter(line => line.includes('created as'));
    assert.equal(lines.length, 1);
    assert.match(lines[0], /created as 'Codespaces: python -m booley\.runtime\.incontainer_live_preview/);
    assert.match(lines[0], /interacted: true, not in use/);
});

test('a used terminal beside a Codespaces lifecycle terminal keeps the panel', async () => {
    const f = fixture({ remote: 'codespaces', lifecycleTerminal: true, userTerminal: true });
    await tick();
    assert.equal(f.terminals[0].shown, false, 'the user terminal must stay in front');
    assert.equal(f.messages.length, 1);
    assert.deepEqual(Array.from(f.messages[0].items), ['Open']);
    f.messages[0].resolve('Open');
    await tick();
    assert.equal(f.terminals[0].shown, true);
    const line = f.log.find(entry => entry.includes('created as'));
    assert.match(line, /'bash' \(created as '', interacted: true, in use\)/);
    assert.match(line, /interacted: true, not in use/);
});

test('a terminal titled like a Codespaces lifecycle terminal does not hold the Dashboard back', async () => {
    const f = fixture({ remote: 'codespaces', titledLifecycleTerminal: true });
    await tick();
    assert.equal(f.terminals[0].shown, true);
    assert.equal(f.terminals[0].preserveFocus, true);
    assert.equal(f.messages.length, 0);
});

test('a terminal created like a Codespaces lifecycle terminal does not hold the Dashboard back', async () => {
    const f = fixture({ remote: 'codespaces', createdLifecycleTerminal: true });
    await tick();
    assert.equal(f.terminals[0].shown, true);
    assert.equal(f.terminals[0].preserveFocus, true);
    assert.equal(f.messages.length, 0);
});

test('outside a Codespace a lifecycle-named terminal still counts', async () => {
    for (const remote of ['dev-container', 'attached-container']) {
        const f = fixture({ remote, lifecycleTerminal: true });
        await tick();
        assert.equal(f.terminals[0].shown, false, remote);
        assert.equal(f.messages.length, 1, remote);
        assert.equal(f.log.length, 0, remote);
    }
});

test('dismissing the notification leaves the Dashboard in the background', async () => {
    const f = fixture({ userTerminal: true });
    await tick();
    f.messages[0].resolve(undefined);
    await tick();
    assert.equal(f.terminals[0].shown, false);
});

test('Open after the Dashboard exited reveals nothing, and exit handling still runs', async () => {
    const f = fixture({ userTerminal: true });
    await tick();
    const terminal = f.terminals[0];
    terminal.exitStatus = { code: 3 };
    f.listeners.close(terminal);
    assert.equal(terminal.disposed, true);
    assert.match(f.log[0], /already open/);
    f.messages[0].resolve('Open');
    await tick();
    assert.equal(terminal.shown, false);
});

const START_HERE = '/work/.booley/START-HERE.md';
const PREVIEW = { '**/START-HERE.md': 'vscode.markdown.preview.editor' };
const OPEN_PREVIEW = ['vscode.openWith', START_HERE, 'vscode.markdown.preview.editor'];
const withoutChatClose = commands => commands.filter(id => id !== 'workbench.action.closeAuxiliaryBar');

test('a Codespace reopens a startup text tab with its editor association', async () => {
    const f = fixture({ remote: 'codespaces', tabs: [START_HERE, '/work/README.md'],
        associations: { ...PREVIEW, '*.v': 'default' } });
    await tick(); await tick();
    assert.deepEqual(withoutChatClose(f.commands), [OPEN_PREVIEW]);
    assert.deepEqual(f.closedTabs, [START_HERE]);
});

test('a file name pattern without a slash matches like VS Code does', async () => {
    const f = fixture({ remote: 'codespaces', tabs: [START_HERE],
        associations: { 'START-HERE.md': 'vscode.markdown.preview.editor' } });
    await tick(); await tick();
    assert.deepEqual(withoutChatClose(f.commands), [OPEN_PREVIEW]);
});

test('a startup tab that opens just after activation is reopened too', async () => {
    const f = fixture({ remote: 'codespaces', associations: PREVIEW });
    await tick();
    f.tabs.push({ input: new f.vscode.TabInputText(fileUri(START_HERE)), isActive: true });
    f.listeners.tabs();
    await tick(); await tick();
    assert.deepEqual(withoutChatClose(f.commands), [OPEN_PREVIEW]);
});

test('associations are applied once per workspace and only in a Codespace', async () => {
    const f = fixture({ remote: 'codespaces', tabs: [START_HERE], associations: PREVIEW });
    await tick(); await tick();
    f.tabs.push({ input: new f.vscode.TabInputText(fileUri(START_HERE)), isActive: true });
    f.reactivate();
    await tick(); await tick();
    assert.deepEqual(withoutChatClose(f.commands), [OPEN_PREVIEW]);
    for (const remote of ['dev-container', undefined]) {
        const other = fixture({ remote, tabs: [START_HERE], associations: PREVIEW });
        await tick(); await tick();
        assert.deepEqual(withoutChatClose(other.commands), []);
    }
});

test('a failed reopen keeps the text tab open', async () => {
    const f = fixture({ remote: 'codespaces', tabs: [START_HERE], associations: PREVIEW,
        commandFails: true });
    await tick(); await tick();
    assert.deepEqual(withoutChatClose(f.commands), [OPEN_PREVIEW]);
    assert.deepEqual(f.closedTabs, []);
    assert.equal(f.tabs.length, 1);
});

// --- Booley: Open Dashboard ----------------------------------------------------

const ALREADY_OPEN = JSON.stringify({ protocol: 1, enabled: false,
    reason: 'The Dashboard is already open in this Sandbox.', already_open: true });

test('the open-Dashboard command opens one fixed terminal in a trusted window', async () => {
    // The first auto-open was skipped (protocol off), so the command has nothing to reveal.
    const f = fixture({ protocol: 0 });
    await tick();
    assert.equal(f.terminals.length, 0);
    await f.runCommand();
    assert.equal(f.terminals.length, 1);
    assert.equal(f.terminals[0].options.shellPath, '/usr/local/bin/booley');
    assert.deepEqual(Array.from(f.terminals[0].options.shellArgs), ['dashboard']);
    assert.equal(f.terminals[0].options.cwd.fsPath, '/work');
    assert.equal(f.terminals[0].options.isTransient, true);
    assert.equal(f.terminals[0].shown, true);
    assert.equal(f.terminals[0].preserveFocus, undefined, 'the command takes focus');
    assert.deepEqual(Array.from(f.calls[0].args), ['dashboard', '--attach-policy']);
});

test('the open-Dashboard command reveals the terminal this window shows', async () => {
    const f = fixture();
    await tick();
    f.terminals[0].shown = false;
    await f.runCommand();
    assert.equal(f.terminals.length, 1);
    assert.equal(f.terminals[0].shown, true);
    assert.equal(f.calls.length, 1, 'no second policy read for a terminal we can show');
});

test('the open-Dashboard command adopts the terminal an earlier extension host created', async () => {
    const f = fixture();
    await tick();
    for (const disposable of f.context.subscriptions) { disposable.dispose(); }
    f.reactivate();
    await tick();
    f.terminals[0].shown = false;
    await f.runCommand();
    assert.equal(f.terminals.length, 1, 'adopted, not duplicated');
    assert.equal(f.terminals[0].shown, true);
    // An exited Dashboard is not shown any more: the command opens a fresh one.
    f.terminals[0].exitStatus = { code: 0 };
    await f.runCommand();
    assert.equal(f.terminals.length, 2);
});

test('the open-Dashboard command opens nothing in an untrusted window', async () => {
    const f = fixture({ trusted: false });
    await tick();
    await f.runCommand();
    assert.equal(f.calls.length, 0, 'no policy read');
    assert.equal(f.terminals.length, 0, 'no Dashboard opens');
    assert.equal(f.messages.length, 1);
    assert.match(f.messages[0].message, /trusted window/);
    assert.match(f.log.at(-1), /trusted window/);
});

test('the open-Dashboard command says why a window is not a Sandbox', async () => {
    for (const [options, reason] of [[{ remote: undefined }, /not a Booley Sandbox/],
        [{ remote: 'ssh-remote' }, /remote 'ssh-remote'/],
        [{ git: false }, /Git Project Root/]]) {
        const f = fixture(options);
        await tick();
        await f.runCommand();
        assert.equal(f.terminals.length, 0);
        assert.match(f.messages.at(-1).message, reason);
    }
});

test('the open-Dashboard command reports a Dashboard open elsewhere instead of a terminal', async () => {
    const f = fixture({ protocol: 0, raw: ALREADY_OPEN });
    await tick();
    await f.runCommand();
    assert.equal(f.terminals.length, 0);
    assert.equal(f.messages.length, 1);
    assert.equal(f.messages[0].message, 'The Dashboard is already open in this Sandbox.');
});

test('the open-Dashboard command waits for a pending auto-open instead of opening a second terminal', async () => {
    const f = fixture({ delayed: true });
    await tick();
    assert.equal(f.terminals.length, 0, 'the policy read is still pending');
    const command = f.runCommand();
    await tick();
    assert.equal(f.terminals.length, 0);
    f.complete();
    await command;
    assert.equal(f.terminals.length, 1, 'one Dashboard, revealed by the command');
    assert.equal(f.terminals[0].shown, true);
    assert.equal(f.calls.length, 1);
});

test('the open-Dashboard command ignores the auto-open opt-outs', async () => {
    const f = fixture({ protocol: 0, permission: 'off', enabled: false });
    await tick();
    assert.equal(f.terminals.length, 0, 'automatic open is off');
    await f.runCommand();
    assert.equal(f.terminals.length, 1);
    assert.deepEqual(Array.from(f.terminals[0].options.shellArgs), ['dashboard']);
});

test('the open-Dashboard command shows a failed policy read in Output', async () => {
    const f = fixture({ protocol: 0, error: new Error('policy unavailable') });
    await tick();
    await f.runCommand();
    assert.equal(f.terminals.length, 0);
    assert.match(f.log.at(-1), /Dashboard open failed: policy unavailable/);
});
