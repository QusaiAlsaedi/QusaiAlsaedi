"""
Integration Full-Body Monitor - FastAPI Application Entry Point
"""
import asyncio
import socket
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import settings
from app.db.base import Base
from app.db.session import engine, AsyncSessionLocal
from app.db.models import User, AlertRule
from app.core.security import hash_password
from app.api.routes import auth, endpoints, probes, hl7, alerts, metrics, dashboard
from app.services.poller import run_poller_loop
from app.services.hl7_tracker import run_hl7_tracker_loop
from app.services.alert_engine import run_alert_engine_loop

log = structlog.get_logger()


async def _seed_defaults():
    """Create default admin user and alert rules if not present."""
    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        result = await db.execute(select(User).where(User.username == "admin"))
        if not result.scalar_one_or_none():
            admin = User(
                username="admin",
                password_hash=hash_password("ChangeMe123!"),
                role="admin",
            )
            db.add(admin)
            log.info("seeded default admin user (change password immediately)")

        # Seed default rules if table empty
        rule_count = await db.execute(select(AlertRule))
        if not rule_count.scalars().first():
            rules = [
                AlertRule(name="Port Down", rule_type="port_down",
                          threshold_value=1, threshold_unit="count", severity="critical",
                          config={"consecutive_failures": 2}),
                AlertRule(name="No ACK >60s ORM", rule_type="no_ack_timeout",
                          threshold_value=60, threshold_unit="seconds", severity="critical",
                          config={"message_types": ["ORM"]}),
                AlertRule(name="No ACK >120s ORU", rule_type="no_ack_timeout",
                          threshold_value=120, threshold_unit="seconds", severity="warning",
                          config={"message_types": ["ORU"]}),
                AlertRule(name="High CPU >85%", rule_type="high_cpu",
                          threshold_value=85, threshold_unit="percent", severity="warning", config={}),
                AlertRule(name="High CPU >95%", rule_type="high_cpu",
                          threshold_value=95, threshold_unit="percent", severity="critical", config={}),
                AlertRule(name="High Disk >85%", rule_type="high_disk",
                          threshold_value=85, threshold_unit="percent", severity="warning", config={}),
                AlertRule(name="High Disk >95%", rule_type="high_disk",
                          threshold_value=95, threshold_unit="percent", severity="critical", config={}),
                AlertRule(name="Service Down", rule_type="service_down",
                          threshold_value=1, threshold_unit="count", severity="critical", config={}),
            ]
            for r in rules:
                db.add(r)
            log.info("seeded default alert rules")

        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    log.info("creating database tables")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _seed_defaults()

    log.info("starting background services")
    tasks = [
        asyncio.create_task(run_poller_loop(), name="poller"),
        asyncio.create_task(run_hl7_tracker_loop(), name="hl7-tracker"),
        asyncio.create_task(run_alert_engine_loop(), name="alert-engine"),
    ]

    yield

    # --- Shutdown ---
    log.info("shutting down background services")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await engine.dispose()


app = FastAPI(
    title=settings.APP_TITLE,
    description="Full-body monitor for hospital RIS/HIS/Interface Engine environments",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs" if settings.DEBUG else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if settings.DEBUG else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.error("unhandled exception", path=request.url.path, error=str(exc))
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# Health check - no auth required
@app.get("/health", tags=["health"])
async def health():
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "error",
        "host": socket.gethostname(),
    }


# Mount routers
app.include_router(auth.router,      prefix="/api/auth",      tags=["auth"])
app.include_router(endpoints.router, prefix="/api/endpoints", tags=["endpoints"])
app.include_router(probes.router,    prefix="/api/probes",    tags=["probes"])
app.include_router(hl7.router,       prefix="/api/hl7",       tags=["hl7"])
app.include_router(alerts.router,    prefix="/api/alerts",    tags=["alerts"])
app.include_router(metrics.router,   prefix="/api/metrics",   tags=["metrics"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"])
