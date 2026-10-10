# Changelog

## 0.1.0

- First release.
- Accepts Vapi's custom transcriber WebSocket (`linear16`, raw, 16 kHz, stereo).
- Forwards the customer channel, and optionally the assistant channel, to .wave as two
  live transcription sessions.
- Turns interim and final results into `transcriber-response` partials and finals, with
  segments closed by `speech_final` or `UtteranceEnd`.
- Reconnects an upstream session after a transient close; stops on credential or protocol errors.
- Optional `x-vapi-secret` check and a `GET /healthz` endpoint.
