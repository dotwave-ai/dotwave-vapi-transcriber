import struct

from dotwave_vapi_transcriber.bridge import Deinterleaver, SegmentAssembler, deinterleave


def stereo(left: list[int], right: list[int]) -> bytes:
    out = bytearray()
    for a, b in zip(left, right, strict=True):
        out += struct.pack("<hh", a, b)
    return bytes(out)


def mono(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def results(text, *, final=False, speech_final=False, from_finalize=False):
    return {
        "type": "Results",
        "is_final": final,
        "speech_final": speech_final,
        "from_finalize": from_finalize,
        "channel": {"alternatives": [{"transcript": text, "confidence": 1.0}]},
    }


def msg(text, channel="customer", kind="partial"):
    return {
        "type": "transcriber-response",
        "transcription": text,
        "channel": channel,
        "transcriptType": kind,
    }


class TestDeinterleave:
    def test_splits_channels(self):
        left, right = deinterleave(stereo([1, -2, 300], [-1, 2, -300]))
        assert left == mono([1, -2, 300])
        assert right == mono([-1, 2, -300])

    def test_empty(self):
        assert deinterleave(b"") == (b"", b"")

    def test_multiple_of_four_has_no_remainder(self):
        left, right = deinterleave(b"\x01\x02\x03\x04" * 5)
        assert left == b"\x01\x02" * 5
        assert right == b"\x03\x04" * 5

    def test_odd_remainder_is_not_converted(self):
        left, right = deinterleave(b"\x01\x02\x03\x04\x05\x06\x07")
        assert (left, right) == (b"\x01\x02", b"\x03\x04")

    def test_accepts_bytearray_and_memoryview(self):
        payload = stereo([5, 6], [7, 8])
        assert deinterleave(bytearray(payload)) == deinterleave(memoryview(payload))


class TestDeinterleaver:
    def test_buffers_odd_remainders_across_payloads(self):
        payload = stereo([1, 2, 3], [4, 5, 6])
        d = Deinterleaver()
        left, right = b"", b""
        for start in range(0, len(payload), 5):  # 5 does not divide a stereo sample
            a, b = d.push(payload[start : start + 5])
            left, right = left + a, right + b
        assert d.pending == 0
        assert (left, right) == (mono([1, 2, 3]), mono([4, 5, 6]))

    def test_reports_pending_bytes(self):
        d = Deinterleaver()
        assert d.push(b"\x01\x02\x03") == (b"", b"")
        assert d.pending == 3
        assert d.push(b"\x04\x05") == (b"\x01\x02", b"\x03\x04")
        assert d.pending == 1

    def test_multiple_of_four_passes_straight_through(self):
        d = Deinterleaver()
        assert d.push(stereo([9], [8])) == (mono([9]), mono([8]))
        assert d.pending == 0


class TestSegmentAssembler:
    def test_interim_becomes_partial(self):
        a = SegmentAssembler("customer")
        assert a.feed(results("hello")) == [msg("hello")]

    def test_interim_replaces_previous_interim(self):
        a = SegmentAssembler("customer")
        a.feed(results("hel"))
        assert a.feed(results("hello wor")) == [msg("hello wor")]

    def test_finals_accumulate_and_interim_follows(self):
        a = SegmentAssembler("customer")
        assert a.feed(results("hello", final=True)) == [msg("hello")]
        assert a.feed(results("there")) == [msg("hello there")]
        assert a.feed(results("there", final=True)) == []  # same text as the last partial
        assert a.feed(results("friend", final=True)) == [msg("hello there friend")]

    def test_speech_final_sends_final_and_resets(self):
        a = SegmentAssembler("customer")
        a.feed(results("one", final=True))
        out = a.feed(results("two", final=True, speech_final=True))
        assert out == [msg("one two", kind="final")]
        assert a.feed(results("three")) == [msg("three")]

    def test_empty_final_adds_nothing(self):
        a = SegmentAssembler("customer")
        assert a.feed(results("", final=True, from_finalize=True)) == []
        a.feed(results("hi", final=True))
        assert a.feed(results("", final=True, from_finalize=True)) == []

    def test_empty_speech_final_closes_segment_with_text(self):
        a = SegmentAssembler("customer")
        a.feed(results("hi", final=True))
        assert a.feed(results("", final=True, speech_final=True)) == [msg("hi", kind="final")]

    def test_utterance_end_sends_accumulated_final(self):
        a = SegmentAssembler("assistant")
        a.feed(results("good", final=True))
        out = a.feed({"type": "UtteranceEnd", "last_word_end": 1.2})
        assert out == [msg("good", channel="assistant", kind="final")]
        assert a.feed({"type": "UtteranceEnd"}) == []

    def test_utterance_end_without_text_sends_nothing(self):
        a = SegmentAssembler("customer")
        assert a.feed({"type": "UtteranceEnd"}) == []

    def test_flush_closes_open_segment(self):
        a = SegmentAssembler("customer")
        a.feed(results("bye", final=True, from_finalize=True))
        assert a.flush() == [msg("bye", kind="final")]
        assert a.flush() == []

    def test_unrelated_events_and_malformed_results_are_ignored(self):
        a = SegmentAssembler("customer")
        assert a.feed({"type": "Metadata"}) == []
        assert a.feed({"type": "SpeechStarted"}) == []
        assert a.feed({"type": "Results", "is_final": False}) == []

    def test_repeated_identical_partial_is_not_resent(self):
        a = SegmentAssembler("customer")
        assert a.feed(results("hello")) == [msg("hello")]
        assert a.feed(results("hello")) == []
