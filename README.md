# Readio TTS

`readio-tts` is a companion service for an Android reading application. The
Android client sends the already segmented sentences of a chapter. This
service generates one WAV utterance for each sentence, concatenates them, and
returns the exact sentence boundaries needed for synchronized highlighting.

## Why Sentence-By-Sentence Generation

Fish Speech produces audio but does not expose alignment timestamps. Generating
one audio segment per sentence makes the boundary timestamps deterministic. The
query API reports them in the sentence-object format already used by Android.
The local service returns sentence timestamps only; it does not claim emotion,
word, or phoneme timing data:

```json
{
  "sentences": [
    {"text": "第一句。", "origin_text": "第一句。", "paragraph_no": 1, "begin_time": 0, "end_time": 1350},
    {"text": "第二句。", "origin_text": "第二句。", "paragraph_no": 1, "begin_time": 1950, "end_time": 3344}
  ]
}
```

The tradeoff is that sentence joins can sound less continuous than synthesizing
a full paragraph. That can later be improved with punctuation-aware sentence
grouping or short crossfades, while retaining known display boundaries.
By default, the gateway inserts `600 ms` of silence between sentence clips to
give audiobook narration room to breathe. The pause is not included in either
sentence's timestamp range.

The gateway writes completed sentence audio directly into a partial chapter WAV
file and publishes it only after the last sentence succeeds. This avoids
holding chapter-sized audio in RAM.

## API Flow

The gateway follows the normal async submit/query lifecycle from
`小模型异步长文本合成接口.md`, adapted for this app's existing sentence
segmentation. Android supplies `sentences` instead of the document's unsplit
`text` value. The public endpoints are:

```http
POST /api/v1/tts_async/submit
GET /api/v1/tts_async/query?appid=123456&task_id=...
```

A chapter may take hours on a laptop GPU, so synthesis is an asynchronous job.
The Android app submits its already ordered sentence list:

```http
POST /api/v1/tts_async/submit
Content-Type: application/json

{
  "appid": "123456",
  "reqid": "android-request-00000001",
  "sentences": ["第一句。", "第二句。"],
  "format": "wav",
  "enable_subtitle": 1,
  "sentence_interval": 600
}
```

The service returns a `task_id` immediately. Poll:

```http
GET /api/v1/tts_async/query?appid=123456&task_id=...
```

When `task_status` becomes `1`, the response includes `audio_url`,
`url_expire_time`, and sentence-level timestamps. The response uses the same
`task_status` values as the external async interface: `0` synthesizing, `1`
success, and `2` failure. Omit `sentence_interval` in the request to use the
configured default, or specify a value from `0` through `3000` milliseconds
for the listener's pacing preference. Download the combined PCM WAV through:

```http
GET /api/v1/tts_async/audio/{task_id}?expires=...&signature=...
```

The returned download URL is signed and expires after one hour; query the task
again for a fresh URL if Android has not downloaded it in time. Completed or
failed task records remain queryable for the configured retention period.

The local gateway supports the document fields used by this application:
`appid`, `reqid`, `format: "wav"`, `enable_subtitle: 1`,
`sentence_interval`, and the local `reference_id`. Other optional cloud
synthesis controls may be included by an existing client but are ignored,
because Fish Speech v1.5 is not configured to implement them here.
`appid` remains part of the Android-compatible request envelope; this
single-user local server does not use it for billing or authentication.
As in the original interface, `reqid` must be unique; repeated submissions
with the same value are rejected instead of generating duplicate audio.

`GET /health` checks the configured speech provider. When Fish Speech is
selected but its v1.5 server cannot answer `POST /v1/health`, the gateway
returns HTTP `503` instead of accepting work blindly.

## Run Without Fish Speech

The default `mock` provider generates short tones and is intended for Android
integration and timestamp testing:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn readio_tts.api:app --reload --port 8090
```

Open `http://127.0.0.1:8090/docs` to exercise the API.

## Development With Fish Speech

To run Fish Speech in Docker while developing the gateway locally with reload,
use the development Compose override. It publishes Fish Speech only to
`127.0.0.1:8080`; the production stack keeps that port private.

Stop the full containerized stack first if it is running:

```powershell
docker compose down
```

Start only Fish Speech:

```powershell
docker compose -f compose.yaml -f compose.dev.yaml up -d fish-speech
docker compose -f compose.yaml -f compose.dev.yaml logs -f fish-speech
```

Create a project-root `.env` for the locally running gateway:

```dotenv
READIO_PROVIDER=fish
READIO_STORAGE_DIR=data/jobs
READIO_SENTENCE_GAP_MS=600
READIO_JOB_RETENTION_DAYS=7
READIO_AUDIO_URL_SIGNING_KEY=replace-with-a-private-random-key
READIO_FISH_BASE_URL=http://127.0.0.1:8080
READIO_FISH_REFERENCE_ID=
READIO_FISH_USE_MEMORY_CACHE=on
```

Then run the gateway on the host:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn readio_tts.api:app --reload --host 0.0.0.0 --port 8090
```

Verify from another terminal:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8090/health
```

In this mode, code edits restart only the lightweight gateway. The GPU model
stays loaded in Docker, avoiding a long Fish Speech restart after every Python
change.

## Docker Deployment

The production-style local stack runs both components through the root
`compose.yaml`:

```text
Android app -> gateway:8090 -> fish-speech:8080 (private Docker network)
```

Only the gateway is published to the laptop network. Fish Speech is reachable
only from the gateway container, so model inference is not exposed directly
without authentication.

Copy `.env.example` to `.env` when selecting a narrator or changing published
gateway settings:

```dotenv
READIO_GATEWAY_BIND=0.0.0.0
READIO_GATEWAY_PORT=8090
READIO_SENTENCE_GAP_MS=600
READIO_JOB_RETENTION_DAYS=7
READIO_AUDIO_URL_SIGNING_KEY=replace-with-a-private-random-key
READIO_FISH_REFERENCE_ID=narrator
```

Start the stack:

```powershell
docker compose up -d --build
docker compose logs -f fish-speech gateway
```

Verify the Android-facing service:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8090/health
```

The gateway waits for Fish Speech to become healthy before starting. Its
job metadata and completed audio files are stored in a Docker named volume and
survive container replacement. Completed or failed jobs are retained for
`READIO_JOB_RETENTION_DAYS` (default `7`) and then cleaned up automatically.
A job interrupted while synthesis is still in progress is recovered as failed
and can be submitted again.

Stop the stack without deleting stored audio:

```powershell
docker compose down
```

Use `docker compose down -v` only when you intentionally want to remove the
stored chapter audio volume.

## Fish Speech Integration

The provider adapter calls the Fish Speech HTTP server at `POST /v1/tts` with
the Fish Speech v1.5 JSON request fields, including `format: "wav"`,
`streaming: false`, and `use_memory_cache: "on"`. The memory cache is valuable
when every sentence uses the same `reference_id`, since Fish Speech can reuse
the encoded narrator reference. The gateway expects an uncompressed PCM WAV
response; concatenating WAV provides exact sentence boundaries without FFmpeg
or lossy re-encoding.

The `fishaudio/fish-speech:v1.5.0` image includes its matching v1.5
checkpoints, including the official Firefly decoder. A named local narrator is
loaded from a directory such as:

```text
references/narrator/
  sample.wav
  sample.lab
```

where `sample.lab` contains the exact transcript of `sample.wav`.

### Extract CSEMOTIONS References

CSEMOTIONS is distributed as parquet shards containing embedded audio and
transcripts. Install the optional extraction dependency:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[reference-tools]"
```

First inspect available speaker and emotion combinations:

```powershell
.\.venv\Scripts\python.exe .\scripts\extract_csemotions_reference.py `
  C:\path\to\train-*.parquet --inspect
```

Extract neutral samples from one selected Mandarin speaker:

```powershell
.\.venv\Scripts\python.exe .\scripts\extract_csemotions_reference.py `
  C:\path\to\train-*.parquet `
  --speaker-id S01 `
  --emotion Neutral `
  --count 3 `
  --output .\references\mandarin_reader
```

The command writes Fish Speech-ready `sample_XX.wav` and `sample_XX.lab`
pairs. After listening to the output and checking the transcripts, set:

```dotenv
READIO_FISH_REFERENCE_ID=mandarin_reader
```

Export the complete local dataset for browsing or offline processing:

```powershell
.\.venv\Scripts\python.exe .\scripts\extract_csemotions_reference.py `
  .\CSEMOTIONS\data\*.parquet `
  --export-all `
  --output .\data\csemotions-extracted
```

This creates `data/csemotions-extracted/<speaker>/<emotion>/` containing WAV
and LAB pairs plus a UTF-8 tab-separated `manifest.tsv`. This bulk export is
local working data and is excluded from git.

## GPU Practicality

The laptop has 8 GB of GPU memory. Fish Speech v1.5 documentation reports a
real-time factor around 1:5 on an RTX 4060 laptop, making v1.5 the intended
first deployment target. Do not substitute the current `latest`/S2 server
image: current Fish Audio CUDA deployment guidance requires more GPU memory
than this laptop provides.

For a single GPU, process utterances sequentially as this starter does. Running
several inference requests concurrently usually increases memory pressure
without improving throughput.

## Fish Speech Settings

The v1.5 provider settings can be configured through `.env`:

```dotenv
READIO_FISH_CHUNK_LENGTH=200
READIO_FISH_MAX_NEW_TOKENS=1024
READIO_FISH_TOP_P=0.7
READIO_FISH_TEMPERATURE=0.7
READIO_FISH_REPETITION_PENALTY=1.2
READIO_FISH_NORMALIZE=true
READIO_FISH_USE_MEMORY_CACHE=on
```

These defaults match the self-hosted Fish Speech v1.5 request schema. Keep
`READIO_FISH_USE_MEMORY_CACHE=on` when synthesizing many sentences with one
reference narrator.

Set `READIO_SENTENCE_GAP_MS` to tune pauses between independently generated
sentence clips. `600` ms is the evidence-based starting default for Mandarin
narration. A later paragraph-aware API can reserve pauses around `1200` ms for
stronger paragraph or scene transitions.

## Tests

```powershell
pytest
```

The tests validate v1.5 request construction, incremental WAV assembly,
contiguous timestamp calculation, job completion, and chapter size rejection
without needing a GPU or Fish Speech.

## Production Follow-Ups

This starter persists task status and completed audio beneath `data/jobs/`,
with automatic expiry for terminal tasks. Synthesis execution still belongs to
the running gateway process; a restart during generation marks the task failed.
Before serving real books, add resumable processing and API authentication.
