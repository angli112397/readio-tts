import asyncio
import json
import logging
from pathlib import Path

import httpx
import pytest

from readio_tts.config import Settings
from readio_tts.providers import GptSoVitsProvider, SynthesisError


def test_gpt_sovits_uses_the_job_reference_snapshot(tmp_path: Path) -> None:
    requests: list[dict[str, object]] = []
    input_dir = tmp_path / "jobs" / "job-id" / "input"
    input_dir.mkdir(parents=True)
    (input_dir / "reference.wav").write_bytes(b"wav-bytes")
    (input_dir / "reference.lab").write_text("Hello prompt.", encoding="utf-8")

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tts":
            requests.append(json.loads(request.content))
            return httpx.Response(200, headers={"content-type": "audio/wav"}, content=b"wav")
        return httpx.Response(404)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
            base_url="http://gpt-sovits",
        ) as client:
            provider = GptSoVitsProvider(
                Settings(
                    data_dir=tmp_path,
                    gpt_job_data_remote_dir="job-data/jobs",
                    gpt_text_lang="zh",
                    gpt_prompt_lang="zh",
                    gpt_text_split_method="cut0",
                ),
                client=client,
            )
            assert await provider.synthesize("Test text.", "job-id") == b"wav"

    asyncio.run(exercise())

    assert requests == [
        {
            "text": "Test text.",
            "ref_audio_path": "job-data/jobs/job-id/input/reference.wav",
            "prompt_text": "Hello prompt.",
            "text_lang": "zh",
            "prompt_lang": "zh",
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
        }
    ]


def test_gpt_sovits_failure_includes_response_detail_and_logs_path(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    input_dir = tmp_path / "jobs" / "job-id" / "input"
    input_dir.mkdir(parents=True)
    (input_dir / "reference.wav").write_bytes(b"wav-bytes")
    (input_dir / "reference.lab").write_text("Prompt.", encoding="utf-8")

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="reference audio is not readable")

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
            base_url="http://gpt-sovits",
        ) as client:
            provider = GptSoVitsProvider(
                Settings(data_dir=tmp_path, gpt_job_data_remote_dir="job-data/jobs"),
                client=client,
            )
            with pytest.raises(SynthesisError) as raised:
                await provider.synthesize("A short sentence.", "job-id")
            assert raised.value.code == "tts_request_rejected"
            assert not raised.value.retryable
            assert (
                str(raised.value)
                == "GPT-SoVITS rejected the synthesis request: reference audio is not readable"
            )

    with caplog.at_level(logging.ERROR, logger="readio_tts.providers"):
        asyncio.run(exercise())

    assert "job-data/jobs/job-id/input/reference.wav" in caplog.text
