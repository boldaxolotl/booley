'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname,
    '../../src/booley/data/vscode/dashboard/extension.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

function fakeEditor(options, records, settings) {
    const { trusted = true } = options;
    // An explicit `remote: undefined` models a local window, as VS Code reports it.
    const remote = 'remote' in options ? options.remote : 'dev-container';
    const { log, terminals, listeners } = records;
    const root = { fsPath: '/work' };
    const output = { appendLine: line => log.push(line), show() {}, dispose() {} };
    const listen = name => fn => { listeners[name] = fn; return { dispose() {} }; };
    return {
        TerminalExitReason: {Shutdown: 1, Process: 2, User: 3, Extension: 4},
        env: { remoteName: remote, sessionId: 'fixture-window' },
        workspace: {
            isTrusted: trusted,
            workspaceFolders: [{ uri: root }],
            getConfiguration: name => ({
                get: () => settings.protocol,
                inspect: () => ({ defaultValue: 'off', globalValue: settings.permission }),
            }),
            onDidGrantWorkspaceTrust: listen('trust'),
            onDidChangeConfiguration: listen('config'),
        },
        window: {
            createOutputChannel: () => output,
            createTerminal: options => {
                const terminal = { options, shown: false, disposed: false,
                    show() { this.shown = true; }, dispose() { this.disposed = true; } };
                terminals.push(terminal);
                return terminal;
            },
            onDidCloseTerminal: listen('close'),
        },
    };
}

function fixture(options = {}) {
    const {enabled = true, error, raw, delayed = false} = options;
    const records = {log: [], terminals: [], calls: [], listeners: {}};
    const {log, terminals, calls, listeners} = records;
    const settings = {protocol: options.protocol ?? 1, permission: options.permission};
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
            return require(name);
        } });
    const state = new Map();
    const context = { subscriptions: [], workspaceState: {
        get: (key, fallback) => state.has(key) ? state.get(key) : fallback,
        update: async (key, value) => { state.set(key, value); },
    } };
    module.exports.activate(context);
    return { calls, context, log, terminals, listeners, vscode, settings, complete: () => complete(),
        reactivate: () => module.exports.activate({subscriptions: [], workspaceState: context.workspaceState}) };
}

test('default-off application permission still opens one fixed remote process', async () => {
    const f = fixture();
    await tick();
    assert.equal(f.terminals.length, 1);
    assert.equal(f.terminals[0].shown, true);
    assert.equal(f.terminals[0].options.shellPath, '/usr/local/bin/booley');
    assert.deepEqual(Array.from(f.terminals[0].options.shellArgs), ['dashboard', '--on-attach']);
    assert.equal(f.terminals[0].options.cwd.fsPath, '/work');
    assert.equal(f.terminals[0].options.isTransient, true);
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
