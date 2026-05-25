from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import wave


@dataclass(frozen=True)
class AudioFormat:
    channels: int
    sample_width: int
    frame_rate: int
    compression_type: str


@dataclass(frozen=True)
class ConcatenationResult:
    audio: bytes
    timestamps_ms: list[tuple[int, int]]
    duration_ms: int


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
            audio_format = _read_format(reader)
            _require_pcm(audio_format)
            if self._audio_format is None:
                self._audio_format = audio_format
                self._writer = wave.open(str(self._output_path), "wb")
                self._writer.setnchannels(audio_format.channels)
                self._writer.setsampwidth(audio_format.sample_width)
                self._writer.setframerate(audio_format.frame_rate)
            elif audio_format != self._audio_format:
                raise ValueError("Every sentence WAV must share the same audio format.")

            assert self._writer is not None
            if self._timestamps_ms and self._sentence_gap_ms:
                silence_frames = round(
                    self._sentence_gap_ms * audio_format.frame_rate / 1000
                )
                self._writer.writeframes(
                    b"\0" * silence_frames * audio_format.channels * audio_format.sample_width
                )
                self._total_frames += silence_frames

            sentence_frames = reader.getnframes()
            start_frame = self._total_frames
            self._total_frames += sentence_frames
            self._timestamps_ms.append(
                (
                    _frames_to_ms(start_frame, audio_format.frame_rate),
                    _frames_to_ms(self._total_frames, audio_format.frame_rate),
                )
            )
            self._writer.writeframes(reader.readframes(sentence_frames))

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


def concatenate_wav_segments(segments: list[bytes]) -> ConcatenationResult:
    if not segments:
        raise ValueError("At least one WAV segment is required.")

    output = BytesIO()
    timestamps: list[tuple[int, int]] = []
    total_frames = 0
    expected_format: AudioFormat | None = None
    all_frames: list[bytes] = []

    for segment in segments:
        with wave.open(BytesIO(segment), "rb") as reader:
            audio_format = _read_format(reader)
            _require_pcm(audio_format)
            if expected_format is None:
                expected_format = audio_format
            elif audio_format != expected_format:
                raise ValueError("Every sentence WAV must share the same audio format.")

            sentence_frames = reader.getnframes()
            start_frame = total_frames
            total_frames += sentence_frames
            timestamps.append(
                (
                    _frames_to_ms(start_frame, audio_format.frame_rate),
                    _frames_to_ms(total_frames, audio_format.frame_rate),
                )
            )
            all_frames.append(reader.readframes(sentence_frames))

    assert expected_format is not None
    with wave.open(output, "wb") as writer:
        writer.setnchannels(expected_format.channels)
        writer.setsampwidth(expected_format.sample_width)
        writer.setframerate(expected_format.frame_rate)
        writer.writeframes(b"".join(all_frames))

    return ConcatenationResult(
        audio=output.getvalue(),
        timestamps_ms=timestamps,
        duration_ms=_frames_to_ms(total_frames, expected_format.frame_rate),
    )


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
