"""Built-in TTS provider implementations."""

from dailycast.tts.providers.edge import EdgeTTSProvider
from dailycast.tts.providers.fake import FakeTTSProvider

__all__ = ["EdgeTTSProvider", "FakeTTSProvider"]
