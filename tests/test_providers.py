import asyncio
from io import BytesIO
import json
import logging
from pathlib import Path
import wave

import httpx
import pytest

from readio_tts.config import Settings
from readio_tts.providers import GptSoVitsProvider, SynthesisError


def make_wav(frame_count: int = 400) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(b"\0\0" * frame_count)
    return output.getvalue()


def test_gpt_sovits_uses_the_job_reference_snapshot(tmp_path: Path) -> None:
    requests: list[dict[str, object]] = []
    input_dir = tmp_path / "jobs" / "job-id" / "snapshot"
    input_dir.mkdir(parents=True)
    (input_dir / "reference.wav").write_bytes(b"wav-bytes")
    (input_dir / "voice.json").write_text(
        '{"reference_language":"en","transcript":"Hello prompt."}',
        encoding="utf-8",
    )

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tts":
            requests.append(json.loads(request.content))
            return httpx.Response(200, headers={"content-type": "audio/wav"}, content=make_wav())
        return httpx.Response(404)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
            base_url="http://gpt-sovits",
        ) as client:
            provider = GptSoVitsProvider(
                Settings(
                    data_dir=tmp_path,
                    api_token="test-readio-api-token",
                    gpt_job_data_remote_dir="job-data/jobs",
                    gpt_text_split_method="cut0",
                ),
                client=client,
            )
            assert await provider.synthesize("Test text.", "job-id", "en") == make_wav()
            (input_dir / "voice.json").write_text(
                '{"reference_language":"zh","transcript":"Changed prompt."}',
                encoding="utf-8",
            )
            await provider.synthesize("Second text.", "job-id", "en")

    asyncio.run(exercise())

    assert requests == [
        {
            "text": "Test text.",
            "ref_audio_path": "job-data/jobs/job-id/snapshot/reference.wav",
            "prompt_text": "Hello prompt.",
            "text_lang": "en",
            "prompt_lang": "en",
            "text_split_method": "cut0",
            "batch_size": 1,
            "top_k": 15,
            "top_p": 1.0,
            "temperature": 1.0,
            "speed_factor": 1.0,
            "fragment_interval": 0.3,
            "seed": -1,
            "media_type": "wav",
            "streaming_mode": False,
        },
        {
            "text": "Second text.",
            "ref_audio_path": "job-data/jobs/job-id/snapshot/reference.wav",
            "prompt_text": "Hello prompt.",
            "text_lang": "en",
            "prompt_lang": "en",
            "text_split_method": "cut0",
            "batch_size": 1,
            "top_k": 15,
            "top_p": 1.0,
            "temperature": 1.0,
            "speed_factor": 1.0,
            "fragment_interval": 0.3,
            "seed": -1,
            "media_type": "wav",
            "streaming_mode": False,
        },
    ]


def test_gpt_sovits_failure_exposes_stable_message_and_logs_detail(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    input_dir = tmp_path / "jobs" / "job-id" / "snapshot"
    input_dir.mkdir(parents=True)
    (input_dir / "reference.wav").write_bytes(b"wav-bytes")
    (input_dir / "voice.json").write_text(
        '{"reference_language":"zh","transcript":"Prompt."}',
        encoding="utf-8",
    )

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="reference audio is not readable")

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
            base_url="http://gpt-sovits",
        ) as client:
            provider = GptSoVitsProvider(
                Settings(
                    data_dir=tmp_path,
                    api_token="test-readio-api-token",
                    gpt_job_data_remote_dir="job-data/jobs",
                ),
                client=client,
            )
            with pytest.raises(SynthesisError) as raised:
                await provider.synthesize("A short sentence.", "job-id", "en")
            assert raised.value.code == "tts_request_rejected"
            assert not raised.value.retryable
            assert str(raised.value) == "The speech engine rejected this sentence."

    with caplog.at_level(logging.ERROR, logger="readio_tts.providers"):
        asyncio.run(exercise())

    assert "job-data/jobs/job-id/snapshot/reference.wav" in caplog.text
    assert "reference audio is not readable" in caplog.text


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"not a wav", "GPT-SoVITS returned invalid audio data."),
        (make_wav(0), "GPT-SoVITS returned empty audio."),
    ],
)
def test_gpt_sovits_rejects_invalid_wav_responses(
    tmp_path: Path,
    content: bytes,
    message: str,
) -> None:
    input_dir = tmp_path / "jobs" / "job-id" / "snapshot"
    input_dir.mkdir(parents=True)
    (input_dir / "reference.wav").write_bytes(b"wav-bytes")
    (input_dir / "voice.json").write_text(
        '{"reference_language":"zh","transcript":"Prompt."}',
        encoding="utf-8",
    )

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "audio/wav"}, content=content)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
            base_url="http://gpt-sovits",
        ) as client:
            provider = GptSoVitsProvider(
                Settings(
                    data_dir=tmp_path,
                    api_token="test-readio-api-token",
                    gpt_job_data_remote_dir="job-data/jobs",
                ),
                client=client,
            )
            with pytest.raises(SynthesisError) as raised:
                await provider.synthesize("A short sentence.", "job-id", "en")
            assert raised.value.code == "invalid_tts_response"
            assert str(raised.value) == message

    asyncio.run(exercise())
