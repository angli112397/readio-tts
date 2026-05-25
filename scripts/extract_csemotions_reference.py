"""Extract Fish Speech reference pairs from CSEMOTIONS parquet shards."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
from pathlib import Path
import sys
from typing import Iterable

import pyarrow.parquet as parquet


TRANSCRIPT_COLUMNS = ("transcript", "text")
SPEAKER_COLUMNS = ("speaker_id", "speaker")
REQUIRED_COLUMNS = ("audio", "emotion")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract CSEMOTIONS audio and transcripts for Fish Speech.",
    )
    parser.add_argument(
        "parquet",
        nargs="+",
        help="One or more parquet files or wildcard patterns, such as data/train-*.parquet.",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="List available speaker/emotion combinations without writing files.",
    )
    parser.add_argument(
        "--export-all",
        action="store_true",
        help="Export every row into output/<speaker>/<emotion> with a manifest file.",
    )
    parser.add_argument("--speaker-id", help="Speaker to extract, for example S01.")
    parser.add_argument(
        "--emotion",
        default="Neutral",
        help="Emotion to extract (default: Neutral).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("references/mandarin_reader"),
        help="Destination reference directory (default: references/mandarin_reader).",
    )
    parser.add_argument(
        "--count",
        type=positive_integer,
        default=3,
        help="Number of reference pairs to extract (default: 3).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacement of existing sample WAV/LAB files.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = resolve_paths(args.parquet)

    if args.inspect:
        inspect_dataset(paths)
        return 0

    if args.export_all:
        exported = export_all_samples(paths, args.output, args.overwrite)
        print(f"Exported {exported} WAV/LAB pair(s) to {args.output}.")
        return 0

    if not args.speaker_id:
        raise SystemExit(
            "--speaker-id is required unless --inspect or --export-all is specified."
        )

    written = extract_references(
        paths=paths,
        output=args.output,
        speaker_id=args.speaker_id,
        emotion=args.emotion,
        count=args.count,
        overwrite=args.overwrite,
    )
    print(f"Extracted {len(written)} Fish Speech reference pair(s) to {args.output}.")
    for wav_path, lab_path in written:
        print(f"  {wav_path.name} + {lab_path.name}")
    return 0


def resolve_paths(patterns: Iterable[str]) -> list[Path]:
    paths: set[Path] = set()
    for pattern in patterns:
        candidate = Path(pattern)
        if any(character in pattern for character in "*?[]"):
            paths.update(path.resolve() for path in candidate.parent.glob(candidate.name))
        elif candidate.exists():
            paths.add(candidate.resolve())
    if not paths:
        raise SystemExit("No parquet files matched the supplied path(s).")
    return sorted(paths)


def inspect_dataset(paths: list[Path]) -> None:
    counts: Counter[tuple[str, str, str]] = Counter()
    for row in iter_rows(paths, include_audio=False):
        counts[
            (
                str(row["speaker_id"]),
                str(row.get("gender", "")),
                str(row["emotion"]),
            )
        ] += 1

    print("speaker_id\tgender\temotion\tcount")
    for (speaker_id, gender, emotion), count in sorted(counts.items()):
        print(f"{speaker_id}\t{gender}\t{emotion}\t{count}")


def extract_references(
    paths: list[Path],
    output: Path,
    speaker_id: str,
    emotion: str,
    count: int,
    overwrite: bool = False,
) -> list[tuple[Path, Path]]:
    matches: list[dict[str, object]] = []
    for row in iter_rows(paths, include_audio=True):
        if (
            str(row["speaker_id"]) == speaker_id
            and str(row["emotion"]).casefold() == emotion.casefold()
        ):
            matches.append(row)
            if len(matches) == count:
                break

    if len(matches) < count:
        raise ValueError(
            f"Found only {len(matches)} matching sample(s) for speaker "
            f"{speaker_id!r} with emotion {emotion!r}; requested {count}."
        )

    destinations: list[tuple[Path, Path]] = []
    for index in range(1, count + 1):
        wav_path = output / f"sample_{index:02d}.wav"
        lab_path = output / f"sample_{index:02d}.lab"
        if not overwrite and (wav_path.exists() or lab_path.exists()):
            raise FileExistsError(
                f"{wav_path.name} or {lab_path.name} already exists; "
                "use --overwrite to replace existing files."
            )
        destinations.append((wav_path, lab_path))

    output.mkdir(parents=True, exist_ok=True)
    for row, (wav_path, lab_path) in zip(matches, destinations, strict=True):
        wav_path.write_bytes(audio_bytes(row["audio"]))
        lab_path.write_text(str(row["transcript"]).strip(), encoding="utf-8")
    return destinations


def export_all_samples(paths: list[Path], output: Path, overwrite: bool = False) -> int:
    manifest_path = output / "manifest.tsv"
    if not overwrite and manifest_path.exists():
        raise FileExistsError(
            f"{manifest_path} already exists; use --overwrite to replace exported files."
        )

    counters: Counter[tuple[str, str]] = Counter()
    manifest_rows: list[tuple[str, str, str, str, str]] = []
    output.mkdir(parents=True, exist_ok=True)

    for row in iter_rows(paths, include_audio=True):
        speaker_id = safe_path_segment(str(row["speaker_id"]))
        emotion = safe_path_segment(str(row["emotion"]))
        key = (speaker_id, emotion)
        counters[key] += 1
        stem = f"sample_{counters[key]:04d}"
        relative_wav = Path(speaker_id) / emotion / f"{stem}.wav"
        relative_lab = Path(speaker_id) / emotion / f"{stem}.lab"
        wav_path = output / relative_wav
        lab_path = output / relative_lab
        if not overwrite and (wav_path.exists() or lab_path.exists()):
            raise FileExistsError(
                f"{wav_path} or {lab_path} already exists; "
                "use --overwrite to replace existing files."
            )

        wav_path.parent.mkdir(parents=True, exist_ok=True)
        transcript = str(row["transcript"]).strip()
        wav_path.write_bytes(audio_bytes(row["audio"]))
        lab_path.write_text(transcript, encoding="utf-8")
        manifest_rows.append(
            (speaker_id, str(row["emotion"]), relative_wav.as_posix(), relative_lab.as_posix(), transcript)
        )

    with manifest_path.open("w", encoding="utf-8", newline="") as manifest:
        writer = csv.writer(manifest, delimiter="\t")
        writer.writerow(("speaker", "emotion", "wav", "lab", "transcript"))
        writer.writerows(manifest_rows)
    return len(manifest_rows)


def iter_rows(paths: list[Path], include_audio: bool) -> Iterable[dict[str, object]]:
    for path in paths:
        parquet_file = parquet.ParquetFile(path)
        schema_columns = set(parquet_file.schema_arrow.names)
        transcript_column = next(
            (column for column in TRANSCRIPT_COLUMNS if column in schema_columns),
            None,
        )
        speaker_column = next(
            (column for column in SPEAKER_COLUMNS if column in schema_columns),
            None,
        )
        missing = set(REQUIRED_COLUMNS) - schema_columns
        if transcript_column is None:
            missing.add("transcript/text")
        if speaker_column is None:
            missing.add("speaker_id/speaker")
        if missing:
            raise ValueError(f"{path} is missing required column(s): {sorted(missing)}")

        assert speaker_column is not None
        selected = [speaker_column, "emotion", transcript_column]
        if "gender" in schema_columns:
            selected.append("gender")
        if include_audio:
            selected.append("audio")

        batch_size = 32 if include_audio else 1024
        for batch in parquet_file.iter_batches(columns=selected, batch_size=batch_size):
            for row in batch.to_pylist():
                row["speaker_id"] = row.pop(speaker_column)
                row["transcript"] = row.pop(transcript_column)
                yield row


def audio_bytes(audio: object) -> bytes:
    if isinstance(audio, (bytes, bytearray, memoryview)):
        return bytes(audio)
    if isinstance(audio, dict) and audio.get("bytes") is not None:
        return bytes(audio["bytes"])
    raise ValueError("Audio row does not contain embedded WAV bytes.")


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def safe_path_segment(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in value.strip()
    )
    if not cleaned:
        raise ValueError("Speaker or emotion cannot be converted to a directory name.")
    return cleaned


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileExistsError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
