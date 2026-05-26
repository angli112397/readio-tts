import asyncio
import json
from pathlib import Path

import readio_tts.jobs as jobs_module
from readio_tts.jobs import JobManager, JobWorker
from readio_tts.models import CreateJobRequest, JobState, SentenceRequest
from readio_tts.providers import MockSpeechProvider
from readio_tts.repository import JobRepository


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


def make_manager(tmp_path: Path) -> JobManager:
    reference = tmp_path / "references" / "reader"
    reference.mkdir(parents=True, exist_ok=True)
    (reference / "voice.wav").write_bytes(b"reference-audio")
    (reference / "voice.lab").write_text("prompt", encoding="utf-8")
    return JobManager(
        JobRepository(tmp_path / "readio.sqlite3"),
        tmp_path / "jobs",
        tmp_path / "references",
        "v2ProPlus",
        1_000,
    )


def test_worker_publishes_audio_manifest_and_sqlite_progress(tmp_path: Path) -> None:
    async def exercise() -> None:
        manager = make_manager(tmp_path)
        job, created = manager.create_job(make_request(), "request-one")
        assert created
        worker = JobWorker(manager, MockSpeechProvider())
        assert await worker.run_once()
        completed = manager.get_job(job.job_id)

        assert completed is not None
        assert completed.state == JobState.SUCCEEDED
        assert completed.completed_sentences == 2
        assert completed.audio_size_bytes
        assert completed.audio_sha256
        assert manager.files(job.job_id).audio.exists()
        manifest = json.loads(manager.files(job.job_id).manifest.read_text(encoding="utf-8"))
        assert manifest["sentences"][1]["begin_ms"] - manifest["sentences"][0]["end_ms"] == 400
        assert not manager.files(job.job_id).segments.exists()

    asyncio.run(exercise())


def test_create_snapshots_selected_reference_for_a_job(tmp_path: Path) -> None:
    reference = tmp_path / "references" / "reader"
    reference.mkdir(parents=True)
    (reference / "voice.wav").write_bytes(b"reference-audio")
    (reference / "voice.lab").write_text("prompt", encoding="utf-8")
    manager = JobManager(
        JobRepository(tmp_path / "readio.sqlite3"),
        tmp_path / "jobs",
        tmp_path / "references",
        "v2ProPlus",
        1_000,
    )

    job, _ = manager.create_job(make_request(), "snapshot")

    assert (manager.files(job.job_id).input / "reference.wav").read_bytes() == b"reference-audio"
    assert (manager.files(job.job_id).input / "reference.lab").read_text(encoding="utf-8") == "prompt"


def test_missing_reference_does_not_leave_an_orphan_job_directory(tmp_path: Path) -> None:
    manager = JobManager(
        JobRepository(tmp_path / "readio.sqlite3"),
        tmp_path / "jobs",
        tmp_path / "references",
        "v2ProPlus",
        1_000,
    )

    try:
        manager.create_job(make_request(), "missing-reference")
    except ValueError:
        pass
    else:
        raise AssertionError("Expected missing voice reference to be rejected.")

    assert list((tmp_path / "jobs").iterdir()) == []


def test_rejects_oversized_chapter(tmp_path: Path) -> None:
    manager = JobManager(
        JobRepository(tmp_path / "readio.sqlite3"),
        tmp_path / "jobs",
        tmp_path / "references",
        "v2ProPlus",
        3,
    )
    request = CreateJobRequest(
        chapter_id="large",
        voice_id="reader",
        sentences=[SentenceRequest(id="s1", text="abcd")],
    )

    try:
        manager.create_job(request, "too-large")
    except ValueError as exc:
        assert "maximum is 3" in str(exc)
    else:
        raise AssertionError("Expected oversized request to be rejected.")


def test_worker_restart_reuses_existing_sentence_checkpoint(tmp_path: Path) -> None:
    class CountingProvider(MockSpeechProvider):
        def __init__(self) -> None:
            self.texts: list[str] = []

        async def synthesize(self, text: str, job_id: str) -> bytes:
            self.texts.append(text)
            return await super().synthesize(text, job_id)

    async def exercise() -> None:
        manager = make_manager(tmp_path)
        job, _ = manager.create_job(make_request(), "resume-me")
        provider = CountingProvider()
        files = manager.files(job.job_id)
        (files.segments / "000000.wav").write_bytes(
            await provider.synthesize("Hello.", job.job_id)
        )
        record = manager.get_job(job.job_id)
        assert record is not None
        record.state = JobState.RUNNING
        record.completed_sentences = 1
        manager.repository.save(record)

        resumed_provider = CountingProvider()
        worker = JobWorker(manager, resumed_provider)
        assert await worker.run_once()
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

        async def synthesize(self, text: str, job_id: str) -> bytes:
            self.calls += 1
            raise RuntimeError("provider failed")

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
        assert failed.error == "provider failed"
        assert provider.calls == 2

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
