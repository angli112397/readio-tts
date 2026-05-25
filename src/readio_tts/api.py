from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import hmac

from fastapi import (
    BackgroundTasks,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from .config import Settings
from .jobs import ChapterJobService
from .models import (
    AsyncTTSQueryResponse,
    AsyncTTSSubmitRequest,
    AsyncTTSSubmitResponse,
    AsyncTTSErrorResponse,
    ChapterJobResponse,
    CreateChapterJobRequest,
    JobStatus,
)
from .providers import create_provider


settings = Settings()
jobs = ChapterJobService(
    provider=create_provider(settings),
    storage_dir=settings.storage_dir,
    max_chapter_characters=settings.max_chapter_characters,
    sentence_gap_ms=settings.sentence_gap_ms,
    job_retention_days=settings.job_retention_days,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await jobs.close()


app = FastAPI(title="Readio TTS", version="0.1.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def request_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    if request.url.path == "/api/v1/tts_async/submit":
        body = exc.body if isinstance(exc.body, dict) else {}
        error = AsyncTTSErrorResponse(
            reqid=str(body.get("reqid", "")),
            code=40000,
            message="Request parameter error.",
        )
        return JSONResponse(status_code=400, content=error.model_dump())
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})


@app.get("/health")
async def health(response: Response) -> dict[str, str]:
    if not await jobs.provider_available():
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "provider": settings.provider}
    return {"status": "ok", "provider": settings.provider}


@app.post(
    "/api/v1/tts_async/submit",
    response_model=AsyncTTSSubmitResponse | AsyncTTSErrorResponse,
    response_model_exclude_none=True,
)
async def submit_async_tts(
    request: AsyncTTSSubmitRequest,
    background_tasks: BackgroundTasks,
) -> AsyncTTSSubmitResponse | AsyncTTSErrorResponse:
    sentence_gap_ms = request.sentence_interval if request.sentence_interval is not None else None
    internal_request = CreateChapterJobRequest(
        reqid=request.reqid,
        sentences=request.sentences,
        reference_id=request.reference_id or settings.fish_reference_id,
        sentence_gap_ms=sentence_gap_ms,
        text_length=request.text_length(),
    )
    try:
        job = await jobs.create_job(internal_request)
    except ValueError as exc:
        return AsyncTTSErrorResponse(
            reqid=request.reqid,
            code=40000,
            message=str(exc),
        )
    background_tasks.add_task(jobs.process, job.job_id, internal_request)
    return AsyncTTSSubmitResponse(
        task_id=job.job_id,
        task_status=0,
        text_length=job.text_length,
    )


@app.get(
    "/api/v1/tts_async/query",
    response_model=AsyncTTSQueryResponse | AsyncTTSErrorResponse,
    response_model_exclude_none=True,
)
async def query_async_tts(
    request: Request,
    appid: str = Query(...),
    task_id: str = Query(...),
) -> AsyncTTSQueryResponse | AsyncTTSErrorResponse:
    del appid
    job = await jobs.get_job(task_id)
    if job is None:
        return AsyncTTSErrorResponse(
            reqid=task_id,
            code=40400,
            message="Task does not exist or has expired.",
        )
    return _to_async_query_response(job, request)


@app.get("/api/v1/tts_async/audio/{task_id}", response_class=FileResponse)
async def get_async_audio(
    task_id: str,
    expires: int = Query(...),
    signature: str = Query(...),
) -> FileResponse:
    if expires < int(datetime.now(UTC).timestamp()):
        raise HTTPException(status_code=403, detail="Audio URL has expired.")
    expected_signature = _audio_signature(task_id, expires)
    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(status_code=403, detail="Invalid audio URL signature.")
    return await _get_audio(task_id)


async def _get_audio(job_id: str) -> FileResponse:
    job = await jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.result is None:
        raise HTTPException(status_code=409, detail="Audio is not ready.")

    audio_path = jobs.audio_path(job_id)
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found.")
    return FileResponse(audio_path, media_type="audio/wav", filename=f"{job_id}.wav")


def _to_async_query_response(job: ChapterJobResponse, request: Request) -> AsyncTTSQueryResponse:
    task_status = {
        JobStatus.QUEUED: 0,
        JobStatus.PROCESSING: 0,
        JobStatus.COMPLETE: 1,
        JobStatus.FAILED: 2,
    }[job.status]

    if job.result is None or job.status != JobStatus.COMPLETE:
        failure_message = job.error if job.status == JobStatus.FAILED else None
        return AsyncTTSQueryResponse(
            task_id=job.job_id,
            task_status=task_status,
            text_length=job.text_length,
            code=50001 if job.status == JobStatus.FAILED else None,
            message=failure_message,
        )

    expires = int((datetime.now(UTC) + timedelta(hours=1)).timestamp())
    signature = _audio_signature(job.job_id, expires)
    return AsyncTTSQueryResponse(
        task_id=job.job_id,
        task_status=task_status,
        text_length=job.text_length,
        audio_url=(
            f"{str(request.base_url).rstrip('/')}/api/v1/tts_async/audio/{job.job_id}"
            f"?expires={expires}&signature={signature}"
        ),
        url_expire_time=expires,
        sentences=job.result.sentences,
    )


def _audio_signature(task_id: str, expires: int) -> str:
    payload = f"{task_id}:{expires}".encode("utf-8")
    return hmac.new(
        settings.audio_url_signing_key.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
