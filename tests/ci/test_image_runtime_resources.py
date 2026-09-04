from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / ".github/scripts/image_runtime_resources.py"
SPEC = importlib.util.spec_from_file_location("image_runtime_resources", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
image_runtime_resources = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = image_runtime_resources
SPEC.loader.exec_module(image_runtime_resources)


def test_parse_max_rss_kib_reads_gnu_time_output() -> None:
    output = """
Command being timed: "python3"
Maximum resident set size (kbytes): 12345
Exit status: 0
"""

    assert image_runtime_resources.parse_max_rss_kib(output) == 12_345


def test_parse_max_rss_kib_rejects_missing_or_ambiguous_output() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        image_runtime_resources.parse_max_rss_kib("no measurement")
    with pytest.raises(ValueError, match="exactly one"):
        image_runtime_resources.parse_max_rss_kib(
            "Maximum resident set size (kbytes): 1\nMaximum resident set size (kbytes): 2\n"
        )


def test_summarize_startup_keeps_every_sample_and_median() -> None:
    assert image_runtime_resources.summarize_startup([30.0, 10.0, 20.0]) == {
        "samples_ms": [30.0, 10.0, 20.0],
        "first_ms": 30.0,
        "median_ms": 20.0,
        "max_ms": 30.0,
    }


def test_unique_references_rejects_duplicate_names() -> None:
    with pytest.raises(ValueError, match="duplicate image name: sandbox"):
        image_runtime_resources._unique_references(
            [("sandbox", "image:first"), ("sandbox", "image:second")]
        )
