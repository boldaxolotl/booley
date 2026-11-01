'use strict';

// Exercises the `bwave gui` VaporView bridge over its real Unix socket with a
// mocked `vscode` module and a fake per-document WCP listener on loopback TCP.

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname,
    '../../src/booley/data/vscode/dashboard/vaporview_bridge.js'), 'utf8');

class FakeUri {
    constructor(fsPath) { this.fsPath = fsPath; }
    toString() { return `file://${this.fsPath.split('/').map(encodeURIComponent).join('/')}`; }
    static file(fsPath) { return new FakeUri(fsPath); }
    static parse(text) {
        assert.ok(text.startsWith('file://'));
        return new FakeUri(text.slice(7).split('/').map(decodeURIComponent).join('/'));
    }
}

function fakeVscode(options) {
    const calls = [];
    const open = new Set(options.open || []);
    const listeners = {};
    const extension = options.vaporview === false ? undefined : {
        activated: 0,
        packageJSON: { version: options.version ?? '1.6.0' },
        async activate() { this.activated += 1; },
    };
    const commands = {
        'waveformViewer.getOpenDocuments': () => [...open],
        'vaporview.openFile': args => {
            if (options.openFails !== true) { open.add(args.uri.toString()); }
        },
        'waveformViewer.getViewerState': args => options.states?.[args.uri],
        'waveformViewer.loadViewerState': args => {
            calls.loaded = JSON.parse(fs.readFileSync(FakeUri.parse(args.sessionFileUri).fsPath, 'utf8'));
        },
        'vaporview.wcp.openWaveform': () => options.endpoint,
    };
    return {
        calls, open, listeners, extension,
        api: {
            Uri: FakeUri,
            extensions: { getExtension: id => (id === 'lramseyer.vaporview' ? extension : undefined) },
            window: {
                state: { focused: options.focused ?? false },
                onDidChangeWindowState: fn => { listeners.focus = fn; return { dispose() {} }; },
            },
            commands: {
                executeCommand: async (name, ...args) => {
                    calls.push({ name, args });
                    if (!commands[name]) { throw new Error(`unexpected command ${name}`); }
                    return commands[name](...args);
                },
            },
        },
    };
}

// A failed assertion skips a test's own cleanup; close everything at the end
// so a failure reports instead of hanging the runner on open handles.
const started = [];
test.after(() => {
    for (const item of started) { item.close(); }
});

async function startBridge(options = {}, makeHome = undefined) {
    // Short root: Unix socket paths are capped near 104 bytes, and test
    // runners often point TMPDIR at a long per-worker directory.
    const home = makeHome ? makeHome()
        : fs.mkdtempSync(path.join(process.platform === 'win32' ? os.tmpdir() : '/tmp', 'vvb-'));
    const editor = fakeVscode(options);
    const environment = { values: {}, cleared: false,
        replace(name, value) { this.values[name] = value; },
        clear() { this.values = {}; this.cleared = true; } };
    const context = { subscriptions: [], environmentVariableCollection: environment };
    const module = { exports: {} };
    vm.runInNewContext(source, {
        exports: module.exports, process, Buffer, setTimeout, clearTimeout,
        require: name => {
            if (name === 'vscode') { return editor.api; }
            if (name === 'node:os') { return { homedir: () => home }; }
            return require(name);
        },
    });
    started.push({ close: () => fs.rmSync(home, { recursive: true, force: true }) });
    const bridge = await module.exports.start(context);
    started.unshift({ close: () => context.subscriptions.forEach(entry => entry.dispose()) });
    return { home, editor, environment, context, bridge, socket: bridge.socketPath };
}

function send(socketPath, payload) {
    return new Promise((resolve, reject) => {
        const client = net.connect(socketPath);
        let text = '';
        client.on('connect', () => client.write(typeof payload === 'string' ? payload : `${JSON.stringify(payload)}\n`));
        client.on('data', data => { text += data; });
        client.on('error', reject);
        client.on('close', () => {
            try { resolve(JSON.parse(text)); } catch (error) { reject(new Error(`bad reply ${text}`)); }
        });
    });
}

const request = (f, op, extra = {}) => send(f.socket, { protocol: 1, op, ...extra });
const stop = f => f.context.subscriptions.forEach(item => item.dispose());
const TRACE = '/work/sim/trace.fst';
const TRACE_URI = FakeUri.file(TRACE).toString();

function fakeWcp(reply) {
    const frames = [];
    const server = net.createServer(socket => {
        let buffer = Buffer.alloc(0);
        socket.on('data', data => {
            buffer = Buffer.concat([buffer, data]);
            let end;
            while ((end = buffer.indexOf(0)) >= 0) {
                const message = JSON.parse(buffer.subarray(0, end).toString());
                buffer = buffer.subarray(end + 1);
                frames.push(message);
                const answer = message.type === 'greeting'
                    ? { type: 'greeting', version: '0', commands: ['set_viewport_range'] }
                    : reply;
                socket.write(`${JSON.stringify(answer)}\0`);
            }
        });
    });
    started.push({ close: () => server.close() });
    return new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve({
        frames, server, endpoint: { host: '127.0.0.1', port: server.address().port } })));
}

test('listens on a private socket and advertises it to terminals', async () => {
    const f = await startBridge();
    const directory = path.join(f.home, '.booley', 'vaporview-bridge');
    assert.equal(f.socket, path.join(directory, `window-${process.pid}.sock`));
    assert.equal(fs.statSync(directory).mode & 0o777, 0o700);
    assert.equal(fs.statSync(f.socket).mode & 0o777, 0o600);
    assert.equal(f.environment.persistent, false);
    assert.equal(f.environment.values.BOOLEY_VAPORVIEW_BRIDGE, f.socket);
    stop(f);
    assert.equal(fs.existsSync(f.socket), false);
    assert.equal(f.environment.cleared, true);
});

test('ping reports VaporView, focus and open documents', async () => {
    const f = await startBridge({ open: [TRACE_URI], focused: true });
    const reply = await request(f, 'ping');
    assert.equal(reply.ok, true);
    assert.equal(reply.result.protocol, 1);
    assert.equal(reply.result.vaporview, '1.6.0');
    assert.equal(reply.result.focused, true);
    assert.ok(reply.result.lastFocused > 0);
    assert.deepEqual(reply.result.documents, [TRACE]);
    assert.equal(f.editor.extension.activated, 1);
    stop(f);
});

test('ping without VaporView answers instead of failing', async () => {
    const f = await startBridge({ vaporview: false });
    const reply = await request(f, 'ping');
    assert.equal(reply.ok, true);
    assert.equal(reply.result.vaporview, null);
    assert.equal(reply.result.lastFocused, 0);
    assert.match(reply.result.error, /not installed in this VS Code window/);
    f.editor.listeners.focus({ focused: true });
    assert.ok((await request(f, 'ping')).result.lastFocused > 0);
    stop(f);
});

test('open distinguishes a fresh tab from an already open one', async () => {
    const f = await startBridge();
    assert.deepEqual(await request(f, 'open', { path: TRACE }), { ok: true, result: { opened: true } });
    const openCall = f.editor.calls.find(call => call.name === 'vaporview.openFile');
    assert.equal(openCall.args[0].uri.fsPath, TRACE);
    assert.deepEqual(await request(f, 'open', { path: TRACE }), { ok: true, result: { opened: false } });
    stop(f);
});

test('open fails when VaporView never lists the document', async () => {
    const f = await startBridge({ openFails: true });
    const reply = await request(f, 'open', { path: TRACE });
    assert.equal(reply.ok, false);
    assert.match(reply.error, /did not open/);
    assert.equal((await request(f, 'open', { path: 'relative.fst' })).ok, false);
    stop(f);
});

test('state reads the tab by exact URI string, or null when not open', async () => {
    const state = { displayedSignals: [{ dataType: 'netlist-variable', name: 'tb.clk', netlistId: 3 }] };
    const f = await startBridge({ open: [TRACE_URI], states: { [TRACE_URI]: state } });
    assert.deepEqual(await request(f, 'state', { path: TRACE }), { ok: true, result: state });
    const call = f.editor.calls.find(c => c.name === 'waveformViewer.getViewerState');
    assert.deepEqual({ ...call.args[0] }, { uri: TRACE_URI });
    assert.deepEqual(await request(f, 'state', { path: '/other.fst' }), { ok: true, result: null });
    stop(f);
});

test('apply writes a private session file and loads it into the tab', async () => {
    const f = await startBridge({ open: [TRACE_URI] });
    const state = { displayedSignals: [], markerTime: 10, fileName: '/elsewhere' };
    assert.deepEqual(await request(f, 'apply', { path: TRACE, state }), { ok: true, result: {} });
    const digest = crypto.createHash('sha256').update(TRACE_URI).digest('hex').slice(0, 16);
    const sessionFile = path.join(f.home, '.booley', 'vaporview-bridge', 'sessions', `${digest}.json`);
    const call = f.editor.calls.find(c => c.name === 'waveformViewer.loadViewerState');
    assert.deepEqual({ ...call.args[0] }, {
        documentUri: TRACE_URI, sessionFileUri: FakeUri.file(sessionFile).toString() });
    assert.deepEqual(f.editor.calls.loaded, { displayedSignals: [], markerTime: 10, fileName: TRACE });
    assert.equal(fs.statSync(sessionFile).mode & 0o777, 0o600);
    assert.deepEqual(fs.readdirSync(path.dirname(sessionFile)), [`${digest}.json`]);
    stop(f);
});

test('apply rejects a closed tab and malformed state before writing', async () => {
    const f = await startBridge({ open: [TRACE_URI] });
    for (const state of [undefined, [], { displayedSignals: {} }, 'x']) {
        const reply = await request(f, 'apply', { path: TRACE, state });
        assert.equal(reply.ok, false);
        assert.match(reply.error, /displayedSignals/);
    }
    const closed = await request(f, 'apply', { path: '/other.fst', state: { displayedSignals: [] } });
    assert.match(closed.error, /not open/);
    assert.equal(f.editor.calls.some(c => c.name === 'waveformViewer.loadViewerState'), false);
    stop(f);
});

test('viewport drives the per-document WCP listener for a range and a time', async () => {
    const wcp = await fakeWcp({ type: 'response', command: 'ack' });
    const f = await startBridge({ open: [TRACE_URI], endpoint: wcp.endpoint });
    assert.deepEqual(await request(f, 'viewport', { path: TRACE, start: 100, end: 200 }),
        { ok: true, result: {} });
    assert.deepEqual(await request(f, 'viewport', { path: TRACE, time: 150 }), { ok: true, result: {} });
    assert.deepEqual(wcp.frames, [
        { type: 'greeting', version: '0', commands: [] },
        { type: 'command', command: 'set_viewport_range', start: 100, end: 200 },
        { type: 'greeting', version: '0', commands: [] },
        { type: 'command', command: 'set_viewport_to', timestamp: 150 },
    ]);
    const call = f.editor.calls.find(c => c.name === 'vaporview.wcp.openWaveform');
    assert.equal(call.args[0].fsPath, TRACE);
    stop(f);
    wcp.server.close();
});

test('viewport surfaces WCP errors and rejects invalid ranges', async () => {
    const wcp = await fakeWcp({ type: 'error', message: 'Invalid time range: 5 to 9' });
    const f = await startBridge({ open: [TRACE_URI], endpoint: wcp.endpoint });
    const rejected = await request(f, 'viewport', { path: TRACE, start: 5, end: 9 });
    assert.deepEqual(rejected, { ok: false, error: 'Invalid time range: 5 to 9' });
    for (const bad of [{ start: 9, end: 5 }, { start: 1 }, { time: 'x' }, { start: 1, end: 2, time: 3 }]) {
        const reply = await request(f, 'viewport', { path: TRACE, ...bad });
        assert.equal(reply.ok, false);
        assert.match(reply.error, /finite/);
    }
    assert.equal((await request(f, 'viewport', { path: '/other.fst', start: 1, end: 2 })).ok, false);
    stop(f);
    wcp.server.close();
});

test('malformed, foreign and oversized requests are refused', async () => {
    const f = await startBridge();
    assert.match((await send(f.socket, 'nope\n')).error, /malformed request/);
    assert.match((await send(f.socket, '[1]\n')).error, /JSON object/);
    assert.match((await send(f.socket, { protocol: 2, op: 'ping' })).error, /unsupported bridge protocol/);
    assert.match((await request(f, 'toString')).error, /unknown bridge op/);
    const huge = 'x'.repeat(4 * 1024 * 1024 + 10);
    assert.match((await send(f.socket, huge)).error, /exceeds 4 MiB/);
    stop(f);
});

test('refuses a socket path the platform would truncate', async () => {
    const long = () => {
        const root = fs.mkdtempSync(path.join('/tmp', 'vvb-'));
        started.push({ close: () => fs.rmSync(root, { recursive: true, force: true }) });
        return path.join(root, 'x'.repeat(90));
    };
    await assert.rejects(startBridge({}, long), /too long for a Unix socket/);
});
