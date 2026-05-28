import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import hmac
import logging
from pathlib import Path
import socket
from typing import Annotated

from fastapi import APIRouter, FastAPI, File, Form, Header, Request, Response, Security, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings
from .jobs import ChapterTooLargeError, IdempotencyConflictError, JobManager
from .logging_config import configure_logging
from .models import (
    CreateJobRequest,
    CreateJobResponse,
    ErrorInfo,
    ErrorResponse,
    LanguageCode,
    JobArtifact,
    JobProgress,
    JobRecord,
    JobResponse,
    JobState,
    VoiceRecord,
)
from .repository import JobRepository, VoiceRepository
from .voices import InvalidVoiceAudioError, VoiceManager, VoiceUnavailableError


logger = logging.getLogger(__name__)
settings = Settings()
configure_logging(settings.log_level)
database_path = settings.data_dir / "readio.sqlite3"
voice_manager = VoiceManager(
    VoiceRepository(database_path),
    settings.data_dir / "voices",
)
manager = JobManager(
    repository=JobRepository(database_path),
    jobs_dir=settings.data_dir / "jobs",
    voice_manager=voice_manager,
    model_revision=settings.gpt_model_revision,
    max_chapter_characters=settings.max_chapter_characters,
    job_retention_days=settings.job_retention_days,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, socket.gethostname(), None, socket.AF_INET
        )
        ips = sorted({info[4][0] for info in infos})
        for ip in ips:
            logger.info("Gateway ready: http://%s:PORT (configure port in uvicorn args)", ip)
    except Exception:
        logger.warning("Could not determine local IP address for Android connection hint.")
    yield


app = FastAPI(title="Readio TTS", version="0.4.0", lifespan=lifespan)


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.error = ErrorInfo(code=code, message=message)


bearer = HTTPBearer(auto_error=False)


def require_api_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> None:
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not hmac.compare_digest(credentials.credentials, settings.api_token)
    ):
        raise ApiError(401, "unauthorized", "Invalid API token.")


router = APIRouter(prefix="/v1", dependencies=[Security(require_api_token)])


@app.exception_handler(ApiError)
async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(error=exc.error).model_dump(exclude_none=True),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first["loc"] if part != "body")
    field = location or "body"
    message = f"Invalid {field}: {first['msg']}."
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=ErrorResponse(
            error=ErrorInfo(code="invalid_request", message=message)
        ).model_dump(exclude_none=True),
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("API request failed: method=%s path=%s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            error=ErrorInfo(
                code="internal_error",
                message="The server could not complete the request.",
            )
        ).model_dump(exclude_none=True),
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/voices", response_model=VoiceRecord, status_code=status.HTTP_201_CREATED)
async def create_voice(
    display_name: Annotated[str, Form(min_length=1, max_length=100)],
    reference_language: Annotated[LanguageCode, Form()],
    transcript: Annotated[str, Form(min_length=1)],
    audio: Annotated[UploadFile, File()],
) -> VoiceRecord:
    if Path(audio.filename or "").suffix.lower() != ".wav":
        raise ApiError(422, "invalid_voice_audio", "Reference audio must be a WAV file.")
    try:
        return voice_manager.create(
            display_name=display_name,
            reference_language=reference_language,
            transcript=transcript,
            audio=await audio.read(voice_manager.max_audio_bytes + 1),
        )
    except InvalidVoiceAudioError as exc:
        raise ApiError(422, "invalid_voice_audio", str(exc)) from exc
    except ValueError as exc:
        raise ApiError(422, "invalid_request", str(exc)) from exc


@router.get("/voices", response_model=list[VoiceRecord])
async def list_voices() -> list[VoiceRecord]:
    return voice_manager.list_all()


@router.get("/voices/{voice_id}", response_model=VoiceRecord)
async def get_voice(voice_id: str) -> VoiceRecord:
    voice = voice_manager.get(voice_id)
    if voice is None:
        raise ApiError(404, "voice_not_found", "Voice not found.")
    return voice


@router.get("/voices/{voice_id}/audio", response_class=FileResponse)
async def get_voice_audio(voice_id: str) -> FileResponse:
    if voice_manager.get(voice_id) is None:
        raise ApiError(404, "voice_not_found", "Voice not found.")
    audio_path = voice_manager.audio_path(voice_id)
    if audio_path is None:
        raise ApiError(404, "voice_audio_not_found", "Voice audio not found.")
    return FileResponse(audio_path, media_type="audio/wav", filename="reference.wav")


@router.delete("/voices/{voice_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_voice(voice_id: str) -> Response:
    voice_manager.delete(voice_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/jobs", response_model=CreateJobResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_job(
    request: CreateJobRequest,
    http_request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> CreateJobResponse:
    try:
        job, created = manager.create_job(request, idempotency_key)
    except ChapterTooLargeError as exc:
        raise ApiError(413, "chapter_too_large", str(exc)) from exc
    except IdempotencyConflictError as exc:
        raise ApiError(409, "idempotency_conflict", str(exc)) from exc
    except VoiceUnavailableError as exc:
        raise ApiError(422, "voice_unavailable", str(exc)) from exc
    status_url = _absolute_url(http_request, f"/v1/jobs/{job.job_id}")
    response.headers["Location"] = status_url
    response.headers["Retry-After"] = "5"
    logger.info(
        "Job accepted: job_id=%s created=%s sentences_total=%s",
        job.job_id,
        created,
        job.total_sentences,
    )
    return CreateJobResponse(job_id=job.job_id, state=job.state, status_url=status_url)


@router.get("/jobs/{job_id}", response_model=JobResponse, response_model_exclude_none=True)
async def get_job(job_id: str, request: Request) -> JobResponse:
    job = _require_job(job_id)
    queue_position = manager.repository.queue_position(job)
    blocked_by = None
    if job.state in (JobState.QUEUED, JobState.RUNNING):
        worker_last_seen = manager.repository.worker_last_seen_at()
        worker_stale_after = timedelta(seconds=settings.worker_stale_seconds)
        if (
            worker_last_seen is None
            or datetime.now(UTC) - worker_last_seen > worker_stale_after
        ):
            blocked_by = "worker_unavailable"
    artifact = None
    if job.state == JobState.SUCCEEDED:
        artifact = JobArtifact(
            audio_url=_absolute_url(request, f"/v1/jobs/{job_id}/audio"),
            manifest_url=_absolute_url(request, f"/v1/jobs/{job_id}/manifest"),
            size_bytes=job.audio_size_bytes or 0,
            sha256=job.audio_sha256 or "",
        )
    return JobResponse(
        job_id=job.job_id,
        chapter_id=job.chapter_id,
        state=job.state,
        progress=JobProgress(
            sentences_completed=job.completed_sentences,
            sentences_total=job.total_sentences,
        ),
        queue_position=queue_position,
        blocked_by=blocked_by,
        created_at=job.created_at,
        updated_at=job.updated_at,
        heartbeat_at=job.heartbeat_at,
        artifact=artifact,
        error=job.error,
    )


@router.get("/jobs/{job_id}/audio", response_class=FileResponse)
async def get_audio(job_id: str) -> FileResponse:
    job = _require_succeeded_job(job_id)
    audio_path = manager.files(job.job_id).audio
    if not audio_path.exists():
        raise ApiError(404, "artifact_not_found", "Audio artifact not found.")
    return FileResponse(audio_path, media_type="audio/wav", filename=f"{job_id}.wav")


@router.get("/jobs/{job_id}/manifest", response_class=FileResponse)
async def get_manifest(job_id: str) -> FileResponse:
    job = _require_succeeded_job(job_id)
    manifest_path = manager.files(job.job_id).manifest
    if not manifest_path.exists():
        raise ApiError(404, "artifact_not_found", "Manifest artifact not found.")
    return FileResponse(manifest_path, media_type="application/json", filename="manifest.json")


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(job_id: str) -> Response:
    job = manager.get_job(job_id)
    manager.delete(job_id)
    logger.info(
        "Job deletion requested: job_id=%s prior_state=%s",
        job_id,
        job.state.value if job is not None else "not_found",
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


app.include_router(router)


def _require_job(job_id: str) -> JobRecord:
    job = manager.get_job(job_id)
    if job is None:
        raise ApiError(404, "job_not_found", "Job not found.")
    return job


def _require_succeeded_job(job_id: str) -> JobRecord:
    job = _require_job(job_id)
    if job.state != JobState.SUCCEEDED:
        raise ApiError(409, "artifact_not_ready", "Job artifact is not ready.")
    return job


def _absolute_url(request: Request, path: str) -> str:
    return f"{str(request.base_url).rstrip('/')}{path}"
