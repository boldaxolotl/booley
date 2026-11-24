"""Sandbox e2e: the image's headless Chrome renders a local page as the Sandbox user (#3875).

Runs under the real session flags (``--cap-drop ALL``, ``no-new-privileges``),
with the network off. Skips when Docker, the image, or the browser is missing,
so a stale local image never fails a run; CI's image contract fails first on a
missing browser. ``BOOLEY_CHROME_E2E_IMAGE`` names another image to test.
"""

from __future__ import annotations

import os
import shutil

import pytest
from tests.docker.isolation.container_names import next_ci_container_name
from tests.subprocess_budget import run_bounded_child

from booley.runtime.devcontainer import EGRESS_NETWORK
from booley.runtime.session_issuance import _FIXED_RUN_ARGS

_IMAGE = os.environ.get("BOOLEY_CHROME_E2E_IMAGE", "booley-sandbox")
_CHROME = "/opt/chrome-headless-shell/chrome-headless-shell"
_SCRIPT = f"""set -eu
test "$(id -u)" -ne 0
test "${{T3CODE_SERVER_BROWSER_SANDBOX:-}}" = 0 || {{ echo "T3CODE_SERVER_BROWSER_SANDBOX is not 0" >&2; exit 1; }}
page="$(mktemp -d)/page.html"
printf '%s' '<p id="m">static</p><script>document.getElementById("m").textContent="rendered"</script>' > "$page"
{_CHROME} --no-sandbox --dump-dom "file://$page"
"""


def test_sandbox_user_renders_a_local_page_headless() -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker not available")
    flags = ["none" if arg == EGRESS_NETWORK else arg for arg in _FIXED_RUN_ARGS]
    name = next_ci_container_name()
    command = [docker, "run", "--rm", *(["--name", name] if name else []), *flags, _IMAGE]
    probe = run_bounded_child([*command, "test", "-x", _CHROME])
    if probe.returncode != 0:
        pytest.skip(f"{_IMAGE} is missing or predates headless Chrome; rebuild it")

    run = run_bounded_child([*command, "bash", "-c", _SCRIPT])

    assert run.returncode == 0, run.stderr
    assert '<p id="m">rendered</p>' in run.stdout
