"""WebSocket server for Vapi's custom transcriber interface."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import signal
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from .bridge import ASSISTANT, CUSTOMER, Deinterleaver
from .config import Config
from .upstream import UpstreamSession

log = logging.getLogger("dotwave_vapi_transcriber")

START_TIMEOUT_SECONDS = 10.0
EXPECTED_START = {"encoding": "linear16", "container": "raw", "sampleRate": 16000, "channels": 2}
FORMAT_REFUSAL = "Unsupported audio format: expected linear16, raw, 16000 Hz, 2 channels."


def make_process_request(
    config: Config,
) -> Callable[[ServerConnection, Request], Response | None]:
    def process_request(connection: ServerConnection, request: Request) -> Response | None:
        if urlsplit(request.path).path == "/healthz":
            return connection.respond(HTTPStatus.OK, "ok\n")
        if config.vapi_secret is not None:
            supplied = request.headers.get("x-vapi-secret", "")
            if not hmac.compare_digest(supplied.encode(), config.vapi_secret.encode()):
                return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        return None

    return process_request


def _is_expected_start(message: Any) -> bool:
    if not isinstance(message, dict) or message.get("type") != "start":
        return False
    return all(message.get(key) == value for key, value in EXPECTED_START.items())


async def _read_start(ws: ServerConnection) -> bool:
    """Wait for Vapi's `start` message. Closes the socket and returns False when refused."""
    try:
        raw = await asyncio.wait_for(ws.recv(), START_TIMEOUT_SECONDS)
    except TimeoutError:
        await ws.close(1008, "Expected a start message.")
        return False
    except ConnectionClosed:
        return False
    try:
        message = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        message = None
    if _is_expected_start(message):
        return True
    log.warning("refused a start message with an unsupported format")
    await ws.close(1003, FORMAT_REFUSAL)
    return False


def make_handler(config: Config) -> Callable[[ServerConnection], Awaitable[None]]:
    async def handle(ws: ServerConnection) -> None:
        if not await _read_start(ws):
            return

        async def emit(message: dict[str, Any]) -> None:
            with contextlib.suppress(ConnectionClosed):
                await ws.send(json.dumps(message))

        closers: list[asyncio.Task[None]] = []

        def on_fatal(code: int, reason: str) -> None:
            if not closers:
                closers.append(asyncio.create_task(ws.close(code, reason)))

        sessions: dict[str, UpstreamSession] = {
            CUSTOMER: UpstreamSession(CUSTOMER, config, emit, on_fatal)
        }
        if config.transcribe_assistant:
            sessions[ASSISTANT] = UpstreamSession(ASSISTANT, config, emit, on_fatal)
        for session in sessions.values():
            session.start()

        deinterleaver = Deinterleaver()
        try:
            async for message in ws:
                if isinstance(message, str):
                    continue
                customer_pcm, assistant_pcm = deinterleaver.push(message)
                await sessions[CUSTOMER].send_audio(customer_pcm)
                if ASSISTANT in sessions:
                    await sessions[ASSISTANT].send_audio(assistant_pcm)
        except ConnectionClosed:
            pass
        finally:
            await asyncio.gather(*(s.finish() for s in sessions.values()))
            if closers:
                await asyncio.gather(*closers, return_exceptions=True)

    return handle


def create_server(config: Config, host: str | None = None, port: int | None = None) -> serve:
    return serve(
        make_handler(config),
        host,
        config.port if port is None else port,
        process_request=make_process_request(config),
        compression=None,
    )


async def run_server(config: Config, host: str = "0.0.0.0") -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    async with create_server(config, host):
        log.info("listening on port %d", config.port)
        await stop.wait()
