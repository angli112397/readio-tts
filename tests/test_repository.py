import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from readio_tts.models import VoiceRecord
from readio_tts.repository import JobRepository, VoiceRepository


def make_voice(voice_id: str, created_at: datetime) -> VoiceRecord:
    return VoiceRecord(
        voice_id=voice_id,
        display_name=f"Voice {voice_id}",
        reference_language="zh",
        transcript="Reference prompt.",
        duration_ms=4_200,
        audio_size_bytes=20_000,
        audio_sha256="a" * 64,
        created_at=created_at,
    )


def test_job_repository_initializes_minimal_voice_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "readio.sqlite3"
    JobRepository(database_path)

    with sqlite3.connect(database_path) as connection:
        columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(voices)").fetchall()
        ]

    assert columns == [
        "voice_id",
        "display_name",
        "reference_language",
        "transcript",
        "duration_ms",
        "audio_size_bytes",
        "audio_sha256",
        "created_at",
    ]


def test_voice_repository_creates_lists_and_deletes_immutable_voices(tmp_path: Path) -> None:
    repository = VoiceRepository(tmp_path / "readio.sqlite3")
    earlier = make_voice("voice_earlier", datetime(2026, 5, 26, tzinfo=UTC))
    later = make_voice("voice_later", earlier.created_at + timedelta(days=1))

    repository.create(earlier)
    repository.create(later)

    assert repository.get("voice_earlier") == earlier
    assert [voice.voice_id for voice in repository.list_all()] == [
        "voice_later",
        "voice_earlier",
    ]

    repository.delete("voice_earlier")
    assert repository.get("voice_earlier") is None


def test_job_repository_records_worker_heartbeat(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "readio.sqlite3")
    heartbeat = datetime(2026, 5, 27, tzinfo=UTC)
    repository.touch_worker(heartbeat)

    assert repository.worker_last_seen_at() == heartbeat
