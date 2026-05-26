from io import BytesIO
import math
import struct
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
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


@dataclass(frozen=True)
class ReferenceProfile:
    audio_path: Path
    prompt_text: str


def _resolve_reference_profile(reference_dir: Path, reference_id: str) -> ReferenceProfile:
    profile_dir = reference_dir / reference_id
    if not profile_dir.is_dir():
        raise ValueError(f"Reference profile '{reference_id}' does not exist.")

    audio_path = _find_reference_audio(profile_dir)
    prompt_text = _find_reference_text(audio_path)
    if not prompt_text.strip():
        raise ValueError(f"Reference profile '{reference_id}' is missing prompt text.")

    return ReferenceProfile(audio_path=audio_path, prompt_text=prompt_text)


def _find_reference_audio(profile_dir: Path) -> Path:
    audio_suffixes = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac"}
    for candidate in sorted(profile_dir.iterdir()):
        if candidate.is_file() and candidate.suffix.lower() in audio_suffixes:
            text_candidate = candidate.with_suffix(".lab")
            if not text_candidate.exists():
                text_candidate = candidate.with_suffix(".txt")
            if text_candidate.exists():
                return candidate
    raise ValueError(
        f"Reference profile '{profile_dir.name}' must contain an audio file "
        "with a matching .lab or .txt transcript."
    )


def _find_reference_text(audio_path: Path) -> str:
    lab_path = audio_path.with_suffix(".lab")
    if lab_path.exists():
        return lab_path.read_text(encoding="utf-8").strip()

    txt_path = audio_path.with_suffix(".txt")
    if txt_path.exists():
        return txt_path.read_text(encoding="utf-8").strip()

    raise ValueError(f"Reference audio '{audio_path.name}' is missing transcript text.")


class GptSoVitsProvider:
    """Client for a self-hosted GPT-SoVITS api_v2 service."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._reference_dir = settings.gpt_reference_dir
        self._default_reference_id = settings.gpt_default_reference_id
        self._options: dict[str, object] = {
            "text_lang": settings.gpt_text_lang,
            "prompt_lang": settings.gpt_prompt_lang,
            "text_split_method": settings.gpt_text_split_method,
            "batch_size": settings.gpt_batch_size,
            "top_k": settings.gpt_top_k,
            "top_p": settings.gpt_top_p,
            "temperature": settings.gpt_temperature,
            "speed_factor": settings.gpt_speed_factor,
            "fragment_interval": settings.gpt_fragment_interval,
            "seed": settings.gpt_seed,
            "media_type": "wav",
            "streaming_mode": False,
        }
        headers = {"Accept": "audio/wav"}
        if settings.gpt_api_key:
            headers["Authorization"] = f"Bearer {settings.gpt_api_key}"
        self._client = client or httpx.AsyncClient(
            base_url=settings.gpt_base_url.rstrip("/"),
            timeout=settings.gpt_timeout_seconds,
            headers=headers,
        )
        self._owns_client = client is None

    async def synthesize(self, text: str, reference_id: str | None = None) -> bytes:
        profile = self._resolve_profile(reference_id)
        remote_audio_path = self._remote_reference_path(profile.audio_path)
        payload = {
            "text": text,
            "ref_audio_path": remote_audio_path,
            "prompt_text": profile.prompt_text,
            **self._options,
        }

        response = await self._client.post("/tts", json=payload)
        response.raise_for_status()
        if response.headers.get("content-type", "").split(";")[0] != "audio/wav":
            raise ValueError("GPT-SoVITS returned a non-WAV response.")
        return response.content

    async def is_available(self) -> bool:
        try:
            response = await self._client.get("/openapi.json")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _resolve_profile(self, reference_id: str | None) -> ReferenceProfile:
        selected_reference = reference_id or self._default_reference_id
        if not selected_reference:
            raise ValueError("GPT-SoVITS requires a reference_id.")
        return _resolve_reference_profile(self._reference_dir, selected_reference)

    def _remote_reference_path(self, audio_path: Path) -> str:
        relative_path = audio_path.relative_to(self._reference_dir).as_posix()
        return str(PurePosixPath("references") / relative_path)


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
    if settings.provider == "gpt":
        return GptSoVitsProvider(settings)
    return MockSpeechProvider()
