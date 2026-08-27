import pytest

from bitcoin_intel.core.config import Settings


def test_settings_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_NAME", "Test Intelligence Platform")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_HOST", "0.0.0.0")
    monkeypatch.setenv("APP_PORT", "9000")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = Settings()

    assert settings.app_name == "Test Intelligence Platform"
    assert settings.app_env == "test"
    assert settings.app_host == "0.0.0.0"
    assert settings.app_port == 9000
    assert settings.log_level == "DEBUG"
