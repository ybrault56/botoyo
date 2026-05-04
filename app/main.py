"""Application entry point and FastAPI app factory for BotYo."""

from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager
from pathlib import Path
import signal
import sys
import threading
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import uvicorn
from uvicorn import Config
from uvicorn.server import HANDLED_SIGNALS, Server

from app.supervisor import BotYoSupervisor, load_config, resolve_config_path
from app.storage.db import init_db
from app.utils.logging import get_logger
from app.web.routes_admin import router as admin_router
from app.web.routes_dashboard import router as dashboard_router
from app.web.routes_journal import router as journal_router

LOGGER = get_logger("app.main")


class BotYoServer(Server):
    """Uvicorn server variant that does not re-raise Ctrl+C after graceful shutdown."""

    @contextlib.contextmanager
    def capture_signals(self):
        if threading.current_thread() is not threading.main_thread():
            yield
            return

        original_handlers = {sig: signal.signal(sig, self.handle_exit) for sig in HANDLED_SIGNALS}
        try:
            yield
        finally:
            for sig, handler in original_handlers.items():
                signal.signal(sig, handler)
        self._captured_signals.clear()

    def request_shutdown(self) -> None:
        """Request a graceful shutdown from inside the application."""

        self.should_exit = True


def create_app(config_path: str | None = None, *, start_supervisor: bool = True) -> FastAPI:
    """Create the FastAPI application and wire the supervisor lifecycle."""

    resolved_config = resolve_config_path(config_path)
    supervisor = BotYoSupervisor(config_path=resolved_config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.supervisor = supervisor
        init_db(supervisor.db_path)
        if start_supervisor:
            await supervisor.start()
        try:
            yield
        finally:
            if start_supervisor:
                await supervisor.stop()

    app = FastAPI(title="BotYo", lifespan=lifespan)
    app.include_router(dashboard_router)
    app.include_router(admin_router)
    app.include_router(journal_router)
    app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "web" / "static"), name="static")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", **app.state.supervisor.status_snapshot()}

    app.state.request_shutdown = lambda: None
    return app


def run_server(app: FastAPI, config: dict[str, Any]) -> None:
    """Run the Uvicorn server for one BotYo application instance."""

    server_config = Config(
        app,
        host=str(config["web"]["host"]),
        port=int(config["web"]["port"]),
        log_level="info",
    )
    server = BotYoServer(server_config)
    app.state.request_shutdown = server.request_shutdown
    server.run()


def main(config_path: str | None = None) -> int:
    """Run the BotYo FastAPI application and normalize Ctrl+C shutdown on Windows."""

    resolved_config = str(resolve_config_path(config_path))
    config = load_config(resolved_config)
    app = create_app(resolved_config)
    try:
        run_server(app, config)
    except KeyboardInterrupt:
        LOGGER.info("shutdown requested by user")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
