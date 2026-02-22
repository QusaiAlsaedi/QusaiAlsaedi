"""
Central agentless poller - performs TCP and HTTP reachability probes.
Runs as a background asyncio task, respects per-endpoint intervals.
"""
import asyncio
import socket
import time
from datetime import datetime, timezone
from typing import Optional
import uuid

import aiohttp
import structlog

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.db.models import Endpoint, Probe

log = structlog.get_logger(__name__)

# Track when we last probed each endpoint
_last_probe: dict[str, datetime] = {}


async def tcp_probe(host: str, port: int, timeout: float) -> tuple[str, Optional[int], Optional[str]]:
    """
    TCP connect probe. Returns (status, latency_ms, error_message).
    status: "up" | "down" | "timeout" | "error"
    """
    start = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        latency_ms = int((time.monotonic() - start) * 1000)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return "up", latency_ms, None
    except asyncio.TimeoutError:
        return "timeout", None, f"TCP connect timed out after {timeout}s"
    except ConnectionRefusedError:
        return "down", None, "Connection refused"
    except OSError as e:
        return "down", None, str(e)
    except Exception as e:
        return "error", None, f"Unexpected: {str(e)[:200]}"


async def http_probe(
    url: str, timeout: float
) -> tuple[str, Optional[int], Optional[int], Optional[str]]:
    """
    HTTP GET probe. Returns (status, latency_ms, http_status_code, error_message).
    """
    start = time.monotonic()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout),
                ssl=False, allow_redirects=True
            ) as resp:
                latency_ms = int((time.monotonic() - start) * 1000)
                if resp.status < 500:
                    return "up", latency_ms, resp.status, None
                else:
                    return "down", latency_ms, resp.status, f"HTTP {resp.status}"
    except asyncio.TimeoutError:
        return "timeout", None, None, f"HTTP request timed out after {timeout}s"
    except aiohttp.ClientConnectorError as e:
        return "down", None, None, f"Connection failed: {str(e)[:200]}"
    except Exception as e:
        return "error", None, None, f"Unexpected: {str(e)[:200]}"


async def probe_endpoint(ep: Endpoint) -> Probe:
    """Run the appropriate probe for this endpoint and return a Probe result."""
    host = ep.host
    port = ep.port
    timeout = float(ep.timeout_seconds)
    poller_host = socket.gethostname()

    if ep.protocol in ("http", "https"):
        url = f"{ep.protocol}://{host}:{port}/"
        status, latency_ms, http_status, error_msg = await http_probe(url, timeout)
        probe = Probe(
            endpoint_id=ep.id, status=status, latency_ms=latency_ms,
            http_status=http_status, error_message=error_msg, probe_source=poller_host,
        )
    else:
        # tcp and mllp both use raw TCP connect
        status, latency_ms, error_msg = await tcp_probe(host, port, timeout)
        probe = Probe(
            endpoint_id=ep.id, status=status, latency_ms=latency_ms,
            error_message=error_msg, probe_source=poller_host,
        )

    return probe


async def probe_endpoint_by_id(endpoint_id: uuid.UUID) -> None:
    """Probe a single endpoint by ID, update last_status on endpoint row."""
    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        result = await db.execute(select(Endpoint).where(Endpoint.id == endpoint_id))
        ep = result.scalar_one_or_none()
        if not ep:
            return
        probe = await probe_endpoint(ep)
        db.add(probe)
        ep.last_status = probe.status
        ep.last_checked_at = probe.probed_at
        ep.last_latency_ms = probe.latency_ms
        await db.commit()
        log.debug("probe complete", endpoint=ep.name, status=probe.status,
                  latency_ms=probe.latency_ms)


async def run_poller_loop() -> None:
    """
    Main poller loop. Wakes every second and dispatches probes for
    endpoints whose interval has elapsed.
    """
    log.info("poller started")
    # Stagger startup to avoid DB stampede
    await asyncio.sleep(3)

    while True:
        try:
            async with AsyncSessionLocal() as db:
                from sqlalchemy import select
                result = await db.execute(
                    select(Endpoint).where(Endpoint.enabled == True)
                )
                endpoints = result.scalars().all()

            now = datetime.now(timezone.utc)
            tasks = []
            for ep in endpoints:
                ep_key = str(ep.id)
                last = _last_probe.get(ep_key)
                interval = ep.check_interval_seconds or settings.DEFAULT_PROBE_INTERVAL_SECONDS
                if last is None or (now - last).total_seconds() >= interval:
                    _last_probe[ep_key] = now
                    tasks.append(probe_endpoint_by_id(ep.id))

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        except asyncio.CancelledError:
            log.info("poller cancelled")
            return
        except Exception as e:
            log.error("poller loop error", error=str(e))

        await asyncio.sleep(1)
