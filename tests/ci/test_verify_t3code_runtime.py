"""The ``bwave-smoke`` T3 Code binary probe judges what ``t3`` prints (#4781)."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.subprocess_budget import run_bounded_child
from tests.t3_cli_fake import write_t3_server_cli

_ROOT = Path(__file__).parents[2]
_SCRIPT = _ROOT / ".github/scripts/verify_t3code_runtime.py"
_LIMITS = json.loads(
    (_ROOT / "src/booley/data/docker/t3code-policy.json").read_text(encoding="utf-8")
)["limits"]
_SETTINGS = 'Settings:\n  worktreesDirectory = "/tmp/wt" (locked)'
_LOADED = "The running server (pid 31, started 2026-11-24T14:01:41.163Z) loaded this policy."
_PAIRING = {
    "id": "1",
    "credential": "c",
    "scopes": ["owner"],
    "expiresAt": (datetime.now(UTC) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
    "pairUrl": "{base}/pair#token=c",
}


def _show(
    *, settings: str = _SETTINGS, limits: dict[str, object] = _LIMITS, loaded: str = _LOADED
) -> str:
    return (
        "Managed by Booley (/etc/t3code/policy.json, fingerprint 47f1ab130165).\n\n"
        f"{settings}\n\nLimits:\n{json.dumps(limits, indent=2)}\n\n{loaded}\n"
    )


_WITHOUT_CONNECT = {k: v for k, v in _LIMITS.items() if k != "connect"}


@pytest.mark.skipif(sys.platform == "win32", reason="the fake t3 is a shebang script")
@pytest.mark.parametrize(
    ("fake", "failure"),
    [
        pytest.param({}, "", id="ok"),
        pytest.param({"check_exit": 77}, "policy check", id="policy-rejected"),
        pytest.param({"serve": "refused"}, "did not become ready", id="not-ready"),
        pytest.param({"serve": "hang"}, "did not become ready", id="hung"),
        pytest.param(
            {"show": _show(limits=_WITHOUT_CONNECT)}, "limits.connect", id="missing-limit"
        ),
        pytest.param(
            {"show": _show(limits={**_LIMITS, "telemetry": True})},
            "limits.telemetry",
            id="different-value",
        ),
        pytest.param(
            {"show": _show(settings="No managed settings.")},
            "worktreesDirectory",
            id="missing-setting",
        ),
        pytest.param(
            {"show": _show(loaded="The running server loaded Booley's policy. Restart T3 Code.")},
            "Restart T3 Code",
            id="restart-pending",
        ),
        pytest.param(
            {"pairing": {**_PAIRING, "pairUrl": "{base}/pair"}}, "pairUrl", id="pairing-shape"
        ),
    ],
)
def test_t3code_runtime_check(tmp_path: Path, fake: dict[str, Any], failure: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_t3_server_cli(bin_dir, **{"show": _show(), "pairing": _PAIRING, **fake})
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "organization": "Booley",
                "limits": _LIMITS,
                "settings": {"worktreesDirectory": "/tmp/wt"},
            }
        ),
        encoding="utf-8",
    )
    evidence = tmp_path / "evidence.json"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
    }

    result = run_bounded_child(
        [sys.executable, str(_SCRIPT), "--policy", str(policy), "--evidence", str(evidence)]
        + (["--ready-timeout", "2"] if fake.get("serve") == "hang" else []),
        env=env,
    )

    if failure:
        assert result.returncode == 1, result.stderr
        assert failure in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        recorded = json.loads(evidence.read_text(encoding="utf-8"))
        assert recorded["policy_show"]["limits"] == _LIMITS
        assert {"pairUrl", "expiresAt"} <= set(recorded["pairing"]["keys"])
    if fake.get("serve") == "hang":
        with pytest.raises(ProcessLookupError):
            os.kill(int((bin_dir / "serve.pid").read_text(encoding="utf-8")), 0)
