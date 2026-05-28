from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import audioop
import wave


@dataclass(frozen=True)
class AudioFormat:
    channels: int
    sample_width: int
    frame_rate: int
    compression_type: str


@dataclass(frozen=True)
class PcmSegment:
    format: AudioFormat
    frames: bytes
    frame_count: int


def _frames_to_ms(frames: int, frame_rate: int) -> int:
    return round(frames * 1000 / frame_rate)


def frames_to_ms(frames: int, frame_rate: int) -> int:
    return _frames_to_ms(frames, frame_rate)


def read_wav_segment(segment: bytes, target_format: AudioFormat | None = None) -> PcmSegment:
    with wave.open(BytesIO(segment), "rb") as reader:
        source_format = _read_format(reader)
        _require_pcm(source_format)
        output_format = target_format or source_format
        raw_frames = reader.readframes(reader.getnframes())
        frames = _normalize_pcm(raw_frames, source_format, output_format)
        return PcmSegment(
            format=output_format,
            frames=frames,
            frame_count=_frame_count(
                frames,
                output_format.channels,
                output_format.sample_width,
            ),
        )


def silence_frames(audio_format: AudioFormat, duration_ms: int) -> bytes:
    if duration_ms <= 0:
        return b""
    frame_count = round(duration_ms * audio_format.frame_rate / 1000)
    return b"\0" * frame_count * audio_format.channels * audio_format.sample_width


def raw_byte_count(frames: int, audio_format: AudioFormat) -> int:
    return frames * audio_format.channels * audio_format.sample_width


def write_wav_from_raw(
    raw_path: Path,
    output_path: Path,
    audio_format: AudioFormat,
    expected_frames: int | None = None,
) -> None:
    with wave.open(str(output_path), "wb") as writer:
        writer.setnchannels(audio_format.channels)
        writer.setsampwidth(audio_format.sample_width)
        writer.setframerate(audio_format.frame_rate)
        with raw_path.open("rb") as raw_file:
            for block in iter(lambda: raw_file.read(1024 * 1024), b""):
                writer.writeframes(block)
    if expected_frames is not None:
        with wave.open(str(output_path), "rb") as reader:
            actual = reader.getnframes()
        if actual != expected_frames:
            raise ValueError(
                f"WAV frame count mismatch after assembly: expected {expected_frames}, got {actual}."
            )


def _read_format(reader: wave.Wave_read) -> AudioFormat:
    return AudioFormat(
        channels=reader.getnchannels(),
        sample_width=reader.getsampwidth(),
        frame_rate=reader.getframerate(),
        compression_type=reader.getcomptype(),
    )


def _require_pcm(audio_format: AudioFormat) -> None:
    if audio_format.compression_type != "NONE":
        raise ValueError("Only uncompressed PCM WAV audio is supported.")


def _normalize_pcm(
    frames: bytes,
    source_format: AudioFormat,
    target_format: AudioFormat,
) -> bytes:
    normalized = frames
    source_channels = source_format.channels
    target_channels = target_format.channels
    if source_channels != target_channels:
        if source_channels == 2 and target_channels == 1:
            normalized = audioop.tomono(normalized, source_format.sample_width, 0.5, 0.5)
        elif source_channels == 1 and target_channels == 2:
            normalized = audioop.tostereo(normalized, source_format.sample_width, 1.0, 1.0)
        else:
            raise ValueError(
                "Unsupported channel conversion for WAV assembly."
            )

    if source_format.sample_width != target_format.sample_width:
        normalized = audioop.lin2lin(
            normalized,
            source_format.sample_width,
            target_format.sample_width,
        )

    if source_format.frame_rate != target_format.frame_rate:
        normalized, _ = audioop.ratecv(
            normalized,
            target_format.sample_width,
            target_channels,
            source_format.frame_rate,
            target_format.frame_rate,
            None,
        )

    return normalized


def _frame_count(frames: bytes, channels: int, sample_width: int) -> int:
    bytes_per_frame = channels * sample_width
    if bytes_per_frame <= 0:
        raise ValueError("Invalid WAV frame size.")
    return len(frames) // bytes_per_frame
