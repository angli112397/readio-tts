import asyncio
import hashlib
import logging
import os
import sqlite3
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from .audio import (
    AudioFormat,
    frames_to_ms,
    raw_byte_count,
    read_wav_segment,
    silence_frames,
    write_wav_from_raw,
)
from .models import (
    ChapterManifest,
    CreateJobRequest,
    ErrorInfo,
    JobRecord,
    JobSentenceRecord,
    JobState,
    LanguageCode,
    ManifestSentence,
)
from .providers import (
    SpeechProvider,
    SynthesisError,
)
from .repository import JobRepository
from .voices import VoiceManager


logger = logging.getLogger(__name__)


class ChapterTooLargeError(ValueError):
    pass


class IdempotencyConflictError(ValueError):
    pass


class _JobCancelled(Exception):
    """Sentinel raised when cancellation is detected mid-process."""


class JobFiles:
    def __init__(self, jobs_dir: Path, job_id: str) -> None:
        self.root = jobs_dir / job_id
        self.snapshot = self.root / "snapshot"
        self.request = self.root / "request.json"
        self.partial_audio = self.root / "audio.partial.raw"
        self.audio = self.root / "audio.wav"
        self.manifest = self.root / "manifest.json"


class JobManager:
    _CLEANUP_INTERVAL = timedelta(minutes=5)

    def __init__(
        self,
        repository: JobRepository,
        jobs_dir: Path,
        voice_manager: VoiceManager,
        model_revision: str,
        max_chapter_characters: int,
        job_retention_days: int = 7,
    ) -> None:
        self.repository = repository
        self.jobs_dir = jobs_dir
        self.voice_manager = voice_manager
        self.model_revision = model_revision
        self.max_chapter_characters = max_chapter_characters
        self.retention = timedelta(days=job_retention_days)
        self._next_cleanup_at = datetime.min.replace(tzinfo=UTC)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def create_job(
        self,
        request: CreateJobRequest,
        idempotency_key: str,
    ) -> tuple[JobRecord, bool]:
        self.cleanup_expired()
        character_count = sum(len(sentence.text) for sentence in request.sentences)
        if character_count > self.max_chapter_characters:
            raise ChapterTooLargeError(
                f"Chapter has {character_count} characters; maximum is "
                f"{self.max_chapter_characters}."
            )

        existing = self.repository.find_by_idempotency_key(idempotency_key)
        if existing is not None:
            if self.load_request(existing.job_id) != request:
                raise IdempotencyConflictError(
                    "Idempotency-Key was already used for a different job request."
                )
            return existing, False

        now = datetime.now(UTC)
        record = JobRecord(
            job_id=str(uuid4()),
            idempotency_key=idempotency_key,
            chapter_id=request.chapter_id,
            voice_id=request.voice_id,
            model_revision=self.model_revision,
            state=JobState.QUEUED,
            total_sentences=len(request.sentences),
            created_at=now,
            updated_at=now,
        )
        files = self.files(record.job_id)
        try:
            files.snapshot.mkdir(parents=True)
            files.request.write_text(request.model_dump_json(indent=2), encoding="utf-8")
            self.voice_manager.snapshot_to(request.voice_id, files.snapshot)
            self.repository.create(record)
        # Race condition: another request inserted the same idempotency key between our lookup and INSERT.
        except sqlite3.IntegrityError as exc:
            shutil.rmtree(files.root, ignore_errors=True)
            existing = self.repository.find_by_idempotency_key(idempotency_key)
            if existing is None:
                raise
            if self.load_request(existing.job_id) != request:
                raise IdempotencyConflictError(
                    "Idempotency-Key was already used for a different job request."
                ) from exc
            return existing, False
        except Exception:
            shutil.rmtree(files.root, ignore_errors=True)
            raise
        return record, True

    def get_job(self, job_id: str) -> JobRecord | None:
        self.cleanup_expired()
        if not _valid_job_id(job_id):
            return None
        return self.repository.get(job_id)

    def load_request(self, job_id: str) -> CreateJobRequest:
        return CreateJobRequest.model_validate_json(
            self.files(job_id).request.read_text(encoding="utf-8")
        )

    def delete(self, job_id: str) -> None:
        if not _valid_job_id(job_id):
            return
        self.purge(job_id)

    def purge(self, job_id: str) -> None:
        shutil.rmtree(self.files(job_id).root, ignore_errors=True)
        self.repository.delete(job_id)

    def files(self, job_id: str) -> JobFiles:
        if not _valid_job_id(job_id):
            raise ValueError("Invalid job identifier.")
        return JobFiles(self.jobs_dir, job_id)

    def cleanup_expired(self, *, force: bool = False) -> None:
        now = datetime.now(UTC)
        if not force and now < self._next_cleanup_at:
            return
        self._next_cleanup_at = now + self._CLEANUP_INTERVAL
        for job_id in self.repository.delete_expired_terminal_jobs(self.retention):
            shutil.rmtree(self.files(job_id).root, ignore_errors=True)


class JobWorker:
    def __init__(
        self,
        manager: JobManager,
        provider: SpeechProvider,
        *,
        poll_seconds: float = 1.0,
        heartbeat_seconds: float = 10.0,
    ) -> None:
        self.manager = manager
        self.provider = provider
        self.poll_seconds = poll_seconds
        self.heartbeat_seconds = heartbeat_seconds

    async def run_forever(self) -> None:
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        try:
            while True:
                if not await self.run_once():
                    await asyncio.sleep(self.poll_seconds)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await self.provider.close()

    async def run_once(self) -> bool:
        self.manager.repository.touch_worker()
        record = self.manager.repository.next_pending()
        if record is None:
            return False
        await self.process(record)
        return True

    async def _heartbeat_loop(self) -> None:
        while True:
            self.manager.repository.touch_worker()
            await asyncio.sleep(self.heartbeat_seconds)

    async def process(self, record: JobRecord) -> None:
        files = self.manager.files(record.job_id)
        artifact_phase = False
        active_sentence_id: str | None = None

        try:
            request = self.manager.load_request(record.job_id)

            if self._cancelled(record.job_id):
                self._purge_cancelled(record.job_id)
                return

            _truncate_partial_audio(record, files)

            completed = record.completed_sentences
            record.state = JobState.RUNNING
            record.error = None

            self._touch(record)
            logger.info(
                "Job processing started: job_id=%s sentences_completed=%s sentences_total=%s",
                record.job_id,
                record.completed_sentences,
                record.total_sentences,
            )

            for index in range(completed, record.total_sentences):

                if self._cancelled(record.job_id):
                    self._purge_cancelled(record.job_id)
                    return

                sentence = request.sentences[index]
                active_sentence_id = sentence.id
                audio = await self._synthesize_with_retry(
                    sentence.text,
                    record.job_id,
                    request.text_language,
                )

                if self._cancelled(record.job_id):
                    self._purge_cancelled(record.job_id)
                    return

                target_format = _audio_format(record)
                pcm = read_wav_segment(audio, target_format)
                if target_format is None:
                    record.audio_channels = pcm.format.channels
                    record.audio_sample_width = pcm.format.sample_width
                    record.audio_frame_rate = pcm.format.frame_rate
                    target_format = pcm.format
                assert target_format is not None

                duration_ms = frames_to_ms(pcm.frame_count, target_format.frame_rate)
                if len(sentence.text) > 20 and duration_ms < 200:
                    logger.warning(
                        "Synthesis produced suspiciously short audio: job_id=%s sentence_id=%s "
                        "text_preview=%r duration_ms=%s",
                        record.job_id,
                        sentence.id,
                        sentence.text[:60],
                        duration_ms,
                    )

                if index > 0 and request.sentence_gap_ms:
                    gap = silence_frames(target_format, request.sentence_gap_ms)
                else:
                    gap = b""

                start_frame = record.committed_frames + _frame_count(gap, target_format)
                end_frame = start_frame + pcm.frame_count
                with files.partial_audio.open("ab") as partial:
                    partial.write(gap)
                    partial.write(pcm.frames)
                    partial.flush()
                    os.fsync(partial.fileno())

                record.completed_sentences = index + 1
                record.committed_frames = end_frame

                self._prepare_touch(record)
                committed = self.manager.repository.commit_sentence(
                    record,
                    JobSentenceRecord(
                        job_id=record.job_id,
                        sentence_index=index,
                        sentence_id=sentence.id,
                        paragraph_index=sentence.paragraph_index,
                        begin_ms=frames_to_ms(start_frame, target_format.frame_rate),
                        end_ms=frames_to_ms(end_frame, target_format.frame_rate),
                    ),
                )
                if not committed:
                    raise _JobCancelled

            artifact_phase = True
            size_bytes, digest = await asyncio.to_thread(
                _publish_artifacts,
                files,
                request,
                record,
                self.manager.repository.list_sentences(record.job_id),
            )

            record.state = JobState.SUCCEEDED
            record.audio_size_bytes = size_bytes
            record.audio_sha256 = digest
            self._touch(record)
            files.partial_audio.unlink(missing_ok=True)
            logger.info(
                "Job succeeded: job_id=%s sentences_total=%s audio_size_bytes=%s",
                record.job_id,
                record.total_sentences,
                record.audio_size_bytes,
            )
        except _JobCancelled:
            self._purge_cancelled(record.job_id)
        except SynthesisError as exc:
            files.partial_audio.unlink(missing_ok=True)
            record.state = JobState.FAILED
            record.error = ErrorInfo(
                code=exc.code,
                message=exc.message,
                sentence_id=active_sentence_id,
            )
            self._touch(record)
            logger.warning(
                "Job failed: job_id=%s error_code=%s sentence_id=%s",
                record.job_id,
                record.error.code,
                record.error.sentence_id,
            )
        except Exception:
            files.partial_audio.unlink(missing_ok=True)
            logger.exception("Job failed unexpectedly: job_id=%s", record.job_id)
            failed = self.manager.repository.get(record.job_id)
            if failed is None:
                return  # Job was deleted concurrently; no state to persist.
            failed.state = JobState.FAILED
            if artifact_phase:
                failed.error = ErrorInfo(
                    code="artifact_publication_failed",
                    message="Failed to publish the generated audio artifact.",
                )
            else:
                failed.error = ErrorInfo(
                    code="internal_error",
                    message="Audio generation failed unexpectedly.",
                    sentence_id=active_sentence_id,
                )
            self._touch(failed)

    async def _synthesize_with_retry(
        self,
        text: str,
        job_id: str,
        text_language: LanguageCode,
    ) -> bytes:
        try:
            return await self.provider.synthesize(text, job_id, text_language)
        except SynthesisError as exc:
            if not exc.retryable:
                raise
            await asyncio.sleep(2)
            if self._cancelled(job_id):
                raise _JobCancelled
            return await self.provider.synthesize(text, job_id, text_language)

    def _cancelled(self, job_id: str) -> bool:
        return self.manager.repository.get(job_id) is None

    def _purge_cancelled(self, job_id: str) -> None:
        logger.info("Job cancellation observed by worker: job_id=%s", job_id)
        self.manager.purge(job_id)

    def _touch(self, record: JobRecord) -> None:
        self._prepare_touch(record)
        self.manager.repository.save(record)

    @staticmethod
    def _prepare_touch(record: JobRecord) -> None:
        now = datetime.now(UTC)
        record.updated_at = now
        if record.state == JobState.RUNNING:
            record.heartbeat_at = now


def _publish_artifacts(
    files: JobFiles,
    request: CreateJobRequest,
    record: JobRecord,
    sentences: list[JobSentenceRecord],
) -> tuple[int, str]:
    audio_format = _require_audio_format(record)
    if len(sentences) != len(request.sentences):
        raise ValueError("Job sentence timing metadata is incomplete.")
    temporary = files.root / "audio.tmp.wav"
    write_wav_from_raw(files.partial_audio, temporary, audio_format, expected_frames=record.committed_frames)
    temporary.replace(files.audio)

    manifest = ChapterManifest(
        chapter_id=request.chapter_id,
        voice_id=request.voice_id,
        text_language=request.text_language,
        duration_ms=frames_to_ms(record.committed_frames, audio_format.frame_rate),
        sentence_gap_ms=request.sentence_gap_ms,
        sentences=[
            ManifestSentence(
                id=sentence.sentence_id,
                paragraph_index=sentence.paragraph_index,
                begin_ms=sentence.begin_ms,
                end_ms=sentence.end_ms,
            )
            for sentence in sentences
        ],
    )
    files.manifest.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return files.audio.stat().st_size, _sha256(files.audio)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as audio_file:
        for block in iter(lambda: audio_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _valid_job_id(job_id: str) -> bool:
    try:
        return str(UUID(job_id)) == job_id
    except ValueError:
        return False


def _audio_format(record: JobRecord) -> AudioFormat | None:
    if (
        record.audio_channels is None
        or record.audio_sample_width is None
        or record.audio_frame_rate is None
    ):
        return None
    return AudioFormat(
        channels=record.audio_channels,
        sample_width=record.audio_sample_width,
        frame_rate=record.audio_frame_rate,
        compression_type="NONE",
    )


def _require_audio_format(record: JobRecord) -> AudioFormat:
    audio_format = _audio_format(record)
    if audio_format is None:
        raise ValueError("Job audio format is missing.")
    return audio_format


def _frame_count(frames: bytes, audio_format: AudioFormat) -> int:
    frame_size = audio_format.channels * audio_format.sample_width
    if frame_size <= 0:
        raise ValueError("Invalid audio frame size.")
    return len(frames) // frame_size


def _truncate_partial_audio(record: JobRecord, files: JobFiles) -> None:
    audio_format = _audio_format(record)
    if audio_format is None:
        files.partial_audio.unlink(missing_ok=True)
        return
    expected_bytes = raw_byte_count(record.committed_frames, audio_format)
    if not files.partial_audio.exists():
        if expected_bytes:
            raise ValueError("Partial audio checkpoint is missing.")
        return
    actual_bytes = files.partial_audio.stat().st_size
    if actual_bytes < expected_bytes:
        raise ValueError("Partial audio checkpoint is shorter than recorded progress.")
    if actual_bytes > expected_bytes:
        with files.partial_audio.open("r+b") as partial:
            partial.truncate(expected_bytes)
