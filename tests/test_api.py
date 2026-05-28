import asyncio
import os
import struct
import wave
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient

os.environ["READIO_DATA_DIR"] = str(Path(".pytest-tmp") / "api-global")
os.environ["READIO_API_TOKEN"] = "test-readio-api-token"

from readio_tts import api
from readio_tts.jobs import JobManager, JobWorker
from readio_tts.models import ErrorInfo, VoiceRecord
from readio_tts.providers import MockSpeechProvider
from readio_tts.repository import JobRepository, VoiceRepository
from readio_tts.voices import VoiceManager


TEST_API_TOKEN = "test-readio-api-token"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_API_TOKEN}"}


def install_manager(tmp_path: Path, monkeypatch) -> JobManager:
    monkeypatch.setattr(api.settings, "api_token", TEST_API_TOKEN)
    database = tmp_path / "readio.sqlite3"
    voices = VoiceManager(VoiceRepository(database), tmp_path / "voices")
    voices.repository.create(
        VoiceRecord(
            voice_id="narrator",
            display_name="Narrator",
            reference_language="en",
            transcript="prompt",
            duration_ms=500,
            audio_size_bytes=15,
            audio_sha256="a" * 64,
            created_at=datetime.now(UTC),
        )
    )
    (voices.voices_dir / "narrator").mkdir()
    (voices.voices_dir / "narrator" / "reference.wav").write_bytes(b"reference-audio")
    manager = JobManager(
        JobRepository(database),
        tmp_path / "jobs",
        voices,
        "v2ProPlus",
        1_000,
    )
    monkeypatch.setattr(api, "manager", manager)
    monkeypatch.setattr(api, "voice_manager", voices)
    return manager


def make_wav(duration_ms: int = 4_000) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(1_000)
        writer.writeframes(struct.pack("<h", 0) * duration_ms)
    return output.getvalue()


def request_payload() -> dict[str, object]:
    return {
        "chapter_id": "book-1/chapter-12",
        "voice_id": "narrator",
        "text_language": "en",
        "sentence_gap_ms": 275,
        "sentences": [
            {"id": "s1", "text": "One.", "paragraph_index": 0},
            {"id": "s2", "text": "Two.", "paragraph_index": 0},
        ],
    }


def process_pending(manager: JobManager) -> None:
    asyncio.run(JobWorker(manager, MockSpeechProvider()).run_once())


def test_health_reports_api_availability() -> None:
    with TestClient(api.app) as client:
        health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}


def test_api_token_protects_v1_routes_but_not_health(tmp_path: Path, monkeypatch) -> None:
    install_manager(tmp_path, monkeypatch)
    with TestClient(api.app) as client:
        health = client.get("/health")
        unauthorized = client.get("/v1/voices")
        authorized = client.get(
            "/v1/voices",
            headers=AUTH_HEADERS,
        )

    assert health.status_code == 200
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "unauthorized"
    assert authorized.status_code == 200


def test_openapi_declares_bearer_auth_for_v1_only() -> None:
    with TestClient(api.app) as client:
        spec = client.get("/openapi.json").json()

    assert spec["paths"]["/v1/jobs"]["post"]["security"]
    assert "HTTPBearer" in spec["components"]["securitySchemes"]
    assert "security" not in spec["paths"]["/health"]["get"]


def test_job_completion_publishes_audio_and_manifest(tmp_path: Path, monkeypatch) -> None:
    manager = install_manager(tmp_path, monkeypatch)
    with TestClient(api.app, headers=AUTH_HEADERS) as client:
        created = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "chapter-12-narrator-v1"},
            json=request_payload(),
        )
        assert created.status_code == 202
        assert created.headers["retry-after"] == "5"
        assert created.headers["location"].endswith(created.json()["job_id"])
        job_id = created.json()["job_id"]
        process_pending(manager)

        result = client.get(f"/v1/jobs/{job_id}").json()
        assert result["state"] == "succeeded"
        assert result["progress"] == {"sentences_completed": 2, "sentences_total": 2}
        assert result["heartbeat_at"]
        assert result["artifact"]["mime_type"] == "audio/wav"

        manifest = client.get(result["artifact"]["manifest_url"]).json()
        assert manifest["text_language"] == "en"
        assert manifest["sentence_gap_ms"] == 275
        assert manifest["sentences"][1]["begin_ms"] - manifest["sentences"][0]["end_ms"] == 275

        audio = client.get(result["artifact"]["audio_url"])
        assert audio.status_code == 200
        assert audio.headers["content-type"] == "audio/wav"


def test_queued_job_exposes_queue_position_and_missing_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    install_manager(tmp_path, monkeypatch)
    with TestClient(api.app, headers=AUTH_HEADERS) as client:
        first = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "queue-first"},
            json=request_payload(),
        ).json()
        second = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "queue-second"},
            json=request_payload(),
        ).json()
        first_status = client.get(f"/v1/jobs/{first['job_id']}").json()
        second_status = client.get(f"/v1/jobs/{second['job_id']}").json()

    assert first_status["queue_position"] == 1
    assert second_status["queue_position"] == 2
    assert first_status["blocked_by"] == "worker_unavailable"
    assert second_status["blocked_by"] == "worker_unavailable"

    manager = api.manager
    manager.repository.touch_worker()
    with TestClient(api.app, headers=AUTH_HEADERS) as client:
        active_worker_status = client.get(f"/v1/jobs/{first['job_id']}").json()

    assert "blocked_by" not in active_worker_status


def test_idempotency_and_contract_validation(tmp_path: Path, monkeypatch) -> None:
    install_manager(tmp_path, monkeypatch)
    with TestClient(api.app, headers=AUTH_HEADERS) as client:
        first = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "same-chapter"},
            json=request_payload(),
        )
        second = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "same-chapter"},
            json=request_payload(),
        )
        changed = request_payload()
        changed["chapter_id"] = "different"
        conflict = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "same-chapter"},
            json=changed,
        )
        missing_header = client.post("/v1/jobs", json=request_payload())
        unexpected = request_payload()
        unexpected["unknown"] = True
        rejected = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "unknown"},
            json=unexpected,
        )
        missing_text_language = request_payload()
        missing_text_language.pop("text_language")
        rejected_text_language = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "missing-text-language"},
            json=missing_text_language,
        )
    assert first.json()["job_id"] == second.json()["job_id"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert missing_header.status_code == 422
    assert missing_header.json()["error"]["code"] == "invalid_request"
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "invalid_request"
    assert rejected_text_language.status_code == 422
    assert rejected_text_language.json()["error"]["code"] == "invalid_request"


def test_job_creation_reports_missing_voice_as_client_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    install_manager(tmp_path, monkeypatch)
    payload = request_payload()
    payload["voice_id"] = "not-installed"

    with TestClient(api.app, headers=AUTH_HEADERS) as client:
        response = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "missing-voice"},
            json=payload,
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "voice_unavailable"


def test_corrupt_existing_job_is_not_misreported_as_missing_voice(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = install_manager(tmp_path, monkeypatch)
    with TestClient(
        api.app,
        headers=AUTH_HEADERS,
        raise_server_exceptions=False,
    ) as client:
        created = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "corrupt-request"},
            json=request_payload(),
        )
        manager.files(created.json()["job_id"]).request.write_text("{", encoding="utf-8")
        retried = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "corrupt-request"},
            json=request_payload(),
        )

    assert retried.status_code == 500
    assert retried.json()["error"]["code"] == "internal_error"


def test_voice_upload_list_audio_and_delete(tmp_path: Path, monkeypatch) -> None:
    install_manager(tmp_path, monkeypatch)
    with TestClient(api.app, headers=AUTH_HEADERS) as client:
        created = client.post(
            "/v1/voices",
            data={
                "display_name": "My English Voice",
                "reference_language": "en",
                "transcript": "Reference prompt.",
            },
            files={"audio": ("voice.wav", make_wav(), "audio/wav")},
        )
        assert created.status_code == 201
        voice_id = created.json()["voice_id"]
        assert created.json()["reference_language"] == "en"
        assert created.json()["duration_ms"] == 4_000
        assert client.get("/v1/voices").json()[0]["voice_id"] == voice_id
        assert client.get(f"/v1/voices/{voice_id}/audio").status_code == 200
        assert client.delete(f"/v1/voices/{voice_id}").status_code == 204
        assert client.get(f"/v1/voices/{voice_id}").status_code == 404


def test_voice_upload_rejects_non_wav(tmp_path: Path, monkeypatch) -> None:
    install_manager(tmp_path, monkeypatch)
    with TestClient(api.app, headers=AUTH_HEADERS) as client:
        response = client.post(
            "/v1/voices",
            data={
                "display_name": "Bad Voice",
                "reference_language": "en",
                "transcript": "Reference prompt.",
            },
            files={"audio": ("voice.mp3", b"not-wav", "audio/mpeg")},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_voice_audio"


def test_voice_upload_rejects_reference_duration_outside_engine_range(
    tmp_path: Path,
    monkeypatch,
) -> None:
    install_manager(tmp_path, monkeypatch)
    with TestClient(api.app, headers=AUTH_HEADERS) as client:
        for duration_ms in (1_000, 11_000):
            response = client.post(
                "/v1/voices",
                data={
                    "display_name": "Out Of Range Voice",
                    "reference_language": "en",
                    "transcript": "Reference prompt.",
                },
                files={"audio": ("voice.wav", make_wav(duration_ms), "audio/wav")},
            )

            assert response.status_code == 422
            assert response.json()["error"] == {
                "code": "invalid_voice_audio",
                "message": "Reference audio duration must be between 3 and 10 seconds.",
            }


def test_deleting_voice_does_not_interrupt_submitted_job(tmp_path: Path, monkeypatch) -> None:
    manager = install_manager(tmp_path, monkeypatch)
    with TestClient(api.app, headers=AUTH_HEADERS) as client:
        created = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "snapshotted-voice"},
            json=request_payload(),
        )
        job_id = created.json()["job_id"]
        assert client.delete("/v1/voices/narrator").status_code == 204
        process_pending(manager)

        result = client.get(f"/v1/jobs/{job_id}").json()

    assert result["state"] == "succeeded"


def test_audio_supports_range_and_delete_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    manager = install_manager(tmp_path, monkeypatch)
    with TestClient(api.app, headers=AUTH_HEADERS) as client:
        created = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "range-test"},
            json=request_payload(),
        )
        job_id = created.json()["job_id"]
        process_pending(manager)
        audio = client.get(f"/v1/jobs/{job_id}/audio", headers={"Range": "bytes=0-9"})
        deleted = client.delete(f"/v1/jobs/{job_id}")
        deleted_again = client.delete(f"/v1/jobs/{job_id}")
        invalid_delete = client.delete("/v1/jobs/not-a-job-id")
        missing = client.get(f"/v1/jobs/{job_id}")
    assert audio.status_code == 206
    assert len(audio.content) == 10
    assert deleted.status_code == 204
    assert deleted_again.status_code == 204
    assert invalid_delete.status_code == 204
    assert missing.status_code == 404
    assert missing.json() == {
        "error": {"code": "job_not_found", "message": "Job not found."}
    }


def test_delete_running_job_removes_it_immediately(tmp_path: Path, monkeypatch) -> None:
    manager = install_manager(tmp_path, monkeypatch)
    with TestClient(api.app, headers=AUTH_HEADERS) as client:
        created = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "cancel-test"},
            json=request_payload(),
        )
        job_id = created.json()["job_id"]
        record = manager.get_job(job_id)
        assert record is not None
        record.state = api.JobState.RUNNING
        manager.repository.save(record)
        assert client.delete(f"/v1/jobs/{job_id}").status_code == 204
    assert manager.get_job(job_id) is None
    assert not manager.files(job_id).root.exists()


def test_unexpected_api_failure_returns_stable_error_shape(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = install_manager(tmp_path, monkeypatch)

    def fail_create(*_args, **_kwargs):
        raise OSError("private local path should not reach the client")

    monkeypatch.setattr(manager, "create_job", fail_create)
    with TestClient(
        api.app,
        headers=AUTH_HEADERS,
        raise_server_exceptions=False,
    ) as client:
        response = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "unexpected-error"},
            json=request_payload(),
        )

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "The server could not complete the request.",
        }
    }


def test_failed_job_exposes_error_code_and_sentence_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = install_manager(tmp_path, monkeypatch)
    job, _ = manager.create_job(api.CreateJobRequest.model_validate(request_payload()), "failed")
    job.state = api.JobState.FAILED
    job.error = ErrorInfo(
        code="tts_request_rejected",
        message="The speech engine rejected this sentence.",
        sentence_id="s2",
    )
    manager.repository.save(job)

    with TestClient(api.app, headers=AUTH_HEADERS) as client:
        result = client.get(f"/v1/jobs/{job.job_id}")

    assert result.status_code == 200
    assert result.json()["error"] == {
        "code": "tts_request_rejected",
        "message": "The speech engine rejected this sentence.",
        "sentence_id": "s2",
    }
