import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import JobRecord, JobState


class JobRepository:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create(self, record: JobRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, idempotency_key, chapter_id, voice_id, model_revision,
                    state, total_sentences, completed_sentences, created_at,
                    updated_at, heartbeat_at, audio_size_bytes, audio_sha256, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._values(record),
            )

    def get(self, job_id: str) -> JobRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return self._record(row)

    def find_by_idempotency_key(self, key: str) -> JobRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        return self._record(row)

    def next_pending(self) -> JobRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state IN (?, ?)
                ORDER BY created_at
                LIMIT 1
                """,
                (JobState.RUNNING.value, JobState.QUEUED.value),
            ).fetchone()
        return self._record(row)

    def save(self, record: JobRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET
                    idempotency_key = ?, chapter_id = ?, voice_id = ?,
                    model_revision = ?, state = ?, total_sentences = ?,
                    completed_sentences = ?, created_at = ?, updated_at = ?, heartbeat_at = ?,
                    audio_size_bytes = ?, audio_sha256 = ?, error = ?
                WHERE job_id = ?
                """,
                (
                    record.idempotency_key,
                    record.chapter_id,
                    record.voice_id,
                    record.model_revision,
                    record.state.value,
                    record.total_sentences,
                    record.completed_sentences,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                    _timestamp(record.heartbeat_at),
                    record.audio_size_bytes,
                    record.audio_sha256,
                    record.error,
                    record.job_id,
                ),
            )

    def delete(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))

    def delete_expired_terminal_jobs(self, retention: timedelta) -> list[str]:
        cutoff = datetime.now(UTC) - retention
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT job_id FROM jobs
                WHERE state IN (?, ?) AND updated_at < ?
                """,
                (
                    JobState.SUCCEEDED.value,
                    JobState.FAILED.value,
                    cutoff.isoformat(),
                ),
            ).fetchall()
            ids = [row["job_id"] for row in rows]
            connection.executemany(
                "DELETE FROM jobs WHERE job_id = ?",
                [(job_id,) for job_id in ids],
            )
        return ids

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    chapter_id TEXT NOT NULL,
                    voice_id TEXT NOT NULL,
                    model_revision TEXT NOT NULL,
                    state TEXT NOT NULL,
                    total_sentences INTEGER NOT NULL,
                    completed_sentences INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    heartbeat_at TEXT,
                    audio_size_bytes INTEGER,
                    audio_sha256 TEXT,
                    error TEXT
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @staticmethod
    def _record(row: sqlite3.Row | None) -> JobRecord | None:
        if row is None:
            return None
        return JobRecord(
            job_id=row["job_id"],
            idempotency_key=row["idempotency_key"],
            chapter_id=row["chapter_id"],
            voice_id=row["voice_id"],
            model_revision=row["model_revision"],
            state=JobState(row["state"]),
            total_sentences=row["total_sentences"],
            completed_sentences=row["completed_sentences"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            heartbeat_at=(
                datetime.fromisoformat(row["heartbeat_at"])
                if row["heartbeat_at"]
                else None
            ),
            audio_size_bytes=row["audio_size_bytes"],
            audio_sha256=row["audio_sha256"],
            error=row["error"],
        )

    @staticmethod
    def _values(record: JobRecord) -> tuple[object, ...]:
        return (
            record.job_id,
            record.idempotency_key,
            record.chapter_id,
            record.voice_id,
            record.model_revision,
            record.state.value,
            record.total_sentences,
            record.completed_sentences,
            record.created_at.isoformat(),
            record.updated_at.isoformat(),
            _timestamp(record.heartbeat_at),
            record.audio_size_bytes,
            record.audio_sha256,
            record.error,
        )


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
