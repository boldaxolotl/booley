"""Exercise first-attach delivery in real desktop VS Code and a disposable Sandbox.

Run with PYTHONPATH=src python3 tests/vscode/attach_smoke.py --image <candidate>.
--baseline demonstrates the old folderOpen task failing without user permission.
Requires a local Docker image, desktop VS Code, its installed Dev Containers
extension, and xvfb-run. It creates an isolated editor profile and never opens
the user's Project. Evidence remains under the printed temporary directory.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

from booley.runtime import dashboard_extension, dashboard_tasks, devcontainer

OBSERVER = r"""
const vscode = require('vscode');
const fs = require('fs');
const {execFileSync} = require('child_process');
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
function running() {
    return fs.readdirSync('/proc').filter(name => /^\d+$/.test(name)).filter(name => {
        try {
            const args = fs.readFileSync(`/proc/${name}/cmdline`, 'utf8').split('\0');
            return args.includes('dashboard') && args.includes('--on-attach');
        } catch (_) { return false; }
    });
}
exports.activate = async () => {
    const stagePath = '/work/attach-stage.json';
    const stage = fs.existsSync(stagePath) ? JSON.parse(fs.readFileSync(stagePath, 'utf8')) : 0;
    const result = {visible: false, session: vscode.env.sessionId,
        namespace: fs.readlinkSync('/proc/self/ns/pid'),
        automaticPermission: vscode.workspace.getConfiguration('task').get('allowAutomaticTasks')};
    try {
        for (let i = 0; i < 100; i++) {
            const terminal = vscode.window.terminals.find(t => t.name === 'Booley Dashboard');
            if (terminal && await terminal.processId && running().length === 1) {
                await wait(1500);
                result.active = vscode.window.activeTerminal === terminal;
                await vscode.commands.executeCommand('workbench.action.terminal.selectAll');
                await vscode.commands.executeCommand('workbench.action.terminal.copySelection');
                const screen = await vscode.env.clipboard.readText();
                fs.writeFileSync('/work/dashboard-screen.txt', screen);
                result.visible = result.active && screen.includes('Booley Dashboard');
                const pid = await terminal.processId;
                result.terminalNamespace = fs.readlinkSync(`/proc/${pid}/ns/pid`);
                result.processes = running().length;
                try {
                    execFileSync('/usr/local/bin/booley', ['dashboard', '--on-attach'],
                        {cwd:'/work', timeout:10000});
                    result.duplicateExit = 0;
                } catch (error) { result.duplicateExit = error.status; }
                terminal.sendText('q', false);
                for (let j = 0; j < 40 && running().length; j++) { await wait(100); }
                await wait(500);
                result.afterClose = running().length;
                break;
            }
            await wait(250);
        }
        result.extensionActive = vscode.extensions.getExtension('booley.dashboard')?.isActive || false;
        if (result.visible && stage === 0) {
            fs.writeFileSync('/work/first-attach-result.json', JSON.stringify(result));
            fs.writeFileSync(stagePath, '2');
            void vscode.commands.executeCommand('workbench.action.reloadWindow').then(() => {}, () => {});
            return;
        }
        if (stage === 2) {
            const first = JSON.parse(fs.readFileSync('/work/first-attach-result.json', 'utf8'));
            result.reloadSessionChanged = result.session !== first.session;
        }
    } catch (error) { result.error = error.message; }
    fs.writeFileSync('/work/attach-result.json', JSON.stringify(result, null, 2));
    await vscode.commands.executeCommand('workbench.action.quit');
};
"""


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, check=True, timeout=30, capture_output=True, text=True, **kwargs)


def observer_vsix(path: Path, *, reload_window: bool = False) -> None:
    manifest = {
        "name": "dashboard-smoke-observer",
        "publisher": "booley",
        "version": "0.0.1",
        "displayName": "Dashboard smoke observer",
        "description": "Disposable test observer",
        "engines": {"vscode": "^1.95.0"},
        "main": "./extension.js",
        "activationEvents": ["onStartupFinished"],
        "extensionKind": ["workspace"],
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("extension/package.json", json.dumps(manifest))
        script = OBSERVER.replace(
            "if (result.visible && stage === 0)",
            "if (result.visible && stage === 0 && " + str(reload_window).lower() + ")",
        )
        archive.writestr("extension/extension.js", script)
        archive.writestr("extension.vsixmanifest", dashboard_extension._vsix_manifest(manifest))
        with zipfile.ZipFile(io.BytesIO(dashboard_extension.render_vsix())) as source:
            archive.writestr("[Content_Types].xml", source.read("[Content_Types].xml"))


def prepare(
    root: Path, image: str, baseline: bool, extension: Path, *, reload_window: bool = False
) -> Path:
    project = root / "project"
    project.mkdir()
    data = project / "booley-state"
    data.mkdir()
    (data / "booley.toml").write_text("[sandbox]\ndashboard = true\n")
    (project / "booley.toml").write_text('[project]\ndir = "booley-state"\n')
    run(["git", "init", "-q", str(project)])
    dashboard_tasks.reconcile(project, data)
    observer_vsix(project / "observer.vsix", reload_window=reload_window)
    customization = devcontainer.build_devcontainer_spec(dashboard=True)["customizations"]
    customization["vscode"]["extensions"] = [dashboard_extension.VSIX_PATH, "/work/observer.vsix"]
    if baseline:
        task = {**dashboard_tasks.TASK, "runOptions": {"runOn": "folderOpen", "instanceLimit": 1}}
        (project / ".vscode/tasks.json").write_text(
            json.dumps({"version": "2.0.0", "tasks": [task]})
        )
        customization["vscode"]["extensions"] = ["/work/observer.vsix"]
        customization["vscode"]["settings"] = {"task.allowAutomaticTasks": "on"}
    config = {
        "name": "Booley Dashboard smoke",
        "image": image,
        "workspaceMount": f"source={project},target=/work,type=bind",
        "workspaceFolder": "/work",
        "remoteUser": "agent",
        "updateRemoteUserUID": False,
        "remoteEnv": {"BOOLEY_DIR": "/work/booley-state", "BOOLEY_MCP_MODE": "interactive"},
        "customizations": customization,
        "shutdownAction": "stopContainer",
    }
    (project / ".devcontainer").mkdir()
    (project / ".devcontainer/devcontainer.json").write_text(json.dumps(config, indent=2))
    user = root / "user/User"
    user.mkdir(parents=True)
    (user / "settings.json").write_text(
        json.dumps(
            {
                "security.workspace.trust.enabled": False,
                "update.mode": "none",
                "telemetry.telemetryLevel": "off",
                "terminal.integrated.enablePersistentSessions": True,
            }
        )
    )
    extensions = root / "extensions"
    extensions.mkdir()
    shutil.copytree(extension, extensions / extension.name)
    return project


def check_result(result: dict, *, reload_window: bool = False) -> None:
    assert result.get("visible"), f"Dashboard was not visible: {result}"
    assert result["automaticPermission"] == "off", "Global automatic-task permission changed"
    assert result["namespace"] == result["terminalNamespace"], "Terminal ran outside the Sandbox"
    assert result["processes"] == 1 and result["duplicateExit"] == 3, "Singleton contract failed"
    assert result["afterClose"] == 0, "Dashboard restarted after deliberate close"
    if reload_window:
        assert result["reloadSessionChanged"], (
            "Window reload did not open a fresh Dashboard session"
        )


def editor_command(root: Path, project: Path) -> list[str]:
    authority = json.dumps({"hostPath": str(project), "localDocker": True}).encode().hex()
    return [
        "xvfb-run",
        "-a",
        "code",
        "--user-data-dir",
        str(root / "user"),
        "--extensions-dir",
        str(root / "extensions"),
        "--skip-welcome",
        "--skip-release-notes",
        "--disable-gpu",
        "--folder-uri",
        f"vscode-remote://dev-container+{authority}/work",
        "--wait",
    ]


def editor_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "HOME",
            "PATH",
            "USER",
            "LOGNAME",
            "LANG",
            "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS",
        }
    }


def await_result(root: Path, project: Path, process: subprocess.Popen) -> dict:
    deadline = time.monotonic() + 120
    result_path = project / "attach-result.json"
    while not result_path.exists() and time.monotonic() < deadline:
        if process.poll() is not None:
            break
        time.sleep(0.25)
    if not result_path.exists():
        raise RuntimeError(f"Editor smoke timed out; inspect {root}")
    result = json.loads(result_path.read_text())
    print(json.dumps(result, indent=2), flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument(
        "--reload", action="store_true", help="also exercise Dev Containers window reload"
    )
    parser.add_argument("--dev-containers", type=Path)
    args = parser.parse_args()
    extension = args.dev_containers or max(
        (Path.home() / ".vscode/extensions").glob("ms-vscode-remote.remote-containers-*"),
        key=lambda path: path.stat().st_mtime,
    )
    root = Path(tempfile.mkdtemp(prefix="booley-dashboard-smoke-"))
    print(f"Evidence: {root}", flush=True)
    project = prepare(root, args.image, args.baseline, extension, reload_window=args.reload)
    command = editor_command(root, project)

    environment = editor_environment()

    with (root / "editor.log").open("w") as log:
        process = subprocess.Popen(
            command, env=environment, stdout=log, stderr=log, start_new_session=True
        )
        try:
            result = await_result(root, project, process)
            check_result(result, reload_window=args.reload)
        finally:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
            ids = run(
                ["docker", "ps", "-aq", "--filter", f"label=devcontainer.local_folder={project}"]
            ).stdout.split()
            for container in ids:
                run(["docker", "rm", "-f", container])


if __name__ == "__main__":
    main()
