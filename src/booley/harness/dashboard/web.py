"""``booley dashboard``: each worktree's Jobs as a page and JSON on the loopback interface.

``GET /api/snapshot?worktree=<absolute path>`` answers for the main checkout or one
registered linked worktree; ``GET /api/report?...`` serves a report the snapshot links to.
Every error is ``{"error": "<sentence>"}`` and carries no Jobs. ``GET /?worktree=<path>``
serves the page that polls the snapshot, and its assets come from ``/assets/``. Without a
worktree, ``GET /`` asks which one: the main checkout or one of T3 Code's worktrees; when
the main checkout is the only choice it redirects there.

While the ``web-run-panel`` Feature Flag is on for the Project, ``GET /api/run/entries``,
``/api/run/targets``, ``/api/run/tests`` and ``/api/run/form`` answer the Run form's choices
for the same ``?worktree=``, with the same refusals; with the flag off they are unknown routes.
With the flag on, ``POST /api/run/command``, ``/check`` and ``/submit`` take a JSON body
``{"kind", "name", "values", "extra"}``: the equivalent CLI command, the review screen's lines
and refusal, and the start of a Standalone Job. Each requires a page of this server (an ``Origin``
of ``http://`` and the Host's own ``127.0.0.1:<port>`` or ``localhost:<port>``) and a JSON body,
so no other site can launch; errors are 403, 415, 400 (a bad body), 404 (an unknown Flow or
Specialist), 422 (the form or the parser refuses; on submit, also the Agent CLI's sign-in check)
and 500 (the argument check itself failed, or the Job record cannot be written).
"""

from __future__ import annotations

import html
import sys
import threading
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from booley.config.feature_flags import enabled_features, feature_enabled
from booley.core.booley_dir import resolve_common_booley_dir, resolve_main_checkout_root
from booley.core.booley_layout import BooleyLayoutError, git_common_directory
from booley.core.boundary import require_dict
from booley.core.resources import package_data_dir
from booley.harness.dashboard import web_run
from booley.harness.dashboard.git_worktrees import (
    Registration,
    WorktreeGitError,
    worktree_registrations,
)
from booley.harness.dashboard.jobs import JobReader
from booley.harness.dashboard.launch import Launcher
from booley.harness.dashboard.model import read_occupants
from booley.harness.dashboard.resources import ResourceSampler
from booley.harness.dashboard.review import Occupants
from booley.harness.dashboard.specialist_form import SimRun
from booley.harness.dashboard.targets import TargetsReader
from booley.harness.dashboard.thread_titles import ThreadTitles
from booley.harness.dashboard.web_snapshot import find_report, finished_sim_runs, read_snapshot
from booley.runtime import job_slots, loopback_http

HOST = "127.0.0.1"
# T3 Code's preview panel names this server ``localhost``; any other name may be rebound.
_LAUNCH_HOSTS = frozenset({HOST, "localhost"})
# The browser refuses any load from outside this server.
_PAGE_HEADERS = {"Content-Security-Policy": "default-src 'self'"}


class _RefusalError(Exception):
    """A request the server answers with a JSON error."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


async def _refuse(_request: Request, exc: Exception) -> Response:
    assert isinstance(exc, _RefusalError)
    return JSONResponse({"error": exc.message}, status_code=exc.status)


def _registrations(main: Path) -> list[Registration]:
    """Every registered worktree, the main checkout first; 503 when Git cannot list them."""
    try:
        return worktree_registrations(main)
    except WorktreeGitError as exc:
        raise _RefusalError(503, f"Git could not list the worktrees: {exc}") from exc


def _branch_name(registration: Registration) -> str | None:
    branch = registration.branch
    return None if branch is None else branch.removeprefix("refs/heads/")


def _accepted_worktree(main: Path, value: str | None) -> tuple[Path, str | None]:
    """The resolved main checkout or linked worktree *value* names, with its branch.

    Raises ``_RefusalError``: 400 for a missing or relative path, 404 for any path that is
    not a registered worktree whose directory is still its checkout, 503 when Git cannot list them.
    """
    if not value:
        raise _RefusalError(400, "worktree is required: pass an absolute path as ?worktree=")
    if not Path(value).is_absolute():
        raise _RefusalError(400, "worktree must be an absolute path")
    registrations = _registrations(main)
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
                return real, _branch_name(registration)
    except (OSError, ValueError, RuntimeError, BooleyLayoutError):
        pass
    raise _RefusalError(404, "worktree is not the main checkout or a worktree of this Project")


def _choices(main: Path, booley_dir: Path) -> list[tuple[Path, str | None]]:
    """The main checkout, then each T3 Code worktree whose directory exists, with branches."""
    first, *linked = _registrations(main)
    t3_worktrees = (booley_dir / "t3-worktrees").resolve()
    chosen = [
        entry
        for entry in linked
        if entry.path.is_dir() and entry.path.resolve().is_relative_to(t3_worktrees)
    ]
    return [(entry.path.resolve(), _branch_name(entry)) for entry in (first, *chosen)]


def _page_link(worktree: Path) -> str:
    return "/?" + urlencode({"worktree": str(worktree)}, errors="replace")


def _picker(choices: list[tuple[Path, str | None]]) -> Response:
    """The page that asks which worktree to show: one link per choice."""
    items = "".join(
        f'<li><a href="{html.escape(_page_link(path))}">{html.escape(str(path))}</a> '
        f'<span class="muted">{html.escape(branch or "detached HEAD")}</span></li>\n'
        for path, branch in choices
    )
    page = (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Booley Dashboard</title>\n"
        '<link rel="stylesheet" href="/assets/dashboard.css">\n</head>\n<body>\n'
        f"<main>\n<h2>Which worktree?</h2>\n<ul>\n{items}</ul>\n</main>\n</body>\n</html>\n"
    )
    # A branch or path that is not UTF-8 shows "?" for what cannot be encoded.
    return HTMLResponse(page.encode(errors="replace"), headers=_PAGE_HEADERS)


def _page(main: Path, booley_dir: Path, assets: Path) -> Callable[[Request], Response]:
    """The handler of ``GET /``: the static page of one worktree, or the choice of one.

    Neither carries Jobs. The static page shows the snapshot's own refusal for a bad worktree.
    """

    def page(request: Request) -> Response:
        if request.query_params.get("worktree"):
            return FileResponse(assets / "index.html", headers=_PAGE_HEADERS)
        choices = _choices(main, booley_dir)
        if len(choices) == 1:
            return RedirectResponse(_page_link(choices[0][0]))
        return _picker(choices)

    return page


def _page_routes(main: Path, booley_dir: Path) -> list[Route | Mount]:
    """``GET /`` and the page's assets, which ship in the package."""
    assets = package_data_dir() / "dashboard_web"
    return [
        Route("/", _page(main, booley_dir, assets)),
        Mount("/assets", StaticFiles(directory=assets)),
    ]


def _required(query: Mapping[str, str], name: str, meaning: str) -> str:
    value = query.get(name)
    if not value:
        raise _RefusalError(400, f"{name} is required: {meaning}")
    return value


def _run_form(
    launcher: Launcher,
    records: Callable[[Path], tuple[SimRun, ...]],
    worktree: Path,
    query: Mapping[str, str],
) -> dict[str, Any]:
    """The form route's answer: the values given are every parameter that is not the route's own."""
    kind = _required(query, "kind", "flow or specialist")
    name = _required(query, "name", "name a built-in Flow or Specialist")
    given = {key: value for key, value in query.items() if key not in {"worktree", "kind", "name"}}
    return web_run.form(launcher, worktree, kind, name, given, lambda: records(worktree))


async def _guarded_json(request: Request) -> dict[str, Any]:
    """The JSON object of a launch request from this server's own page, else a refusal.

    Refused before any Git or disk read: 403 unless the ``Origin`` is ``http://`` and the
    Host, whose name is ``127.0.0.1`` (the address the server binds) or ``localhost`` (T3
    Code's preview panel's name for it), so a rebound name is refused too; 415 for another
    content type, 400 for a body that is not a JSON object.
    """
    host = request.headers.get("host", "")
    from_page = request.headers.get("origin") == f"http://{host}"
    if not from_page or host.split(":")[0] not in _LAUNCH_HOSTS:
        raise _RefusalError(403, "a launch must come from this Dashboard's own page")
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise _RefusalError(415, "a launch takes a JSON body")
    try:
        body: object = await request.json()
    except ValueError as exc:
        raise _RefusalError(400, f"the body is not JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise _RefusalError(400, "the body must be a JSON object")
    return require_dict(body, field="body")


def _run_posts(
    main: Path, booley_dir: Path, launcher: Launcher, reader: JobReader, lock: threading.Lock
) -> list[Route]:
    """``POST /api/run/{command,check,submit}``: the guard, the worktree, then the run's own refusals."""

    def occupants_of(worktree: Path) -> Callable[[], Occupants]:
        def read() -> Occupants:
            with lock:  # the Job reader's artifact cache is shared
                return read_occupants(reader, worktree)

        return read

    def submit(worktree: Path, run: web_run.RunRequest) -> dict[str, Any]:
        try:
            return web_run.submit(launcher, booley_dir, worktree, run)
        except OSError as exc:
            raise _RefusalError(500, f"Could not start the Job: {exc}") from exc

    works: dict[str, Callable[[Path, web_run.RunRequest], dict[str, Any]]] = {
        "command": lambda worktree, run: web_run.command(launcher, worktree, run),
        "check": lambda worktree, run: web_run.check(
            launcher, worktree, run, occupants_of(worktree)
        ),
        "submit": submit,
    }
    statuses = {
        web_run.InvalidRunError: 400,
        web_run.UnknownEntryError: 404,
        web_run.RunRefusedError: 422,
        web_run.CheckFailedError: 500,
    }

    def posted(
        work: Callable[[Path, web_run.RunRequest], dict[str, Any]],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def route(request: Request) -> Response:
            body = await _guarded_json(request)

            def answer() -> dict[str, Any]:
                worktree, _branch = _accepted_worktree(main, request.query_params.get("worktree"))
                try:
                    return work(worktree, web_run.run_request(body))
                except tuple(statuses) as exc:
                    raise _RefusalError(statuses[type(exc)], str(exc)) from exc

            return JSONResponse(await run_in_threadpool(answer))

        return route

    return [
        Route(f"/api/run/{name}", posted(work), methods=["POST"]) for name, work in works.items()
    ]


def _run_routes(
    main: Path, booley_dir: Path, reader: JobReader, lock: threading.Lock, features: frozenset[str]
) -> list[Route]:
    """The ``/api/run/*`` routes while the Feature Flag is on, else none.

    Each accepts the worktree first, so its refusals are the snapshot's, then reads its own
    parameters.
    """
    if not feature_enabled("web-run-panel", features):
        return []
    launcher = Launcher(booley_dir)
    targets = TargetsReader()

    def records(worktree: Path) -> tuple[SimRun, ...]:
        with lock:  # the Job reader's artifact cache is shared
            return finished_sim_runs(reader, worktree)

    reads: dict[str, Callable[[Path, Mapping[str, str]], dict[str, Any]]] = {
        "entries": lambda worktree, _query: web_run.entries(launcher, worktree),
        "targets": lambda worktree, query: web_run.targets(
            launcher, targets, worktree, _required(query, "flow", "name a built-in Flow")
        ),
        "tests": lambda worktree, query: web_run.tests(
            targets, worktree, _required(query, "target", "pass a Target selector")
        ),
        "form": lambda worktree, query: _run_form(launcher, records, worktree, query),
    }

    def served(
        read: Callable[[Path, Mapping[str, str]], dict[str, Any]],
    ) -> Callable[[Request], Response]:
        def route(request: Request) -> Response:
            worktree, _branch = _accepted_worktree(main, request.query_params.get("worktree"))
            try:
                return JSONResponse(read(worktree, request.query_params))
            except web_run.UnknownEntryError as exc:
                raise _RefusalError(404, str(exc)) from exc

        return route

    return [
        *(Route(f"/api/run/{name}", served(read)) for name, read in reads.items()),
        *_run_posts(main, booley_dir, launcher, reader, lock),
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
    reader = JobReader(booley_dir, main=main, slots_root=slots, proc_root=proc_root)
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
            *_page_routes(main, booley_dir),
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
