"""Reproduce Coverage evidence query time and traced-memory characterization."""

from __future__ import annotations

import argparse
import json
import platform
import resource
import subprocess
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

from booley.flows.sim.coverage_campaign_store import (
    load_coverage_campaign,
    publish_coverage_campaign,
)
from booley.flows.sim.coverage_evidence import CoverageEvidenceSession
from tests.flows.sim.coverage_scale_fixture import scale_campaign


def _measure(call):
    tracemalloc.start()
    started = time.perf_counter()
    result = call()
    elapsed = time.perf_counter() - started
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return result, elapsed, peak


def _worker(campaign_path: Path, point_count: int) -> dict[str, object]:
    loaded, load_seconds, load_peak = _measure(lambda: load_coverage_campaign(campaign_path))
    session, session_seconds, session_peak = _measure(
        lambda: CoverageEvidenceSession(loaded.campaign, None)
    )
    point_id = loaded.campaign.points[point_count // 2].id
    _, exact_seconds, exact_peak = _measure(
        lambda: session.query({"view": "points", "point_ids": [point_id], "limit": 1})
    )
    request: dict[str, object] = {
        "view": "points",
        "metric": "line",
        "disposition": "eligible",
        "limit": 50,
    }
    pages = []
    for _ in range(10):
        page, elapsed, peak = _measure(lambda request=request: session.query(request))
        pages.append({"seconds": elapsed, "traced_peak_bytes": peak})
        cursor = page["next_cursor"]
        if cursor is None:
            break
        request = {**request, "cursor": cursor}
    reference = loaded.summary.point_store
    assert reference is not None
    return {
        "points": point_count,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "compressed_bytes": reference.bytes,
        "uncompressed_bytes": reference.uncompressed_bytes,
        "load_seconds": load_seconds,
        "load_traced_peak_bytes": load_peak,
        "session_seconds": session_seconds,
        "session_traced_peak_bytes": session_peak,
        "exact_seconds": exact_seconds,
        "exact_traced_peak_bytes": exact_peak,
        "filtered_pages": pages,
        "process_max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }


def _controller(point_count: int, samples: int) -> None:
    command = [sys.executable, "-m", "tests.performance.coverage_query_profile"]
    with tempfile.TemporaryDirectory(prefix="booley-coverage-profile-") as raw_directory:
        campaign_path = publish_coverage_campaign(
            Path(raw_directory), scale_campaign(point_count)
        ).campaign
        for sample in range(samples):
            completed = subprocess.run(
                [
                    *command,
                    "--worker",
                    "--points",
                    str(point_count),
                    "--campaign",
                    str(campaign_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            result = json.loads(completed.stdout)
            print(json.dumps({"sample": sample + 1, **result}, sort_keys=True), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--points", type=int, required=True)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--campaign", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.points <= 0 or args.samples <= 0:
        parser.error("points and samples must be positive")
    if args.worker:
        if args.campaign is None:
            parser.error("--campaign is required with --worker")
        print(json.dumps(_worker(args.campaign, args.points), sort_keys=True))
        return
    _controller(args.points, args.samples)


if __name__ == "__main__":
    main()
