import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from readio_tts.jobs import ChapterJobService
from readio_tts.models import CreateChapterJobRequest, JobStatus
from readio_tts.providers import MockSpeechProvider


def test_processes_a_chapter_into_audio_and_sentence_timestamps(tmp_path: Path) -> None:
    async def exercise() -> None:
        service = ChapterJobService(MockSpeechProvider(), tmp_path, 1000)
        request = CreateChapterJobRequest(
            sentences=["Hello.", "This is sentence two."],
        )

        job = await service.create_job(request)
        await service.process(job.job_id, request)
        completed = await service.get_job(job.job_id)

        assert completed is not None
        assert completed.status == JobStatus.COMPLETE
        assert completed.result is not None
        assert completed.text_length == len("Hello.") + len("This is sentence two.")
        assert len(completed.result.sentences) == 2
        assert completed.result.sentences[0].begin_time == 0
        assert (
            completed.result.sentences[0].end_time
            == completed.result.sentences[1].begin_time
        )
        assert service.audio_path(job.job_id).exists()

    asyncio.run(exercise())


def test_rejects_oversized_chapter(tmp_path: Path) -> None:
    async def exercise() -> None:
        service = ChapterJobService(MockSpeechProvider(), tmp_path, 3)
        request = CreateChapterJobRequest(sentences=["abcd"])

        try:
            await service.create_job(request)
        except ValueError as exc:
            assert "maximum is 3" in str(exc)
        else:
            raise AssertionError("Expected oversized request to be rejected.")

    asyncio.run(exercise())


def test_applies_sentence_gap_to_timestamp_boundaries(tmp_path: Path) -> None:
    async def exercise() -> None:
        service = ChapterJobService(
            MockSpeechProvider(),
            tmp_path,
            1_000,
            sentence_gap_ms=400,
        )
        request = CreateChapterJobRequest(sentences=["One.", "Two."])

        job = await service.create_job(request)
        await service.process(job.job_id, request)
        completed = await service.get_job(job.job_id)

        assert completed is not None
        assert completed.result is not None
        first_end = completed.result.sentences[0].end_time
        second_start = completed.result.sentences[1].begin_time
        assert second_start - first_end == 400

    asyncio.run(exercise())


def test_request_sentence_gap_overrides_configured_default(tmp_path: Path) -> None:
    async def exercise() -> None:
        service = ChapterJobService(
            MockSpeechProvider(),
            tmp_path,
            1_000,
            sentence_gap_ms=600,
        )
        request = CreateChapterJobRequest(
            sentences=["One.", "Two."],
            sentence_gap_ms=250,
        )

        job = await service.create_job(request)
        await service.process(job.job_id, request)
        completed = await service.get_job(job.job_id)

        assert completed is not None
        assert completed.result is not None
        first_end = completed.result.sentences[0].end_time
        second_start = completed.result.sentences[1].begin_time
        assert second_start - first_end == 250

    asyncio.run(exercise())


def test_completed_job_is_recovered_after_service_restart(tmp_path: Path) -> None:
    async def exercise() -> None:
        first_service = ChapterJobService(
            MockSpeechProvider(),
            tmp_path,
            1_000,
            sentence_gap_ms=600,
        )
        request = CreateChapterJobRequest(sentences=["One.", "Two."])
        job = await first_service.create_job(request)
        await first_service.process(job.job_id, request)

        restarted_service = ChapterJobService(MockSpeechProvider(), tmp_path, 1_000)
        recovered = await restarted_service.get_job(job.job_id)

        assert recovered is not None
        assert recovered.status == JobStatus.COMPLETE
        assert recovered.result is not None
        assert (
            recovered.result.sentences[1].begin_time
            - recovered.result.sentences[0].end_time
            == 600
        )
        assert restarted_service.audio_path(job.job_id).exists()

    asyncio.run(exercise())


def test_interrupted_job_is_reported_failed_after_service_restart(tmp_path: Path) -> None:
    async def exercise() -> None:
        first_service = ChapterJobService(MockSpeechProvider(), tmp_path, 1_000)
        job = await first_service.create_job(
            CreateChapterJobRequest(sentences=["Not processed yet."])
        )

        restarted_service = ChapterJobService(MockSpeechProvider(), tmp_path, 1_000)
        recovered = await restarted_service.get_job(job.job_id)

        assert recovered is not None
        assert recovered.status == JobStatus.FAILED
        assert recovered.error == "Gateway restarted before chapter synthesis completed."

    asyncio.run(exercise())


def test_new_submission_marks_unpolled_interrupted_task_failed(tmp_path: Path) -> None:
    async def exercise() -> None:
        first_service = ChapterJobService(MockSpeechProvider(), tmp_path, 1_000)
        interrupted = await first_service.create_job(
            CreateChapterJobRequest(sentences=["Interrupted."])
        )

        restarted_service = ChapterJobService(MockSpeechProvider(), tmp_path, 1_000)
        await restarted_service.create_job(CreateChapterJobRequest(sentences=["New task."]))
        recovered = await restarted_service.get_job(interrupted.job_id)

        assert recovered is not None
        assert recovered.status == JobStatus.FAILED

    asyncio.run(exercise())


def test_expired_unreadable_job_directory_is_cleaned_up(tmp_path: Path) -> None:
    async def exercise() -> None:
        stale_dir = tmp_path / "unreadable-job"
        stale_dir.mkdir()
        state_path = stale_dir / "job.json"
        state_path.write_text("{old-result-format}", encoding="utf-8")
        expired = (datetime.now(UTC) - timedelta(days=8)).timestamp()
        os.utime(state_path, (expired, expired))

        service = ChapterJobService(MockSpeechProvider(), tmp_path, 1_000)
        await service.create_job(CreateChapterJobRequest(sentences=["Fresh."]))

        assert not stale_dir.exists()

    asyncio.run(exercise())


def test_persisted_result_uses_sentence_timing_objects_only(tmp_path: Path) -> None:
    async def exercise() -> None:
        service = ChapterJobService(MockSpeechProvider(), tmp_path, 1_000)
        request = CreateChapterJobRequest(sentences=["One.", "Two."])
        job = await service.create_job(request)
        await service.process(job.job_id, request)

        payload = json.loads((tmp_path / job.job_id / "result.json").read_text(encoding="utf-8"))

        assert "timestamps_ms" not in payload
        assert set(payload) == {"sentences"}
        assert payload["sentences"][0]["begin_time"] == 0
        assert "end_time" in payload["sentences"][0]

    asyncio.run(exercise())
