import asyncio

from .config import Settings
from .jobs import JobManager, JobWorker
from .providers import create_provider
from .repository import JobRepository


def main() -> None:
    settings = Settings()
    manager = JobManager(
        repository=JobRepository(settings.data_dir / "readio.sqlite3"),
        jobs_dir=settings.data_dir / "jobs",
        reference_dir=settings.gpt_reference_dir,
        model_revision=settings.gpt_model_revision,
        max_chapter_characters=settings.max_chapter_characters,
        job_retention_days=settings.job_retention_days,
    )
    worker = JobWorker(manager, create_provider(settings))
    asyncio.run(worker.run_forever())


if __name__ == "__main__":
    main()
