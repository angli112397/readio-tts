import asyncio
import logging

from .config import Settings
from .jobs import JobManager, JobWorker
from .logging_config import configure_logging
from .providers import create_provider
from .repository import JobRepository, VoiceRepository
from .voices import VoiceManager


logger = logging.getLogger("readio_tts.worker")


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    logger.info(
        "Worker starting: provider=%s log_level=%s",
        settings.provider,
        settings.log_level,
    )
    database_path = settings.data_dir / "readio.sqlite3"
    manager = JobManager(
        repository=JobRepository(database_path),
        jobs_dir=settings.data_dir / "jobs",
        voice_manager=VoiceManager(
            VoiceRepository(database_path),
            settings.data_dir / "voices",
        ),
        model_revision=settings.gpt_model_revision,
        max_chapter_characters=settings.max_chapter_characters,
        job_retention_days=settings.job_retention_days,
    )
    worker = JobWorker(manager, create_provider(settings))
    asyncio.run(worker.run_forever())


if __name__ == "__main__":
    main()
