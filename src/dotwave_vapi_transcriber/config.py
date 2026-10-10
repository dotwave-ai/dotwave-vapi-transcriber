"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlencode

DEFAULT_LISTEN_URL = "wss://api.dotwave.ai/v1/listen"
DEFAULT_MODEL = "nemotron-asr-streaming"
MIN_UTTERANCE_END_MS = 1000
MAX_UTTERANCE_END_MS = 3200

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    """The environment does not describe a usable configuration."""


@dataclass(frozen=True)
class Config:
    api_key: str = field(repr=False)
    language: str | None = None
    utterance_end_ms: int = 1000
    transcribe_assistant: bool = True
    vapi_secret: str | None = field(default=None, repr=False)
    port: int = 8080
    listen_url: str = DEFAULT_LISTEN_URL
    model: str = DEFAULT_MODEL
    reconnect_base_delay: float = 0.25

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ConfigError("DOTWAVE_API_KEY is required")
        if not MIN_UTTERANCE_END_MS <= self.utterance_end_ms <= MAX_UTTERANCE_END_MS:
            raise ConfigError(
                f"DOTWAVE_UTTERANCE_END_MS must be between {MIN_UTTERANCE_END_MS} "
                f"and {MAX_UTTERANCE_END_MS}"
            )

    def upstream_url(self) -> str:
        """The .wave listen URL for one 16 kHz mono PCM16 channel."""
        params: list[tuple[str, str]] = [("encoding", "linear16"), ("sample_rate", "16000")]
        if self.language:
            params.append(("language", self.language))
        params += [
            ("model", self.model),
            ("interim_results", "true"),
            ("utterance_end_ms", str(self.utterance_end_ms)),
        ]
        separator = "&" if "?" in self.listen_url else "?"
        return f"{self.listen_url}{separator}{urlencode(params)}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        return cls(
            api_key=env.get("DOTWAVE_API_KEY", "").strip(),
            language=env.get("DOTWAVE_LANGUAGE", "").strip() or None,
            utterance_end_ms=_parse_int(env, "DOTWAVE_UTTERANCE_END_MS", 1000),
            transcribe_assistant=_parse_bool(env, "WAVE_TRANSCRIBE_ASSISTANT", True),
            vapi_secret=env.get("VAPI_SECRET") or None,
            port=_parse_int(env, "PORT", 8080),
            listen_url=env.get("DOTWAVE_LISTEN_URL", "").strip() or DEFAULT_LISTEN_URL,
        )


def _parse_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None


def _parse_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false")
