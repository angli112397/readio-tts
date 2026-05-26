from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CreateChapterJobRequest(BaseModel):
    sentences: list[str] = Field(min_length=1)
    reference_id: str | None = None
    sentence_gap_ms: int | None = Field(default=None, ge=0, le=5_000)
    text_length: int | None = Field(default=None, ge=1)

    @field_validator("sentences")
    @classmethod
    def require_non_empty_sentences(cls, sentences: list[str]) -> list[str]:
        if any(not sentence.strip() for sentence in sentences):
            raise ValueError("Sentences cannot be blank.")
        return sentences

class AsyncTTSSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    appid: str | None = None
    reqid: str | None = None
    format: Literal["wav"] = "wav"
    enable_subtitle: Literal[1] = 1
    sentence_interval: int | None = Field(default=None, ge=0, le=3_000)
    reference_id: str | None = None
    sentences: list[str] = Field(min_length=1)

    @field_validator("sentences")
    @classmethod
    def require_non_empty_sentences(cls, sentences: list[str]) -> list[str]:
        if any(not sentence.strip() for sentence in sentences):
            raise ValueError("Sentences cannot be blank.")
        return sentences

    def text_length(self) -> int:
        return sum(len(sentence) for sentence in self.sentences)


class AsyncTTSSubmitResponse(BaseModel):
    task_id: str
    task_status: int
    text_length: int


class AsyncTTSErrorResponse(BaseModel):
    code: int
    message: str


class AsyncTTSSentence(BaseModel):
    text: str
    origin_text: str
    paragraph_no: int
    begin_time: int
    end_time: int


class AsyncTTSQueryResponse(BaseModel):
    task_id: str
    task_status: int
    text_length: int
    code: int | None = None
    message: str | None = None
    audio_url: str | None = None
    url_expire_time: int | None = None
    sentences: list[AsyncTTSSentence] | None = None


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETE = "complete"
    FAILED = "failed"


class ChapterResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sentences: list[AsyncTTSSentence] = Field(default_factory=list)


class ChapterJobResponse(BaseModel):
    job_id: str
    status: JobStatus
    created_at: datetime
    processed_sentences: int = 0
    sentence_count: int
    text_length: int = 0
    result: ChapterResult | None = None
    error: str | None = None
