from io import BytesIO
import struct
import wave

from readio_tts.audio import WavFileAssembler


def make_wav(duration_ms: int, frame_rate: int = 1_000) -> bytes:
    frame_count = duration_ms * frame_rate // 1000
    output = BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(frame_rate)
        writer.writeframes(struct.pack("<h", 0) * frame_count)
    return output.getvalue()


def test_writes_chapter_audio_incrementally_to_disk(tmp_path) -> None:
    output_path = tmp_path / "audio.wav"

    with WavFileAssembler(output_path) as assembler:
        assembler.append(make_wav(120))
        assembler.append(make_wav(330))
        assembler.append(make_wav(50))
        result = assembler.result()

    assert result.timestamps_ms == [(0, 120), (120, 450), (450, 500)]
    assert result.duration_ms == 500
    with wave.open(str(output_path), "rb") as combined:
        assert combined.getnframes() == 500


def test_normalizes_segments_with_inconsistent_formats(tmp_path) -> None:
    output_path = tmp_path / "normalized.wav"

    with WavFileAssembler(output_path) as assembler:
        assembler.append(make_wav(100))
        assembler.append(make_wav(100, frame_rate=2_000))
        result = assembler.result()

    assert result.timestamps_ms == [(0, 100), (100, 200)]
    assert result.duration_ms == 200


def test_inserts_configured_silence_between_sentences(tmp_path) -> None:
    output_path = tmp_path / "paced.wav"

    with WavFileAssembler(output_path, sentence_gap_ms=400) as assembler:
        assembler.append(make_wav(120))
        assembler.append(make_wav(330))
        result = assembler.result()

    assert result.timestamps_ms == [(0, 120), (520, 850)]
    assert result.duration_ms == 850
    with wave.open(str(output_path), "rb") as combined:
        assert combined.getnframes() == 850
