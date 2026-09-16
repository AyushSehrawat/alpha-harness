"""``alpha-harness``: the backend and its built UI in one process, opened in the browser."""

from __future__ import annotations

import asyncio
import webbrowser

import uvicorn

HOST = "127.0.0.1"
PORT = 8000


async def _serve() -> None:
    server = uvicorn.Server(uvicorn.Config("alpha_harness.main:app", host=HOST, port=PORT))
    serving = asyncio.create_task(server.serve())
    # Startup reconciles in-flight simulations first; open the page once it can answer.
    # uvicorn exposes readiness only as the ``started`` flag, never an event.
    while not server.started and not serving.done():  # noqa: ASYNC110
        await asyncio.sleep(0.1)
    if server.started:
        webbrowser.open(f"http://{HOST}:{PORT}")
    await serving


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
