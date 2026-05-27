import hashlib
import shutil
import wave
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from .models import LanguageCode, VoiceRecord, VoiceSnapshot
from .repository import VoiceRepository


class InvalidVoiceAudioError(ValueError):
    pass


class VoiceUnavailableError(ValueError):
    pass


class VoiceManager:
    min_duration_ms = 3_000
    max_duration_ms = 10_000

    def __init__(
        self,
        repository: VoiceRepository,
        voices_dir: Path,
        max_audio_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self.repository = repository
        self.voices_dir = voices_dir
        self.max_audio_bytes = max_audio_bytes
        self.voices_dir.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        display_name: str,
        reference_language: LanguageCode,
        transcript: str,
        audio: bytes,
    ) -> VoiceRecord:
        if len(audio) > self.max_audio_bytes:
            raise InvalidVoiceAudioError("Reference audio exceeds the upload size limit.")
        duration_ms = _wav_duration_ms(audio)
        if not self.min_duration_ms <= duration_ms <= self.max_duration_ms:
            raise InvalidVoiceAudioError(
                "Reference audio duration must be between 3 and 10 seconds."
            )
        voice_id = str(uuid4())
        record = VoiceRecord(
            voice_id=voice_id,
            display_name=display_name,
            reference_language=reference_language,
            transcript=transcript,
            duration_ms=duration_ms,
            audio_size_bytes=len(audio),
            audio_sha256=hashlib.sha256(audio).hexdigest(),
            created_at=datetime.now(UTC),
        )
        files = VoiceFiles(self.voices_dir, voice_id)
        try:
            files.root.mkdir()
            temporary = files.root / "reference.wav.tmp"
            temporary.write_bytes(audio)
            temporary.replace(files.audio)
            self.repository.create(record)
        except Exception:
            shutil.rmtree(files.root, ignore_errors=True)
            raise
        return record

    def get(self, voice_id: str) -> VoiceRecord | None:
        return self.repository.get(voice_id)

    def list_all(self) -> list[VoiceRecord]:
        return self.repository.list_all()

    def audio_path(self, voice_id: str) -> Path | None:
        record = self.repository.get(voice_id)
        if record is None:
            return None
        path = VoiceFiles(self.voices_dir, record.voice_id).audio
        return path if path.exists() else None

    def delete(self, voice_id: str) -> None:
        record = self.repository.get(voice_id)
        if record is None:
            return
        shutil.rmtree(VoiceFiles(self.voices_dir, record.voice_id).root, ignore_errors=True)
        self.repository.delete(record.voice_id)

    def snapshot_to(self, voice_id: str, destination: Path) -> None:
        record = self.repository.get(voice_id)
        if record is None:
            raise VoiceUnavailableError(f"Voice '{voice_id}' does not exist.")
        source = VoiceFiles(self.voices_dir, voice_id).audio
        if not source.exists():
            raise VoiceUnavailableError(f"Voice '{voice_id}' audio is missing.")
        shutil.copyfile(source, destination / "reference.wav")
        snapshot = VoiceSnapshot(
            reference_language=record.reference_language,
            transcript=record.transcript,
        )
        (destination / "voice.json").write_text(
            snapshot.model_dump_json(indent=2),
            encoding="utf-8",
        )


class VoiceFiles:
    def __init__(self, voices_dir: Path, voice_id: str) -> None:
        self.root = voices_dir / voice_id
        self.audio = self.root / "reference.wav"


def _wav_duration_ms(audio: bytes) -> int:
    try:
        with wave.open(BytesIO(audio), "rb") as reader:
            if reader.getcomptype() != "NONE":
                raise InvalidVoiceAudioError("Reference audio must be an uncompressed PCM WAV.")
            frame_rate = reader.getframerate()
            if frame_rate <= 0 or reader.getnframes() <= 0:
                raise InvalidVoiceAudioError("Reference audio WAV is empty or invalid.")
            return round(reader.getnframes() * 1000 / frame_rate)
    except (EOFError, wave.Error) as exc:
        raise InvalidVoiceAudioError("Reference audio must be a valid WAV file.") from exc
