import asyncio
import json
from pathlib import Path

from readio_tts.jobs import JobService
from readio_tts.models import CreateJobRequest, JobState, SentenceRequest
from readio_tts.providers import MockSpeechProvider


class FailingProvider(MockSpeechProvider):
    async def synthesize(self, text: str, voice_id: str) -> bytes:
        raise RuntimeError("provider failed")


def make_request() -> CreateJobRequest:
    return CreateJobRequest(
        chapter_id="chapter-1",
        voice_id="reader",
        sentence_gap_ms=400,
        sentences=[
            SentenceRequest(id="s1", text="Hello."),
            SentenceRequest(id="s2", text="This is sentence two."),
        ],
    )


def test_processes_job_into_audio_manifest_and_progress(tmp_path: Path) -> None:
    async def exercise() -> None:
        service = JobService(MockSpeechProvider(), tmp_path, 1_000)
        job, created = await service.create_job(make_request(), "request-one")
        assert created
        await service.process(job.job_id)
        completed = await service.get_job(job.job_id)

        assert completed is not None
        assert completed.state == JobState.COMPLETED
        assert completed.sentences_completed == 2
        assert completed.audio_size_bytes
        assert completed.audio_sha256
        assert service.audio_path(job.job_id).exists()
        manifest = json.loads(service.manifest_path(job.job_id).read_text(encoding="utf-8"))
        assert manifest["sentences"][1]["begin_ms"] - manifest["sentences"][0]["end_ms"] == 400
        persisted_state = json.loads((tmp_path / job.job_id / "job.json").read_text(encoding="utf-8"))
        persisted_request = json.loads(
            (tmp_path / job.job_id / "request.json").read_text(encoding="utf-8")
        )
        assert "request" not in persisted_state
        assert len(persisted_request["sentences"]) == 2

    asyncio.run(exercise())


def test_rejects_oversized_chapter(tmp_path: Path) -> None:
    async def exercise() -> None:
        service = JobService(MockSpeechProvider(), tmp_path, 3)
        request = CreateJobRequest(
            chapter_id="large",
            voice_id="reader",
            sentences=[SentenceRequest(id="s1", text="abcd")],
        )
        try:
            await service.create_job(request, "too-large")
        except ValueError as exc:
            assert "maximum is 3" in str(exc)
        else:
            raise AssertionError("Expected oversized request to be rejected.")

    asyncio.run(exercise())


def test_resume_pending_job_reuses_existing_sentence_checkpoint(tmp_path: Path) -> None:
    class CountingProvider(MockSpeechProvider):
        def __init__(self) -> None:
            self.texts: list[str] = []

        async def synthesize(self, text: str, voice_id: str) -> bytes:
            self.texts.append(text)
            return await super().synthesize(text, voice_id)

    async def exercise() -> None:
        first_provider = CountingProvider()
        first_service = JobService(first_provider, tmp_path, 1_000)
        job, _ = await first_service.create_job(make_request(), "resume-me")
        segments = tmp_path / job.job_id / "segments"
        segments.mkdir()
        first_segment = await first_provider.synthesize("Hello.", "reader")
        (segments / "000000.wav").write_bytes(first_segment)
        await first_service._update(
            job.job_id,
            lambda current: setattr(current, "synthesis_signature", "mock:reader"),
        )

        second_provider = CountingProvider()
        resumed_service = JobService(second_provider, tmp_path, 1_000)
        await resumed_service.resume_pending_jobs()
        await asyncio.gather(*resumed_service._tasks)
        completed = await resumed_service.get_job(job.job_id)

        assert completed is not None
        assert completed.state == JobState.COMPLETED
        assert second_provider.texts == ["This is sentence two."]

    asyncio.run(exercise())


def test_resume_pending_job_restarts_when_synthesis_signature_changes(tmp_path: Path) -> None:
    class VersionedProvider(MockSpeechProvider):
        def __init__(self, signature: str) -> None:
            self.signature = signature
            self.texts: list[str] = []

        async def synthesize(self, text: str, voice_id: str) -> bytes:
            self.texts.append(text)
            return await super().synthesize(text, voice_id)

        async def synthesis_signature(self, voice_id: str) -> str:
            return f"{self.signature}:{voice_id}"

    async def exercise() -> None:
        first_provider = VersionedProvider("v2")
        first_service = JobService(first_provider, tmp_path, 1_000)
        job, _ = await first_service.create_job(make_request(), "switch-model")
        segments = tmp_path / job.job_id / "segments"
        segments.mkdir()
        (segments / "000000.wav").write_bytes(
            await first_provider.synthesize("Hello.", "reader")
        )
        await first_service._update(
            job.job_id,
            lambda current: setattr(current, "synthesis_signature", "v2:reader"),
        )

        proplus_provider = VersionedProvider("v2ProPlus")
        resumed_service = JobService(proplus_provider, tmp_path, 1_000)
        await resumed_service.resume_pending_jobs()
        await asyncio.gather(*resumed_service._tasks)
        completed = await resumed_service.get_job(job.job_id)

        assert completed is not None
        assert completed.state == JobState.COMPLETED
        assert completed.synthesis_signature == "v2ProPlus:reader"
        assert proplus_provider.texts == ["Hello.", "This is sentence two."]

    asyncio.run(exercise())


def test_ack_removes_completed_artifacts(tmp_path: Path) -> None:
    async def exercise() -> None:
        service = JobService(MockSpeechProvider(), tmp_path, 1_000)
        job, _ = await service.create_job(make_request(), "ack-me")
        await service.process(job.job_id)
        assert await service.acknowledge(job.job_id)
        assert await service.get_job(job.job_id) is None
        assert not (tmp_path / job.job_id).exists()

    asyncio.run(exercise())


def test_cancel_removes_queued_job_before_processing(tmp_path: Path) -> None:
    async def exercise() -> None:
        service = JobService(MockSpeechProvider(), tmp_path, 1_000)
        job, _ = await service.create_job(make_request(), "cancel-me")
        assert await service.cancel(job.job_id)
        assert await service.get_job(job.job_id) is None
        assert not (tmp_path / job.job_id).exists()

    asyncio.run(exercise())


def test_provider_failure_is_persisted_for_status_queries(tmp_path: Path) -> None:
    async def exercise() -> None:
        service = JobService(FailingProvider(), tmp_path, 1_000)
        job, _ = await service.create_job(make_request(), "failed-job")
        await service.process(job.job_id)
        failed = await service.get_job(job.job_id)

        assert failed is not None
        assert failed.state == JobState.FAILED
        assert failed.error == "provider failed"
        assert not service.audio_path(job.job_id).exists()

    asyncio.run(exercise())
