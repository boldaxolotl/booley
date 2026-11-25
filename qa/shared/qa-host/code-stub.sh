#!/bin/sh
# Stand-in for the VS Code CLI. Host Bootstrap only lists extensions and
# installs the Dev Containers extension; report it as installed.
case "$1" in
  --version) echo "0.0.0-qa-host-stub" ;;
  --list-extensions) echo "ms-vscode-remote.remote-containers" ;;
  --install-extension) exit 0 ;;
  *) echo "code: QA host stub, '$1' is not supported" >&2; exit 1 ;;
esac
