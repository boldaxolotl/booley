'use strict';

// Loopback bridge between `bwave gui` (Python, in the Sandbox) and VaporView's
// public VS Code commands, hosted in this window's extension host. It is a
// thin set of generic primitives (ping/open/state/apply/viewport); bwave
// builds the requested view and verifies the read-back state itself.
//
// Protocol v1: one Unix-socket connection per request. The client writes one
// JSON object followed by "\n"; the bridge answers one JSON line and closes:
//   {"ok": true, "result": ...}  or  {"ok": false, "error": "..."}

const vscode = require('vscode');
const net = require('node:net');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');

const PROTOCOL = 1;
const VAPORVIEW_ID = 'lramseyer.vaporview';
const ENV_NAME = 'BOOLEY_VAPORVIEW_BRIDGE';
const MAX_REQUEST_BYTES = 4 * 1024 * 1024;
const WCP_TIMEOUT_MS = 10000;
// A client that connects but never finishes its request line is dropped.
const REQUEST_IDLE_MS = 30000;
// sun_path holds 104 bytes on macOS and 108 on Linux, including the NUL; a
// longer path is silently truncated by some platforms, so refuse it instead.
const MAX_SOCKET_PATH_BYTES = 103;
const MISSING_VAPORVIEW = `VaporView (${VAPORVIEW_ID}) is not installed in this VS Code window`;

function bridgeDir() {
    return path.join(os.homedir(), '.booley', 'vaporview-bridge');
}

// Resolve and activate VaporView; its commands exist only after activation.
async function vaporview() {
    const extension = vscode.extensions.getExtension(VAPORVIEW_ID);
    if (!extension) { throw new Error(MISSING_VAPORVIEW); }
    await extension.activate();
    return extension;
}

async function openDocumentUris() {
    const uris = await vscode.commands.executeCommand('waveformViewer.getOpenDocuments');
    return Array.isArray(uris) ? uris.filter(uri => typeof uri === 'string') : [];
}

function requirePath(request) {
    if (typeof request.path !== 'string' || !path.isAbsolute(request.path)) {
        throw new Error('path must be an absolute file path');
    }
    return vscode.Uri.file(request.path);
}

// Whether VaporView has a tab for *uri*; activates VaporView first.
async function isOpen(uri) {
    await vaporview();
    return (await openDocumentUris()).includes(uri.toString());
}

async function requireOpen(uri) {
    if (!(await isOpen(uri))) {
        throw new Error(`waveform is not open in VaporView: ${uri.fsPath}`);
    }
}

function isFiniteNumber(value) {
    return typeof value === 'number' && Number.isFinite(value);
}

// Write the session file atomically so loadViewerState never reads a partial
// file; it also becomes the tab's save target, so it lives in a Booley-owned
// directory rather than next to the trace.
function writeSessionFile(uri, state) {
    const directory = path.join(bridgeDir(), 'sessions');
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    const digest = crypto.createHash('sha256').update(uri.toString()).digest('hex');
    const target = path.join(directory, `${digest.slice(0, 16)}.json`);
    // Unique per write: two bwave processes may update the same tab at once.
    const temporary = `${target}.${process.pid}.${crypto.randomBytes(6).toString('hex')}.tmp`;
    try {
        fs.writeFileSync(temporary, JSON.stringify({ ...state, fileName: uri.fsPath }),
            { mode: 0o600 });
        fs.chmodSync(temporary, 0o600);
        fs.renameSync(temporary, target);
    } finally {
        fs.rmSync(temporary, { force: true });
    }
    return target;
}

// Minimal WCP v0 client for VaporView's per-document listener: NUL-framed
// JSON, client greeting first, then one command and its single response.
function wcpCommand(endpoint, command) {
    return new Promise((resolve, reject) => {
        const socket = net.connect({ host: endpoint.host, port: endpoint.port });
        let buffer = Buffer.alloc(0);
        let greeted = false;
        let settled = false;
        const finish = (error, value) => {
            if (settled) { return; }
            settled = true;
            clearTimeout(timer);
            socket.destroy();
            if (error) { reject(error); } else { resolve(value); }
        };
        const timer = setTimeout(() => finish(new Error('VaporView WCP request timed out')),
            WCP_TIMEOUT_MS);
        const send = message => socket.write(`${JSON.stringify(message)}\0`);
        socket.on('connect', () => send({ type: 'greeting', version: '0', commands: [] }));
        socket.on('error', error => finish(error));
        socket.on('close', () => finish(new Error('VaporView WCP connection closed')));
        socket.on('data', data => {
            buffer = Buffer.concat([buffer, data]);
            let end = buffer.indexOf(0);
            while (end >= 0 && !settled) {
                let message;
                try {
                    message = JSON.parse(buffer.subarray(0, end).toString('utf8'));
                } catch (error) {
                    finish(new Error(`malformed VaporView WCP frame: ${error.message}`));
                    return;
                }
                buffer = buffer.subarray(end + 1);
                if (message && message.type === 'error') {
                    finish(new Error(String(message.message)));
                } else if (!greeted) {
                    if (!message || message.type !== 'greeting') {
                        finish(new Error('VaporView WCP greeting missing'));
                        return;
                    }
                    greeted = true;
                    send(command);
                } else if (message && message.type === 'response' && message.command === 'ack') {
                    finish(null, undefined);
                } else {
                    finish(new Error(`unexpected VaporView WCP reply: ${JSON.stringify(message)}`));
                }
                end = buffer.indexOf(0);
            }
        });
    });
}

class Bridge {
    constructor(context) {
        this.context = context;
        this.lastFocused = vscode.window.state && vscode.window.state.focused ? Date.now() : 0;
        this.socketPath = path.join(bridgeDir(), `window-${process.pid}.sock`);
        this.handlers = {
            ping: () => this.ping(),
            open: request => this.open(request),
            state: request => this.state(request),
            apply: request => this.apply(request),
            viewport: request => this.viewport(request),
        };
    }

    async listen() {
        if (Buffer.byteLength(this.socketPath) > MAX_SOCKET_PATH_BYTES) {
            throw new Error(`bridge socket path is too long for a Unix socket: ${this.socketPath}`);
        }
        const directory = bridgeDir();
        fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
        fs.chmodSync(directory, 0o700);
        // Only our own pid-named path: a leftover from a recycled pid is ours.
        fs.rmSync(this.socketPath, { force: true });
        this.server = net.createServer(socket => this.accept(socket));
        await new Promise((resolve, reject) => {
            this.server.once('error', reject);
            this.server.listen(this.socketPath, () => {
                this.server.off('error', reject);
                resolve();
            });
        });
        // Accept-time errors affect one connection; keep serving the rest.
        this.server.on('error', () => {});
        fs.chmodSync(this.socketPath, 0o600);
        this.identity = fs.statSync(this.socketPath).ino;
        const environment = this.context.environmentVariableCollection;
        environment.persistent = false;
        environment.replace(ENV_NAME, this.socketPath);
        this.focusListener = vscode.window.onDidChangeWindowState(state => {
            if (state.focused) { this.lastFocused = Date.now(); }
        });
    }

    accept(socket) {
        let buffer = Buffer.alloc(0);
        let handled = false;
        const reply = payload => {
            handled = true;
            socket.end(`${JSON.stringify(payload)}\n`);
        };
        socket.on('error', () => socket.destroy());
        socket.setTimeout(REQUEST_IDLE_MS, () => socket.destroy());
        socket.on('data', data => {
            if (handled) { return; }
            buffer = Buffer.concat([buffer, data]);
            const end = buffer.indexOf(10);
            if (end < 0) {
                if (buffer.length > MAX_REQUEST_BYTES) {
                    reply({ ok: false, error: 'request exceeds 4 MiB' });
                }
                return;
            }
            handled = true;
            // Opening a large trace may outlast the idle limit; the request is in.
            socket.setTimeout(0);
            this.dispatch(buffer.subarray(0, end).toString('utf8'))
                .then(reply, error => reply({ ok: false, error: error.message }));
        });
    }

    async dispatch(line) {
        let request;
        try {
            request = JSON.parse(line);
        } catch (error) {
            return { ok: false, error: `malformed request: ${error.message}` };
        }
        if (!request || typeof request !== 'object' || Array.isArray(request)) {
            return { ok: false, error: 'request must be a JSON object' };
        }
        if (request.protocol !== PROTOCOL) {
            return { ok: false, error: `unsupported bridge protocol ${request.protocol}; expected ${PROTOCOL}` };
        }
        const handler = Object.prototype.hasOwnProperty.call(this.handlers, request.op)
            ? this.handlers[request.op] : undefined;
        if (!handler) { return { ok: false, error: `unknown bridge op ${request.op}` }; }
        try {
            return { ok: true, result: await handler(request) };
        } catch (error) {
            return { ok: false, error: error instanceof Error ? error.message : String(error) };
        }
    }

    // Never throws: Booley diagnoses a missing or broken VaporView from here.
    async ping() {
        const result = {
            protocol: PROTOCOL,
            vaporview: null,
            focused: Boolean(vscode.window.state && vscode.window.state.focused),
            lastFocused: this.lastFocused,
            documents: [],
        };
        try {
            const extension = await vaporview();
            result.documents = (await openDocumentUris()).map(uri => vscode.Uri.parse(uri).fsPath);
            const version = extension.packageJSON && extension.packageJSON.version;
            result.vaporview = typeof version === 'string' ? version : null;
            if (result.vaporview === null) { result.error = 'VaporView reports no version'; }
        } catch (error) {
            result.vaporview = null;
            result.error = error instanceof Error ? error.message : String(error);
        }
        return result;
    }

    async open(request) {
        const uri = requirePath(request);
        const wasOpen = await isOpen(uri);
        await vscode.commands.executeCommand('vaporview.openFile', { uri });
        if (!(await isOpen(uri))) {
            throw new Error(`VaporView did not open ${uri.fsPath}`);
        }
        return { opened: !wasOpen };
    }

    async state(request) {
        const uri = requirePath(request);
        if (!(await isOpen(uri))) { return null; }
        const state = await vscode.commands.executeCommand(
            'waveformViewer.getViewerState', { uri: uri.toString() });
        return state === undefined ? null : state;
    }

    async apply(request) {
        const uri = requirePath(request);
        const state = request.state;
        if (!state || typeof state !== 'object' || Array.isArray(state) ||
            !Array.isArray(state.displayedSignals)) {
            throw new Error('state must be an object with a displayedSignals array');
        }
        await requireOpen(uri);
        const sessionFile = writeSessionFile(uri, state);
        await vscode.commands.executeCommand('waveformViewer.loadViewerState', {
            documentUri: uri.toString(),
            sessionFileUri: vscode.Uri.file(sessionFile).toString(),
        });
        return {};
    }

    async viewport(request) {
        const uri = requirePath(request);
        let command;
        if (isFiniteNumber(request.start) && isFiniteNumber(request.end) &&
            request.start < request.end && request.time === undefined) {
            command = { type: 'command', command: 'set_viewport_range',
                start: request.start, end: request.end };
        } else if (isFiniteNumber(request.time) && request.start === undefined &&
            request.end === undefined) {
            command = { type: 'command', command: 'set_viewport_to', timestamp: request.time };
        } else {
            throw new Error('viewport needs finite start < end, or a finite time');
        }
        await requireOpen(uri);
        const endpoint = await vscode.commands.executeCommand('vaporview.wcp.openWaveform', uri);
        if (!endpoint || typeof endpoint.host !== 'string' || !Number.isInteger(endpoint.port)) {
            throw new Error('VaporView returned no WCP endpoint');
        }
        await wcpCommand(endpoint, command);
        return {};
    }

    dispose() {
        if (this.disposed) { return; }
        this.disposed = true;
        if (this.focusListener) { this.focusListener.dispose(); }
        if (this.server) { this.server.close(); }
        try {
            // Unlink only if the path is still our socket, not a successor's.
            if (fs.statSync(this.socketPath).ino === this.identity) { fs.rmSync(this.socketPath); }
        } catch (_) { /* already gone */ }
        this.context.environmentVariableCollection.clear();
    }
}

exports.start = async context => {
    const bridge = new Bridge(context);
    context.subscriptions.push(bridge);
    await bridge.listen();
    return bridge;
};
