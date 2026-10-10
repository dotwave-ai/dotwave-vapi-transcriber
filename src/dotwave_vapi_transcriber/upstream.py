"""One live .wave transcription session for a single audio channel."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from .bridge import SegmentAssembler
from .config import Config

log = logging.getLogger("dotwave_vapi_transcriber")

CLOSE_CREDENTIAL = 4401
CLOSE_PROTOCOL = 4400

# Reasons are shown to the Vapi side, so they say what to do rather than what failed inside.
FATAL_CLOSES: dict[int, tuple[int, str]] = {
    CLOSE_CREDENTIAL: (1008, "The .wave API key was rejected. Check DOTWAVE_API_KEY."),
    CLOSE_PROTOCOL: (1011, "The .wave service rejected the audio stream."),
}
UNAVAILABLE_CLOSE = (1011, "The .wave transcription service is unavailable.")

MAX_CONSECUTIVE_FAILURES = 8
MAX_BACKOFF_SECONDS = 5.0
HEALTHY_SESSION_SECONDS = 5.0
# Audio that arrives while a session is connecting is held briefly and sent first, so the
# start of speech is not lost. It is capped well under the 640 ms .wave tolerates ahead of
# real time (320 ms of 16 kHz mono PCM16).
MAX_BUFFERED_BYTES = 10240

Emit = Callable[[dict[str, Any]], Awaitable[None]]
Fatal = Callable[[int, str], None]


class UpstreamSession:
    """Forwards mono PCM16 to .wave and turns its results into Vapi messages.

    The session reconnects after transient closes while the Vapi socket is open. Audio that
    arrives while it is reconnecting is dropped, because sending stale audio in a burst would
    run ahead of real time.
    """

    def __init__(self, channel: str, config: Config, emit: Emit, on_fatal: Fatal) -> None:
        self.channel = channel
        self._config = config
        self._emit = emit
        self._on_fatal = on_fatal
        self._assembler = SegmentAssembler(channel)
        self._ws: ClientConnection | None = None
        self._task: asyncio.Task[None] | None = None
        self._finishing = False
        self._buffer = bytearray()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"upstream-{self.channel}")

    async def send_audio(self, pcm: bytes) -> None:
        if not pcm:
            return
        ws = self._ws
        if ws is None:
            self._buffer += pcm
            del self._buffer[:-MAX_BUFFERED_BYTES]
            return
        try:
            await ws.send(pcm)
        except ConnectionClosed:
            pass

    async def finish(self, grace_seconds: float = 5.0) -> None:
        """Ask .wave to finish the open text, forward the last final, and stop."""
        self._finishing = True
        task = self._task
        ws = self._ws
        if ws is not None:
            try:
                await ws.send(json.dumps({"type": "CloseStream"}))
            except ConnectionClosed:
                pass
        if task is not None:
            if ws is None:
                task.cancel()
            await asyncio.wait({task}, timeout=grace_seconds)
            if not task.done():
                task.cancel()
                await asyncio.wait({task})
        for message in self._assembler.flush():
            await self._emit(message)

    async def _run(self) -> None:
        failures = 0
        while not self._finishing:
            started = time.monotonic()
            code = await self._session_once()
            for message in self._assembler.flush():
                await self._emit(message)
            if self._finishing:
                return
            if code in FATAL_CLOSES:
                log.error("%s: .wave closed the session with code %s", self.channel, code)
                self._on_fatal(*FATAL_CLOSES[code])
                return
            if time.monotonic() - started >= HEALTHY_SESSION_SECONDS:
                failures = 0
            failures += 1
            if failures > MAX_CONSECUTIVE_FAILURES:
                log.error("%s: giving up after %d failed sessions", self.channel, failures - 1)
                self._on_fatal(*UNAVAILABLE_CLOSE)
                return
            delay = min(
                self._config.reconnect_base_delay * 2 ** (failures - 1), MAX_BACKOFF_SECONDS
            )
            log.warning(
                "%s: session ended (code %s), reconnecting in %.2fs", self.channel, code, delay
            )
            await asyncio.sleep(delay)

    async def _session_once(self) -> int | None:
        """Run one upstream connection. Returns the close code, or a code standing in for it."""
        try:
            async with connect(
                self._config.upstream_url(),
                additional_headers={"Authorization": f"Token {self._config.api_key}"},
                open_timeout=10,
                compression=None,
            ) as ws:
                while self._buffer:
                    held = bytes(self._buffer)
                    self._buffer.clear()
                    await ws.send(held)
                self._ws = ws
                log.info("%s: connected to .wave", self.channel)
                await self._receive(ws)
                return ws.close_code
        except InvalidStatus as exc:
            status = exc.response.status_code
            log.warning("%s: .wave refused the connection (HTTP %d)", self.channel, status)
            return CLOSE_CREDENTIAL if status in (401, 403) else None
        except (OSError, TimeoutError, WebSocketException) as exc:
            log.warning("%s: could not reach .wave (%s)", self.channel, type(exc).__name__)
            return None
        finally:
            self._ws = None

    async def _receive(self, ws: ClientConnection) -> None:
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "Error":
                    log.warning("%s: .wave reported an error: %.200s", self.channel, raw)
                    continue
                for message in self._assembler.feed(event):
                    await self._emit(message)
        except ConnectionClosed:
            pass
