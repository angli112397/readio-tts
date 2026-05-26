# Readio TTS

`readio-tts` is a local asynchronous TTS gateway for an Android reading app.
Android sends an already segmented chapter, GPT-SoVITS generates each
sentence, and the gateway publishes one complete offline WAV file plus a
sentence timing manifest.

The service is intentionally designed around offline listening:

- Android receives a complete chapter artifact that supports random seeking.
- The gateway retains output only until Android confirms it has persisted it.
- Sentence audio is checkpointed internally so an interrupted gateway can
  continue a long-running chapter instead of starting over.

## Android API Contract

The following endpoints form the complete public API.

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

### Poll Job Status

```http
GET /v1/jobs/{job_id}
```

While processing:

```json
{
  "job_id": "f4a17b6a-9b1b-4bc2-b7f6-d87577835d53",
  "chapter_id": "book-1/chapter-12",
  "state": "processing",
  "progress": {
    "sentences_completed": 384,
    "sentences_total": 2210
  },
  "created_at": "2026-05-26T08:00:00Z",
  "updated_at": "2026-05-26T09:42:00Z"
}
```

When complete:

```json
{
  "job_id": "f4a17b6a-9b1b-4bc2-b7f6-d87577835d53",
  "chapter_id": "book-1/chapter-12",
  "state": "completed",
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

Job states are `queued`, `processing`, `completed`, `failed`, and
`cancelled`. Failed jobs include an `error` field.

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

### Confirm Persistence And Cancel

After Android has safely stored both artifacts:

```http
POST /v1/jobs/{job_id}/ack
```

Response: `204 No Content`. The gateway removes the temporary server-side job
data immediately. Completed jobs that are never acknowledged expire after
`READIO_JOB_RETENTION_DAYS`.

To stop generation or discard an unneeded artifact:

```http
DELETE /v1/jobs/{job_id}
```

Response: `204 No Content`.

## Processing Model

The gateway synthesizes one sentence at a time to retain deterministic
sentence boundaries. It stores the immutable chapter request once in
`request.json`, updates lightweight job progress in `job.json`, and stores
internal sentence WAV checkpoints in `data/jobs/<job_id>/segments/`. On
restart, queued or processing jobs are scheduled again and existing segment
files are reused. Once the chapter WAV and manifest are published, the
internal segment files are deleted.

Each pending job persists a synthesis signature derived from the model
revision, inference settings, prompt transcript, and reference audio. If the
engine configuration or narrator sample changes before a job resumes, the
gateway discards its partial sentence audio and regenerates the chapter with
one consistent voice and model.

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

Start GPT-SoVITS in Docker and run the gateway with reload:

```powershell
docker compose -f compose.yaml -f compose.gpt.yaml -f compose.gpt.dev.yaml up -d gpt-sovits
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn readio_tts.api:app --reload --host 0.0.0.0 --port 8090
```

Relevant `.env` values:

```dotenv
READIO_PROVIDER=gpt
READIO_GPT_BASE_URL=http://127.0.0.1:9880
READIO_GPT_MODEL_REVISION=v2ProPlus
READIO_GPT_REFERENCE_DIR=references/gpt
```

Check readiness:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8090/health
```

The readiness check probes GPT-SoVITS metadata without generating audio.

## Docker Deployment

```powershell
docker compose -f compose.yaml -f compose.gpt.yaml up -d --build
```

Only the gateway is published to the laptop network. GPT-SoVITS stays behind
the Docker network.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```
