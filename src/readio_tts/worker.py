import asyncio

from .config import Settings
from .jobs import JobManager, JobWorker
from .providers import create_provider
from .repository import JobRepository, VoiceRepository
from .voices import VoiceManager


def main() -> None:
    settings = Settings()
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
