"""The dashboard process. Loopback only, no credentials, twelve routes.

The loopback bind IS the access control (A4/A8). There is no sign-in, no token and
no role check anywhere in this package, and there must never be: the only way to
reach the dashboard from outside the VPS is

    ssh -L 8080:localhost:8080 user@vps

which means the SSH key is the credential. A non-loopback bind is refused at
startup rather than warned about, because a bind that silently succeeded on
0.0.0.0 would expose a trading control panel with no password to the internet, and
nothing in the process would look wrong.
"""

from __future__ import annotations

import asyncio
import ipaddress
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from arc.api.routes import router
from arc.errors import ArcFatalError

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from arc.runtime.engine import ArcRuntime
    from arc.runtime.supervisor import RuntimeSupervisor

__all__ = ["WEB_ROOT", "build_app", "check_bind", "serve"]

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


def check_bind(host: str) -> str:
    """Refuse any bind that is not loopback. Raises rather than warns."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ArcFatalError(
            f"API_BIND={host!r} is not an IP address. ARC has no sign-in by "
            "design; the bind address is the only access control, so it must be "
            "loopback. Use 127.0.0.1 and ssh -L for remote access."
        ) from exc
    if not address.is_loopback:
        raise ArcFatalError(
            f"API_BIND={host} is not loopback. ARC has no sign-in by design; "
            "binding a trading control panel to a routable address would expose it "
            "with no password. Use 127.0.0.1 and ssh -L for remote access."
        )
    return host


def build_app(run: ArcRuntime, supervisor: RuntimeSupervisor | None = None) -> FastAPI:
    """The app, with the runtime attached to state and the twelve routes mounted.

    The runtime is attached rather than injected per request so every route and the
    websocket read the same live object — a per-request copy would render a snapshot
    from whenever the request began.

    When a supervisor is attached the routes read `supervisor.runtime` instead, and
    `app.state.runtime` is only the fallback for tests that mount a runtime with no
    lifecycle around it. Two sources sounds like one too many, but the supervisor
    REPLACES its runtime on every start, so the attached reference is a stale object
    the moment anyone presses START.
    """
    app = FastAPI(title="ARC Operations Center", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = run
    app.state.supervisor = supervisor
    app.include_router(router)

    if WEB_ROOT.is_dir():
        # Static files, not a route: the workspaces are one HTML document and the
        # mount is what serves it. It adds no API surface, so the twelve-route
        # contract is untouched.
        app.mount("/static", StaticFiles(directory=str(WEB_ROOT)), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(WEB_ROOT / "index.html")

    return app


async def serve(
    run: ArcRuntime, *, host: str, port: int, supervisor: RuntimeSupervisor | None = None
) -> None:
    """Run the dashboard until cancelled. Never returns on its own.

    The OUTER task of the process, not a child of the runtime: `arc run` serves this
    and the supervisor starts and stops runtimes underneath it. A dashboard owned by
    the runtime could not survive stopping the runtime, so STOP would take away the
    only surface able to report that the stop worked.
    """
    config = uvicorn.Config(
        build_app(run, supervisor),
        host=check_bind(host),
        port=port,
        log_config=None,
        # ARC has its own logging and its own Signal Tank. Uvicorn's access log would
        # print one line per websocket frame and drown the runtime's own output.
        access_log=False,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    try:
        await server.serve()
    except asyncio.CancelledError:
        server.should_exit = True
        raise
