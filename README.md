# Readio TTS

`readio-tts` is a small async TTS gateway for an Android reading app. The
Android client sends a chapter as an ordered list of sentences. This service
calls GPT-SoVITS sentence by sentence, stitches the results into one WAV
chapter file, and returns sentence-level `begin_time` / `end_time` values for
synchronized highlighting.

The project is intentionally narrow: it keeps the gateway logic local, leaves
long-term storage to Android, and does not try to become a general media
library.

## API

The gateway follows the async long-text contract used by the Android app:

```http
POST /api/v1/tts_async/submit
GET /api/v1/tts_async/query?task_id=...
GET /api/v1/tts_async/audio/{task_id}?expires=...&signature=...
```

The submit request expects already segmented `sentences`:

```json
{
  "sentences": ["第一句。", "第二句。"],
  "enable_subtitle": 1,
  "sentence_interval": 600,
  "reference_id": "my_mandarin_narrator"
}
```

`appid` and `reqid` may still be present for DTO compatibility, but they do
not affect task identity. The gateway returns a generated `task_id`, and the
query response reports `task_status`, `audio_url`, `url_expire_time`, and the
sentence timing objects:

```json
{
  "task_id": "bd0c2171-4b38-4c05-b685-11f3d240ee8d",
  "task_status": 1,
  "text_length": 12,
  "audio_url": "http://127.0.0.1:8090/api/v1/tts_async/audio/...",
  "url_expire_time": 1780000000,
  "sentences": [
    {
      "text": "第一句。",
      "origin_text": "第一句。",
      "paragraph_no": 1,
      "begin_time": 0,
      "end_time": 820
    }
  ]
}
```

`task_status` values:

- `0`: queued or processing
- `1`: complete
- `2`: failed

The gateway keeps completed and failed jobs for `READIO_JOB_RETENTION_DAYS`
and signs audio download URLs for one hour.

## GPT-SoVITS Only

This repository now targets GPT-SoVITS only. The alternate-engine integration
and its compose files were removed because GPT-SoVITS is the only engine we
want to maintain here.

The gateway talks to the GPT-SoVITS HTTP service at `POST /tts` and expects a
WAV response. Sentence timestamps are still assembled locally.

## Reference Layout

GPT-SoVITS uses a narrator folder layout under `references/gpt/`:

```text
references/gpt/my_mandarin_narrator/
  sample_0004.wav
  sample_0004.lab
  sample_0005.wav
  sample_0005.lab
```

The first audio file in the folder becomes `ref_audio_path`, and the matching
`.lab` or `.txt` file becomes `prompt_text`.

For model weights, the GPT-SoVITS source tree is mounted from the local
`GPT-SoVITS/` directory into the container's
`/workspace/GPT-SoVITS/GPT_SoVITS/pretrained_models` path.

## Local Development

Run the gateway locally with reload and GPT-SoVITS in Docker:

```powershell
docker compose -f compose.yaml -f compose.gpt.yaml -f compose.gpt.dev.yaml up -d gpt-sovits
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn readio_tts.api:app --reload --host 0.0.0.0 --port 8090
```

Check health:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8090/health
```

The health check probes GPT-SoVITS readiness without triggering audio
generation, so Docker health checks do not consume inference time.

The default provider is GPT:

```dotenv
READIO_PROVIDER=gpt
READIO_GPT_BASE_URL=http://127.0.0.1:9880
READIO_GPT_REFERENCE_DIR=references/gpt
READIO_GPT_DEFAULT_REFERENCE_ID=my_mandarin_narrator
```

## Docker Deployment

The production-style stack is the gateway plus GPT-SoVITS:

```powershell
docker compose -f compose.yaml -f compose.gpt.yaml up -d --build
```

Only the gateway is exposed to the laptop network. GPT-SoVITS stays private
behind the Docker network.

## Tests

```powershell
.\\.venv\\Scripts\\python.exe -m pytest
```

The tests cover submit/query flow, timestamp calculation, job persistence,
reference file resolution, and GPT request construction.
