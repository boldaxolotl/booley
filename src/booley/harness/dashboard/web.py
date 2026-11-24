"""``booley dashboard --web``: each worktree's Jobs as a page and JSON on the loopback interface.

``GET /api/snapshot?worktree=<absolute path>`` answers for the main checkout or one
registered linked worktree; ``GET /api/report?...`` serves a report the snapshot links to.
Every error is ``{"error": "<sentence>"}`` and carries no Jobs. ``GET /`` serves the page
that polls the snapshot, and its assets come from ``/assets/``.

While the ``web-run-panel`` Feature Flag is on for the Project, ``GET /api/run/entries``,
``/api/run/targets``, ``/api/run/tests`` and ``/api/run/form`` answer the Run form's choices
for the same ``?worktree=``, with the same refusals; with the flag off they are unknown routes.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from booley.config.feature_flags import enabled_features, feature_enabled
from booley.core.booley_dir import resolve_common_booley_dir, resolve_main_checkout_root
from booley.core.booley_layout import BooleyLayoutError, git_common_directory
from booley.core.resources import package_data_dir
from booley.harness.dashboard import web_run
from booley.harness.dashboard.jobs import JobReader
from booley.harness.dashboard.launch import Launcher
from booley.harness.dashboard.resources import ResourceSampler
from booley.harness.dashboard.specialist_form import SimRun
from booley.harness.dashboard.targets import TargetsReader
from booley.harness.dashboard.thread_titles import ThreadTitles
from booley.harness.dashboard.web_snapshot import find_report, finished_sim_runs, read_snapshot
from booley.harness.worktree_list import WorktreeListError, worktree_registrations
from booley.runtime import job_slots, loopback_http

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
    not a registered worktree whose directory is still its checkout, 503 when Git cannot list them.
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
            # A directory recreated over a deleted worktree is not that worktree: it must
            # still be a checkout of this repository.
            if (
                real.is_dir()
                and registration.path.resolve() == real
                and git_common_directory(real) == git_common_directory(main)
            ):
                branch = registration.branch
                return real, None if branch is None else branch.removeprefix("refs/heads/")
    except (OSError, ValueError, RuntimeError, BooleyLayoutError):
        pass
    raise _RefusalError(404, "worktree is not the main checkout or a worktree of this Project")


def _page(assets: Path) -> Callable[[Request], Response]:
    """The handler of ``GET /``: the static page, which carries no Jobs.

    It shows the snapshot's own refusal for a bad worktree. The policy makes the browser
    refuse any load from outside this server.
    """

    def page(_request: Request) -> Response:
        return FileResponse(
            assets / "index.html", headers={"Content-Security-Policy": "default-src 'self'"}
        )

    return page


def _page_routes() -> list[Route | Mount]:
    """``GET /`` and the page's assets, which ship in the package."""
    assets = package_data_dir() / "dashboard_web"
    return [Route("/", _page(assets)), Mount("/assets", StaticFiles(directory=assets))]


def _required(request: Request, name: str, meaning: str) -> str:
    value = request.query_params.get(name)
    if not value:
        raise _RefusalError(400, f"{name} is required: {meaning}")
    return value


def _run_routes(
    main: Path, booley_dir: Path, reader: JobReader, lock: threading.Lock, features: frozenset[str]
) -> list[Route]:
    """The ``/api/run/*`` routes while the Feature Flag is on, else none; read fresh per request."""
    if not feature_enabled("web-run-panel", features):
        return []
    launcher = Launcher(booley_dir)
    targets = TargetsReader(main)

    def answer(request: Request, read: Callable[[Path], dict[str, Any]]) -> Response:
        worktree, _branch = _accepted_worktree(main, request.query_params.get("worktree"))
        try:
            return JSONResponse(read(worktree))
        except LookupError as exc:
            raise _RefusalError(404, str(exc)) from exc

    def entries(request: Request) -> Response:
        return answer(request, lambda worktree: web_run.entries(launcher, worktree))

    def flow_targets(request: Request) -> Response:
        flow = _required(request, "flow", "name a built-in Flow")
        return answer(request, lambda worktree: web_run.targets(launcher, targets, worktree, flow))

    def tests(request: Request) -> Response:
        selector = _required(request, "target", "pass a Target selector")
        return answer(request, lambda worktree: web_run.tests(targets, worktree, selector))

    def form(request: Request) -> Response:
        kind = _required(request, "kind", "flow or specialist")
        name = _required(request, "name", "name a built-in Flow or Specialist")
        given = {
            key: value
            for key, value in request.query_params.items()
            if key not in {"worktree", "kind", "name"}
        }

        def read(worktree: Path) -> dict[str, Any]:
            def records() -> tuple[SimRun, ...]:
                with lock:  # the Job reader's artifact cache is shared
                    return finished_sim_runs(reader, worktree)

            return web_run.form(launcher, worktree, kind, name, given, records)

        return answer(request, read)

    return [
        Route("/api/run/entries", entries),
        Route("/api/run/targets", flow_targets),
        Route("/api/run/tests", tests),
        Route("/api/run/form", form),
    ]


def create_app(
    main: Path,
    booley_dir: Path,
    *,
    proc_root: Path = Path("/proc"),
    cgroup: Path = Path("/sys/fs/cgroup"),
    features: frozenset[str] = frozenset(),
) -> Starlette:
    """The page and JSON application of the Project whose main checkout is *main*.

    Requests run one at a time: the Job reader's artifact cache and the CPU sampler
    keep state between reads.
    """
    slots = job_slots.slots_dir(booley_dir)
    reader = JobReader(booley_dir, slots_root=slots, proc_root=proc_root)
    sampler = ResourceSampler(cgroup)
    titles = ThreadTitles()
    lock = threading.Lock()

    def snapshot(request: Request) -> Response:
        worktree, branch = _accepted_worktree(main, request.query_params.get("worktree"))
        with lock:
            body = read_snapshot(
                reader,
                sampler,
                slots=slots,
                proc_root=proc_root,
                worktree=worktree,
                branch=branch,
                titles=titles,
            )
        return JSONResponse(body)

    def report(request: Request) -> Response:
        worktree, _branch = _accepted_worktree(main, request.query_params.get("worktree"))
        wanted = request.query_params.get("report")
        if not wanted:
            raise _RefusalError(400, "report is required: use the report_url of a snapshot Job")
        with lock:
            path = find_report(reader, worktree, wanted)
        try:
            data = None if path is None else path.read_bytes()
        except OSError:
            data = None
        if data is None:
            raise _RefusalError(404, "no such report in this worktree")
        return Response(data, media_type="application/json")

    return Starlette(
        routes=[
            *_page_routes(),
            Route("/api/snapshot", snapshot),
            Route("/api/report", report),
            *_run_routes(main, booley_dir, reader, lock, features),
        ],
        exception_handlers={_RefusalError: _refuse},
    )


def serve(project_root: Path, port: int) -> int:
    """Serve the Project's Jobs on ``127.0.0.1:<port>`` until stopped."""
    try:
        features = enabled_features(project_root)
    except ValueError:  # An unparsable booley.toml: the page still serves, without gated routes.
        features = frozenset()
    app = create_app(
        resolve_main_checkout_root(project_root),
        resolve_common_booley_dir(project_root),
        features=features,
    )
    try:
        sock = loopback_http.listen(HOST, port)
    except loopback_http.PortUnavailableError as exc:
        print(f"booley dashboard: {exc}; choose another port with --port", file=sys.stderr)
        return 2
    print(f"Booley Dashboard (web): http://{HOST}:{sock.getsockname()[1]}", flush=True)
    loopback_http.serve(app, sock)
    return 0
