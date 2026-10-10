"""A fake .wave /v1/listen server and helpers to run the adapter against it."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from dotwave_vapi_transcriber.config import Config
from dotwave_vapi_transcriber.server import create_server

API_KEY = "test-key"

# Customer audio is built from 0x01 bytes and assistant audio from 0x02 bytes, so the fake
# can tell which channel a session carries from its first binary message.
STEREO_FRAME = b"\x01\x01\x02\x02"


def results(text, *, final=False, speech_final=False, from_finalize=False):
    return {
        "type": "Results",
        "channel_index": [0, 1],
        "start": 0.0,
        "duration": 0.08,
        "is_final": final,
        "speech_final": speech_final,
        "from_finalize": from_finalize,
        "metadata": {"request_id": "sess_test"},
        "channel": {"alternatives": [{"transcript": text, "confidence": 1.0, "words": []}]},
    }


@dataclass
class Connection:
    headers: dict[str, str]
    path: str
    channel: str | None = None
    audio: bytearray = field(default_factory=bytearray)
    texts: list[dict[str, Any]] = field(default_factory=list)
    closed: asyncio.Event = field(default_factory=asyncio.Event)


class FakeWave:
    """Plays a scripted sequence for each upstream connection, per channel.

    A script is a list of ("send", message) and ("close", code) steps run after the first
    audio arrives. When `on_close_stream` is set, those messages are sent when the adapter
    asks to finish the stream, followed by a normal close.
    """

    def __init__(self, scripts: dict[str, list[list[tuple]]]):
        self.scripts = scripts
        self.connections: list[Connection] = []
        self.on_close_stream: dict[str, list[dict[str, Any]]] = {}
        self.port = 0

    def of(self, channel: str) -> list[Connection]:
        return [c for c in self.connections if c.channel == channel]

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/v1/listen"

    async def handle(self, ws: ServerConnection) -> None:
        conn = Connection(
            headers={k.lower(): v for k, v in ws.request.headers.raw_items()}, path=ws.request.path
        )
        self.connections.append(conn)
        await ws.send(json.dumps({"type": "Metadata", "request_id": "sess_test"}))
        scripted = False
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    if conn.channel is None:
                        conn.channel = "customer" if message[0] == 1 else "assistant"
                    conn.audio += message
                    if not scripted:
                        scripted = True
                        index = len(self.of(conn.channel)) - 1
                        script = self.scripts.get(conn.channel, [])
                        steps = script[min(index, len(script) - 1)] if script else []
                        for kind, value in steps:
                            if kind == "send":
                                await ws.send(json.dumps(value))
                            else:
                                await ws.close(value)
                                return
                else:
                    event = json.loads(message)
                    conn.texts.append(event)
                    if event.get("type") == "CloseStream":
                        for reply in self.on_close_stream.get(conn.channel or "", []):
                            await ws.send(json.dumps(reply))
                        await ws.close(1000)
                        return
        except ConnectionClosed:
            pass
        finally:
            conn.closed.set()


@contextlib.asynccontextmanager
async def fake_wave(scripts: dict[str, list[list[tuple]]]) -> AsyncIterator[FakeWave]:
    fake = FakeWave(scripts)
    async with serve(fake.handle, "127.0.0.1", 0, compression=None) as server:
        fake.port = next(iter(server.sockets)).getsockname()[1]
        yield fake


@contextlib.asynccontextmanager
async def adapter(fake: FakeWave, **overrides: Any) -> AsyncIterator[str]:
    settings: dict[str, Any] = {
        "api_key": API_KEY,
        "language": "en-US",
        "listen_url": fake.url,
        "reconnect_base_delay": 0.01,
    }
    settings.update(overrides)
    async with create_server(Config(**settings), "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield f"ws://127.0.0.1:{port}/api/custom-transcriber"


async def eventually(predicate, seconds: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)
