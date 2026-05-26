import asyncio
import hashlib
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from .audio import WavFileAssembler
from .models import (
    ChapterManifest,
    CreateJobRequest,
    JobRecord,
    JobState,
    ManifestSentence,
    PersistedJobState,
)
from .providers import SpeechProvider


class ChapterTooLargeError(ValueError):
    pass


class IdempotencyConflictError(ValueError):
    pass


class JobService:
    _STATE_FILENAME = "job.json"
    _REQUEST_FILENAME = "request.json"
    _MANIFEST_FILENAME = "manifest.json"
    _CLEANUP_INTERVAL = timedelta(minutes=5)

    def __init__(
        self,
        provider: SpeechProvider,
        storage_dir: Path,
        max_chapter_characters: int,
        job_retention_days: int = 7,
    ) -> None:
        self._provider = provider
        self._storage_dir = storage_dir
        self._max_chapter_characters = max_chapter_characters
        self._retention = timedelta(days=job_retention_days)
        self._jobs: dict[str, JobRecord] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._processing_lock = asyncio.Lock()
        self._next_cleanup_at = datetime.min.replace(tzinfo=UTC)
        self._storage_dir.mkdir(parents=True, exist_ok=True)

    async def create_job(
        self,
        request: CreateJobRequest,
        idempotency_key: str,
    ) -> tuple[JobRecord, bool]:
        self._cleanup_expired_jobs()
        character_count = sum(len(sentence.text) for sentence in request.sentences)
        if character_count > self._max_chapter_characters:
            raise ChapterTooLargeError(
                f"Chapter has {character_count} characters; maximum is "
                f"{self._max_chapter_characters}."
            )

        async with self._lock:
            existing = self._find_by_idempotency_key(idempotency_key)
            if existing is not None:
                if existing.request != request:
                    raise IdempotencyConflictError(
                        "Idempotency-Key was already used for a different job request."
                    )
                return existing, False

            now = datetime.now(UTC)
            job = JobRecord(
                job_id=str(uuid4()),
                idempotency_key=idempotency_key,
                state=JobState.QUEUED,
                request=request,
                created_at=now,
                updated_at=now,
            )
            job_dir = self._job_dir_required(job.job_id)
            job_dir.mkdir(parents=True, exist_ok=True)
            self._write_json(
                job_dir / self._REQUEST_FILENAME,
                request.model_dump_json(indent=2),
            )
            self._persist(job)
            self._jobs[job.job_id] = job
        return job, True

    async def resume_pending_jobs(self) -> None:
        self._cleanup_expired_jobs(force=True)
        for job_dir in self._storage_dir.iterdir():
            record = self._load(job_dir.name)
            if record is not None and record.state in {
                JobState.QUEUED,
                JobState.PROCESSING,
            }:
                self.schedule(record.job_id)

    def schedule(self, job_id: str) -> None:
        task = asyncio.create_task(self.process(job_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def process(self, job_id: str) -> None:
        async with self._processing_lock:
            record = await self.get_job(job_id)
            if record is None or record.state in {
                JobState.COMPLETED,
                JobState.FAILED,
                JobState.CANCELLED,
            }:
                return
            await self._process(record)

    async def _process(self, job: JobRecord) -> None:
        job_dir = self._job_dir_required(job.job_id)
        segments_dir = job_dir / "segments"
        partial_audio_path = job_dir / "audio.partial.wav"
        audio_path = job_dir / "audio.wav"
        segments_dir.mkdir(parents=True, exist_ok=True)
        try:
            current_signature = await self._provider.synthesis_signature(job.request.voice_id)
            if job.synthesis_signature is None:
                await asyncio.to_thread(
                    self._discard_generated_artifacts,
                    job_dir,
                    segments_dir,
                )
                await self._update(
                    job.job_id,
                    lambda current: self._reset_synthesis(
                        current,
                        current_signature,
                    ),
                )
            elif current_signature != job.synthesis_signature:
                await asyncio.to_thread(
                    self._discard_generated_artifacts,
                    job_dir,
                    segments_dir,
                )
                await self._update(
                    job.job_id,
                    lambda current: self._reset_synthesis(
                        current,
                        current_signature,
                    ),
                )
            await self._update(job.job_id, lambda current: setattr(current, "state", JobState.PROCESSING))
            for index, sentence in enumerate(job.request.sentences):
                if await self._is_cancelled(job.job_id):
                    await self._purge(job.job_id)
                    return
                segment_path = segments_dir / f"{index:06d}.wav"
                if not segment_path.exists():
                    audio = await self._provider.synthesize(sentence.text, job.request.voice_id)
                    if await self._is_cancelled(job.job_id):
                        await self._purge(job.job_id)
                        return
                    temporary_path = segment_path.with_suffix(".wav.tmp")
                    temporary_path.write_bytes(audio)
                    temporary_path.replace(segment_path)
                await self._update(
                    job.job_id,
                    lambda current, completed=index + 1: setattr(
                        current, "sentences_completed", completed
                    ),
                )

            if await self._is_cancelled(job.job_id):
                await self._purge(job.job_id)
                return
            size_bytes, digest = await asyncio.to_thread(
                self._publish_artifacts,
                job,
                segments_dir,
                partial_audio_path,
                audio_path,
            )
            if await self._is_cancelled(job.job_id):
                await self._purge(job.job_id)
                return
            await self._update(
                job.job_id,
                lambda current: self._mark_completed(
                    current,
                    size_bytes,
                    digest,
                ),
            )
            await asyncio.to_thread(shutil.rmtree, segments_dir, True)
        except Exception as exc:
            partial_audio_path.unlink(missing_ok=True)
            await self._update(
                job.job_id,
                lambda current: self._mark_failed(current, str(exc)),
            )

    async def get_job(self, job_id: str) -> JobRecord | None:
        self._cleanup_expired_jobs()
        async with self._lock:
            record = self._jobs.get(job_id)
        if record is not None:
            return record
        record = self._load(job_id)
        if record is not None:
            async with self._lock:
                self._jobs.setdefault(job_id, record)
        return record

    async def acknowledge(self, job_id: str) -> bool:
        record = await self.get_job(job_id)
        if record is None or record.state != JobState.COMPLETED:
            return False
        await self._purge(job_id)
        return True

    async def cancel(self, job_id: str) -> bool:
        record = await self.get_job(job_id)
        if record is None:
            return False
        if record.state == JobState.COMPLETED:
            await self._purge(job_id)
            return True
        if record.state == JobState.PROCESSING:
            await self._update(job_id, lambda current: setattr(current, "state", JobState.CANCELLED))
        else:
            await self._purge(job_id)
        return True

    def audio_path(self, job_id: str) -> Path:
        return self._job_dir_required(job_id) / "audio.wav"

    def manifest_path(self, job_id: str) -> Path:
        return self._job_dir_required(job_id) / self._MANIFEST_FILENAME

    async def provider_available(self) -> bool:
        return await self._provider.is_available()

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._provider.close()

    async def _is_cancelled(self, job_id: str) -> bool:
        record = await self.get_job(job_id)
        return record is None or record.state == JobState.CANCELLED

    async def _update(
        self,
        job_id: str,
        change: Callable[[JobRecord], None],
    ) -> JobRecord:
        async with self._lock:
            record = self._jobs[job_id]
            change(record)
            record.updated_at = datetime.now(UTC)
            snapshot = record.model_copy(deep=True)
        self._persist(snapshot)
        return snapshot

    @staticmethod
    def _mark_completed(record: JobRecord, size_bytes: int, sha256: str) -> None:
        record.state = JobState.COMPLETED
        record.audio_size_bytes = size_bytes
        record.audio_sha256 = sha256
        record.error = None

    @staticmethod
    def _reset_synthesis(record: JobRecord, signature: str) -> None:
        record.synthesis_signature = signature
        record.sentences_completed = 0
        record.audio_size_bytes = None
        record.audio_sha256 = None
        record.error = None

    @staticmethod
    def _mark_failed(record: JobRecord, error: str) -> None:
        record.state = JobState.FAILED
        record.error = error

    def _find_by_idempotency_key(self, key: str) -> JobRecord | None:
        for record in self._jobs.values():
            if record.idempotency_key == key:
                return record
        for job_dir in self._storage_dir.iterdir():
            record = self._load(job_dir.name)
            if record is not None and record.idempotency_key == key:
                return record
        return None

    def _load(self, job_id: str) -> JobRecord | None:
        job_dir = self._job_dir(job_id)
        if job_dir is None:
            return None
        state_path = job_dir / self._STATE_FILENAME
        request_path = job_dir / self._REQUEST_FILENAME
        if not state_path.exists() or not request_path.exists():
            return None
        try:
            state = PersistedJobState.model_validate_json(
                state_path.read_text(encoding="utf-8")
            )
            request = CreateJobRequest.model_validate_json(
                request_path.read_text(encoding="utf-8")
            )
            return JobRecord(**state.model_dump(), request=request)
        except (OSError, ValueError):
            return None

    def _load_state(self, job_dir: Path) -> PersistedJobState | None:
        state_path = job_dir / self._STATE_FILENAME
        if not state_path.exists():
            return None
        try:
            return PersistedJobState.model_validate_json(
                state_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None

    def _persist(self, record: JobRecord) -> None:
        state = PersistedJobState.model_validate(
            record.model_dump(exclude={"request"})
        )
        self._write_json(
            self._job_dir_required(record.job_id) / self._STATE_FILENAME,
            state.model_dump_json(indent=2),
        )

    def _publish_artifacts(
        self,
        job: JobRecord,
        segments_dir: Path,
        partial_audio_path: Path,
        audio_path: Path,
    ) -> tuple[int, str]:
        with WavFileAssembler(
            partial_audio_path,
            sentence_gap_ms=job.request.sentence_gap_ms,
        ) as assembler:
            for index in range(len(job.request.sentences)):
                assembler.append((segments_dir / f"{index:06d}.wav").read_bytes())
            assembly = assembler.result()
        partial_audio_path.replace(audio_path)

        manifest = ChapterManifest(
            chapter_id=job.request.chapter_id,
            voice_id=job.request.voice_id,
            duration_ms=assembly.duration_ms,
            sentence_gap_ms=job.request.sentence_gap_ms,
            sentences=[
                ManifestSentence(
                    id=sentence.id,
                    paragraph_index=sentence.paragraph_index,
                    begin_ms=begin_ms,
                    end_ms=end_ms,
                )
                for sentence, (begin_ms, end_ms) in zip(
                    job.request.sentences,
                    assembly.timestamps_ms,
                    strict=True,
                )
            ],
        )
        self._write_json(
            self._job_dir_required(job.job_id) / self._MANIFEST_FILENAME,
            manifest.model_dump_json(indent=2),
        )
        return audio_path.stat().st_size, self._sha256(audio_path)

    def _discard_generated_artifacts(self, job_dir: Path, segments_dir: Path) -> None:
        shutil.rmtree(segments_dir, ignore_errors=True)
        segments_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "audio.partial.wav").unlink(missing_ok=True)
        (job_dir / "audio.wav").unlink(missing_ok=True)
        (job_dir / self._MANIFEST_FILENAME).unlink(missing_ok=True)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as audio_file:
            for block in iter(lambda: audio_file.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _write_json(path: Path, payload: str) -> None:
        temporary_path = path.with_suffix(path.suffix + ".tmp")
        temporary_path.write_text(payload, encoding="utf-8")
        temporary_path.replace(path)

    async def _purge(self, job_id: str) -> None:
        async with self._lock:
            self._jobs.pop(job_id, None)
        job_dir = self._job_dir(job_id)
        if job_dir is not None:
            await asyncio.to_thread(shutil.rmtree, job_dir, True)

    def _purge_sync(self, job_id: str) -> None:
        job_dir = self._job_dir(job_id)
        if job_dir is not None:
            shutil.rmtree(job_dir, ignore_errors=True)
        self._jobs.pop(job_id, None)

    def _cleanup_expired_jobs(self, *, force: bool = False) -> None:
        now = datetime.now(UTC)
        if not force and now < self._next_cleanup_at:
            return
        self._next_cleanup_at = now + self._CLEANUP_INTERVAL
        cutoff = now - self._retention
        for job_dir in self._storage_dir.iterdir():
            state = self._load_state(job_dir)
            if state is None:
                if (
                    datetime.fromtimestamp(job_dir.stat().st_mtime, tz=UTC)
                    < cutoff
                ):
                    shutil.rmtree(job_dir, ignore_errors=True)
                continue
            if (
                state.state in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
                and state.updated_at < cutoff
            ):
                self._purge_sync(state.job_id)

    def _job_dir(self, job_id: str) -> Path | None:
        try:
            if str(UUID(job_id)) != job_id:
                return None
        except ValueError:
            return None
        return self._storage_dir / job_id

    def _job_dir_required(self, job_id: str) -> Path:
        job_dir = self._job_dir(job_id)
        if job_dir is None:
            raise ValueError("Invalid job identifier.")
        return job_dir
