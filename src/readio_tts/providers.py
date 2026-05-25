from io import BytesIO
import math
import struct
from typing import Protocol
import wave

import httpx

from .config import Settings


class SpeechProvider(Protocol):
    async def synthesize(self, text: str, reference_id: str | None = None) -> bytes:
        """Return one uncompressed PCM WAV utterance."""

    async def is_available(self) -> bool:
        """Report whether synthesis requests can currently be accepted."""

    async def close(self) -> None:
        """Release any resources owned by the provider."""


class FishSpeechProvider:
    """Client for the self-hosted Fish Speech v1.5 TTS API."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._default_reference_id = settings.fish_reference_id
        self._options: dict[str, object] = {
            "format": "wav",
            "streaming": False,
            "normalize": settings.fish_normalize,
            "chunk_length": settings.fish_chunk_length,
            "max_new_tokens": settings.fish_max_new_tokens,
            "top_p": settings.fish_top_p,
            "temperature": settings.fish_temperature,
            "repetition_penalty": settings.fish_repetition_penalty,
            "use_memory_cache": settings.fish_use_memory_cache,
        }
        headers = {"Accept": "audio/wav"}
        if settings.fish_api_key:
            headers["Authorization"] = f"Bearer {settings.fish_api_key}"
        self._client = client or httpx.AsyncClient(
            base_url=settings.fish_base_url.rstrip("/"),
            timeout=settings.fish_timeout_seconds,
            headers=headers,
        )
        self._owns_client = client is None

    async def synthesize(self, text: str, reference_id: str | None = None) -> bytes:
        payload = {"text": text, **self._options}
        selected_reference = reference_id or self._default_reference_id
        if selected_reference:
            payload["reference_id"] = selected_reference

        response = await self._client.post("/v1/tts", json=payload)
        response.raise_for_status()
        if response.headers.get("content-type", "").split(";")[0] != "audio/wav":
            raise ValueError("Fish Speech returned a non-WAV response.")
        return response.content

    async def is_available(self) -> bool:
        try:
            response = await self._client.post("/v1/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class MockSpeechProvider:
    """Generate deterministic WAV tones so the gateway is testable without a GPU."""

    frame_rate = 24_000

    async def synthesize(self, text: str, reference_id: str | None = None) -> bytes:
        duration_ms = max(80, min(2_000, len(text) * 35))
        frame_count = round(self.frame_rate * duration_ms / 1000)
        frequency = 330
        amplitude = 4_000
        frames = bytearray()
        for frame in range(frame_count):
            sample = int(amplitude * math.sin(2 * math.pi * frequency * frame / self.frame_rate))
            frames.extend(struct.pack("<h", sample))

        output = BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(self.frame_rate)
            writer.writeframes(frames)
        return output.getvalue()

    async def close(self) -> None:
        return None

    async def is_available(self) -> bool:
        return True


def create_provider(settings: Settings) -> SpeechProvider:
    if settings.provider == "fish":
        return FishSpeechProvider(settings)
    return MockSpeechProvider()
