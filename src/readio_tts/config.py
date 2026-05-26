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

    provider: Literal["mock", "gpt"] = "gpt"
    storage_dir: Path = Path("data/jobs")
    max_chapter_characters: int = 200_000
    job_retention_days: int = Field(default=7, ge=1, le=365)

    gpt_base_url: str = "http://127.0.0.1:9880"
    gpt_api_key: str | None = None
    gpt_model_revision: str = "v2ProPlus"
    gpt_timeout_seconds: float = 300.0
    gpt_reference_dir: Path = Path("references/gpt")
    gpt_text_lang: str = "zh"
    gpt_prompt_lang: str = "zh"
    gpt_text_split_method: str = "cut0"
    gpt_batch_size: int = Field(default=1, ge=1, le=16)
    gpt_top_k: int = Field(default=15, ge=1, le=100)
    gpt_top_p: float = Field(default=1.0, ge=0.1, le=1.0)
    gpt_temperature: float = Field(default=1.0, ge=0.1, le=2.0)
    gpt_speed_factor: float = Field(default=1.0, ge=0.25, le=3.0)
    gpt_fragment_interval: float = Field(default=0.3, ge=0.0, le=3.0)
    gpt_seed: int = Field(default=-1, ge=-1)
