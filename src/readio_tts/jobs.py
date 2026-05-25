import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from .audio import WavFileAssembler
from .models import (
    AsyncTTSSentence,
    ChapterJobResponse,
    ChapterResult,
    CreateChapterJobRequest,
    JobStatus,
)
from .providers import SpeechProvider


class ChapterJobService:
    _STATE_FILENAME = "job.json"
    _RESULT_FILENAME = "result.json"

    def __init__(
        self,
        provider: SpeechProvider,
        storage_dir: Path,
        max_chapter_characters: int,
        sentence_gap_ms: int = 0,
        job_retention_days: int = 7,
    ) -> None:
        self._provider = provider
        self._storage_dir = storage_dir
        self._max_chapter_characters = max_chapter_characters
        self._sentence_gap_ms = sentence_gap_ms
        self._job_retention = timedelta(days=job_retention_days)
        self._jobs: dict[str, ChapterJobResponse] = {}
        self._lock = asyncio.Lock()
        self._processing_lock = asyncio.Lock()
        self._storage_dir.mkdir(parents=True, exist_ok=True)

    async def create_job(self, request: CreateChapterJobRequest) -> ChapterJobResponse:
        self._cleanup_expired_jobs()
        if request.reqid is not None and self._has_request_id(request.reqid):
            raise ValueError(f"Request ID has already been submitted: {request.reqid}.")
        character_count = sum(len(sentence) for sentence in request.sentences)
        if character_count > self._max_chapter_characters:
            raise ValueError(
                f"Chapter has {character_count} characters; maximum is "
                f"{self._max_chapter_characters}."
            )

        job = ChapterJobResponse(
            job_id=str(uuid4()),
            reqid=request.reqid,
            status=JobStatus.QUEUED,
            created_at=datetime.now(UTC),
            sentence_count=len(request.sentences),
            text_length=request.text_length or character_count,
        )
        job_dir = self._required_job_dir(job.job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        self._persist_job(job)
        async with self._lock:
            self._jobs[job.job_id] = job
        return job

    async def process(self, job_id: str, request: CreateChapterJobRequest) -> None:
        async with self._processing_lock:
            await self._process(job_id, request)

    async def _process(self, job_id: str, request: CreateChapterJobRequest) -> None:
        job_dir = self._required_job_dir(job_id)
        partial_audio_path = job_dir / "audio.partial.wav"
        sentence_gap_ms = (
            request.sentence_gap_ms
            if request.sentence_gap_ms is not None
            else self._sentence_gap_ms
        )
        try:
            await self._set_status(job_id, JobStatus.PROCESSING)
            job_dir.mkdir(parents=True, exist_ok=True)
            with WavFileAssembler(
                partial_audio_path,
                sentence_gap_ms=sentence_gap_ms,
            ) as assembler:
                for index, sentence in enumerate(request.sentences, start=1):
                    segment = await self._provider.synthesize(sentence, request.reference_id)
                    assembler.append(segment)
                    await self._set_progress(job_id, index)
                combined = assembler.result()

            partial_audio_path.replace(job_dir / "audio.wav")

            result = ChapterResult(
                sentences=self._build_sentence_timings(request.sentences, combined.timestamps_ms),
            )
            (job_dir / self._RESULT_FILENAME).write_text(
                result.model_dump_json(indent=2),
                encoding="utf-8",
            )
            await self._complete(job_id, result)
        except Exception as exc:
            partial_audio_path.unlink(missing_ok=True)
            await self._fail(job_id, str(exc))

    async def get_job(self, job_id: str) -> ChapterJobResponse | None:
        self._cleanup_expired_jobs()
        async with self._lock:
            job = self._jobs.get(job_id)
        if job is not None:
            return job

        loaded = self._load_job(job_id)
        if loaded is None:
            return None
        async with self._lock:
            return self._jobs.setdefault(job_id, loaded)

    def audio_path(self, job_id: str) -> Path:
        return self._required_job_dir(job_id) / "audio.wav"

    async def provider_available(self) -> bool:
        return await self._provider.is_available()

    async def close(self) -> None:
        await self._provider.close()

    async def _set_status(self, job_id: str, status: JobStatus) -> None:
        async with self._lock:
            self._jobs[job_id].status = status
            snapshot = self._jobs[job_id].model_copy(deep=True)
        self._persist_job(snapshot)

    async def _set_progress(self, job_id: str, processed_sentences: int) -> None:
        async with self._lock:
            self._jobs[job_id].processed_sentences = processed_sentences
            snapshot = self._jobs[job_id].model_copy(deep=True)
        self._persist_job(snapshot)

    async def _complete(self, job_id: str, result: ChapterResult) -> None:
        async with self._lock:
            job = self._jobs[job_id]
            job.status = JobStatus.COMPLETE
            job.result = result
            snapshot = job.model_copy(deep=True)
        self._persist_job(snapshot)

    async def _fail(self, job_id: str, error: str) -> None:
        async with self._lock:
            job = self._jobs[job_id]
            job.status = JobStatus.FAILED
            job.error = error
            snapshot = job.model_copy(deep=True)
        self._persist_job(snapshot)

    def _load_job(self, job_id: str) -> ChapterJobResponse | None:
        job_dir = self._job_dir(job_id)
        if job_dir is None:
            return None

        state_path = job_dir / self._STATE_FILENAME
        if state_path.exists():
            try:
                job = ChapterJobResponse.model_validate_json(
                    state_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                return None

            if job.status in {JobStatus.QUEUED, JobStatus.PROCESSING}:
                recovered = self._load_completed_job(job_id, job.created_at)
                if recovered is not None:
                    self._persist_job(recovered)
                    return recovered
                job.status = JobStatus.FAILED
                job.error = "Gateway restarted before chapter synthesis completed."
                self._persist_job(job)
            return job

        recovered = self._load_completed_job(job_id)
        if recovered is not None:
            self._persist_job(recovered)
        return recovered

    def _load_completed_job(
        self,
        job_id: str,
        created_at: datetime | None = None,
    ) -> ChapterJobResponse | None:
        job_dir = self._required_job_dir(job_id)
        audio_path = job_dir / "audio.wav"
        result_path = job_dir / self._RESULT_FILENAME
        if not audio_path.exists() or not result_path.exists():
            return None
        try:
            result = ChapterResult.model_validate_json(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        text_length = sum(len(sentence.origin_text) for sentence in result.sentences)
        return ChapterJobResponse(
            job_id=job_id,
            status=JobStatus.COMPLETE,
            created_at=created_at
            or datetime.fromtimestamp(audio_path.stat().st_mtime, tz=UTC),
            processed_sentences=len(result.sentences),
            sentence_count=len(result.sentences),
            text_length=text_length,
            result=result,
        )

    def _persist_job(self, job: ChapterJobResponse) -> None:
        state_path = self._required_job_dir(job.job_id) / self._STATE_FILENAME
        temporary_path = state_path.with_suffix(".json.tmp")
        temporary_path.write_text(job.model_dump_json(indent=2), encoding="utf-8")
        temporary_path.replace(state_path)

    def _job_dir(self, job_id: str) -> Path | None:
        try:
            if str(UUID(job_id)) != job_id:
                return None
        except ValueError:
            return None
        return self._storage_dir / job_id

    def _required_job_dir(self, job_id: str) -> Path:
        job_dir = self._job_dir(job_id)
        if job_dir is None:
            raise ValueError("Invalid job identifier.")
        return job_dir

    @staticmethod
    def _build_sentence_timings(
        sentences: list[str],
        timestamps_ms: list[tuple[int, int]] | list[list[int]],
    ) -> list[AsyncTTSSentence]:
        return [
            AsyncTTSSentence(
                text=sentence,
                origin_text=sentence,
                paragraph_no=1,
                begin_time=begin,
                end_time=end,
            )
            for sentence, (begin, end) in zip(sentences, timestamps_ms, strict=True)
        ]

    def _cleanup_expired_jobs(self) -> None:
        cutoff = datetime.now(UTC) - self._job_retention
        expired_job_ids: list[str] = []
        for job_dir in self._storage_dir.iterdir():
            if not job_dir.is_dir():
                continue
            state_path = job_dir / self._STATE_FILENAME
            if not state_path.exists():
                continue
            try:
                job = ChapterJobResponse.model_validate_json(
                    state_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError, json.JSONDecodeError):
                if datetime.fromtimestamp(state_path.stat().st_mtime, tz=UTC) < cutoff:
                    shutil.rmtree(job_dir, ignore_errors=True)
                continue
            if (
                job.status in {JobStatus.QUEUED, JobStatus.PROCESSING}
                and job.job_id not in self._jobs
            ):
                recovered = self._load_completed_job(job.job_id, job.created_at)
                if recovered is not None:
                    job = recovered
                else:
                    job.status = JobStatus.FAILED
                    job.error = "Gateway restarted before chapter synthesis completed."
                self._persist_job(job)

            if job.status not in {JobStatus.COMPLETE, JobStatus.FAILED}:
                continue

            candidate_paths = [state_path]
            result_path = job_dir / self._RESULT_FILENAME
            if result_path.exists():
                candidate_paths.append(result_path)

            last_modified = max(path.stat().st_mtime for path in candidate_paths)
            if datetime.fromtimestamp(last_modified, tz=UTC) < cutoff:
                shutil.rmtree(job_dir, ignore_errors=True)
                expired_job_ids.append(job_dir.name)

        for job_id in expired_job_ids:
            self._jobs.pop(job_id, None)

    def _has_request_id(self, reqid: str) -> bool:
        if any(job.reqid == reqid for job in self._jobs.values()):
            return True
        for state_path in self._storage_dir.glob(f"*/{self._STATE_FILENAME}"):
            try:
                job = ChapterJobResponse.model_validate_json(
                    state_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if job.reqid == reqid:
                return True
        return False
