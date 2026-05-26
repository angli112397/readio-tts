from pathlib import Path
import time

from fastapi.testclient import TestClient

from readio_tts import api
from readio_tts.jobs import JobService
from readio_tts.providers import MockSpeechProvider


def install_mock_service(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        api,
        "jobs",
        JobService(MockSpeechProvider(), tmp_path, 1_000),
    )


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


def wait_for_completion(client: TestClient, job_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = client.get(f"/v1/jobs/{job_id}").json()
        if result["state"] == "completed":
            return result
        time.sleep(0.01)
    raise AssertionError(f"Job {job_id} did not complete before timeout.")


def test_health_reports_provider_availability(tmp_path: Path, monkeypatch) -> None:
    install_mock_service(tmp_path, monkeypatch)
    with TestClient(api.app) as client:
        health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "provider": api.settings.provider}


def test_job_completion_publishes_audio_and_manifest(tmp_path: Path, monkeypatch) -> None:
    install_mock_service(tmp_path, monkeypatch)
    with TestClient(api.app) as client:
        created = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "chapter-12-narrator-v1"},
            json=request_payload(),
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]

        result = wait_for_completion(client, job_id)
        assert result["state"] == "completed"
        assert result["progress"] == {
            "sentences_completed": 2,
            "sentences_total": 2,
        }
        assert result["artifact"]["mime_type"] == "audio/wav"
        assert result["artifact"]["size_bytes"] > 0
        assert len(result["artifact"]["sha256"]) == 64

        manifest = client.get(result["artifact"]["manifest_url"]).json()
        assert manifest["chapter_id"] == "book-1/chapter-12"
        assert manifest["sentence_gap_ms"] == 275
        assert manifest["sentences"][0]["id"] == "s1"
        assert (
            manifest["sentences"][1]["begin_ms"]
            - manifest["sentences"][0]["end_ms"]
            == 275
        )

        audio = client.get(result["artifact"]["audio_url"])
        assert audio.status_code == 200
        assert audio.headers["content-type"] == "audio/wav"


def test_idempotency_key_returns_existing_job(tmp_path: Path, monkeypatch) -> None:
    install_mock_service(tmp_path, monkeypatch)
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
    assert first.json()["job_id"] == second.json()["job_id"]


def test_idempotency_key_rejects_a_different_request(tmp_path: Path, monkeypatch) -> None:
    install_mock_service(tmp_path, monkeypatch)
    changed = request_payload()
    changed["chapter_id"] = "book-1/chapter-13"
    with TestClient(api.app) as client:
        client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "reused-key"},
            json=request_payload(),
        )
        conflict = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "reused-key"},
            json=changed,
        )
    assert conflict.status_code == 409


def test_rejects_chapter_larger_than_configured_limit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        api,
        "jobs",
        JobService(MockSpeechProvider(), tmp_path, 3),
    )
    payload = request_payload()
    payload["sentences"] = [{"id": "s1", "text": "long"}]
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "too-large"},
            json=payload,
        )
    assert response.status_code == 413
    assert "maximum is 3" in response.json()["detail"]


def test_request_requires_idempotency_key_and_unique_sentence_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:
    install_mock_service(tmp_path, monkeypatch)
    payload = request_payload()
    payload["sentences"] = [
        {"id": "same", "text": "One."},
        {"id": "same", "text": "Two."},
    ]
    with TestClient(api.app) as client:
        missing_header = client.post("/v1/jobs", json=request_payload())
        duplicate_ids = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "duplicate-sentences"},
            json=payload,
        )
    assert missing_header.status_code == 422
    assert duplicate_ids.status_code == 422


def test_request_rejects_unknown_contract_fields(tmp_path: Path, monkeypatch) -> None:
    install_mock_service(tmp_path, monkeypatch)
    payload = request_payload()
    payload["unexpected_setting"] = "ignored-by-no-one"
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "bad-contract"},
            json=payload,
        )
    assert response.status_code == 422


def test_request_rejects_voice_path_traversal(tmp_path: Path, monkeypatch) -> None:
    install_mock_service(tmp_path, monkeypatch)
    payload = request_payload()
    payload["voice_id"] = "../private"
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "invalid-voice-path"},
            json=payload,
        )
    assert response.status_code == 422


def test_audio_supports_range_downloads(tmp_path: Path, monkeypatch) -> None:
    install_mock_service(tmp_path, monkeypatch)
    with TestClient(api.app) as client:
        created = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "range-test"},
            json=request_payload(),
        )
        job_id = created.json()["job_id"]
        wait_for_completion(client, job_id)
        audio = client.get(f"/v1/jobs/{job_id}/audio", headers={"Range": "bytes=0-9"})
    assert audio.status_code == 206
    assert audio.headers["content-range"].startswith("bytes 0-9/")
    assert len(audio.content) == 10


def test_ack_removes_downloaded_job(tmp_path: Path, monkeypatch) -> None:
    install_mock_service(tmp_path, monkeypatch)
    with TestClient(api.app) as client:
        created = client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "ack-test"},
            json=request_payload(),
        )
        job_id = created.json()["job_id"]
        wait_for_completion(client, job_id)
        acknowledged = client.post(f"/v1/jobs/{job_id}/ack")
        missing = client.get(f"/v1/jobs/{job_id}")
    assert acknowledged.status_code == 204
    assert missing.status_code == 404
