import asyncio
import json

import httpx

from readio_tts.config import Settings
from readio_tts.providers import FishSpeechProvider


def test_fish_speech_v15_request_uses_local_server_options() -> None:
    requests: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/health":
            return httpx.Response(200, json={"status": "ok"})
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "audio/wav"}, content=b"wav")

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
            base_url="http://fish-speech",
        ) as client:
            provider = FishSpeechProvider(
                Settings(
                    fish_reference_id="narrator",
                    fish_chunk_length=180,
                    fish_use_memory_cache="on",
                ),
                client=client,
            )
            assert await provider.is_available()
            audio = await provider.synthesize("Hello.")
            assert audio == b"wav"

    asyncio.run(exercise())

    assert requests == [
        {
            "text": "Hello.",
            "format": "wav",
            "streaming": False,
            "normalize": True,
            "chunk_length": 180,
            "max_new_tokens": 1024,
            "top_p": 0.7,
            "temperature": 0.7,
            "repetition_penalty": 1.2,
            "use_memory_cache": "on",
            "reference_id": "narrator",
        }
    ]


def test_fish_speech_rejects_non_audio_response() -> None:
    async def exercise() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    content=b"{}",
                )
            ),
            base_url="http://fish-speech",
        )
        provider = FishSpeechProvider(Settings(), client=client)
        try:
            await provider.synthesize("Hello.")
        except ValueError as exc:
            assert "non-WAV" in str(exc)
        else:
            raise AssertionError("Expected a non-audio Fish Speech response to fail.")
        finally:
            await client.aclose()

    asyncio.run(exercise())
