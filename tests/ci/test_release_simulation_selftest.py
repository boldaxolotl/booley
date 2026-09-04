from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / ".github/scripts"))

from release_validation import simulation_selftest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="release container validation requires POSIX executables"
)


def _doctor(root: Path, output: str) -> Path:
    executable = root / "booley"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "assert sys.argv[1:] == ['doctor', '--deep', '--skip-agent-checks']\n"
        "print(os.environ['DOCTOR_OUTPUT'])\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def test_simulation_selftest_proves_good_and_bad_runtime_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = "\n".join(
        [
            "[OK] sim self-test good case 'smoke' passes",
            "[OK] sim self-test bad case 'smoke + bad overlay' correctly graded a failure",
            "0 failed.",
        ]
    )
    monkeypatch.setenv("DOCTOR_OUTPUT", output)

    evidence = simulation_selftest.validate(
        project=tmp_path,
        booley=_doctor(tmp_path, output),
        candidate_sha="candidate-sha",
        image_digest="sha256:image",
    )

    assert evidence["candidate"] == {
        "sha": "candidate-sha",
        "image_digest": "sha256:image",
    }
    assert evidence["checks"] == [
        {"id": "simulation-selftest.good", "status": "pass"},
        {"id": "simulation-selftest.bad-overlay", "status": "pass"},
        {"id": "doctor.summary", "status": "pass"},
    ]


def test_simulation_selftest_rejects_false_passing_bad_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = "\n".join(
        [
            "[OK] sim self-test good case 'smoke' passes",
            "[XX] sim self-test bad case 'smoke + bad overlay' FALSE-PASSED",
            "1 failed.",
        ]
    )
    monkeypatch.setenv("DOCTOR_OUTPUT", output)

    with pytest.raises(RuntimeError, match="bad runtime overlay"):
        simulation_selftest.validate(
            project=tmp_path,
            booley=_doctor(tmp_path, output),
            candidate_sha="candidate-sha",
            image_digest="sha256:image",
        )
