"""Doctor freshness stamp -- "when did doctor last bless this environment?"

``booley doctor`` is the diagnosis command and preflight is the per-ticket gate,
but nothing used to bridge them: an environment could drift for weeks (stale
sandbox wheel, regenerated devcontainer, edited ``booley.toml``) and burn a
multi-hour ticket sweep before anyone thought to re-run doctor. The stamp
closes that gap cheaply.

On every doctor run that ends with **zero FAILs and zero active WARNs**, doctor
records a timestamp plus an environment fingerprint into project runtime state at
``<project_dir>/runtime/doctor_stamp.json`` (beside the developer probe and
the slot store -- the same ADR 0028 bookkeeping home, bind-mounted into the
Session Runtime, so a host-written stamp is readable in-container and vice
versa). Session start (``booley session up``) and the per-sweep ticket loop
then check the stamp in microseconds -- no docker round-trips -- and emit a
one-line advisory nag when doctor's blessing has gone stale or the
fingerprint no longer matches the config on disk.

The fingerprint deliberately contains only inputs both launch contexts hash to the
same bytes (``booley.toml``, ``doctor-waivers.toml``, and
``devcontainer.json`` ride the bind mounts). The package version is recorded
separately and must match across contexts; image digests remain Doctor's own
job because they legitimately read differently host-side and in-container.

Everything here is advisory and fail-soft by contract: a missing, corrupt, or
unwritable stamp must never block doctor, a session, or a ticket run.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from booley.harness.devcontainer import devcontainer_path
from booley.harness.doctor_waivers import WAIVER_FILENAME
from booley.runtime import runtime_context
from booley.runtime.project_dir import resolve_project_dir
from booley.runtime.timefmt import format_human_date, parse_timestamp, utc_now_rfc3339

STAMP_FILENAME = "doctor_stamp.json"

# A clean doctor run older than this is considered stale. One week matches
# the cadence at which sandbox images and agent CLIs realistically drift.
MAX_AGE_DAYS = 7


def stamp_path(project_dir: Path) -> Path:
    """Location of the stamp inside project runtime state.

    Lives beside the developer probe and the slot store
    (``runtime/jobs/slots``) -- container-lifetime bookkeeping that both
    contexts reach through the project-dir bind mount.
    """
    return project_dir / "runtime" / STAMP_FILENAME


def _sha256_or_none(path: Path) -> str | None:
    """Content hash of *path*, or None when the file is absent/unreadable."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def compute_fingerprint(project_dir: Path, project_root: Path) -> dict[str, str | None]:
    """Hash the config inputs whose drift should invalidate the stamp.

    Both files are identical bytes on host and in-container (project dir and
    workspace are bind-mounted), so a fingerprint recorded on either side
    compares cleanly on the other.
    """
    return {
        "booley_toml_sha256": _sha256_or_none(project_dir / "booley.toml"),
        "doctor_waivers_sha256": _sha256_or_none(project_dir / WAIVER_FILENAME),
        "devcontainer_json_sha256": _sha256_or_none(devcontainer_path(project_root)),
    }


def record_clean_run(project_dir: Path, project_root: Path, *, deep: bool) -> Path | None:
    """Atomically record a fully clean Doctor run; returns the file written.

    Fail-soft: a read-only or missing state dir returns None rather than
    turning a green doctor run red.
    """
    import booley

    path = stamp_path(project_dir)
    payload = {
        "passed_at": utc_now_rfc3339(),
        "deep": bool(deep),
        "runtime": "session-runtime" if runtime_context.inside_session_runtime() else "host",
        "booley_version": booley.__version__,
        "fingerprint": compute_fingerprint(project_dir, project_root),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # tmp + rename so a concurrent reader never sees a partial file.
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return None
    return path


def load_stamp(project_dir: Path) -> dict | None:
    """The recorded stamp dict, or None when absent/corrupt."""
    try:
        data = json.loads(stamp_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _version_drift_message(stamp: dict) -> str | None:
    """Describe package drift since the stamped Doctor run, if any."""
    import booley

    stamped_version = stamp.get("booley_version")
    if stamped_version == booley.__version__:
        return None
    previous = str(stamped_version) if stamped_version else "unknown"
    return (
        f"Booley version changed from {previous} to {booley.__version__} "
        "since the last clean `booley doctor` run -- invoke /booley-heal"
    )


def check_stamp(
    project_dir: Path,
    project_root: Path,
    *,
    max_age_days: int = MAX_AGE_DAYS,
    now: datetime | None = None,
) -> str | None:
    """One-line advisory nag, or None when doctor's blessing is current.

    Conditions are evaluated strongest first: a missing/corrupt stamp, an
    unreadable timestamp, package-version drift, config drift, then age beyond
    *max_age_days*.
    """
    stamp = load_stamp(project_dir)
    if stamp is None:
        return (
            "no warning-free `booley doctor` run is stamped -- Doctor records "
            "freshness only after zero FAILs and zero active WARNs; resolve or "
            "waive the warnings, then re-run `booley doctor`"
        )

    try:
        passed_at = parse_timestamp(str(stamp.get("passed_at")))
    except ValueError:
        # An unparsable timestamp is as good as no stamp.
        return "the recorded `booley doctor` stamp is unreadable -- re-run `booley doctor`"
    passed_on = format_human_date(passed_at)

    if version_message := _version_drift_message(stamp):
        return version_message

    if stamp.get("fingerprint") != compute_fingerprint(project_dir, project_root):
        return (
            f"Doctor config or devcontainer.json changed since the last clean "
            f"`booley doctor` run ({passed_on}) -- re-run `booley doctor`"
        )

    age_days = ((now or datetime.now(tz=UTC)) - passed_at).days
    if age_days > max_age_days:
        return (
            f"last clean `booley doctor` run was {age_days} days ago "
            f"({passed_on}) -- re-run `booley doctor`"
        )
    return None


def warn_if_stale(project_root: Path, emit: Callable[[str], None]) -> None:
    """Run the stamp check and hand any nag to *emit*; never raises.

    The one-call entry point for session start and the ticket sweep: the
    stamp is advisory, so an unresolvable project dir (or any other surprise)
    silently skips the check rather than blocking real work.
    """
    try:
        message = check_stamp(resolve_project_dir(project_root), project_root)
    except Exception:  # noqa: BLE001 — advisory by contract; never block a session or sweep on the stamp
        return
    if message:
        emit(message)
