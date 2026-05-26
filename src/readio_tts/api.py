from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse

from .config import Settings
from .jobs import ChapterTooLargeError, IdempotencyConflictError, JobManager
from .models import (
    CreateJobRequest,
    CreateJobResponse,
    JobArtifact,
    JobProgress,
    JobRecord,
    JobResponse,
    JobState,
)
from .repository import JobRepository


settings = Settings()
manager = JobManager(
    repository=JobRepository(settings.data_dir / "readio.sqlite3"),
    jobs_dir=settings.data_dir / "jobs",
    reference_dir=settings.gpt_reference_dir,
    model_revision=settings.gpt_model_revision,
    max_chapter_characters=settings.max_chapter_characters,
    job_retention_days=settings.job_retention_days,
)
app = FastAPI(title="Readio TTS", version="0.3.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/jobs", response_model=CreateJobResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_job(
    request: CreateJobRequest,
    http_request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> CreateJobResponse:
    try:
        job, _created = manager.create_job(request, idempotency_key)
    except ChapterTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    status_url = _absolute_url(http_request, f"/v1/jobs/{job.job_id}")
    response.headers["Location"] = status_url
    response.headers["Retry-After"] = "5"
    return CreateJobResponse(job_id=job.job_id, state=job.state, status_url=status_url)


@app.get("/v1/jobs/{job_id}", response_model=JobResponse, response_model_exclude_none=True)
async def get_job(job_id: str, request: Request) -> JobResponse:
    job = _require_job(job_id)
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
        created_at=job.created_at,
        updated_at=job.updated_at,
        heartbeat_at=job.heartbeat_at,
        artifact=artifact,
        error=job.error,
    )


@app.get("/v1/jobs/{job_id}/audio", response_class=FileResponse)
async def get_audio(job_id: str) -> FileResponse:
    job = _require_succeeded_job(job_id)
    audio_path = manager.files(job.job_id).audio
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="Audio artifact not found.")
    return FileResponse(audio_path, media_type="audio/wav", filename=f"{job_id}.wav")


@app.get("/v1/jobs/{job_id}/manifest", response_class=FileResponse)
async def get_manifest(job_id: str) -> FileResponse:
    job = _require_succeeded_job(job_id)
    manifest_path = manager.files(job.job_id).manifest
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Manifest artifact not found.")
    return FileResponse(manifest_path, media_type="application/json", filename="manifest.json")


@app.delete("/v1/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(job_id: str) -> Response:
    manager.delete(job_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _require_job(job_id: str) -> JobRecord:
    job = manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


def _require_succeeded_job(job_id: str) -> JobRecord:
    job = _require_job(job_id)
    if job.state != JobState.SUCCEEDED:
        raise HTTPException(status_code=409, detail="Job artifact is not ready.")
    return job


def _absolute_url(request: Request, path: str) -> str:
    return f"{str(request.base_url).rstrip('/')}{path}"
