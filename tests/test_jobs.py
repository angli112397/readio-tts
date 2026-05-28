import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest
import readio_tts.jobs as jobs_module
from readio_tts.audio import frames_to_ms, read_wav_segment
from readio_tts.jobs import IdempotencyConflictError, JobManager, JobWorker
from readio_tts.models import (
    CreateJobRequest,
    JobSentenceRecord,
    JobState,
    SentenceRequest,
    VoiceRecord,
)
from readio_tts.providers import MockSpeechProvider, SynthesisError
from readio_tts.repository import JobRepository, VoiceRepository
from readio_tts.voices import VoiceManager, VoiceUnavailableError


def make_request() -> CreateJobRequest:
    return CreateJobRequest(
        chapter_id="chapter-1",
        voice_id="reader",
        text_language="en",
        sentence_gap_ms=400,
        sentences=[
            SentenceRequest(id="s1", text="Hello."),
            SentenceRequest(id="s2", text="This is sentence two."),
        ],
    )


def make_manager(tmp_path: Path) -> JobManager:
    database = tmp_path / "readio.sqlite3"
    voices = VoiceManager(VoiceRepository(database), tmp_path / "voices")
    install_voice(voices)
    return JobManager(
        JobRepository(database),
        tmp_path / "jobs",
        voices,
        "v2ProPlus",
        1_000,
    )


def install_voice(voices: VoiceManager) -> None:
    voices.repository.create(
        VoiceRecord(
            voice_id="reader",
            display_name="Reader",
            reference_language="en",
            transcript="prompt",
            duration_ms=500,
            audio_size_bytes=15,
            audio_sha256="a" * 64,
            created_at=datetime.now(UTC),
        )
    )
    (voices.voices_dir / "reader").mkdir()
    (voices.voices_dir / "reader" / "reference.wav").write_bytes(b"reference-audio")


def test_worker_publishes_audio_manifest_and_sqlite_progress(tmp_path: Path) -> None:
    async def exercise() -> None:
        manager = make_manager(tmp_path)
        job, created = manager.create_job(make_request(), "request-one")
        assert created
        worker = JobWorker(manager, MockSpeechProvider())
        assert await worker.run_once()
        completed = manager.get_job(job.job_id)

        assert completed is not None
        assert manager.repository.worker_last_seen_at() is not None
        assert completed.state == JobState.SUCCEEDED
        assert completed.completed_sentences == 2
        assert completed.audio_size_bytes
        assert completed.audio_sha256
        assert manager.files(job.job_id).audio.exists()
        assert not manager.files(job.job_id).partial_audio.exists()
        manifest = json.loads(manager.files(job.job_id).manifest.read_text(encoding="utf-8"))
        assert manifest["sentences"][1]["begin_ms"] - manifest["sentences"][0]["end_ms"] == 400

    asyncio.run(exercise())


def test_create_snapshots_selected_reference_for_a_job(tmp_path: Path) -> None:
    database = tmp_path / "readio.sqlite3"
    voices = VoiceManager(VoiceRepository(database), tmp_path / "voices")
    install_voice(voices)
    manager = JobManager(
        JobRepository(database),
        tmp_path / "jobs",
        voices,
        "v2ProPlus",
        1_000,
    )

    job, _ = manager.create_job(make_request(), "snapshot")

    assert (manager.files(job.job_id).snapshot / "reference.wav").read_bytes() == b"reference-audio"
    assert (
        json.loads((manager.files(job.job_id).snapshot / "voice.json").read_text(encoding="utf-8"))
        == {"reference_language": "en", "transcript": "prompt"}
    )


def test_missing_reference_does_not_leave_an_orphan_job_directory(tmp_path: Path) -> None:
    database = tmp_path / "readio.sqlite3"
    manager = JobManager(
        JobRepository(database),
        tmp_path / "jobs",
        VoiceManager(VoiceRepository(database), tmp_path / "voices"),
        "v2ProPlus",
        1_000,
    )

    with pytest.raises(VoiceUnavailableError):
        manager.create_job(make_request(), "missing-reference")

    assert list((tmp_path / "jobs").iterdir()) == []


def test_voice_with_missing_audio_does_not_leave_an_orphan_job_directory(tmp_path: Path) -> None:
    database = tmp_path / "readio.sqlite3"
    voices = VoiceManager(VoiceRepository(database), tmp_path / "voices")
    voices.repository.create(
        VoiceRecord(
            voice_id="reader",
            display_name="Reader",
            reference_language="en",
            transcript="prompt",
            duration_ms=500,
            audio_size_bytes=15,
            audio_sha256="a" * 64,
            created_at=datetime.now(UTC),
        )
    )
    manager = JobManager(
        JobRepository(database),
        tmp_path / "jobs",
        voices,
        "v2ProPlus",
        1_000,
    )

    with pytest.raises(VoiceUnavailableError, match="audio is missing"):
        manager.create_job(make_request(), "missing-audio")

    assert list((tmp_path / "jobs").iterdir()) == []


def test_rejects_oversized_chapter(tmp_path: Path) -> None:
    database = tmp_path / "readio.sqlite3"
    manager = JobManager(
        JobRepository(database),
        tmp_path / "jobs",
        VoiceManager(VoiceRepository(database), tmp_path / "voices"),
        "v2ProPlus",
        3,
    )
    request = CreateJobRequest(
        chapter_id="large",
        voice_id="reader",
        text_language="en",
        sentences=[SentenceRequest(id="s1", text="abcd")],
    )

    with pytest.raises(ValueError, match="maximum is 3"):
        manager.create_job(request, "too-large")


def test_concurrent_idempotent_submissions_share_one_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = make_manager(tmp_path)
    request = make_request()
    barrier = Barrier(2)
    snapshot_to = manager.voice_manager.snapshot_to

    def synchronized_snapshot(*args, **kwargs) -> None:
        snapshot_to(*args, **kwargs)
        barrier.wait(timeout=5)

    monkeypatch.setattr(manager.voice_manager, "snapshot_to", synchronized_snapshot)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(manager.create_job, request, "same-idempotency-key")
            for _ in range(2)
        ]
        results = [future.result(timeout=5) for future in futures]

    assert len({record.job_id for record, _created in results}) == 1
    assert sorted(created for _record, created in results) == [False, True]
    assert len(list(manager.jobs_dir.iterdir())) == 1


def test_concurrent_conflicting_idempotency_submissions_reject_one(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = make_manager(tmp_path)
    first_request = make_request()
    second_request = first_request.model_copy(update={"chapter_id": "different-chapter"})
    barrier = Barrier(2)
    snapshot_to = manager.voice_manager.snapshot_to

    def synchronized_snapshot(*args, **kwargs) -> None:
        snapshot_to(*args, **kwargs)
        barrier.wait(timeout=5)

    monkeypatch.setattr(manager.voice_manager, "snapshot_to", synchronized_snapshot)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(manager.create_job, request, "conflicting-key")
            for request in (first_request, second_request)
        ]

        successes = []
        failures = []
        for future in futures:
            try:
                successes.append(future.result(timeout=5))
            except IdempotencyConflictError as exc:
                failures.append(exc)

    assert len(successes) == 1
    assert len(failures) == 1
    assert len(list(manager.jobs_dir.iterdir())) == 1


def test_worker_restart_reuses_existing_sentence_checkpoint(tmp_path: Path) -> None:
    class CountingProvider(MockSpeechProvider):
        def __init__(self) -> None:
            self.texts: list[str] = []

        async def synthesize(self, text: str, job_id: str, text_language: str) -> bytes:
            self.texts.append(text)
            return await super().synthesize(text, job_id, text_language)

    async def exercise() -> None:
        manager = make_manager(tmp_path)
        job, _ = manager.create_job(make_request(), "resume-me")
        provider = CountingProvider()
        files = manager.files(job.job_id)
        first_sentence = read_wav_segment(
            await provider.synthesize("Hello.", job.job_id, "en")
        )
        files.partial_audio.write_bytes(first_sentence.frames)
        record = manager.get_job(job.job_id)
        assert record is not None
        record.state = JobState.RUNNING
        record.completed_sentences = 1
        record.committed_frames = first_sentence.frame_count
        record.audio_channels = first_sentence.format.channels
        record.audio_sample_width = first_sentence.format.sample_width
        record.audio_frame_rate = first_sentence.format.frame_rate
        manager.repository.commit_sentence(
            record,
            JobSentenceRecord(
                job_id=job.job_id,
                sentence_index=0,
                sentence_id="s1",
                paragraph_index=0,
                begin_ms=0,
                end_ms=frames_to_ms(
                    first_sentence.frame_count,
                    first_sentence.format.frame_rate,
                ),
            ),
        )

        resumed_provider = CountingProvider()
        worker = JobWorker(manager, resumed_provider)
        assert await worker.run_once()
        completed = manager.get_job(job.job_id)

        assert completed is not None
        assert completed.state == JobState.SUCCEEDED
        assert resumed_provider.texts == ["This is sentence two."]

    asyncio.run(exercise())


def test_worker_truncates_partial_audio_back_to_sql_checkpoint(tmp_path: Path) -> None:
    class CountingProvider(MockSpeechProvider):
        def __init__(self) -> None:
            self.texts: list[str] = []

        async def synthesize(self, text: str, job_id: str, text_language: str) -> bytes:
            self.texts.append(text)
            return await super().synthesize(text, job_id, text_language)

    async def exercise() -> None:
        manager = make_manager(tmp_path)
        job, _ = manager.create_job(make_request(), "truncate-partial")
        provider = CountingProvider()
        files = manager.files(job.job_id)
        first_sentence = read_wav_segment(
            await provider.synthesize("Hello.", job.job_id, "en")
        )
        files.partial_audio.write_bytes(first_sentence.frames + b"\xff" * 40)
        record = manager.get_job(job.job_id)
        assert record is not None
        record.state = JobState.RUNNING
        record.completed_sentences = 1
        record.committed_frames = first_sentence.frame_count
        record.audio_channels = first_sentence.format.channels
        record.audio_sample_width = first_sentence.format.sample_width
        record.audio_frame_rate = first_sentence.format.frame_rate
        manager.repository.commit_sentence(
            record,
            JobSentenceRecord(
                job_id=job.job_id,
                sentence_index=0,
                sentence_id="s1",
                paragraph_index=0,
                begin_ms=0,
                end_ms=frames_to_ms(
                    first_sentence.frame_count,
                    first_sentence.format.frame_rate,
                ),
            ),
        )

        resumed_provider = CountingProvider()
        await JobWorker(manager, resumed_provider).run_once()
        completed = manager.get_job(job.job_id)

        assert completed is not None
        assert completed.state == JobState.SUCCEEDED
        assert resumed_provider.texts == ["This is sentence two."]

    asyncio.run(exercise())


def test_sentence_failure_retries_once_then_marks_job_failed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class AlwaysFailingProvider(MockSpeechProvider):
        def __init__(self) -> None:
            self.calls = 0

        async def synthesize(self, text: str, job_id: str, text_language: str) -> bytes:
            self.calls += 1
            raise SynthesisError(
                "tts_unavailable",
                "GPT-SoVITS is unavailable.",
                retryable=True,
            )

    async def exercise() -> None:
        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr(jobs_module.asyncio, "sleep", no_sleep)
        manager = make_manager(tmp_path)
        job, _ = manager.create_job(make_request(), "failed-job")
        provider = AlwaysFailingProvider()
        worker = JobWorker(manager, provider)
        await worker.run_once()
        failed = manager.get_job(job.job_id)

        assert failed is not None
        assert failed.state == JobState.FAILED
        assert failed.error is not None
        assert failed.error.code == "tts_unavailable"
        assert failed.error.message == "GPT-SoVITS is unavailable."
        assert failed.error.sentence_id == "s1"
        assert provider.calls == 2

    asyncio.run(exercise())


def test_non_retryable_sentence_failure_is_recorded_without_retry(tmp_path: Path) -> None:
    class RejectedProvider(MockSpeechProvider):
        def __init__(self) -> None:
            self.calls = 0

        async def synthesize(self, text: str, job_id: str, text_language: str) -> bytes:
            self.calls += 1
            raise SynthesisError(
                "tts_request_rejected",
                "The speech engine rejected this sentence.",
            )

    async def exercise() -> None:
        manager = make_manager(tmp_path)
        job, _ = manager.create_job(make_request(), "rejected-job")
        provider = RejectedProvider()
        await JobWorker(manager, provider).run_once()
        failed = manager.get_job(job.job_id)

        assert failed is not None
        assert failed.error is not None
        assert failed.error.code == "tts_request_rejected"
        assert failed.error.sentence_id == "s1"
        assert provider.calls == 1

    asyncio.run(exercise())


def test_cancelling_during_retry_delay_does_not_retry_sentence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class RetryableProvider(MockSpeechProvider):
        def __init__(self) -> None:
            self.calls = 0

        async def synthesize(self, text: str, job_id: str, text_language: str) -> bytes:
            self.calls += 1
            raise SynthesisError(
                "tts_unavailable",
                "GPT-SoVITS is unavailable.",
                retryable=True,
            )

    async def exercise() -> None:
        manager = make_manager(tmp_path)
        job, _ = manager.create_job(make_request(), "cancel-during-retry")
        provider = RetryableProvider()

        async def cancel_instead_of_sleep(_seconds: float) -> None:
            manager.delete(job.job_id)

        monkeypatch.setattr(jobs_module.asyncio, "sleep", cancel_instead_of_sleep)
        await JobWorker(manager, provider).run_once()

        assert provider.calls == 1
        assert manager.get_job(job.job_id) is None

    asyncio.run(exercise())


def test_delete_succeeded_job_removes_artifacts_and_metadata(tmp_path: Path) -> None:
    async def exercise() -> None:
        manager = make_manager(tmp_path)
        job, _ = manager.create_job(make_request(), "delete-me")
        await JobWorker(manager, MockSpeechProvider()).run_once()
        manager.delete(job.job_id)

        assert manager.get_job(job.job_id) is None
        assert not manager.files(job.job_id).root.exists()

    asyncio.run(exercise())


def test_cancelling_running_job_does_not_start_the_next_sentence(tmp_path: Path) -> None:
    class BlockingProvider(MockSpeechProvider):
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.texts: list[str] = []

        async def synthesize(self, text: str, job_id: str, text_language: str) -> bytes:
            self.texts.append(text)
            self.started.set()
            await self.release.wait()
            return await super().synthesize(text, job_id, text_language)

    async def exercise() -> None:
        manager = make_manager(tmp_path)
        job, _ = manager.create_job(make_request(), "cancel-running")
        provider = BlockingProvider()
        processing = asyncio.create_task(JobWorker(manager, provider).run_once())
        await provider.started.wait()

        manager.delete(job.job_id)
        provider.release.set()
        await processing

        assert provider.texts == ["Hello."]
        assert manager.get_job(job.job_id) is None
        assert not manager.files(job.job_id).root.exists()

    asyncio.run(exercise())


def test_artifact_publication_failure_is_reported_separately(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fail_publish(*_args, **_kwargs):
        raise OSError("disk failure")

    monkeypatch.setattr(jobs_module, "_publish_artifacts", fail_publish)

    async def exercise() -> None:
        manager = make_manager(tmp_path)
        job, _ = manager.create_job(make_request(), "publication-failure")
        await JobWorker(manager, MockSpeechProvider()).run_once()
        failed = manager.get_job(job.job_id)

        assert failed is not None
        assert failed.state == JobState.FAILED
        assert failed.error is not None
        assert failed.error.code == "artifact_publication_failed"
        assert failed.error.message == "Failed to publish the generated audio artifact."
        assert failed.error.sentence_id is None

    asyncio.run(exercise())
