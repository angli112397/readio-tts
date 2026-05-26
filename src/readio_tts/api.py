from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse

from .config import Settings
from .jobs import ChapterTooLargeError, IdempotencyConflictError, JobService
from .models import (
    CreateJobRequest,
    CreateJobResponse,
    JobArtifact,
    JobProgress,
    JobRecord,
    JobResponse,
    JobState,
)
from .providers import create_provider


settings = Settings()
jobs = JobService(
    provider=create_provider(settings),
    storage_dir=settings.storage_dir,
    max_chapter_characters=settings.max_chapter_characters,
    job_retention_days=settings.job_retention_days,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await jobs.resume_pending_jobs()
    yield
    await jobs.close()


app = FastAPI(title="Readio TTS", version="0.2.0", lifespan=lifespan)


@app.get("/health")
async def health(response: Response) -> dict[str, str]:
    if not await jobs.provider_available():
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "provider": settings.provider}
    return {"status": "ok", "provider": settings.provider}


@app.post("/v1/jobs", response_model=CreateJobResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_job(
    request: CreateJobRequest,
    http_request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> CreateJobResponse:
    try:
        job, created = await jobs.create_job(request, idempotency_key)
    except ChapterTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if created:
        jobs.schedule(job.job_id)
    return CreateJobResponse(
        job_id=job.job_id,
        state=job.state,
        status_url=_absolute_url(http_request, f"/v1/jobs/{job.job_id}"),
    )


@app.get("/v1/jobs/{job_id}", response_model=JobResponse, response_model_exclude_none=True)
async def get_job(job_id: str, request: Request) -> JobResponse:
    job = await _require_job(job_id)
    artifact = None
    if job.state == JobState.COMPLETED:
        artifact = JobArtifact(
            audio_url=_absolute_url(request, f"/v1/jobs/{job_id}/audio"),
            manifest_url=_absolute_url(request, f"/v1/jobs/{job_id}/manifest"),
            size_bytes=job.audio_size_bytes or 0,
            sha256=job.audio_sha256 or "",
        )
    return JobResponse(
        job_id=job.job_id,
        chapter_id=job.request.chapter_id,
        state=job.state,
        progress=JobProgress(
            sentences_completed=job.sentences_completed,
            sentences_total=len(job.request.sentences),
        ),
        created_at=job.created_at,
        updated_at=job.updated_at,
        artifact=artifact,
        error=job.error,
    )


@app.get("/v1/jobs/{job_id}/audio", response_class=FileResponse)
async def get_audio(job_id: str) -> FileResponse:
    job = await _require_completed_job(job_id)
    audio_path = jobs.audio_path(job.job_id)
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="Audio artifact not found.")
    return FileResponse(audio_path, media_type="audio/wav", filename=f"{job_id}.wav")


@app.get("/v1/jobs/{job_id}/manifest", response_class=FileResponse)
async def get_manifest(job_id: str) -> FileResponse:
    job = await _require_completed_job(job_id)
    manifest_path = jobs.manifest_path(job.job_id)
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Manifest artifact not found.")
    return FileResponse(manifest_path, media_type="application/json", filename="manifest.json")


@app.post("/v1/jobs/{job_id}/ack", status_code=status.HTTP_204_NO_CONTENT)
async def acknowledge_job(job_id: str) -> Response:
    if not await jobs.acknowledge(job_id):
        raise HTTPException(status_code=409, detail="Completed job not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.delete("/v1/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_job(job_id: str) -> Response:
    if not await jobs.cancel(job_id):
        raise HTTPException(status_code=404, detail="Job not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _require_job(job_id: str) -> JobRecord:
    job = await jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


async def _require_completed_job(job_id: str) -> JobRecord:
    job = await _require_job(job_id)
    if job.state != JobState.COMPLETED:
        raise HTTPException(status_code=409, detail="Job artifact is not ready.")
    return job


def _absolute_url(request: Request, path: str) -> str:
    return f"{str(request.base_url).rstrip('/')}{path}"
