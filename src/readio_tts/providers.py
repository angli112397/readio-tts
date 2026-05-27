from io import BytesIO
import logging
import math
import struct
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Protocol
import wave

import httpx

from .config import Settings
from .models import LanguageCode, VoiceSnapshot


logger = logging.getLogger(__name__)


class SynthesisError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class SpeechProvider(Protocol):
    async def synthesize(self, text: str, job_id: str, text_language: LanguageCode) -> bytes:
        """Return one uncompressed PCM WAV utterance."""

    async def close(self) -> None:
        """Release any resources owned by the provider."""


@dataclass(frozen=True)
class JobVoiceInput:
    audio_path: Path
    prompt_text: str
    language: LanguageCode


class GptSoVitsProvider:
    """Client for a self-hosted GPT-SoVITS api_v2 service."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._jobs_dir = settings.data_dir / "jobs"
        self._remote_jobs_dir = PurePosixPath(settings.gpt_job_data_remote_dir)
        self._options: dict[str, object] = {
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
        self._client = client or httpx.AsyncClient(
            base_url=settings.gpt_base_url.rstrip("/"),
            timeout=settings.gpt_timeout_seconds,
            headers=headers,
        )
        self._owns_client = client is None

    async def synthesize(self, text: str, job_id: str, text_language: LanguageCode) -> bytes:
        voice = self._resolve_job_voice(job_id)
        remote_audio_path = str(
            self._remote_jobs_dir / job_id / "input" / voice.audio_path.name
        )
        payload = {
            "text": text,
            "text_lang": text_language,
            "ref_audio_path": remote_audio_path,
            "prompt_text": voice.prompt_text,
            "prompt_lang": voice.language,
            **self._options,
        }

        try:
            response = await self._client.post("/tts", json=payload)
        except httpx.HTTPError as exc:
            logger.exception("GPT-SoVITS request failed to connect: job_id=%s", job_id)
            raise SynthesisError(
                "tts_unavailable",
                "GPT-SoVITS is unavailable.",
                retryable=True,
            ) from exc
        if response.is_error:
            detail = _response_detail(response)
            logger.error(
                "GPT-SoVITS request failed: job_id=%s status=%s ref_audio_path=%s "
                "text_preview=%r response=%s",
                job_id,
                response.status_code,
                remote_audio_path,
                text[:80],
                detail,
            )
            if response.status_code < 500:
                raise SynthesisError(
                    "tts_request_rejected",
                    "The speech engine rejected this sentence.",
                )
            raise SynthesisError(
                "tts_unavailable",
                "GPT-SoVITS failed while synthesizing audio.",
                retryable=True,
            )
        if response.headers.get("content-type", "").split(";")[0] != "audio/wav":
            raise SynthesisError(
                "invalid_tts_response",
                "GPT-SoVITS returned invalid audio data.",
            )
        return response.content

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _resolve_job_voice(self, job_id: str) -> JobVoiceInput:
        input_dir = self._jobs_dir / job_id / "input"
        if not input_dir.is_dir():
            raise SynthesisError(
                "reference_snapshot_missing",
                "The job reference audio snapshot is missing.",
            )
        audio = input_dir / "reference.wav"
        if not audio.exists():
            raise SynthesisError(
                "reference_snapshot_missing",
                "The job reference audio snapshot is missing.",
            )
        metadata_path = input_dir / "voice.json"
        if not metadata_path.exists():
            raise SynthesisError(
                "reference_snapshot_missing",
                "The job voice snapshot is missing.",
            )
        try:
            metadata = VoiceSnapshot.model_validate_json(
                metadata_path.read_text(encoding="utf-8")
            )
        except ValueError as exc:
            raise SynthesisError(
                "reference_snapshot_invalid",
                "The job voice snapshot is invalid.",
            ) from exc
        return JobVoiceInput(
            audio_path=audio,
            prompt_text=metadata.transcript,
            language=metadata.reference_language,
        )


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict) and isinstance(payload.get("message"), str):
            return payload["message"][:200]
    except ValueError:
        pass
    detail = " ".join(response.text.split())
    return (detail or response.reason_phrase)[:200]


class MockSpeechProvider:
    """Generate deterministic WAV tones so the gateway is testable without a GPU."""

    frame_rate = 24_000

    async def synthesize(self, text: str, job_id: str, text_language: LanguageCode) -> bytes:
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

def create_provider(settings: Settings) -> SpeechProvider:
    if settings.provider == "gpt":
        return GptSoVitsProvider(settings)
    return MockSpeechProvider()
