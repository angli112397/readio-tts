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

The complete public API is:

| Method | Path | Purpose |
| --- | --- | --- |
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
Idempotency-Key: book-1-chapter-12-reader-v1
Content-Type: application/json
```

```json
{
  "chapter_id": "book-1/chapter-12",
  "voice_id": "my_mandarin_narrator",
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
| `Idempotency-Key` header | yes | Stable key for this chapter, voice, and settings. Retrying the same submission returns the existing job. |
| `chapter_id` | yes | Android's chapter identifier. |
| `voice_id` | yes | Folder name under `references/gpt/`; use ASCII letters, digits, `_`, or `-`. |
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
  "created_at": "2026-05-26T08:00:00Z",
  "updated_at": "2026-05-26T08:00:00Z"
}
```

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
| `invalid_request` | Invalid JSON fields or missing required input. | Correct the request; do not poll. |
| `invalid_voice_profile` | The selected local narrator is missing or invalid. | Prompt for another installed voice. |
| `chapter_too_large` | Text exceeds the server size limit. | Split into separate artifacts. |
| `idempotency_conflict` | A key was reused with different content. | Generate a new key for the new request. |
| `job_not_found` | A queried job no longer exists. | Remove stale local tracking. |
| `artifact_not_ready` | Download requested before `succeeded`. | Continue polling. |
| `artifact_not_found` | A completed artifact is missing on disk. | Discard the job and resubmit if needed. |
| `tts_request_rejected` | GPT-SoVITS rejected sentence/reference input. | Show/log the message; resubmit after fixing the input. |
| `tts_unavailable` | GPT-SoVITS was unavailable after one retry. | Show/log failure; allow a new submission. |
| `invalid_tts_response` | GPT-SoVITS returned unusable audio. | Show/log failure; allow a new submission. |
| `reference_snapshot_missing` | Job-local narrator files are missing. | Delete the job and submit again. |
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

## Server Processing

The API process stores task metadata in local SQLite and returns status
queries; a separate single worker process performs GPU synthesis. The worker
synthesizes one sentence at a time to retain deterministic sentence
boundaries, storing sentence WAV checkpoints under
`READIO_DATA_DIR/jobs/<job_id>/segments/`. On worker restart, any queued or
running task resumes from its existing consecutive segment files.

When a job is submitted, its selected reference audio and transcript are
copied into `READIO_DATA_DIR/jobs/<job_id>/input/`. This snapshot prevents
changes to `references/gpt/<voice_id>/` from changing a chapter midway
through generation.

Do not change the GPT-SoVITS model configuration while a job is running.
Model upgrades should be applied after active jobs have finished or been
deleted.

The final format remains WAV because it preserves predictable seek timing for
offline synchronized reading.

## GPT-SoVITS Setup

Voice samples live under `references/gpt/`:

```text
references/gpt/my_mandarin_narrator/
  sample_0004.wav
  sample_0004.lab
```

The first audio file with a matching `.lab` or `.txt` transcript is supplied
to GPT-SoVITS as the selected voice profile.

GPT-SoVITS weights are kept locally in `GPT-SoVITS/` and mounted into the
container by [compose.gpt.yaml](./compose.gpt.yaml). The model directory and
voice recordings are ignored by Git.

### Model Configuration

The deployment uses GPT-SoVITS `v2ProPlus`, selected for its improved
narration quality in local listening tests. It requires these local pretrained
assets:

```text
GPT-SoVITS/
  s1v3.ckpt
  v2Pro/s2Gv2ProPlus.pth
```

The tracked runtime configuration is stored in
`deployment/gpt_sovits/tts_infer.yaml`. GPT-SoVITS writes back to its runtime
config while initializing, so Compose copies this template to a writable
temporary file inside the container before starting the API server.
The startup script in `deployment/gpt_sovits/start-api.sh` exposes the
`G2PWModel` already bundled in the upstream image at GPT-SoVITS's expected
Chinese frontend path. This avoids both an additional local model copy and a
first-request model download.

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

Generated local auditions are stored under `data/samples/`, which is ignored
by Git.

## Local Development

Start GPT-SoVITS in Docker, then run the API and worker locally in separate
terminals:

```powershell
docker compose -f compose.yaml -f compose.gpt.yaml -f compose.gpt.dev.yaml up -d gpt-sovits
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
$env:READIO_DATA_DIR = Join-Path $env:LOCALAPPDATA "ReadioTTS"
uvicorn readio_tts.api:app --reload --host 0.0.0.0 --port 8090
```

Second terminal:

```powershell
cd C:\Users\angli\OneDrive\Documents\readio-tts
.\.venv\Scripts\Activate.ps1
$env:READIO_DATA_DIR = Join-Path $env:LOCALAPPDATA "ReadioTTS"
python -m readio_tts.worker
```

Keep `READIO_DATA_DIR` outside OneDrive or another synchronized folder. It
contains SQLite metadata, reference snapshots, sentence checkpoints, and
completed artifacts. Run exactly one worker for the single local GPU.

Relevant `.env` values:

```dotenv
READIO_PROVIDER=gpt
READIO_DATA_DIR=C:/Users/angli/AppData/Local/ReadioTTS
READIO_GPT_BASE_URL=http://127.0.0.1:9880
READIO_GPT_MODEL_REVISION=v2ProPlus
READIO_GPT_REFERENCE_DIR=references/gpt
```

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
is published to the laptop LAN; GPT-SoVITS is exposed on localhost for
development access.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```
