#!/usr/bin/env python3
"""Print the Renovate image the runner workflow pins, as a GitHub step output.

The ``renovate-config`` job validates the configuration with exactly this image,
so the validator and the runner cannot drift apart. The runner holds the only
reference, and Renovate's custom manager moves it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_RUNNER_WORKFLOW = Path(".github/workflows/renovate.yml")
_RUNNER_STEP = "Run Renovate"


def runner_image(workflow_text: str) -> str:
    """Return ``env.IMAGE`` of the runner's "Run Renovate" step; raise when it is absent."""
    workflow: dict[str, Any] = yaml.safe_load(workflow_text)
    steps: list[dict[str, Any]] = workflow["jobs"]["renovate"]["steps"]
    step = next(step for step in steps if step.get("name") == _RUNNER_STEP)
    image: str = step["env"]["IMAGE"]
    return image


def main() -> None:
    """Print ``image=<reference>`` for the workflow at the working directory's repository."""
    print(f"image={runner_image(_RUNNER_WORKFLOW.read_text(encoding='utf-8'))}")


if __name__ == "__main__":
    main()
