import os
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", ".")) / "ReadioTTS" / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="READIO_",
        env_file=".env",
        extra="ignore",
    )

    provider: Literal["mock", "gpt"] = "gpt"
    data_dir: Path = DEFAULT_DATA_DIR
    max_chapter_characters: int = 500_000
    job_retention_days: int = Field(default=7, ge=1, le=365)
    api_token: str = Field(min_length=16)
    worker_stale_seconds: float = Field(default=30.0, ge=5.0, le=300.0)

    gpt_base_url: str = "http://127.0.0.1:9880"
    gpt_model_revision: str = "v2ProPlus"
    gpt_timeout_seconds: float = 300.0
    gpt_job_data_remote_dir: str = "/var/lib/readio/jobs"
    gpt_text_split_method: str = "cut0"
    gpt_batch_size: int = Field(default=1, ge=1, le=16)
    gpt_top_k: int = Field(default=15, ge=1, le=100)
    gpt_top_p: float = Field(default=1.0, ge=0.1, le=1.0)
    gpt_temperature: float = Field(default=1.0, ge=0.1, le=2.0)
    gpt_speed_factor: float = Field(default=1.0, ge=0.25, le=3.0)
    gpt_fragment_interval: float = Field(default=0.3, ge=0.0, le=3.0)
    gpt_seed: int = Field(default=-1, ge=-1)
