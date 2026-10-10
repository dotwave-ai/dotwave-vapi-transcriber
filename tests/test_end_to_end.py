import asyncio
import contextlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import API_KEY, STEREO_FRAME, adapter, eventually, fake_wave, results
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

START = {
    "type": "start",
    "encoding": "linear16",
    "container": "raw",
    "sampleRate": 16000,
    "channels": 2,
}

CUSTOMER_SCRIPT = [
    ("send", {"type": "SpeechStarted"}),
    ("send", results("hello")),
    ("send", results("hello", final=True)),
    ("send", results("there")),
    ("send", results("there", final=True, speech_final=True)),
]
ASSISTANT_SCRIPT = [
    ("send", results("hi", final=True)),
    ("send", {"type": "UtteranceEnd", "last_word_end": 1.0}),
]


def msg(text, channel, kind):
    return {
        "type": "transcriber-response",
        "transcription": text,
        "channel": channel,
        "transcriptType": kind,
    }


async def receive(client, count, seconds=3.0):
    out = []
    async with asyncio.timeout(seconds):
        while len(out) < count:
            out.append(json.loads(await client.recv()))
    return out


@contextlib.asynccontextmanager
async def pumping(client, interval=0.01):
    """Stream stereo audio at a steady pace, as Vapi does, while the block runs."""

    async def run():
        while True:
            await client.send(STEREO_FRAME * 160)
            await asyncio.sleep(interval)

    task = asyncio.create_task(run())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.wait({task})


@contextlib.asynccontextmanager
async def vapi_client(url, **kwargs):
    async with connect(url, **kwargs) as client:
        await client.send(json.dumps(START))
        yield client


async def test_each_channel_goes_to_its_own_upstream_session():
    scripts = {"customer": [CUSTOMER_SCRIPT], "assistant": [ASSISTANT_SCRIPT]}
    async with fake_wave(scripts) as wave, adapter(wave) as url, vapi_client(url) as client:
        await client.send(STEREO_FRAME * 160)
        messages = await receive(client, 5)

        assert [m for m in messages if m["channel"] == "customer"] == [
            msg("hello", "customer", "partial"),
            msg("hello there", "customer", "partial"),
            msg("hello there", "customer", "final"),
        ]
        assert [m for m in messages if m["channel"] == "assistant"] == [
            msg("hi", "assistant", "partial"),
            msg("hi", "assistant", "final"),
        ]

        # One upstream session per channel, each carrying only its own channel's audio.
        assert len(wave.of("customer")) == 1
        assert len(wave.of("assistant")) == 1
        await eventually(lambda: wave.of("customer")[0].audio and wave.of("assistant")[0].audio)
        assert set(wave.of("customer")[0].audio) == {1}
        assert set(wave.of("assistant")[0].audio) == {2}


async def test_upstream_request_carries_token_and_query():
    async with fake_wave({}) as wave, adapter(wave, utterance_end_ms=1500) as url:
        async with vapi_client(url) as client:
            await client.send(STEREO_FRAME * 160)
            await eventually(lambda: len(wave.connections) == 2)

    for conn in wave.connections:
        assert conn.headers["authorization"] == f"Token {API_KEY}"
        assert urlsplit(conn.path).path == "/v1/listen"
        assert parse_qs(urlsplit(conn.path).query) == {
            "encoding": ["linear16"],
            "sample_rate": ["16000"],
            "language": ["en-US"],
            "model": ["nemotron-asr-streaming"],
            "interim_results": ["true"],
            "utterance_end_ms": ["1500"],
        }


async def test_language_is_omitted_for_automatic_detection():
    async with fake_wave({}) as wave, adapter(wave, language=None) as url:
        async with vapi_client(url) as client:
            await client.send(STEREO_FRAME * 160)
            await eventually(lambda: len(wave.connections) == 2)
    for conn in wave.connections:
        assert "language" not in parse_qs(urlsplit(conn.path).query)


async def test_assistant_channel_can_be_disabled():
    scripts = {"customer": [CUSTOMER_SCRIPT]}
    async with fake_wave(scripts) as wave, adapter(wave, transcribe_assistant=False) as url:
        async with vapi_client(url) as client:
            await client.send(STEREO_FRAME * 160)
            messages = await receive(client, 3)
            assert {m["channel"] for m in messages} == {"customer"}
            await asyncio.sleep(0.1)
        assert len(wave.connections) == 1
        assert wave.connections[0].channel == "customer"


async def test_odd_payload_sizes_are_buffered():
    async with fake_wave({}) as wave, adapter(wave, transcribe_assistant=False) as url:
        async with vapi_client(url) as client:
            # Two stereo samples, split in the middle of a sample.
            await client.send(b"\x01\x01\x02\x02\x01")
            await client.send(b"\x01\x02\x02")
            await eventually(lambda: wave.connections and len(wave.connections[0].audio) == 4)
            assert bytes(wave.connections[0].audio) == b"\x01\x01\x01\x01"


async def test_closing_vapi_asks_both_upstream_sessions_to_finish():
    async with fake_wave({}) as wave, adapter(wave) as url:
        async with vapi_client(url) as client:
            await client.send(STEREO_FRAME * 160)
            await eventually(lambda: len(wave.connections) == 2)
            await eventually(lambda: all(c.audio for c in wave.connections))
        await eventually(lambda: all(c.closed.is_set() for c in wave.connections))
    for conn in wave.connections:
        assert conn.texts == [{"type": "CloseStream"}]


async def test_text_held_open_at_close_is_flushed_as_a_final():
    # The Vapi socket is already closed by then, so this checks the adapter stays healthy
    # while the upstream replies to CloseStream with a final of the open text.
    async with fake_wave({}) as wave, adapter(wave, transcribe_assistant=False) as url:
        wave.on_close_stream["customer"] = [results("last words", final=True, from_finalize=True)]
        async with vapi_client(url) as client:
            await client.send(STEREO_FRAME * 160)
            await eventually(lambda: wave.connections and wave.connections[0].audio)
        await eventually(lambda: wave.connections[0].closed.is_set())
        assert wave.connections[0].texts == [{"type": "CloseStream"}]


async def test_upstream_is_reconnected_after_a_transient_close():
    scripts = {
        "customer": [[("close", 1012)], [("send", results("again", final=True, speech_final=True))]]
    }
    async with fake_wave(scripts) as wave, adapter(wave, transcribe_assistant=False) as url:
        async with vapi_client(url) as client, pumping(client):
            (message,) = await receive(client, 1)
        assert message == msg("again", "customer", "final")
        assert len(wave.of("customer")) == 2


@pytest.mark.parametrize("code", [1011, 4413, 4429])
async def test_other_retryable_closes_reconnect(code):
    scripts = {
        "customer": [[("close", code)], [("send", results("ok", final=True, speech_final=True))]]
    }
    async with fake_wave(scripts) as wave, adapter(wave, transcribe_assistant=False) as url:
        async with vapi_client(url) as client, pumping(client):
            (message,) = await receive(client, 1)
        assert message["transcription"] == "ok"


async def test_rejected_api_key_closes_vapi_with_a_reason():
    scripts = {"customer": [[("close", 4401)]]}
    async with fake_wave(scripts) as wave, adapter(wave, transcribe_assistant=False) as url:
        async with vapi_client(url) as client, pumping(client):
            with pytest.raises(ConnectionClosed) as info:
                await receive(client, 1)
        assert info.value.rcvd.code == 1008
        assert "API key" in info.value.rcvd.reason
        assert len(wave.of("customer")) == 1  # not retried


async def test_protocol_error_from_upstream_closes_vapi_without_retry():
    scripts = {"customer": [[("close", 4400)]]}
    async with fake_wave(scripts) as wave, adapter(wave, transcribe_assistant=False) as url:
        async with vapi_client(url) as client, pumping(client):
            with pytest.raises(ConnectionClosed) as info:
                await receive(client, 1)
        assert info.value.rcvd.code == 1011
        assert len(wave.of("customer")) == 1


@pytest.mark.parametrize(
    "start",
    [
        {**START, "channels": 1},
        {**START, "sampleRate": 8000},
        {**START, "encoding": "mulaw"},
        {**START, "container": "wav"},
        {"type": "hello"},
    ],
)
async def test_unsupported_start_is_refused_with_1003(start):
    async with fake_wave({}) as wave, adapter(wave) as url, connect(url) as client:
        await client.send(json.dumps(start))
        with pytest.raises(ConnectionClosed) as info:
            await client.recv()
        assert info.value.rcvd.code == 1003
        assert "linear16" in info.value.rcvd.reason
        assert wave.connections == []


async def test_secret_is_checked_when_configured():
    async with fake_wave({}) as wave, adapter(wave, vapi_secret="s3cret") as url:
        for headers in ({}, {"x-vapi-secret": "wrong"}):
            with pytest.raises(InvalidStatus) as info:
                async with connect(url, additional_headers=headers):
                    pass
            assert info.value.response.status_code == 401
        assert wave.connections == []

        async with vapi_client(url, additional_headers={"x-vapi-secret": "s3cret"}) as client:
            await client.send(STEREO_FRAME * 160)
            await eventually(lambda: len(wave.connections) == 2)


async def test_no_secret_configured_accepts_any_client():
    async with fake_wave({}) as wave, adapter(wave) as url:
        async with vapi_client(url, additional_headers={"x-vapi-secret": "anything"}):
            pass


async def test_healthz():
    async with fake_wave({}) as wave, adapter(wave, vapi_secret="s3cret") as url:
        port = urlsplit(url).port
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        await writer.drain()
        raw = await reader.read()
        writer.close()
        assert raw.startswith(b"HTTP/1.1 200")
        assert raw.endswith(b"ok\n")
