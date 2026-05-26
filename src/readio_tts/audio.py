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
class AssemblyResult:
    timestamps_ms: list[tuple[int, int]]
    duration_ms: int


class WavFileAssembler:
    """Append sentence WAVs to one output file without retaining chapter audio in RAM."""

    def __init__(self, output_path: Path, sentence_gap_ms: int = 0) -> None:
        if sentence_gap_ms < 0:
            raise ValueError("Sentence gap cannot be negative.")
        self._output_path = output_path
        self._sentence_gap_ms = sentence_gap_ms
        self._writer: wave.Wave_write | None = None
        self._audio_format: AudioFormat | None = None
        self._total_frames = 0
        self._timestamps_ms: list[tuple[int, int]] = []

    def __enter__(self) -> "WavFileAssembler":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def append(self, segment: bytes) -> None:
        with wave.open(BytesIO(segment), "rb") as reader:
            source_format = _read_format(reader)
            _require_pcm(source_format)
            raw_frames = reader.readframes(reader.getnframes())
            if self._audio_format is None:
                self._audio_format = source_format
                self._writer = wave.open(str(self._output_path), "wb")
                self._writer.setnchannels(source_format.channels)
                self._writer.setsampwidth(source_format.sample_width)
                self._writer.setframerate(source_format.frame_rate)

            assert self._audio_format is not None
            normalized_frames = _normalize_pcm(
                raw_frames,
                source_format,
                self._audio_format,
            )

            assert self._writer is not None
            if self._timestamps_ms and self._sentence_gap_ms:
                silence_frames = round(
                    self._sentence_gap_ms * self._audio_format.frame_rate / 1000
                )
                self._writer.writeframes(
                    b"\0"
                    * silence_frames
                    * self._audio_format.channels
                    * self._audio_format.sample_width
                )
                self._total_frames += silence_frames

            sentence_frames = _frame_count(
                normalized_frames,
                self._audio_format.channels,
                self._audio_format.sample_width,
            )
            start_frame = self._total_frames
            self._total_frames += sentence_frames
            self._timestamps_ms.append(
                (
                    _frames_to_ms(start_frame, self._audio_format.frame_rate),
                    _frames_to_ms(self._total_frames, self._audio_format.frame_rate),
                )
            )
            self._writer.writeframes(normalized_frames)

    def result(self) -> AssemblyResult:
        if self._audio_format is None:
            raise ValueError("At least one WAV segment is required.")
        return AssemblyResult(
            timestamps_ms=list(self._timestamps_ms),
            duration_ms=_frames_to_ms(self._total_frames, self._audio_format.frame_rate),
        )

    def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None


def _frames_to_ms(frames: int, frame_rate: int) -> int:
    return round(frames * 1000 / frame_rate)


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
