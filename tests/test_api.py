import asyncio
import os
from pathlib import Path

from fastapi.testclient import TestClient

os.environ["READIO_DATA_DIR"] = str(Path(".pytest-tmp") / "api-global")

from readio_tts import api
from readio_tts.jobs import JobManager, JobWorker
from readio_tts.providers import MockSpeechProvider
from readio_tts.repository import JobRepository


def install_manager(tmp_path: Path, monkeypatch) -> JobManager:
    reference = tmp_path / "references" / "narrator"
    reference.mkdir(parents=True, exist_ok=True)
    (reference / "voice.wav").write_bytes(b"reference-audio")
    (reference / "voice.lab").write_text("prompt", encoding="utf-8")
    manager = JobManager(
        JobRepository(tmp_path / "readio.sqlite3"),
        tmp_path / "jobs",
        tmp_path / "references",
        "v2ProPlus",
        1_000,
    )
    monkeypatch.setattr(api, "manager", manager)
    return manager


def request_payload() -> dict[str, object]:
    return {
        "chapter_id": "book-1/chapter-12",
        "voice_id": "narrator",
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


def test_job_completion_publishes_audio_and_manifest(tmp_path: Path, monkeypatch) -> None:
    manager = install_manager(tmp_path, monkeypatch)
    with TestClient(api.app) as client:
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
        assert manifest["sentence_gap_ms"] == 275
        assert manifest["sentences"][1]["begin_ms"] - manifest["sentences"][0]["end_ms"] == 275

        audio = client.get(result["artifact"]["audio_url"])
        assert audio.status_code == 200
        assert audio.headers["content-type"] == "audio/wav"


def test_idempotency_and_contract_validation(tmp_path: Path, monkeypatch) -> None:
    install_manager(tmp_path, monkeypatch)
    with TestClient(api.app) as client:
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
    assert first.json()["job_id"] == second.json()["job_id"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert missing_header.status_code == 422
    assert missing_header.json()["error"]["code"] == "invalid_request"
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "invalid_request"


def test_audio_supports_range_and_delete_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    manager = install_manager(tmp_path, monkeypatch)
    with TestClient(api.app) as client:
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
    with TestClient(api.app) as client:
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
    with TestClient(api.app, raise_server_exceptions=False) as client:
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
    job.error_code = "tts_request_rejected"
    job.error_message = "GPT-SoVITS rejected the synthesis request: invalid input."
    job.error_sentence_id = "s2"
    manager.repository.save(job)

    with TestClient(api.app) as client:
        result = client.get(f"/v1/jobs/{job.job_id}")

    assert result.status_code == 200
    assert result.json()["error"] == {
        "code": "tts_request_rejected",
        "message": "GPT-SoVITS rejected the synthesis request: invalid input.",
        "sentence_id": "s2",
    }
