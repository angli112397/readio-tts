import pytest
from pydantic import ValidationError

from readio_tts.config import Settings


def test_api_token_is_required_at_startup(monkeypatch) -> None:
    monkeypatch.delenv("READIO_API_TOKEN", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
