from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SentenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    paragraph_index: int = Field(default=0, ge=0)

    @field_validator("text")
    @classmethod
    def require_spoken_text(cls, text: str) -> str:
        if not text.strip():
            raise ValueError("Sentence text cannot be blank.")
        return text


class CreateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(min_length=1)
    voice_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    )
    sentence_gap_ms: int = Field(default=600, ge=0, le=5_000)
    sentences: list[SentenceRequest] = Field(min_length=1)

    @field_validator("sentences")
    @classmethod
    def require_unique_sentence_ids(
        cls,
        sentences: list[SentenceRequest],
    ) -> list[SentenceRequest]:
        sentence_ids = [sentence.id for sentence in sentences]
        if len(sentence_ids) != len(set(sentence_ids)):
            raise ValueError("Sentence IDs must be unique within a chapter.")
        return sentences


class JobState(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ManifestSentence(BaseModel):
    id: str
    paragraph_index: int
    begin_ms: int
    end_ms: int


class ChapterManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    voice_id: str
    duration_ms: int
    sentence_gap_ms: int
    sentences: list[ManifestSentence]


class PersistedJobState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    idempotency_key: str
    synthesis_signature: str | None = None
    state: JobState
    created_at: datetime
    updated_at: datetime
    sentences_completed: int = 0
    audio_size_bytes: int | None = None
    audio_sha256: str | None = None
    error: str | None = None


class JobRecord(PersistedJobState):
    request: CreateJobRequest


class CreateJobResponse(BaseModel):
    job_id: str
    state: JobState
    status_url: str


class JobProgress(BaseModel):
    sentences_completed: int
    sentences_total: int


class JobArtifact(BaseModel):
    audio_url: str
    manifest_url: str
    mime_type: str = "audio/wav"
    size_bytes: int
    sha256: str


class JobResponse(BaseModel):
    job_id: str
    chapter_id: str
    state: JobState
    progress: JobProgress
    created_at: datetime
    updated_at: datetime
    artifact: JobArtifact | None = None
    error: str | None = None
