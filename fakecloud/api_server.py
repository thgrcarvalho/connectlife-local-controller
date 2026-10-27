#!/usr/bin/env python3
"""Local HTTP + WebSocket API for desktop and LAN clients.

Binds to the loopback address by default. Every request goes through the
controller's action lock, exactly like the line-based control socket.
"""

import asyncio
import json

from aiohttp import web, WSMsgType

DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8767


class ApiServer:
    def __init__(self, controller, host=DEFAULT_API_HOST, port=DEFAULT_API_PORT):
        self.controller = controller
        self.host = host
        self.port = port
        self.sockets = set()
        self.runner = None
        app = web.Application(middlewares=[self.cors])
        app.router.add_get("/api/status", self.status)
        app.router.add_get("/api/actions", self.actions)
        app.router.add_post("/api/actions/{action}", self.action)
        app.router.add_get("/api/events", self.events)
        app.router.add_route("OPTIONS", "/api/{tail:.*}", self.preflight)
        self.app = app

    @web.middleware
    async def cors(self, request, handler):
        response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response

    async def preflight(self, request):
        return web.Response(status=204)

    async def start(self):
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()
        self.controller.log(f"api listening on http://{self.host}:{self.port}")

    async def stop(self):
        for socket in list(self.sockets):
            await socket.close()
        if self.runner is not None:
            await self.runner.cleanup()

    def json(self, payload, status=200):
        return web.json_response(payload, status=status, dumps=lambda value: json.dumps(
            value, ensure_ascii=False))

    async def status(self, request):
        if request.query.get("refresh") == "1":
            try:
                await self.controller.run_action("status")
            except Exception as error:
                return self.json({**self.controller.snapshot, "ok": False, "error": str(error)}, status=503)
        return self.json({"ok": True, **self.controller.snapshot})

    async def actions(self, request):
        return self.json({"ok": True, "actions": self.controller.action_catalog()})

    async def action(self, request):
        action = request.match_info["action"]
        if action not in self.controller.allowed_actions():
            return self.json({"ok": False, "error": "unsupported action"}, status=404)
        try:
            result = await self.controller.run_action(action)
        except asyncio.TimeoutError:
            return self.json({**self.controller.snapshot, "ok": False, "error": "action timed out"}, status=504)
        except ValueError as error:
            return self.json({**self.controller.snapshot, "ok": False, "error": str(error)}, status=409)
        except Exception as error:
            return self.json({**self.controller.snapshot, "ok": False, "error": str(error)}, status=502)
        if action == "diagnose":
            return self.json({"ok": True, "diagnostics": result, **self.controller.snapshot})
        return self.json({"ok": True, **self.controller.snapshot})

    async def events(self, request):
        socket = web.WebSocketResponse(heartbeat=30)
        await socket.prepare(request)
        self.sockets.add(socket)
        try:
            await socket.send_json(self.controller.snapshot)
            async for message in socket:
                if message.type == WSMsgType.TEXT and message.data == "refresh":
                    try:
                        await self.controller.run_action("status")
                    except Exception:
                        pass
                elif message.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            self.sockets.discard(socket)
        return socket

    def publish(self, snapshot):
        for socket in list(self.sockets):
            if socket.closed:
                self.sockets.discard(socket)
                continue
            asyncio.ensure_future(self._send(socket, snapshot))

    async def _send(self, socket, snapshot):
        try:
            await socket.send_json(snapshot)
        except (ConnectionError, RuntimeError):
            self.sockets.discard(socket)
