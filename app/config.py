"""Settings, all from environment variables (see .env.example)."""
from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


GROCY_URL = _env("GROCY_URL", "http://host.docker.internal:9283")
GROCY_API_KEY = _env("GROCY_API_KEY")

CLAUDE_MODEL = _env("CLAUDE_MODEL", "claude-sonnet-5-5")
CLAUDE_EFFORT = _env("CLAUDE_EFFORT", "medium")
CLAUDE_MAX_TOKENS = _int("CLAUDE_MAX_TOKENS", 16000)

# Speech-to-text: any OpenAI-compatible /audio/transcriptions endpoint.
# Leave STT_API_KEY empty to fall back to the phone's built-in recognition.
STT_API_KEY = _env("STT_API_KEY")
STT_BASE_URL = _env("STT_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
STT_MODEL = _env("STT_MODEL", "whisper-large-v3-turbo")
STT_LANGUAGE = _env("STT_LANGUAGE", "en")

# Deepgram live transcription (words appear while speaking). Takes priority over STT_* when set.
DEEPGRAM_API_KEY = _env("DEEPGRAM_API_KEY")
DEEPGRAM_MODEL = _env("DEEPGRAM_MODEL", "nova-3")
DEEPGRAM_LANGUAGE = _env("DEEPGRAM_LANGUAGE", "multi")  # "multi" handles Hindi and English mixed

DATA_DIR = Path(_env("DATA_DIR", "./data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "assistant.db"

TIMEZONE = _env("TZ", "America/New_York")

# A conversation idle for longer than this is left in history and a fresh one starts.
IDLE_HOURS = _int("IDLE_HOURS", 6)
DAILY_MESSAGE_LIMIT = _int("DAILY_MESSAGE_LIMIT", 300)
MAX_STEPS = _int("MAX_STEPS", 14)
MAX_IMAGES = 8
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_AUDIO_BYTES = 20 * 1024 * 1024

# Cloudflare Access. When both are set, every request must carry a valid Access JWT.
CF_ACCESS_TEAM_DOMAIN = _env("CF_ACCESS_TEAM_DOMAIN").removeprefix("https://").rstrip("/")
CF_ACCESS_AUD = _env("CF_ACCESS_AUD")

def _pairs(name: str) -> dict[str, str]:
    """Parse "email=value,email2=value2"."""
    return dict(
        (k.strip().lower(), v.strip())
        for k, _, v in (pair.partition("=") for pair in _env(name).split(","))
        if k.strip() and v.strip()
    )


# How the assistant addresses people: "email=Name,email2=Name2".
USER_NAMES = _pairs("USER_NAMES")
# Optional per-person Grocy API keys ("email=key,..."), so Grocy's own journal shows who
# made each change. People without one use GROCY_API_KEY.
GROCY_USER_KEYS = _pairs("GROCY_USER_KEYS")
# Address of Grocy as people open it in a browser, for the "Open Grocy" link.
GROCY_PUBLIC_URL = _env("GROCY_PUBLIC_URL")
