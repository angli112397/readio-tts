from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
from pathlib import Path
import struct
import wave

import pyarrow as arrow
import pyarrow.parquet as parquet
import pytest


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "extract_csemotions_reference.py"
SPEC = spec_from_file_location("extract_csemotions_reference", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
extractor = module_from_spec(SPEC)
SPEC.loader.exec_module(extractor)


def make_wav() -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24_000)
        writer.writeframes(struct.pack("<h", 0) * 240)
    return output.getvalue()


def write_parquet(path: Path, speaker_column: str = "speaker_id") -> None:
    audio = [{"bytes": make_wav(), "path": None} for _ in range(3)]
    table = arrow.table(
        {
            "audio": audio,
            "transcript": ["first", "second", "third"],
            speaker_column: ["S01", "S01", "S02"],
            "emotion": ["Neutral", "Neutral", "Happy"],
            "gender": ["Female", "Female", "Male"],
        }
    )
    parquet.write_table(table, path)


def test_extracts_matching_wav_and_lab_pairs(tmp_path: Path) -> None:
    parquet_path = tmp_path / "train.parquet"
    output = tmp_path / "references" / "reader"
    write_parquet(parquet_path)

    written = extractor.extract_references(
        paths=[parquet_path],
        output=output,
        speaker_id="S01",
        emotion="neutral",
        count=2,
    )

    assert [path.name for path, _ in written] == ["sample_01.wav", "sample_02.wav"]
    assert (output / "sample_01.wav").read_bytes() == make_wav()
    assert (output / "sample_01.lab").read_text(encoding="utf-8") == "first"
    assert (output / "sample_02.lab").read_text(encoding="utf-8") == "second"


def test_does_not_write_partial_output_when_too_few_samples(tmp_path: Path) -> None:
    parquet_path = tmp_path / "train.parquet"
    output = tmp_path / "references" / "reader"
    write_parquet(parquet_path)

    with pytest.raises(ValueError, match="Found only 2"):
        extractor.extract_references(
            paths=[parquet_path],
            output=output,
            speaker_id="S01",
            emotion="Neutral",
            count=3,
        )

    assert not output.exists()


def test_accepts_parquet_speaker_and_text_column_names(tmp_path: Path) -> None:
    parquet_path = tmp_path / "train.parquet"
    output = tmp_path / "references" / "reader"
    audio = [{"bytes": make_wav(), "path": None}]
    table = arrow.table(
        {
            "audio": audio,
            "text": ["sample transcript"],
            "speaker": ["voice_a"],
            "emotion": ["neutral"],
        }
    )
    parquet.write_table(table, parquet_path)

    extractor.extract_references(
        paths=[parquet_path],
        output=output,
        speaker_id="voice_a",
        emotion="neutral",
        count=1,
    )

    assert (output / "sample_01.lab").read_text(encoding="utf-8") == "sample transcript"


def test_exports_complete_dataset_with_speaker_emotion_folders_and_manifest(
    tmp_path: Path,
) -> None:
    parquet_path = tmp_path / "train.parquet"
    output = tmp_path / "storage"
    write_parquet(parquet_path)

    count = extractor.export_all_samples([parquet_path], output)

    assert count == 3
    assert (output / "S01" / "Neutral" / "sample_0001.wav").exists()
    assert (output / "S01" / "Neutral" / "sample_0002.lab").read_text(
        encoding="utf-8"
    ) == "second"
    assert (output / "S02" / "Happy" / "sample_0001.wav").exists()
    manifest = (output / "manifest.tsv").read_text(encoding="utf-8")
    assert "speaker\temotion\twav\tlab\ttranscript" in manifest
    assert "S01\tNeutral\tS01/Neutral/sample_0001.wav" in manifest
