"""Closing the app from outside the browser.

The tray icon in the Windows launcher is the only caller that matters: it has no way into
the running process, and killing it outright would skip the lifespan that closes DuckDB and
SQLite. So it asks here instead, over the same loopback port the UI uses.
"""

from __future__ import annotations

import asyncio
import signal
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Request

from ..schemas import Out

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/shutdown", tags=["shutdown"])

#: Only when no server handle was published — a reload-mode dev server. Long enough for the
#: response to have left, short enough that the user is not left waiting on it.
GOODBYE_SECONDS = 0.5


class ShutdownStarted(Out):
    #: Accepted, not finished: the port goes quiet a moment after this response.
    stopping: bool


@router.post("")
async def close(request: Request, background: BackgroundTasks) -> ShutdownStarted:
    """Close the app. Nothing here decides whether it comes back — the launcher does that."""
    background.add_task(stop_server, getattr(request.app.state, "server", None))
    return ShutdownStarted(stopping=True)


async def stop_server(server: Any) -> None:
    """Close the app once this response has been flushed to the socket.

    A background task runs after the body is written, so there is no race with the browser
    and no arbitrary delay to tune. ``should_exit`` is uvicorn's own graceful path: it
    unwinds the lifespan, which is what closes DuckDB and SQLite.

    Without a server handle — ``uvicorn --reload`` in development — SIGINT reaches the same
    handler. Stopping the loop outright would skip the lifespan and leave both stores open,
    so that is deliberately not a fallback here.
    """
    if server is not None:
        server.should_exit = True
        return
    log.warning("shutdown.no_server_handle")
    asyncio.get_running_loop().call_later(GOODBYE_SECONDS, signal.raise_signal, signal.SIGINT)
