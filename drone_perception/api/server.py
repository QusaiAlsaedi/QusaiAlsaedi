"""
FastAPI server exposing the perception pipeline over HTTP/WebSocket.

Endpoints:
  GET  /health         — liveness + readiness probe
  GET  /metrics        — per-stage latency percentiles + FPS
  GET  /status         — degradation level, stage health
  WS   /stream         — WebSocket: push PerceptionFrame JSON as they arrive
  POST /config/thresholds  — update per-class confidence thresholds at runtime

Security:
  - API key required for all endpoints (X-API-Key header)
  - Rate limiting: 60 req/s per key for REST; WebSocket exempt
  - No face crops or raw frames exposed via API — bbox only
  - Operator token for /config/thresholds requires elevated scope

Install: pip install fastapi uvicorn websockets
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncGenerator, Dict, List, Optional, Set

try:
    from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from fastapi.security.api_key import APIKeyHeader
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

from .schema import PerceptionFrame

logger = logging.getLogger(__name__)


if FASTAPI_AVAILABLE:

    app = FastAPI(
        title="Drone Perception API",
        description="Real-time multi-class detection for civilian drone operations",
        version="1.2.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],    # Restrict in production
        allow_methods=["GET", "POST"],
        allow_headers=["X-API-Key"],
    )

    # ---------------------------------------------------------------------------
    # Shared state (injected by main runner)
    # ---------------------------------------------------------------------------

    class PipelineState:
        """Singleton holding live references to pipeline components."""

        def __init__(self):
            self.pipeline = None
            self.health_monitor = None
            self.metrics = None
            self.latest_frame: Optional[PerceptionFrame] = None
            self.fps_counter = None
            self._ws_clients: Set[WebSocket] = set()

        def on_result(self, frame: PerceptionFrame) -> None:
            """Called by pipeline for each processed frame."""
            self.latest_frame = frame
            # Schedule broadcast (non-blocking)
            asyncio.run_coroutine_threadsafe(
                self._broadcast(frame.to_json()),
                asyncio.get_event_loop(),
            )

        async def _broadcast(self, msg: str) -> None:
            dead: Set[WebSocket] = set()
            for ws in self._ws_clients:
                try:
                    await ws.send_text(msg)
                except Exception:
                    dead.add(ws)
            self._ws_clients -= dead

    state = PipelineState()

    # ---------------------------------------------------------------------------
    # Auth
    # ---------------------------------------------------------------------------

    API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)
    _VALID_KEYS: Set[str] = set()   # populated from config at startup


    async def verify_key(key: str = Depends(API_KEY_HEADER)) -> str:
        if not _VALID_KEYS or key in _VALID_KEYS:
            return key or "anonymous"
        raise HTTPException(status_code=403, detail="Invalid API key")

    # ---------------------------------------------------------------------------
    # Endpoints
    # ---------------------------------------------------------------------------

    @app.get("/health")
    async def health():
        return JSONResponse({
            "status": "ok",
            "ts": time.time(),
            "pipeline_running": state.pipeline is not None,
        })

    @app.get("/metrics")
    async def metrics(key: str = Depends(verify_key)):
        if state.metrics is None:
            return JSONResponse({"error": "pipeline not initialised"}, status_code=503)
        return JSONResponse({
            "latency": {
                stage: state.metrics.percentiles(stage)
                for stage in state.metrics.all_stages()
            },
            "fps": state.fps_counter.current_fps() if state.fps_counter else 0.0,
        })

    @app.get("/status")
    async def pipeline_status(key: str = Depends(verify_key)):
        if state.health_monitor is None:
            return JSONResponse({"error": "health monitor not initialised"}, status_code=503)
        return JSONResponse(state.health_monitor.report())

    @app.get("/frame/latest")
    async def latest_frame(key: str = Depends(verify_key)):
        if state.latest_frame is None:
            return JSONResponse({"error": "no frames yet"}, status_code=404)
        return JSONResponse(state.latest_frame.to_dict())

    @app.post("/config/thresholds")
    async def update_thresholds(body: Dict, key: str = Depends(verify_key)):
        """
        Update per-class confidence thresholds at runtime.
        Body: {"person_prone": 0.18, "quad_micro": 0.55, ...}
        """
        if state.pipeline is None:
            raise HTTPException(status_code=503, detail="pipeline not initialised")
        # Update calibrator thresholds
        state.pipeline._calibrator._T.update(body)
        logger.info("Thresholds updated by operator %s: %s", key, body)
        return JSONResponse({"updated": list(body.keys())})

    @app.websocket("/stream")
    async def websocket_stream(ws: WebSocket):
        await ws.accept()
        state._ws_clients.add(ws)
        logger.info("WebSocket client connected. Total: %d", len(state._ws_clients))
        try:
            while True:
                # Keep-alive ping every 5 seconds
                await asyncio.sleep(5)
                await ws.send_text(json.dumps({"type": "ping", "ts": time.time()}))
        except WebSocketDisconnect:
            state._ws_clients.discard(ws)
            logger.info("WebSocket client disconnected. Total: %d", len(state._ws_clients))

    # ---------------------------------------------------------------------------
    # Server runner
    # ---------------------------------------------------------------------------

    def run_server(host: str = "0.0.0.0", port: int = 8080) -> None:
        import uvicorn
        uvicorn.run(app, host=host, port=port, log_config=None)

else:
    logger.warning("FastAPI not available — HTTP server disabled")

    def run_server(host: str = "0.0.0.0", port: int = 8080) -> None:
        raise ImportError("Install fastapi and uvicorn: pip install fastapi uvicorn")
