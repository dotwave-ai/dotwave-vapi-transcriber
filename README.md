# dotwave-vapi-transcriber

A small open-source server that lets [Vapi](https://vapi.ai) voice agents use
[.wave](https://dotwave.ai) as their transcriber, through Vapi's
[custom transcriber](https://docs.vapi.ai/customization/custom-transcriber) interface.

Vapi connects to this server over a WebSocket and streams the call's audio. The server sends
each audio channel to .wave as its own live transcription session and returns Vapi's
`transcriber-response` messages as the words arrive.

```
Vapi  --stereo PCM16-->  dotwave-vapi-transcriber  --mono PCM16-->  .wave  (customer)
      <-transcriber-response-                      --mono PCM16-->  .wave  (assistant)
```

## Run it

Python 3.11 or newer.

```sh
pip install .
export DOTWAVE_API_KEY=...        # your .wave API key
dotwave-vapi-transcriber          # listens on port 8080
```

Or with Docker:

```sh
docker build -t dotwave-vapi-transcriber .
docker run --rm -p 8080:8080 -e DOTWAVE_API_KEY dotwave-vapi-transcriber
```

Vapi only connects to `wss://` addresses, so the server must be reachable over TLS. Put it
behind something that terminates TLS (ngrok for a quick test, or a host such as Fly or
Railway for a deployment) and use that address in the assistant configuration below.

`GET /healthz` returns `200 ok` for health checks.

## Configuration

All settings are environment variables.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DOTWAVE_API_KEY` | required | Your .wave API key. |
| `DOTWAVE_LANGUAGE` | unset | Language tag such as `en-US` or `pt-BR`. Omit it and .wave detects the language automatically. |
| `DOTWAVE_UTTERANCE_END_MS` | `1000` | Silence, in milliseconds (1000 to 3200), after which a segment of speech is closed and sent to Vapi as a final. Vapi sends no end-of-speech signal, so this decides when a turn ends. |
| `WAVE_TRANSCRIBE_ASSISTANT` | `true` | Also transcribe the assistant's channel. Set to `false` to discard channel 1 and open only the customer session. |
| `VAPI_SECRET` | unset | When set, the `x-vapi-secret` header on the WebSocket handshake must match it, otherwise the connection is refused with 401. |
| `PORT` | `8080` | Port to listen on. |
| `DOTWAVE_LISTEN_URL` | `wss://api.dotwave.ai/v1/listen` | The .wave live transcription endpoint. |

## Vapi assistant configuration

```json
{
  "transcriber": {
    "provider": "custom-transcriber",
    "server": {
      "url": "wss://your-host.example.com/api/custom-transcriber",
      "credentialId": "cred_..."
    }
  }
}
```

If you use `VAPI_SECRET`, give Vapi the same value so it sends it as `x-vapi-secret` on the
handshake. The server accepts any path, so use whatever path you like.

## How it behaves

- Vapi's first message must be `start` with `linear16`, `raw`, `16000` Hz and `2` channels.
  Anything else is refused with close code 1003 and a message saying what is expected.
- Each binary message is interleaved stereo PCM16: channel 0 is the customer and channel 1 is
  the assistant. The server splits it into two mono streams and forwards them as they arrive.
  A payload that ends in the middle of a stereo sample is buffered until the rest arrives.
- Results become `transcriber-response` messages for the matching `channel`
  (`customer` or `assistant`):
  - A provisional result is sent as `transcriptType: "partial"`: the finalized words of the
    current segment, then the provisional words, joined with a space. Each partial replaces
    the previous one.
  - When .wave finalizes words they are added to the segment and sent again as a partial.
  - When .wave ends the segment (`speech_final`, or `UtteranceEnd` after the silence set by
    `DOTWAVE_UTTERANCE_END_MS`), the whole segment is sent as `transcriptType: "final"` and the
    next segment starts empty.
- When Vapi closes the connection, the server asks .wave to finish both sessions and sends any
  last final it can before the connection is gone.
- If a .wave session closes with 1011, 1012, 4413, 4429 or another transient reason while the
  Vapi connection is open, it reconnects after a short backoff. Audio that arrives in the
  meantime is held for a short moment and then dropped, so the stream never runs ahead of real
  time.
- If .wave closes with 4401 (API key rejected) or 4400 (protocol error), the session is not
  retried and the Vapi connection is closed with an explanatory reason.
- The API key and the Vapi secret are never logged.

## Pricing

$0.00045 per minute per channel transcribed. With the assistant channel enabled, a call is
billed for two channels; set `WAVE_TRANSCRIBE_ASSISTANT=false` to transcribe only the
customer. See [pricing and limits](https://dotwave.ai/docs/pricing-and-limits/).

## Development

```sh
pip install -e '.[dev]'
pytest
ruff check . && ruff format --check .
```

The tests run the server against a fake .wave endpoint; no network access or API key is needed.

## License

Apache-2.0. Copyright 2026 Dollyglot, Inc.
