from io import BytesIO
import struct
import wave

from readio_tts.audio import read_wav_segment, silence_frames, write_wav_from_raw


def make_wav(
    duration_ms: int,
    frame_rate: int = 1_000,
    channels: int = 1,
) -> bytes:
    frame_count = duration_ms * frame_rate // 1000
    output = BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(frame_rate)
        writer.writeframes(struct.pack("<h", 0) * frame_count * channels)
    return output.getvalue()


def test_reads_pcm_frames_from_wav_segment() -> None:
    segment = read_wav_segment(make_wav(120))

    assert segment.frame_count == 120
    assert segment.format.channels == 1
    assert segment.format.sample_width == 2
    assert segment.format.frame_rate == 1_000


def test_normalizes_segments_with_inconsistent_formats() -> None:
    first = read_wav_segment(make_wav(100))
    second = read_wav_segment(make_wav(100, frame_rate=2_000, channels=2), first.format)

    assert second.format == first.format
    assert second.frame_count == 100


def test_writes_final_wav_from_raw_pcm(tmp_path) -> None:
    first = read_wav_segment(make_wav(120))
    gap = silence_frames(first.format, 400)
    second = read_wav_segment(make_wav(330), first.format)
    raw_path = tmp_path / "audio.partial.raw"
    output_path = tmp_path / "audio.wav"

    raw_path.write_bytes(first.frames + gap + second.frames)
    write_wav_from_raw(raw_path, output_path, first.format)

    with wave.open(str(output_path), "rb") as output:
        assert output.getnframes() == 850
        assert output.getframerate() == 1_000
