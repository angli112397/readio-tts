from pathlib import Path
from time import time

from fastapi.testclient import TestClient

from readio_tts import api
from readio_tts.jobs import ChapterJobService
from readio_tts.providers import MockSpeechProvider


def test_health_reports_provider_availability(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        api,
        "jobs",
        ChapterJobService(MockSpeechProvider(), tmp_path, 1_000),
    )

    with TestClient(api.app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"


def test_async_submit_and_query_follow_android_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        api,
        "jobs",
        ChapterJobService(MockSpeechProvider(), tmp_path, 1_000, sentence_gap_ms=600),
    )

    with TestClient(api.app) as client:
        created = client.post(
            "/api/v1/tts_async/submit",
            json={
                "appid": "123456",
                "reqid": "android-request-00000001",
                "sentences": ["火山引擎异步长文本合成。", "第二句。"],
                "format": "wav",
                "enable_subtitle": 1,
            },
        )
        assert created.status_code == 200
        assert created.json()["task_status"] == 0
        task_id = created.json()["task_id"]
        assert created.json()["text_length"] == len("火山引擎异步长文本合成。第二句。")

        status_response = client.get(
            "/api/v1/tts_async/query",
            params={"appid": "123456", "task_id": task_id},
        )
        assert status_response.status_code == 200
        payload = status_response.json()
        assert payload["task_id"] == task_id
        assert payload["task_status"] == 1
        assert payload["text_length"] == len("火山引擎异步长文本合成。第二句。")
        assert payload["url_expire_time"] > 0
        assert f"/api/v1/tts_async/audio/{task_id}?" in payload["audio_url"]
        assert len(payload["sentences"]) == 2
        assert payload["sentences"][0]["begin_time"] == 0
        assert "end_time" in payload["sentences"][0]
        assert "emotion" not in payload["sentences"][0]
        assert "timestamps_ms" not in payload

        audio = client.get(payload["audio_url"])
        assert audio.status_code == 200
        assert audio.headers["content-type"] == "audio/wav"


def test_client_can_override_sentence_interval(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        api,
        "jobs",
        ChapterJobService(MockSpeechProvider(), tmp_path, 1_000, sentence_gap_ms=600),
    )

    with TestClient(api.app) as client:
        created = client.post(
            "/api/v1/tts_async/submit",
            json={
                "appid": "123456",
                "reqid": "android-request-00000002",
                "sentences": ["One.", "Two."],
                "sentence_interval": 275,
            },
        )
        assert created.status_code == 200

        result = client.get(
            "/api/v1/tts_async/query",
            params={"appid": "123456", "task_id": created.json()["task_id"]},
        ).json()
        assert result["sentences"][1]["begin_time"] - result["sentences"][0]["end_time"] == 275


def test_duplicate_reqid_does_not_create_a_second_task(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        api,
        "jobs",
        ChapterJobService(MockSpeechProvider(), tmp_path, 1_000),
    )
    payload = {
        "appid": "123456",
        "reqid": "android-request-duplicate-01",
        "sentences": ["One."],
    }

    with TestClient(api.app) as client:
        first = client.post("/api/v1/tts_async/submit", json=payload)
        second = client.post("/api/v1/tts_async/submit", json=payload)

    assert first.json()["task_id"]
    assert second.json()["code"] == 40000
    assert second.json()["reqid"] == payload["reqid"]


def test_rejects_out_of_range_sentence_interval(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        api,
        "jobs",
        ChapterJobService(MockSpeechProvider(), tmp_path, 1_000, sentence_gap_ms=600),
    )

    with TestClient(api.app) as client:
        response = client.post(
            "/api/v1/tts_async/submit",
            json={
                "appid": "123456",
                "reqid": "android-request-00000003",
                "sentences": ["One."],
                "sentence_interval": 3_001,
            },
        )

    assert response.status_code == 400
    assert response.json()["code"] == 40000
    assert response.json()["reqid"] == "android-request-00000003"


def test_raw_text_submission_is_rejected_without_android_sentences(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        api,
        "jobs",
        ChapterJobService(MockSpeechProvider(), tmp_path, 1_000),
    )

    with TestClient(api.app) as client:
        response = client.post(
            "/api/v1/tts_async/submit",
            json={
                "appid": "123456",
                "reqid": "android-request-00000004",
                "text": "No splitting here.",
            },
        )

    assert response.status_code == 400
    assert response.json()["code"] == 40000


def test_unsupported_optional_synthesis_parameters_are_ignored(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        api,
        "jobs",
        ChapterJobService(MockSpeechProvider(), tmp_path, 1_000),
    )

    with TestClient(api.app) as client:
        response = client.post(
            "/api/v1/tts_async/submit",
            json={
                "appid": "123456",
                "reqid": "android-request-00000005",
                "sentences": ["One."],
                "format": "wav",
                "enable_subtitle": 1,
                "style": "happy",
                "speed": 1.2,
                "callback_url": "https://example.invalid/callback",
            },
        )

    assert response.status_code == 200


def test_only_wav_and_sentence_subtitles_are_supported(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        api,
        "jobs",
        ChapterJobService(MockSpeechProvider(), tmp_path, 1_000),
    )

    with TestClient(api.app) as client:
        mp3 = client.post(
            "/api/v1/tts_async/submit",
            json={
                "appid": "123456",
                "reqid": "android-request-00000006",
                "sentences": ["One."],
                "format": "mp3",
            },
        )
        words = client.post(
            "/api/v1/tts_async/submit",
            json={
                "appid": "123456",
                "reqid": "android-request-00000007",
                "sentences": ["One."],
                "enable_subtitle": 2,
            },
        )

    assert mp3.status_code == 400
    assert words.status_code == 400


def test_removed_alias_routes_are_not_available(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        api,
        "jobs",
        ChapterJobService(MockSpeechProvider(), tmp_path, 1_000),
    )

    with TestClient(api.app) as client:
        chapter_jobs = client.post("/v1/chapter-jobs", json={"sentences": ["One."]})
        emotion = client.post(
            "/api/v1/tts_async_with_emotion/submit",
            json={
                "appid": "123456",
                "reqid": "android-request-00000009",
                "sentences": ["One."],
            },
        )
        unprefixed = client.post(
            "/v1/tts_async/submit",
            json={"sentences": ["One."]},
        )

    assert chapter_jobs.status_code == 404
    assert emotion.status_code == 404
    assert unprefixed.status_code == 404


def test_unknown_or_invalid_task_identifier_is_not_found(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        api,
        "jobs",
        ChapterJobService(MockSpeechProvider(), tmp_path, 1_000),
    )

    with TestClient(api.app) as client:
        response = client.get(
            "/api/v1/tts_async/query",
            params={"appid": "123456", "task_id": "not-a-job-id"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "reqid": "not-a-job-id",
        "code": 40400,
        "message": "Task does not exist or has expired.",
    }


def test_expired_audio_url_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        api,
        "jobs",
        ChapterJobService(MockSpeechProvider(), tmp_path, 1_000),
    )

    with TestClient(api.app) as client:
        created = client.post(
            "/api/v1/tts_async/submit",
            json={
                "appid": "123456",
                "reqid": "android-request-00000008",
                "sentences": ["One."],
            },
        )
        task_id = created.json()["task_id"]
        expires = int(time()) - 1
        signature = api._audio_signature(task_id, expires)
        audio = client.get(
            f"/api/v1/tts_async/audio/{task_id}",
            params={"expires": expires, "signature": signature},
        )

    assert audio.status_code == 403
