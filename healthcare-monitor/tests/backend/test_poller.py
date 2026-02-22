"""
Unit tests for the TCP/HTTP poller.
Uses asyncio and mocking to avoid real network calls.
"""
import asyncio
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../backend'))


class TestTCPProbe:
    def test_up_on_successful_connect(self):
        """Test that a successful TCP connection returns 'up'."""
        # Run a local echo server briefly
        async def _test():
            from app.services.poller import tcp_probe
            # Start a simple server
            server = await asyncio.start_server(
                lambda r, w: w.close(), '127.0.0.1', 0
            )
            port = server.sockets[0].getsockname()[1]
            status, latency, err = await tcp_probe('127.0.0.1', port, timeout=2.0)
            server.close()
            await server.wait_closed()
            return status, latency, err

        status, latency, err = asyncio.get_event_loop().run_until_complete(_test())
        assert status == "up"
        assert latency is not None
        assert latency >= 0
        assert err is None

    def test_down_on_refused_connection(self):
        """Test that connection refused returns 'down'."""
        async def _test():
            from app.services.poller import tcp_probe
            # Port 1 is almost certainly not listening
            status, latency, err = await tcp_probe('127.0.0.1', 19999, timeout=2.0)
            return status, err

        status, err = asyncio.get_event_loop().run_until_complete(_test())
        assert status in ("down", "timeout", "error")

    def test_timeout_returns_timeout(self):
        """Test that a non-routable address returns 'timeout'."""
        async def _test():
            from app.services.poller import tcp_probe
            # 192.0.2.0/24 is TEST-NET-1 (RFC 5737), should not route
            status, latency, err = await tcp_probe('192.0.2.1', 9999, timeout=0.2)
            return status

        status = asyncio.get_event_loop().run_until_complete(_test())
        assert status == "timeout"


class TestHTTPProbe:
    def test_successful_http_probe(self):
        """Test HTTP probe against a mock server."""
        async def _test():
            from aiohttp import web
            from app.services.poller import http_probe

            async def handler(request):
                return web.Response(text="ok", status=200)

            app = web.Application()
            app.router.add_get('/', handler)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, '127.0.0.1', 0)
            await site.start()
            port = runner._server.sockets[0].getsockname()[1]

            status, latency, http_status, err = await http_probe(
                f'http://127.0.0.1:{port}/', timeout=5.0
            )
            await runner.cleanup()
            return status, latency, http_status

        status, latency, http_status = asyncio.get_event_loop().run_until_complete(_test())
        assert status == "up"
        assert http_status == 200
        assert latency is not None

    def test_http_500_returns_down(self):
        async def _test():
            from aiohttp import web
            from app.services.poller import http_probe

            async def handler(request):
                return web.Response(text="error", status=500)

            app = web.Application()
            app.router.add_get('/', handler)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, '127.0.0.1', 0)
            await site.start()
            port = runner._server.sockets[0].getsockname()[1]

            result = await http_probe(f'http://127.0.0.1:{port}/', timeout=5.0)
            await runner.cleanup()
            return result

        status, latency, http_status, err = asyncio.get_event_loop().run_until_complete(_test())
        assert status == "down"
        assert http_status == 500
