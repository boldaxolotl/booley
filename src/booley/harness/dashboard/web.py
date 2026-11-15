"""``booley dashboard --web``: each worktree's Jobs as JSON on the loopback interface.

``GET /api/snapshot?worktree=<absolute path>`` answers for the main checkout or one
registered linked worktree; ``GET /api/report?...`` serves a report the snapshot links to.
Every error is ``{"error": "<sentence>"}`` and carries no Jobs.
"""

from __future__ import annotations

import threading
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from booley.core.booley_dir import resolve_common_booley_dir, resolve_main_checkout_root
from booley.harness.dashboard.jobs import JobReader
from booley.harness.dashboard.resources import ResourceSampler
from booley.harness.dashboard.web_snapshot import find_report, read_snapshot
from booley.harness.worktree_list import WorktreeListError, worktree_registrations
from booley.runtime import job_slots

HOST = "127.0.0.1"
DEFAULT_PORT = 8090


class _RefusalError(Exception):
    """A request the server answers with a JSON error."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


async def _refuse(_request: Request, exc: Exception) -> Response:
    assert isinstance(exc, _RefusalError)
    return JSONResponse({"error": exc.message}, status_code=exc.status)


def _accepted_worktree(main: Path, value: str | None) -> tuple[Path, str | None]:
    """The resolved main checkout or linked worktree *value* names, with its branch.

    Raises ``_RefusalError``: 400 for a missing or relative path, 404 for any path that is
    not a registered worktree whose directory exists, 503 when Git cannot list them.
    """
    if not value:
        raise _RefusalError(400, "worktree is required: pass an absolute path as ?worktree=")
    if not Path(value).is_absolute():
        raise _RefusalError(400, "worktree must be an absolute path")
    try:
        registrations = worktree_registrations(main)
    except WorktreeListError as exc:
        raise _RefusalError(503, f"Git could not list the worktrees: {exc}") from exc
    try:
        real = Path(value).resolve()
        for registration in registrations:
            if real.is_dir() and registration.path.resolve() == real:
                branch = registration.branch
                return real, None if branch is None else branch.removeprefix("refs/heads/")
    except (OSError, ValueError, RuntimeError):
        pass
    raise _RefusalError(404, "worktree is not the main checkout or a worktree of this Project")


def create_app(
    main: Path,
    booley_dir: Path,
    *,
    proc_root: Path = Path("/proc"),
    cgroup: Path = Path("/sys/fs/cgroup"),
) -> Starlette:
    """The JSON application of the Project whose main checkout is *main*.

    Requests run one at a time: the Job reader's artifact cache and the CPU sampler
    keep state between reads.
    """
    slots = job_slots.slots_dir(booley_dir)
    reader = JobReader(booley_dir, slots_root=slots, proc_root=proc_root)
    sampler = ResourceSampler(cgroup)
    lock = threading.Lock()

    def snapshot(request: Request) -> Response:
        worktree, branch = _accepted_worktree(main, request.query_params.get("worktree"))
        with lock:
            body = read_snapshot(
                reader,
                sampler,
                booley_dir=booley_dir,
                slots=slots,
                proc_root=proc_root,
                worktree=worktree,
                branch=branch,
            )
        return JSONResponse(body)

    def report(request: Request) -> Response:
        worktree, _branch = _accepted_worktree(main, request.query_params.get("worktree"))
        wanted = request.query_params.get("report")
        if not wanted:
            raise _RefusalError(400, "report is required: use the report_url of a snapshot Job")
        with lock:
            path = find_report(reader, booley_dir, worktree, wanted)
        try:
            data = None if path is None else path.read_bytes()
        except OSError:
            data = None
        if data is None:
            raise _RefusalError(404, "no such report in this worktree")
        return Response(data, media_type="application/json")

    return Starlette(
        routes=[Route("/api/snapshot", snapshot), Route("/api/report", report)],
        exception_handlers={_RefusalError: _refuse},
    )


def serve(project_root: Path, port: int) -> int:
    """Serve the Project's Jobs on ``127.0.0.1:<port>`` until stopped."""
    import uvicorn

    app = create_app(
        resolve_main_checkout_root(project_root), resolve_common_booley_dir(project_root)
    )
    print(f"Booley Dashboard (web): http://{HOST}:{port}", flush=True)
    uvicorn.run(app, host=HOST, port=port, log_level="warning")
    return 0
