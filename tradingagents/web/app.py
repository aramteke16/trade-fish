"""FastAPI backend for the trading dashboard."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from tradingagents.dataflows.indian_market import IST
from tradingagents.pipeline.dispatcher import register_dispatcher
from .database import init_db
from . import websocket as ws_manager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    ws_manager.set_loop(asyncio.get_running_loop())

    scheduler = BackgroundScheduler(timezone=IST)
    register_dispatcher(scheduler)
    scheduler.start()
    app.state.scheduler = scheduler
    logger.info("FastAPI startup complete; cron dispatcher running")

    # Best-effort Telegram boot notification. Gated by both
    # telegram_notifications_enabled and telegram_startup_message_enabled
    # and wrapped so a flaky bot can never block the FastAPI startup.
    try:
        from tradingagents.web.telegram_notifier import notify_startup
        notify_startup()
    except Exception as e:  # noqa: BLE001 - never let Telegram break boot
        logger.warning("[telegram] startup notification failed silently: %s", e)

    # Two-way Telegram command bot (/status, /today, /help). Polls only
    # while telegram_notifications_enabled is true so flipping the toggle
    # off pauses everything in ≤15s.
    try:
        from tradingagents.web import telegram_bot as _tb
        _tb.start()
    except Exception as e:  # noqa: BLE001
        logger.warning("[telegram-bot] failed to start command poller: %s", e)

    try:
        yield
    finally:
        try:
            from tradingagents.web import telegram_bot as _tb
            _tb.stop()
        except Exception as e:  # noqa: BLE001
            logger.warning("[telegram-bot] stop failed: %s", e)
        scheduler.shutdown(wait=False)
        logger.info("scheduler stopped")


def create_app() -> FastAPI:
    app = FastAPI(title="Intraday Trading Agent Dashboard", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from .routes import (
        admin, analyze, config, dashboard, debates, files, history,
        performance, pipeline, positions, stats, telegram, trades,
    )

    app.include_router(dashboard.router, prefix="/api")
    app.include_router(debates.router, prefix="/api")
    app.include_router(positions.router, prefix="/api")
    app.include_router(performance.router, prefix="/api")
    app.include_router(history.router, prefix="/api")
    app.include_router(config.router, prefix="/api")
    app.include_router(pipeline.router, prefix="/api")
    app.include_router(admin.router, prefix="/api")
    app.include_router(analyze.router, prefix="/api")
    app.include_router(trades.router, prefix="/api")
    app.include_router(stats.router, prefix="/api")
    app.include_router(files.router, prefix="/api")
    app.include_router(telegram.router, prefix="/api")

    @app.websocket("/ws/live")
    async def websocket_endpoint(websocket: WebSocket):
        await ws_manager.connect(websocket)
        try:
            while True:
                data = await websocket.receive_text()
                await ws_manager.broadcast({"type": "ping", "data": data})
        except WebSocketDisconnect:
            ws_manager.disconnect(websocket)

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    static_dir = os.path.join(repo_root, "frontend", "dist")
    if os.path.isdir(static_dir):
        app.mount("/assets", StaticFiles(directory=os.path.join(static_dir, "assets")), name="assets")

        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str):
            index_path = os.path.join(static_dir, "index.html")
            if os.path.exists(index_path):
                return FileResponse(index_path)
            return {"detail": "Frontend not built yet."}

    return app
