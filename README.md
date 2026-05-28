# Readio TTS

`readio-tts` is a local TTS gateway for an Android reading app. Android sends
an already-segmented chapter; the gateway produces one complete WAV file and
a sentence timing manifest for offline playback.

- Android receives a complete chapter artifact that supports random seeking.
- The gateway retains output only until Android confirms it has persisted it.
- Sentence audio is checkpointed so an interrupted worker can resume a long
  chapter instead of starting over.

## Android API

Android owns chapter text, sentence segmentation, downloaded artifacts, and
offline playback. The gateway owns asynchronous synthesis and temporary
server-side job data.

`READIO_API_TOKEN` is required at startup. Android includes
`Authorization: Bearer <token>` in every `/v1/*` request. `/health` is
unauthenticated for the container health check.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/v1/voices` | Upload one 3–10 second reference WAV and its transcript. |
| `GET` | `/v1/voices` | List installed voices. |
| `GET` | `/v1/voices/{voice_id}` | Get voice metadata. |
| `GET` | `/v1/voices/{voice_id}/audio` | Download the uploaded reference WAV. |
| `DELETE` | `/v1/voices/{voice_id}` | Delete a voice from future job selection. |
| `POST` | `/v1/jobs` | Submit one already-segmented chapter for synthesis. |
| `GET` | `/v1/jobs/{job_id}` | Poll progress and obtain artifact URLs after success. |
| `GET` | `/v1/jobs/{job_id}/audio` | Download the completed chapter WAV. |
| `GET` | `/v1/jobs/{job_id}/manifest` | Download sentence timing metadata. |
| `DELETE` | `/v1/jobs/{job_id}` | Cancel generation or remove finished/failed job data. |

### Client Flow

1. Android segments a chapter into stable sentence IDs and calls `POST /v1/jobs`.
2. Android persists the returned `job_id` and periodically calls `GET /v1/jobs/{job_id}`.
3. While state is `queued` or `running`, Android continues polling.
4. When state is `succeeded`, Android downloads both `audio_url` and `manifest_url`.
5. After both files are safely persisted locally, Android calls `DELETE /v1/jobs/{job_id}`.
6. When state is `failed`, Android shows or logs `error`, then calls `DELETE`; a user retry is a new submission.

When the user cancels a running generation, Android calls `DELETE /v1/jobs/{job_id}` and
treats `204` as final. It must not automatically submit a replacement job until the user
requests generation again.

### Create a Job

```http
POST /v1/jobs
Authorization: Bearer <token>
Idempotency-Key: book-1-chapter-12-reader-v1
Content-Type: application/json; charset=utf-8
```

```json
{
  "chapter_id": "book-1/chapter-12",
  "voice_id": "my_mandarin_narrator",
  "text_language": "zh",
  "sentence_gap_ms": 600,
  "sentences": [
    { "id": "s000001", "text": "夜色渐渐沉了下来。", "paragraph_index": 0 },
    { "id": "s000002", "text": "远处的灯火在风中轻轻摇曳。", "paragraph_index": 0 }
  ]
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `Authorization` header | yes | `Bearer <READIO_API_TOKEN>` on all `/v1/*` requests. |
| `Idempotency-Key` header | yes | Stable key for this chapter+voice+settings. Retrying returns the existing job. |
| `chapter_id` | yes | Android's chapter identifier. |
| `voice_id` | yes | Installed voice ID (ASCII letters, digits, `_`, `-`). |
| `text_language` | yes | `zh`, `en`, `ja`, `ko`, or `yue`. |
| `sentence_gap_ms` | no | Silence between sentences; default `600`, range `0–5000`. |
| `sentences[].id` | yes | Stable Android sentence ID; unique within the chapter. |
| `sentences[].text` | yes | Text sent to GPT-SoVITS. Send as UTF-8. |
| `sentences[].paragraph_index` | no | Paragraph position; default `0`. |

Response: `202 Accepted`

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

Reusing the same `Idempotency-Key` with different content returns `409`. Unknown
JSON fields are rejected with `422`. The default `READIO_MAX_CHAPTER_CHARACTERS=500000`
accommodates the tested full-volume workload of approximately 370 000 characters.

### Poll Job Status

```http
GET /v1/jobs/{job_id}
```

**Queued:**

```json
{
  "job_id": "...",
  "chapter_id": "book-1/chapter-12",
  "state": "queued",
  "progress": { "sentences_completed": 0, "sentences_total": 2210 },
  "queue_position": 1,
  "created_at": "2026-05-26T08:00:00Z",
  "updated_at": "2026-05-26T08:00:00Z"
}
```

`queue_position` is present only for `queued` jobs. Position `1` means next to run; a
currently running job occupies position `1`, so the first waiting job shows `2`.

If no worker heartbeat has been received recently the response also includes:

```json
{ "state": "queued", "queue_position": 1, "blocked_by": "worker_unavailable" }
```

`blocked_by` is diagnostic, not terminal. Android may show a "service unavailable"
message and continue polling.

**Running:**

```json
{
  "state": "running",
  "progress": { "sentences_completed": 384, "sentences_total": 2210 },
  "heartbeat_at": "2026-05-26T09:42:00Z"
}
```

**Succeeded:**

```json
{
  "state": "succeeded",
  "progress": { "sentences_completed": 2210, "sentences_total": 2210 },
  "artifact": {
    "audio_url": "http://192.168.1.6:8090/v1/jobs/.../audio",
    "manifest_url": "http://192.168.1.6:8090/v1/jobs/.../manifest",
    "mime_type": "audio/wav",
    "size_bytes": 1589000000,
    "sha256": "45a0..."
  }
}
```

**Failed:**

```json
{
  "state": "failed",
  "progress": { "sentences_completed": 384, "sentences_total": 2210 },
  "error": {
    "code": "tts_unavailable",
    "message": "GPT-SoVITS is unavailable.",
    "sentence_id": "s000385"
  }
}
```

`heartbeat_at` shows the most recent worker ping. Android should determine completion
from `state`, not from heartbeat age. Branch on `error.code`; use `error.message` for
diagnostics or display. `error.sentence_id` identifies which sentence triggered the
failure. Detailed GPT request context is in server logs only.

All immediate HTTP errors use the same shape:

```json
{ "error": { "code": "invalid_request", "message": "Invalid sentences: Field required." } }
```

### Download Artifacts

```http
GET /v1/jobs/{job_id}/audio
GET /v1/jobs/{job_id}/manifest
```

The audio endpoint returns the complete chapter WAV and supports HTTP Range requests,
so Android can resume a large interrupted download.

Manifest format:

```json
{
  "chapter_id": "book-1/chapter-12",
  "voice_id": "my_mandarin_narrator",
  "text_language": "zh",
  "duration_ms": 18000000,
  "sentence_gap_ms": 600,
  "sentences": [
    { "id": "s000001", "paragraph_index": 0, "begin_ms": 0,    "end_ms": 2840 },
    { "id": "s000002", "paragraph_index": 0, "begin_ms": 3440, "end_ms": 6810 }
  ]
}
```

Android already owns sentence text, so the manifest carries only identity and timing.

### Delete or Cancel

```http
DELETE /v1/jobs/{job_id}
```

`204 No Content`. Idempotent; immediately removes the job record and its files. If a
GPT-SoVITS request is already in flight, that single request is not interrupted; its
response is discarded when it returns. Successful jobs that are never deleted expire
after `READIO_JOB_RETENTION_DAYS`.

### Status Codes

| Request | Status | Android Handling |
| --- | --- | --- |
| Missing or incorrect API token | `401` | Ask the user to check server connection settings. |
| Valid `POST /v1/jobs` | `202` | Persist `job_id`, then begin polling. |
| Repeated identical `Idempotency-Key` | `202` | Reuse the returned existing `job_id`. |
| Reused key with different request body | `409` | Treat as an app bug or create a new submission with a new key. |
| Invalid body, missing voice, or invalid `voice_id` | `422` | Treat as request/configuration error; do not poll. |
| Chapter exceeds server character limit | `413` | Split the chapter into separate offline artifacts. |
| Unknown or deleted job queried | `404` | Remove stale local pending-job tracking. |
| Audio or manifest requested before success | `409` | Continue polling job status. |
| Any `DELETE /v1/jobs/{job_id}` | `204` | Server-side temporary data removed. |

### Error Codes

| Code | Returned For | Android Handling |
| --- | --- | --- |
| `unauthorized` | Missing or incorrect bearer token. | Check server settings; do not poll. |
| `invalid_request` | Invalid JSON fields or missing required input. | Correct the request; do not poll. |
| `voice_unavailable` | Selected voice is missing or its audio file is absent. | Prompt for another installed voice. |
| `invalid_voice_audio` | Upload is not a valid 3–10 second PCM WAV. | Ask the user to upload a valid reference sample. |
| `voice_not_found` | Queried voice no longer exists. | Remove stale local voice selection. |
| `voice_audio_not_found` | Voice metadata exists but audio is absent on disk. | Delete and re-upload the voice. |
| `chapter_too_large` | Text exceeds the server size limit. | Split into separate artifacts. |
| `idempotency_conflict` | Key reused with different content. | Generate a new key for the new request. |
| `job_not_found` | Queried job no longer exists. | Remove stale local tracking. |
| `artifact_not_ready` | Download requested before `succeeded`. | Continue polling. |
| `artifact_not_found` | Completed artifact is missing on disk. | Discard and resubmit if needed. |
| `tts_request_rejected` | Engine rejected the sentence or reference input. | Show message; fix input and resubmit. |
| `tts_unavailable` | GPT-SoVITS was unavailable after one retry. | Show/log failure; allow a new submission. |
| `invalid_tts_response` | GPT-SoVITS returned unusable audio. | Show/log failure; allow a new submission. |
| `reference_snapshot_missing` | Job-local narrator files are missing. | Delete the job and resubmit. |
| `reference_snapshot_invalid` | Job-local voice snapshot is corrupt. | Delete the job and resubmit. |
| `artifact_publication_failed` | Final WAV or manifest could not be written. | Delete and resubmit after checking storage. |
| `internal_error` | Unexpected server failure. | Show/log failure; inspect server logs. |

## Job States

| State | Meaning | Android Action |
| --- | --- | --- |
| `queued` | Accepted and waiting for the single worker. | Continue polling. |
| `running` | Worker is generating or finalizing the chapter. | Continue polling. |
| `succeeded` | WAV and manifest are ready. | Download both, persist locally, then `DELETE`. |
| `failed` | Generation stopped with a terminal error. | Show or log `error`, then `DELETE`; resubmit only on user request. |

The worker retries a sentence synthesis call once for transient GPT-SoVITS failures.
If the job is cancelled during the retry wait, that retry is not sent. Invalid input
fails immediately. A worker restart is not a new Android-visible state: a `queued` or
`running` job resumes from the saved PCM checkpoint and SQLite sentence progress.

## Server Processing

The API process stores job metadata in SQLite and serves status queries. A separate
single worker process performs GPU synthesis — one sentence at a time for deterministic
sentence boundaries. Each successful sentence is appended to
`READIO_DATA_DIR/jobs/<job_id>/audio.partial.raw`; SQLite then atomically records
`completed_sentences`, committed PCM frames, and sentence begin/end timing. On restart,
any active job truncates the partial raw audio back to the last SQLite checkpoint and
resumes from the next sentence.

When a job is submitted, the selected reference audio and transcript are copied into
`READIO_DATA_DIR/jobs/<job_id>/snapshot/`. This snapshot prevents voice deletion or
replacement from affecting a chapter that is mid-generation.

The final format is WAV because it preserves predictable seek timing for offline
synchronized reading. Do not change the GPT-SoVITS model configuration while a job
is running; apply model upgrades after active jobs have finished or been deleted.

### Stored Metadata

| Voice field | Purpose |
| --- | --- |
| `voice_id` | Stable ID referenced by jobs. |
| `display_name` | Name shown in the client. |
| `reference_language` | Language spoken in the reference recording. |
| `transcript` | Reference text supplied to GPT-SoVITS as `prompt_text`. |
| `duration_ms` | Enforces the 3–10 second reference range; used in diagnostics. |
| `audio_size_bytes`, `audio_sha256` | Storage and integrity diagnostics. |
| `created_at` | Display order and troubleshooting. |

Voice records are immutable: created or deleted, never edited. Reference audio is
stored at `READIO_DATA_DIR/voices/<voice_id>/reference.wav`. A submitted job snapshots
its voice immediately; deleting a voice does not interrupt existing jobs.

## GPT-SoVITS Setup

### Upload a Voice

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
| `audio` | PCM WAV file between 3 and 10 seconds long. |

`text_language` and `reference_language` may differ. To use a Mandarin narrator voice
for an English book, pass `"text_language": "en"` in the job request. For the most
natural results, the reference language should match the synthesis language.

### Voice Preview

Voice preview uses the ordinary job API. Upload a voice, submit a short preview passage
with that `voice_id`, poll it, download and play the result, then retain or discard the
voice and preview job. There is no separate preview workflow.

### Model Configuration

The deployment uses GPT-SoVITS `v2ProPlus`. Required local pretrained assets
(all placed under `READIO_GPT_MODELS_DIR`):

```text
models/gpt-sovits/
  s1v3.ckpt
  v2Pro/s2Gv2ProPlus.pth
  chinese-roberta-wwm-ext-large/   # BERT model for Chinese text frontend
  chinese-hubert-base/             # HuBERT model for reference audio encoding
```

The runtime configuration and startup adapter are in `deployment/gpt_sovits/` and
injected through Compose `configs`. GPT-SoVITS writes back to its runtime config on
startup, so the adapter copies the template to a writable temporary file.

Start GPT-SoVITS:

```powershell
docker compose -f compose.yaml -f compose.gpt.yaml up -d gpt-sovits
```

Do not use the first request after model installation as a performance benchmark —
GPT-SoVITS may download additional assets on that request. One warm-run result on an
RTX 4060 Laptop GPU:

| Model | Output audio | Wall time | RTF |
| --- | ---: | ---: | ---: |
| `v2ProPlus` | `35.44 s` | `15.06 s` | `0.425` |

## Local Development

Start GPT-SoVITS in Docker, then run the API and worker in separate terminals:

**Terminal 1 — API:**

```powershell
docker compose -f compose.yaml -f compose.gpt.yaml -f compose.gpt.dev.yaml up -d gpt-sovits
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
$env:READIO_DATA_DIR = Join-Path $env:LOCALAPPDATA "ReadioTTS\data"
$env:READIO_GPT_BASE_URL = "http://127.0.0.1:9880"
uvicorn readio_tts.api:app --reload --host 0.0.0.0 --port 8090
```

At startup the gateway logs its LAN IP addresses for easy Android pairing:

```
INFO  Gateway ready: http://192.168.1.6:PORT (configure port in uvicorn args)
```

**Terminal 2 — Worker:**

```powershell
.\.venv\Scripts\Activate.ps1
$env:READIO_DATA_DIR = Join-Path $env:LOCALAPPDATA "ReadioTTS\data"
$env:READIO_GPT_BASE_URL = "http://127.0.0.1:9880"
python -m readio_tts.worker
```

Keep `READIO_DATA_DIR` outside OneDrive or any cloud-synced folder. It contains SQLite
metadata, voice snapshots, sentence checkpoints, and completed artifacts. Run exactly
one worker for the single local GPU.

Log levels: the gateway terminal prints accepted and deleted jobs; the worker terminal
prints job start, completion, cancellation, and failure events. GPT-SoVITS inference
output is in its container logs:

```powershell
docker compose -f compose.yaml -f compose.gpt.yaml -f compose.gpt.dev.yaml logs -f gpt-sovits
```

Set `READIO_LOG_LEVEL=DEBUG` for additional application diagnostics. Client responses
expose stable error codes with safe messages; upstream GPT-SoVITS response details are
in server logs only.

**`.env` reference:**

```dotenv
READIO_PROVIDER=gpt
READIO_DATA_DIR=C:/Users/<user>/AppData/Local/ReadioTTS/data
READIO_GPT_MODELS_DIR=C:/Users/<user>/AppData/Local/ReadioTTS/models/gpt-sovits
READIO_API_TOKEN=replace-with-a-long-random-token
READIO_LOG_LEVEL=INFO
READIO_WORKER_STALE_SECONDS=30
READIO_GPT_MODEL_REVISION=v2ProPlus
```

Set `READIO_GPT_BASE_URL` in the terminal for local Python development. For the full
Docker stack, Compose sets the container-internal GPT URL automatically.

Check readiness:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8090/health
```

## Docker Deployment

```powershell
docker compose -f compose.yaml -f compose.gpt.yaml up -d --build
```

The stack starts `gateway`, one `worker`, and `gpt-sovits`. Only the gateway is
published to the LAN. GPT-SoVITS is internal; the development overlay exposes it on
`localhost` only when the gateway or worker runs outside Docker.

Host storage layout:

```text
ReadioTTS/
  data/
    readio.sqlite3
    voices/
    jobs/
  models/
    gpt-sovits/
```

`data/` holds user and task data. `models/` holds replaceable downloaded model assets.
The GPT-SoVITS container mounts `models/` and `data/jobs/` read-only; it does not
access SQLite or the voice store directly.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```
