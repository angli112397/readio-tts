# Readio TTS

`readio-tts` is a local TTS gateway for an Android reading app. Android sends
an already segmented chapter. The gateway generates one complete WAV file and
a sentence timing manifest for offline playback.

The basic behavior is:

- Android receives a complete chapter artifact that supports random seeking.
- The gateway retains output only until Android confirms it has persisted it.
- Sentence audio is checkpointed internally so an interrupted gateway can
  continue a long-running chapter instead of starting over.

## Android API

Android owns chapter text, sentence segmentation, downloaded artifacts, and
offline playback. The gateway owns asynchronous synthesis and temporary
server-side job data.

`READIO_API_TOKEN` is required at startup. Android includes
`Authorization: Bearer <token>` in every `/v1/*` request. `/health` remains
unauthenticated for the container health check.

The complete public API is:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/v1/voices` | Upload one reference voice WAV and its transcript. |
| `GET` | `/v1/voices` | List installed voices. |
| `GET` | `/v1/voices/{voice_id}` | Get voice metadata. |
| `GET` | `/v1/voices/{voice_id}/audio` | Download the uploaded reference WAV. |
| `DELETE` | `/v1/voices/{voice_id}` | Delete a voice from future job selection. |
| `POST` | `/v1/jobs` | Submit one already-segmented chapter for synthesis. |
| `GET` | `/v1/jobs/{job_id}` | Poll progress and obtain artifact URLs after success. |
| `GET` | `/v1/jobs/{job_id}/audio` | Download the completed chapter WAV. |
| `GET` | `/v1/jobs/{job_id}/manifest` | Download sentence timing metadata. |
| `DELETE` | `/v1/jobs/{job_id}` | Cancel generation or remove downloaded/failed job data. |

### Client Flow

1. Android segments a chapter into stable sentence IDs and calls `POST /v1/jobs`.
2. Android stores the returned `job_id` and periodically calls `GET /v1/jobs/{job_id}`.
3. While the response state is `queued` or `running`, Android continues polling.
4. When state is `succeeded`, Android downloads both `audio_url` and `manifest_url`.
5. After both files are safely persisted locally, Android calls `DELETE /v1/jobs/{job_id}`.
6. When state is `failed`, Android displays or logs `error`, then calls `DELETE`; a user retry is a new submission.

### Create A Job

```http
POST /v1/jobs
Authorization: Bearer <token>
Idempotency-Key: book-1-chapter-12-reader-v1
Content-Type: application/json
```

```json
{
  "chapter_id": "book-1/chapter-12",
  "voice_id": "my_mandarin_narrator",
  "text_language": "zh",
  "sentence_gap_ms": 600,
  "sentences": [
    {
      "id": "s000001",
      "text": "夜色渐渐沉了下来。",
      "paragraph_index": 0
    },
    {
      "id": "s000002",
      "text": "远处的灯火在风中轻轻摇曳。",
      "paragraph_index": 0
    }
  ]
}
```

Fields:

| Field | Required | Meaning |
| --- | --- | --- |
| `Authorization` header | yes | `Bearer <READIO_API_TOKEN>` for all `/v1/*` requests. |
| `Idempotency-Key` header | yes | Stable key for this chapter, voice, and settings. Retrying the same submission returns the existing job. |
| `chapter_id` | yes | Android's chapter identifier. |
| `voice_id` | yes | Installed voice ID; use ASCII letters, digits, `_`, or `-`. |
| `text_language` | yes | Language of the chapter text: `zh`, `en`, `ja`, `ko`, or `yue`. |
| `sentence_gap_ms` | no | Silence between generated sentences, default `600`, range `0-5000`. |
| `sentences[].id` | yes | Stable Android sentence identifier; unique within the chapter. |
| `sentences[].text` | yes | Text sent to GPT-SoVITS. |
| `sentences[].paragraph_index` | no | Paragraph position, default `0`. |

Response: `202 Accepted`

Headers:

```http
Location: http://192.168.1.6:8090/v1/jobs/f4a17b6a-9b1b-4bc2-b7f6-d87577835d53
Retry-After: 5
```

```json
{
  "job_id": "f4a17b6a-9b1b-4bc2-b7f6-d87577835d53",
  "state": "queued",
  "status_url": "http://192.168.1.6:8090/v1/jobs/f4a17b6a-9b1b-4bc2-b7f6-d87577835d53"
}
```

If the same `Idempotency-Key` is submitted with different content, the
gateway returns `409 Conflict` rather than serving the wrong chapter artifact.
Unknown JSON fields are rejected with `422`, so client and server contract
drift is visible during development.
The default `READIO_MAX_CHAPTER_CHARACTERS=500000` accommodates the tested
full-volume workload of approximately `370000` characters.

### Poll Job Status

```http
GET /v1/jobs/{job_id}
```

Queued response:

```json
{
  "job_id": "f4a17b6a-9b1b-4bc2-b7f6-d87577835d53",
  "chapter_id": "book-1/chapter-12",
  "state": "queued",
  "progress": {
    "sentences_completed": 0,
    "sentences_total": 2210
  },
  "queue_position": 1,
  "created_at": "2026-05-26T08:00:00Z",
  "updated_at": "2026-05-26T08:00:00Z"
}
```

`queue_position` is returned only for `queued` jobs. Position `1` means the
job will run next; a currently running job occupies position `1`, so the
first waiting job is then position `2`.

If a queued or running task cannot advance because no worker heartbeat has
been received recently, the same response includes:

```json
{
  "state": "queued",
  "queue_position": 1,
  "blocked_by": "worker_unavailable"
}
```

Android may show a service-not-running message while continuing to poll.
`blocked_by` is diagnostic state, not terminal failure.

While processing:

```json
{
  "job_id": "f4a17b6a-9b1b-4bc2-b7f6-d87577835d53",
  "chapter_id": "book-1/chapter-12",
  "state": "running",
  "progress": {
    "sentences_completed": 384,
    "sentences_total": 2210
  },
  "created_at": "2026-05-26T08:00:00Z",
  "updated_at": "2026-05-26T09:42:00Z",
  "heartbeat_at": "2026-05-26T09:42:00Z"
}
```

When complete:

```json
{
  "job_id": "f4a17b6a-9b1b-4bc2-b7f6-d87577835d53",
  "chapter_id": "book-1/chapter-12",
  "state": "succeeded",
  "progress": {
    "sentences_completed": 2210,
    "sentences_total": 2210
  },
  "created_at": "2026-05-26T08:00:00Z",
  "updated_at": "2026-05-26T13:12:00Z",
  "artifact": {
    "audio_url": "http://192.168.1.6:8090/v1/jobs/f4a17b6a-9b1b-4bc2-b7f6-d87577835d53/audio",
    "manifest_url": "http://192.168.1.6:8090/v1/jobs/f4a17b6a-9b1b-4bc2-b7f6-d87577835d53/manifest",
    "mime_type": "audio/wav",
    "size_bytes": 1589000000,
    "sha256": "45a0..."
  }
}
```

Failed response:

```json
{
  "job_id": "f4a17b6a-9b1b-4bc2-b7f6-d87577835d53",
  "chapter_id": "book-1/chapter-12",
  "state": "failed",
  "progress": {
    "sentences_completed": 384,
    "sentences_total": 2210
  },
  "created_at": "2026-05-26T08:00:00Z",
  "updated_at": "2026-05-26T09:42:00Z",
  "error": {
    "code": "tts_unavailable",
    "message": "GPT-SoVITS is unavailable.",
    "sentence_id": "s000385"
  }
}
```

`heartbeat_at` is the time of the most recent progress update while a job is
running. Android may display it for diagnostics, but should determine task
completion from `state`, not infer failure from heartbeat age.

All immediate HTTP failures use the same compact shape:

```json
{
  "error": {
    "code": "invalid_request",
    "message": "Invalid sentences: Field required."
  }
}
```

Android should branch on `error.code` and use `error.message` for diagnostics
or display. For sentence synthesis failures, `error.sentence_id` identifies
the Android sentence that could not be generated. Detailed GPT request
context is retained in server logs rather than returned to the device.

### Download Artifacts

```http
GET /v1/jobs/{job_id}/audio
GET /v1/jobs/{job_id}/manifest
```

The audio endpoint returns the complete chapter WAV and supports HTTP Range
requests, so Android can resume a large interrupted download.

The manifest is intentionally separate from status polling:

```json
{
  "chapter_id": "book-1/chapter-12",
  "voice_id": "my_mandarin_narrator",
  "text_language": "zh",
  "duration_ms": 18000000,
  "sentence_gap_ms": 600,
  "sentences": [
    {
      "id": "s000001",
      "paragraph_index": 0,
      "begin_ms": 0,
      "end_ms": 2840
    },
    {
      "id": "s000002",
      "paragraph_index": 0,
      "begin_ms": 3440,
      "end_ms": 6810
    }
  ]
}
```

Android already owns sentence text, so the manifest carries only sentence
identity and timing data.

### Delete Or Cancel

After Android has safely stored both artifacts, or when a user no longer wants
the generation task:

```http
DELETE /v1/jobs/{job_id}
```

Response: `204 No Content`. Deletion is idempotent and immediately removes
the job record and its cached files. If a GPT-SoVITS sentence request is
already running, that single request cannot be interrupted; its response is
discarded when it returns.
Successful jobs that are never deleted expire after
`READIO_JOB_RETENTION_DAYS`.

### Status Codes

| Request | Status | Android Handling |
| --- | --- | --- |
| Missing or incorrect API token | `401` | Ask the user to check server connection settings. |
| Valid `POST /v1/jobs` | `202` | Persist `job_id`, then begin polling. |
| Repeated identical `Idempotency-Key` | `202` | Reuse the returned existing `job_id`. |
| Reused key with different request body | `409` | Treat as an app bug or create an intentional new submission with a new key. |
| Invalid body, missing voice, or invalid `voice_id` | `422` | Treat as request/configuration error; do not poll. |
| Chapter exceeds server character limit | `413` | Split the chapter into separate offline artifacts. |
| Unknown or deleted job queried | `404` | Remove stale local pending-job tracking. |
| Audio or manifest requested before success | `409` | Continue polling job status. |
| Any `DELETE /v1/jobs/{job_id}` | `204` | Consider server-side temporary data removed. |

Error codes:

| Code | Returned For | Android Handling |
| --- | --- | --- |
| `unauthorized` | Missing or incorrect bearer token. | Check server connection settings; do not poll. |
| `invalid_request` | Invalid JSON fields or missing required input. | Correct the request; do not poll. |
| `voice_unavailable` | A selected job voice is missing or its saved audio is missing. | Prompt for another installed voice. |
| `invalid_voice_audio` | A voice upload is not a valid PCM WAV. | Ask the user to upload a valid WAV. |
| `voice_not_found` | A queried voice no longer exists. | Remove stale local voice selection. |
| `voice_audio_not_found` | Saved voice metadata exists but its audio is absent. | Delete and upload the voice again. |
| `chapter_too_large` | Text exceeds the server size limit. | Split into separate artifacts. |
| `idempotency_conflict` | A key was reused with different content. | Generate a new key for the new request. |
| `job_not_found` | A queried job no longer exists. | Remove stale local tracking. |
| `artifact_not_ready` | Download requested before `succeeded`. | Continue polling. |
| `artifact_not_found` | A completed artifact is missing on disk. | Discard the job and resubmit if needed. |
| `tts_request_rejected` | GPT-SoVITS rejected sentence/reference input. | Show/log the message; resubmit after fixing the input. |
| `tts_unavailable` | GPT-SoVITS was unavailable after one retry. | Show/log failure; allow a new submission. |
| `invalid_tts_response` | GPT-SoVITS returned unusable audio. | Show/log failure; allow a new submission. |
| `reference_snapshot_missing` | Job-local narrator files are missing. | Delete the job and submit again. |
| `reference_snapshot_invalid` | Job-local voice snapshot is invalid. | Delete the job and resubmit. |
| `artifact_publication_failed` | Final WAV or manifest could not be published. | Delete and submit again after checking storage. |
| `internal_error` | Unexpected server failure. | Show/log failure and inspect server logs. |

## Job States

Android only needs four observable states:

| State | Meaning | Android Action |
| --- | --- | --- |
| `queued` | Accepted and waiting for the single worker. | Continue polling. |
| `running` | Worker is generating or finalizing the chapter. | Continue polling. |
| `succeeded` | WAV and manifest are ready. | Download both, persist locally, then `DELETE`. |
| `failed` | Generation stopped with a terminal error. | Show or log `error`, then `DELETE`; resubmit only if desired. |

The worker retries one sentence synthesis call once only for temporary
GPT-SoVITS availability failures. Invalid input is failed immediately. A
worker restart does not create a new Android-visible state: a `queued` or
`running` job continues from saved sentence WAV checkpoints when the single
worker runs again.

The worker records one small global heartbeat in SQLite while running.
`GET /v1/jobs/{job_id}` uses it only to expose
`blocked_by: "worker_unavailable"` for active work that cannot advance.

## Server Processing

The API process stores task metadata in local SQLite and returns status
queries; a separate single worker process performs GPU synthesis. The worker
synthesizes one sentence at a time to retain deterministic sentence
boundaries, storing sentence WAV checkpoints under
`READIO_DATA_DIR/jobs/<job_id>/segments/`. On worker restart, any queued or
running task resumes from its existing consecutive segment files.

When a job is submitted, its selected reference audio and transcript are
copied into `READIO_DATA_DIR/jobs/<job_id>/input/`. This snapshot prevents
voice deletion or replacement from changing a chapter midway through
generation.

Do not change the GPT-SoVITS model configuration while a job is running.
Model upgrades should be applied after active jobs have finished or been
deleted.

The final format remains WAV because it preserves predictable seek timing for
offline synchronized reading.

### Stored Metadata

SQLite keeps job state and a minimal immutable `voices` catalog. The voice
catalog is prepared for client-managed reference uploads:

| Voice field | Purpose |
| --- | --- |
| `voice_id` | Stable ID selected by synthesis jobs. |
| `display_name` | Name shown by the client. |
| `reference_language` | Language spoken in the reference recording. |
| `transcript` | Reference recording text supplied to GPT-SoVITS as `prompt_text`. |
| `duration_ms` | Basic upload validation and diagnostics. |
| `audio_size_bytes`, `audio_sha256` | Local storage and integrity diagnostics. |
| `created_at` | Display order and troubleshooting. |

Voice records are immutable: they are created or deleted, not edited.
Reference audio is stored at `READIO_DATA_DIR/voices/<voice_id>/reference.wav`,
so the database does not need an audio path field. A submitted job immediately
snapshots its voice input; deleting a voice does not interrupt existing jobs.

## GPT-SoVITS Setup

Upload a voice with `multipart/form-data`:

```http
POST /v1/voices
Authorization: Bearer <token>
Content-Type: multipart/form-data
```

| Field | Meaning |
| --- | --- |
| `display_name` | Client-facing name for the voice. |
| `reference_language` | Language spoken in the uploaded WAV: `zh`, `en`, `ja`, `ko`, or `yue`. |
| `transcript` | Exact spoken text of the reference WAV. |
| `audio` | Non-empty PCM WAV reference file. |

The job `text_language` and voice `reference_language` may differ. For
example, to use a Chinese narrator voice for an English book:

```json
{
  "chapter_id": "english-book/chapter-1",
  "voice_id": "my_mandarin_narrator",
  "text_language": "en",
  "sentences": [
    {
      "id": "s000001",
      "text": "The rain had stopped before midnight."
    }
  ]
}
```

For the most natural English narration, upload an English reference recording
with `reference_language=en`.

### Voice Preview

Voice preview uses the ordinary job API. After uploading a voice, Android can
submit a short preview passage with that `voice_id`, poll it, download and
play the result, then either retain the voice or delete the voice and preview
job. The gateway does not maintain a separate preview workflow.

GPT-SoVITS weights are stored outside this repository under
`READIO_GPT_MODELS_DIR` and mounted read-only into the container by
[compose.gpt.yaml](./compose.gpt.yaml). Voice recordings and job data are
stored under `READIO_DATA_DIR`.

### Model Configuration

The deployment uses GPT-SoVITS `v2ProPlus`, selected for its improved
narration quality in local listening tests. It requires these local pretrained
assets:

```text
models/gpt-sovits/
  s1v3.ckpt
  v2Pro/s2Gv2ProPlus.pth
```

The tracked runtime configuration and startup adapter are stored in
`deployment/gpt_sovits/` and injected through Compose `configs`; they are not
external user data. GPT-SoVITS writes back to its runtime configuration while
initializing, so the adapter copies the template to a writable temporary
file. When required by the pinned upstream image, it also exposes the bundled
`G2PWModel` at GPT-SoVITS's expected Chinese frontend path. This does not
require a separate host mount.

Start GPT-SoVITS:

```powershell
docker compose -f compose.yaml -f compose.gpt.yaml up -d gpt-sovits
```

Do not use the first request after model installation as a performance
measurement: GPT-SoVITS may download additional assets during that request.
One warm-run result on the RTX 4060 Laptop GPU was:

| Model | Output audio | Generation wall time | RTF |
| --- | ---: | ---: | ---: |
| `v2ProPlus` | `35.44 s` | `15.06 s` | `0.425` |

Generated local auditions should be stored under `READIO_DATA_DIR/samples/`.

## Local Development

Start GPT-SoVITS in Docker, then run the API and worker locally in separate
terminals:

```powershell
docker compose -f compose.yaml -f compose.gpt.yaml -f compose.gpt.dev.yaml up -d gpt-sovits
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
$env:READIO_DATA_DIR = Join-Path $env:LOCALAPPDATA "ReadioTTS\data"
$env:READIO_GPT_BASE_URL = "http://127.0.0.1:9880"
uvicorn readio_tts.api:app --reload --host 0.0.0.0 --port 8090
```

Second terminal:

```powershell
cd C:\Users\angli\OneDrive\Documents\readio-tts
.\.venv\Scripts\Activate.ps1
$env:READIO_DATA_DIR = Join-Path $env:LOCALAPPDATA "ReadioTTS\data"
$env:READIO_GPT_BASE_URL = "http://127.0.0.1:9880"
python -m readio_tts.worker
```

Keep `READIO_DATA_DIR` outside OneDrive or another synchronized folder. It
contains SQLite metadata, reference snapshots, sentence checkpoints, and
completed artifacts. Run exactly one worker for the single local GPU.

Relevant `.env` values:

```dotenv
READIO_PROVIDER=gpt
READIO_DATA_DIR=C:/Users/angli/AppData/Local/ReadioTTS/data
READIO_GPT_MODELS_DIR=C:/Users/angli/AppData/Local/ReadioTTS/models/gpt-sovits
READIO_API_TOKEN=replace-with-a-long-random-token
READIO_WORKER_STALE_SECONDS=30
READIO_GPT_MODEL_REVISION=v2ProPlus
```

For local Python development, set `READIO_GPT_BASE_URL` in the terminal as
shown above. For the full Docker stack, Compose fixes its container-internal
GPT URL automatically.

Check readiness:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8090/health
```

The gateway readiness check reports API availability. The worker reports
GPT-SoVITS errors through the affected job state.

## Docker Deployment

```powershell
docker compose -f compose.yaml -f compose.gpt.yaml up -d --build
```

The stack starts `gateway`, one `worker`, and `gpt-sovits`. Only the gateway
is published to the laptop LAN. GPT-SoVITS is internal in this deployment;
the development overlay publishes it on localhost only when running the
gateway or worker outside Docker.

Persistent host storage is limited to:

```text
ReadioTTS/
  data/
    readio.sqlite3
    voices/
    jobs/
  models/
    gpt-sovits/
```

`data/` contains user and task data. `models/` contains replaceable downloaded
model assets. The GPT-SoVITS container sees its model directory and
`data/jobs/` read-only; it does not read SQLite or the original voice store.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```
