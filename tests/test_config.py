from urllib.parse import parse_qs, urlsplit

import pytest

from dotwave_vapi_transcriber.config import Config, ConfigError


def test_defaults():
    config = Config.from_env({"DOTWAVE_API_KEY": "k"})
    assert config.language is None
    assert config.utterance_end_ms == 1000
    assert config.transcribe_assistant is True
    assert config.vapi_secret is None
    assert config.port == 8080
    assert config.listen_url == "wss://api.dotwave.ai/v1/listen"


def test_overrides():
    config = Config.from_env(
        {
            "DOTWAVE_API_KEY": "k",
            "DOTWAVE_LANGUAGE": "pt-BR",
            "DOTWAVE_UTTERANCE_END_MS": "2000",
            "WAVE_TRANSCRIBE_ASSISTANT": "false",
            "VAPI_SECRET": "s",
            "PORT": "9000",
            "DOTWAVE_LISTEN_URL": "wss://example.test/v1/listen",
        }
    )
    assert config.language == "pt-BR"
    assert config.utterance_end_ms == 2000
    assert config.transcribe_assistant is False
    assert config.vapi_secret == "s"
    assert config.port == 9000
    url = urlsplit(config.upstream_url())
    assert f"{url.scheme}://{url.netloc}{url.path}" == "wss://example.test/v1/listen"
    assert parse_qs(url.query)["language"] == ["pt-BR"]


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"DOTWAVE_API_KEY": "k", "DOTWAVE_UTTERANCE_END_MS": "500"},
        {"DOTWAVE_API_KEY": "k", "DOTWAVE_UTTERANCE_END_MS": "4000"},
        {"DOTWAVE_API_KEY": "k", "DOTWAVE_UTTERANCE_END_MS": "soon"},
        {"DOTWAVE_API_KEY": "k", "WAVE_TRANSCRIBE_ASSISTANT": "maybe"},
    ],
)
def test_invalid_environment_is_rejected(env):
    with pytest.raises(ConfigError):
        Config.from_env(env)


def test_secrets_are_not_in_repr():
    config = Config(api_key="key-123", vapi_secret="secret-456")
    assert "key-123" not in repr(config)
    assert "secret-456" not in repr(config)
