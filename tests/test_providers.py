import asyncio
import json
from pathlib import Path

import httpx

from readio_tts.config import Settings
from readio_tts.providers import GptSoVitsProvider


def test_gpt_sovits_availability_does_not_run_synthesis(tmp_path: Path) -> None:
    requests: list[tuple[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json={"openapi": "3.1.0"})
        return httpx.Response(500)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
            base_url="http://gpt-sovits",
        ) as client:
            provider = GptSoVitsProvider(
                Settings(
                    provider="gpt",
                    gpt_reference_dir=tmp_path / "does-not-need-to-exist",
                ),
                client=client,
            )
            assert await provider.is_available()

    asyncio.run(exercise())

    assert requests == [("GET", "/openapi.json")]


def test_gpt_sovits_request_uses_reference_profile_files(tmp_path: Path) -> None:
    requests: list[dict[str, object]] = []
    reference_dir = tmp_path / "references" / "mandarin_reader"
    reference_dir.mkdir(parents=True)
    (reference_dir / "sample.wav").write_bytes(b"wav-bytes")
    (reference_dir / "sample.lab").write_text("你好，世界。", encoding="utf-8")

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tts":
            payload = json.loads(request.content)
            requests.append(payload)
            return httpx.Response(200, headers={"content-type": "audio/wav"}, content=b"wav")
        return httpx.Response(404)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
            base_url="http://gpt-sovits",
        ) as client:
            provider = GptSoVitsProvider(
                Settings(
                    provider="gpt",
                    gpt_reference_dir=tmp_path / "references",
                    gpt_text_lang="zh",
                    gpt_prompt_lang="zh",
                    gpt_text_split_method="cut0",
                ),
                client=client,
            )
            audio = await provider.synthesize("第一句。", "mandarin_reader")
            assert audio == b"wav"

    asyncio.run(exercise())

    assert requests == [
        {
            "text": "第一句。",
            "ref_audio_path": "references/mandarin_reader/sample.wav",
            "prompt_text": "你好，世界。",
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


def test_gpt_signature_changes_with_model_or_reference_audio(tmp_path: Path) -> None:
    reference_dir = tmp_path / "references" / "mandarin_reader"
    reference_dir.mkdir(parents=True)
    audio_path = reference_dir / "sample.wav"
    audio_path.write_bytes(b"voice-a")
    (reference_dir / "sample.lab").write_text("prompt", encoding="utf-8")

    async def signature(model_revision: str) -> str:
        provider = GptSoVitsProvider(
            Settings(
                provider="gpt",
                gpt_model_revision=model_revision,
                gpt_reference_dir=tmp_path / "references",
            )
        )
        try:
            return await provider.synthesis_signature("mandarin_reader")
        finally:
            await provider.close()

    v2 = asyncio.run(signature("v2"))
    proplus = asyncio.run(signature("v2ProPlus"))
    audio_path.write_bytes(b"voice-b")
    changed_reference = asyncio.run(signature("v2ProPlus"))

    assert v2 != proplus
    assert proplus != changed_reference
