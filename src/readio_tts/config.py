from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="READIO_",
        env_file=".env",
        extra="ignore",
    )

    provider: Literal["mock", "fish"] = "mock"
    storage_dir: Path = Path("data/jobs")
    max_chapter_characters: int = 200_000
    sentence_gap_ms: int = Field(default=600, ge=0, le=5_000)
    job_retention_days: int = Field(default=7, ge=1, le=365)
    audio_url_signing_key: str = Field(default="replace-for-non-local-use", min_length=16)

    fish_base_url: str = "http://127.0.0.1:8080"
    fish_api_key: str | None = None
    fish_reference_id: str | None = None
    fish_timeout_seconds: float = 300.0
    fish_chunk_length: int = Field(default=200, ge=100, le=300)
    fish_max_new_tokens: int = Field(default=1024, ge=1)
    fish_top_p: float = Field(default=0.7, ge=0.1, le=1.0)
    fish_temperature: float = Field(default=0.7, ge=0.1, le=1.0)
    fish_repetition_penalty: float = Field(default=1.2, ge=0.9, le=2.0)
    fish_normalize: bool = True
    fish_use_memory_cache: Literal["on", "off"] = "on"
