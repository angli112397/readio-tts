import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import ErrorInfo, JobRecord, JobSentenceRecord, JobState, VoiceRecord


class JobRepository:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create(self, record: JobRecord) -> None:
        with _connect(self._database_path) as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, idempotency_key, chapter_id, voice_id, model_revision,
                    state, total_sentences, completed_sentences, committed_frames,
                    audio_channels, audio_sample_width, audio_frame_rate, created_at,
                    updated_at, heartbeat_at, audio_size_bytes, audio_sha256,
                    error_code, error_message, error_sentence_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._values(record),
            )

    def get(self, job_id: str) -> JobRecord | None:
        with _connect(self._database_path) as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return self._record(row)

    def find_by_idempotency_key(self, key: str) -> JobRecord | None:
        with _connect(self._database_path) as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        return self._record(row)

    def next_pending(self) -> JobRecord | None:
        with _connect(self._database_path) as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state IN (?, ?)
                ORDER BY CASE state WHEN ? THEN 0 ELSE 1 END, created_at, job_id
                LIMIT 1
                """,
                (
                    JobState.RUNNING.value,
                    JobState.QUEUED.value,
                    JobState.RUNNING.value,
                ),
            ).fetchone()
        return self._record(row)

    def queue_position(self, record: JobRecord) -> int | None:
        if record.state != JobState.QUEUED:
            return None
        with _connect(self._database_path) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS position FROM jobs
                WHERE state = ?
                   OR (
                        state = ?
                        AND (created_at < ? OR (created_at = ? AND job_id <= ?))
                   )
                """,
                (
                    JobState.RUNNING.value,
                    JobState.QUEUED.value,
                    record.created_at.isoformat(),
                    record.created_at.isoformat(),
                    record.job_id,
                ),
            ).fetchone()
        return int(row["position"]) if row is not None else None

    def touch_worker(self, timestamp: datetime | None = None) -> None:
        value = (timestamp or datetime.now(UTC)).isoformat()
        with _connect(self._database_path) as connection:
            connection.execute(
                """
                INSERT INTO service_state (key, timestamp) VALUES ('worker', ?)
                ON CONFLICT(key) DO UPDATE SET timestamp = excluded.timestamp
                """,
                (value,),
            )

    def worker_last_seen_at(self) -> datetime | None:
        with _connect(self._database_path) as connection:
            row = connection.execute(
                "SELECT timestamp FROM service_state WHERE key = 'worker'"
            ).fetchone()
        return datetime.fromisoformat(row["timestamp"]) if row is not None else None

    def save(self, record: JobRecord) -> None:
        with _connect(self._database_path) as connection:
            connection.execute(
                """
                UPDATE jobs SET
                    idempotency_key = ?, chapter_id = ?, voice_id = ?,
                    model_revision = ?, state = ?, total_sentences = ?,
                    completed_sentences = ?, committed_frames = ?, audio_channels = ?,
                    audio_sample_width = ?, audio_frame_rate = ?, created_at = ?,
                    updated_at = ?, heartbeat_at = ?, audio_size_bytes = ?,
                    audio_sha256 = ?, error_code = ?, error_message = ?,
                    error_sentence_id = ?
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
                    record.committed_frames,
                    record.audio_channels,
                    record.audio_sample_width,
                    record.audio_frame_rate,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                    _timestamp(record.heartbeat_at),
                    record.audio_size_bytes,
                    record.audio_sha256,
                    record.error.code if record.error else None,
                    record.error.message if record.error else None,
                    record.error.sentence_id if record.error else None,
                    record.job_id,
                ),
            )

    def commit_sentence(self, record: JobRecord, sentence: JobSentenceRecord) -> bool:
        """Atomically commit a sentence and update job progress. Returns False if the job no longer exists."""
        with _connect(self._database_path) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO job_sentences (
                    job_id, sentence_index, sentence_id, paragraph_index, begin_ms, end_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    sentence.job_id,
                    sentence.sentence_index,
                    sentence.sentence_id,
                    sentence.paragraph_index,
                    sentence.begin_ms,
                    sentence.end_ms,
                ),
            )
            cursor = connection.execute(
                """
                UPDATE jobs SET
                    idempotency_key = ?, chapter_id = ?, voice_id = ?,
                    model_revision = ?, state = ?, total_sentences = ?,
                    completed_sentences = ?, committed_frames = ?, audio_channels = ?,
                    audio_sample_width = ?, audio_frame_rate = ?, created_at = ?,
                    updated_at = ?, heartbeat_at = ?, audio_size_bytes = ?,
                    audio_sha256 = ?, error_code = ?, error_message = ?,
                    error_sentence_id = ?
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
                    record.committed_frames,
                    record.audio_channels,
                    record.audio_sample_width,
                    record.audio_frame_rate,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                    _timestamp(record.heartbeat_at),
                    record.audio_size_bytes,
                    record.audio_sha256,
                    record.error.code if record.error else None,
                    record.error.message if record.error else None,
                    record.error.sentence_id if record.error else None,
                    record.job_id,
                ),
            )
            return cursor.rowcount > 0

    def list_sentences(self, job_id: str) -> list[JobSentenceRecord]:
        with _connect(self._database_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM job_sentences
                WHERE job_id = ?
                ORDER BY sentence_index
                """,
                (job_id,),
            ).fetchall()
        return [
            JobSentenceRecord(
                job_id=row["job_id"],
                sentence_index=row["sentence_index"],
                sentence_id=row["sentence_id"],
                paragraph_index=row["paragraph_index"],
                begin_ms=row["begin_ms"],
                end_ms=row["end_ms"],
            )
            for row in rows
        ]

    def delete(self, job_id: str) -> None:
        with _connect(self._database_path) as connection:
            connection.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            connection.execute("DELETE FROM job_sentences WHERE job_id = ?", (job_id,))

    def delete_expired_terminal_jobs(self, retention: timedelta) -> list[str]:
        cutoff = datetime.now(UTC) - retention
        with _connect(self._database_path) as connection:
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
            connection.executemany(
                "DELETE FROM job_sentences WHERE job_id = ?",
                [(job_id,) for job_id in ids],
            )
        return ids

    def _initialize(self) -> None:
        with _connect(self._database_path) as connection:
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
                    committed_frames INTEGER NOT NULL DEFAULT 0,
                    audio_channels INTEGER,
                    audio_sample_width INTEGER,
                    audio_frame_rate INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    heartbeat_at TEXT,
                    audio_size_bytes INTEGER,
                    audio_sha256 TEXT,
                    error_code TEXT,
                    error_message TEXT CHECK (error_message IS NOT NULL OR error_code IS NULL),
                    error_sentence_id TEXT
                )
                """
            )
            _ensure_column(connection, "jobs", "committed_frames", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(connection, "jobs", "audio_channels", "INTEGER")
            _ensure_column(connection, "jobs", "audio_sample_width", "INTEGER")
            _ensure_column(connection, "jobs", "audio_frame_rate", "INTEGER")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_sentences (
                    job_id TEXT NOT NULL,
                    sentence_index INTEGER NOT NULL,
                    sentence_id TEXT NOT NULL,
                    paragraph_index INTEGER NOT NULL,
                    begin_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    PRIMARY KEY (job_id, sentence_index)
                )
                """
            )
            _create_voices_table(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS service_state (
                    key TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL
                )
                """
            )

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
            committed_frames=row["committed_frames"],
            audio_channels=row["audio_channels"],
            audio_sample_width=row["audio_sample_width"],
            audio_frame_rate=row["audio_frame_rate"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            heartbeat_at=(
                datetime.fromisoformat(row["heartbeat_at"])
                if row["heartbeat_at"]
                else None
            ),
            audio_size_bytes=row["audio_size_bytes"],
            audio_sha256=row["audio_sha256"],
            error=(
                ErrorInfo(
                    code=row["error_code"],
                    message=row["error_message"] or "",
                    sentence_id=row["error_sentence_id"],
                )
                if row["error_code"] is not None
                else None
            ),
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
            record.committed_frames,
            record.audio_channels,
            record.audio_sample_width,
            record.audio_frame_rate,
            record.created_at.isoformat(),
            record.updated_at.isoformat(),
            _timestamp(record.heartbeat_at),
            record.audio_size_bytes,
            record.audio_sha256,
            record.error.code if record.error else None,
            record.error.message if record.error else None,
            record.error.sentence_id if record.error else None,
        )

class VoiceRepository:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with _connect(self._database_path) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            _create_voices_table(connection)

    def create(self, record: VoiceRecord) -> None:
        with _connect(self._database_path) as connection:
            connection.execute(
                """
                INSERT INTO voices (
                    voice_id, display_name, reference_language, transcript,
                    duration_ms, audio_size_bytes, audio_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _voice_values(record),
            )

    def get(self, voice_id: str) -> VoiceRecord | None:
        with _connect(self._database_path) as connection:
            row = connection.execute(
                "SELECT * FROM voices WHERE voice_id = ?",
                (voice_id,),
            ).fetchone()
        return _voice_record(row)

    def list_all(self) -> list[VoiceRecord]:
        with _connect(self._database_path) as connection:
            rows = connection.execute(
                "SELECT * FROM voices ORDER BY created_at DESC, voice_id"
            ).fetchall()
        return [_voice_record(row) for row in rows if row is not None]

    def delete(self, voice_id: str) -> None:
        with _connect(self._database_path) as connection:
            connection.execute("DELETE FROM voices WHERE voice_id = ?", (voice_id,))


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    name: str,
    definition: str,
) -> None:
    columns = {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if name not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


@contextmanager
def _connect(database_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(database_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _create_voices_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS voices (
            voice_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            reference_language TEXT NOT NULL,
            transcript TEXT NOT NULL,
            duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),
            audio_size_bytes INTEGER NOT NULL CHECK (audio_size_bytes > 0),
            audio_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


def _voice_record(row: sqlite3.Row | None) -> VoiceRecord | None:
    if row is None:
        return None
    return VoiceRecord(
        voice_id=row["voice_id"],
        display_name=row["display_name"],
        reference_language=row["reference_language"],
        transcript=row["transcript"],
        duration_ms=row["duration_ms"],
        audio_size_bytes=row["audio_size_bytes"],
        audio_sha256=row["audio_sha256"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _voice_values(record: VoiceRecord) -> tuple[object, ...]:
    return (
        record.voice_id,
        record.display_name,
        record.reference_language,
        record.transcript,
        record.duration_ms,
        record.audio_size_bytes,
        record.audio_sha256,
        record.created_at.isoformat(),
    )
