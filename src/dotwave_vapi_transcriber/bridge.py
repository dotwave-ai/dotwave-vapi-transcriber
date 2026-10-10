"""Socket-free logic: stereo de-interleaving and transcript segment assembly."""

from __future__ import annotations

from typing import Any

FRAME_BYTES = 4  # one stereo PCM16 sample pair: 2 bytes per channel

CUSTOMER = "customer"
ASSISTANT = "assistant"


def deinterleave(frame: bytes | bytearray | memoryview) -> tuple[bytes, bytes]:
    """Split interleaved stereo PCM16 into (channel 0, channel 1) mono PCM16.

    Only whole stereo samples (multiples of 4 bytes) are converted; a trailing remainder is
    ignored. Use `Deinterleaver` when payloads may be split at arbitrary byte offsets.
    """
    usable = len(frame) - len(frame) % FRAME_BYTES
    data = bytes(frame[:usable])
    half = usable // 2
    left = bytearray(half)
    right = bytearray(half)
    left[0::2] = data[0::4]
    left[1::2] = data[1::4]
    right[0::2] = data[2::4]
    right[1::2] = data[3::4]
    return bytes(left), bytes(right)


class Deinterleaver:
    """Stateful `deinterleave` that carries an odd remainder over to the next payload."""

    def __init__(self) -> None:
        self._pending = b""

    @property
    def pending(self) -> int:
        """Bytes buffered while waiting for the rest of a stereo sample."""
        return len(self._pending)

    def push(self, payload: bytes | bytearray | memoryview) -> tuple[bytes, bytes]:
        data = self._pending + bytes(payload)
        usable = len(data) - len(data) % FRAME_BYTES
        self._pending = data[usable:]
        return deinterleave(data[:usable])


def transcriber_response(text: str, channel: str, final: bool) -> dict[str, Any]:
    return {
        "type": "transcriber-response",
        "transcription": text,
        "channel": channel,
        "transcriptType": "final" if final else "partial",
    }


class SegmentAssembler:
    """Turn one upstream session's events into Vapi `transcriber-response` messages.

    Finals accumulate inside the current segment and are joined with one space. An interim
    is shown after them as a partial. A segment ends on `speech_final` or `UtteranceEnd`;
    the accumulated text is then sent as a final and the segment starts over.
    """

    def __init__(self, channel: str) -> None:
        self.channel = channel
        self._finals: list[str] = []
        self._last_partial = ""

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        kind = event.get("type")
        if kind == "Results":
            return self._results(event)
        if kind == "UtteranceEnd":
            return self.flush()
        return []

    def flush(self) -> list[dict[str, Any]]:
        """End the current segment, returning its final if it holds any text."""
        out: list[dict[str, Any]] = []
        if self._finals:
            out.append(transcriber_response(" ".join(self._finals), self.channel, final=True))
        self._finals = []
        self._last_partial = ""
        return out

    def _results(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        text = _transcript(event)
        if not event.get("is_final"):
            return self._partial(text) if text else []
        if text:
            self._finals.append(text)
        if event.get("speech_final"):
            return self.flush()
        return self._partial("") if text else []

    def _partial(self, interim: str) -> list[dict[str, Any]]:
        joined = " ".join([*self._finals, interim] if interim else self._finals)
        if not joined or joined == self._last_partial:
            return []
        self._last_partial = joined
        return [transcriber_response(joined, self.channel, final=False)]


def _transcript(event: dict[str, Any]) -> str:
    try:
        alternatives = event["channel"]["alternatives"]
        return str(alternatives[0].get("transcript", "")).strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""
