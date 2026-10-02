"""`Settings` 单测（7.1 测试要求：配置覆盖优先级 + prod 密钥校验）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.config import DEFAULT_SECRET_KEY, Settings, get_settings
from app.core.errors import ConfigInvalidError, ErrorCode


def test_defaults_match_documented_baseline() -> None:
    settings = Settings(_env_file=None)
    assert settings.app_env == "dev"
    assert settings.database_url == "sqlite+aiosqlite:///./data/app.db"
    assert settings.vector_store_kind == "chroma"
    assert settings.max_concurrent_runs == 4
    assert settings.eval_concurrency == 2
    assert settings.agent_max_steps == 8
    assert settings.python_execute_enabled is False
    assert settings.file_write_enabled is False
    assert settings.cors_origins == ["http://localhost:5173"]


def test_env_var_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """第二层：`.env`（此处以进程环境变量代表）覆盖默认值。"""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("MAX_CONCURRENT_RUNS", "7")
    monkeypatch.setenv("CORS_ORIGINS", '["http://localhost:3000"]')
    settings = Settings(_env_file=None)
    assert settings.log_level == "DEBUG"
    assert settings.max_concurrent_runs == 7
    assert settings.cors_origins == ["http://localhost:3000"]


def test_explicit_kwargs_win_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    settings = Settings(_env_file=None, log_level="WARNING")
    assert settings.log_level == "WARNING"


def test_invalid_enum_value_is_rejected() -> None:
    with pytest.raises(PydanticValidationError):
        Settings(_env_file=None, app_env="staging")


def test_get_settings_is_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
        assert get_settings().log_level == "ERROR"
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("key_field", ["secret_key", "encryption_key"])
def test_production_refuses_placeholder_secrets(key_field: str) -> None:
    """6.2：prod 下密钥仍是默认值 → 启动即失败（CONFIG_INVALID）。"""
    settings = Settings(_env_file=None, app_env="prod")
    with pytest.raises(ConfigInvalidError) as excinfo:
        settings.assert_production_secrets()
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert key_field.upper() in excinfo.value.details["env_vars"]


def test_production_accepts_overridden_secrets() -> None:
    settings = Settings(
        _env_file=None,
        app_env="prod",
        secret_key="real-secret",
        encryption_key="real-fernet-key",
    )
    settings.assert_production_secrets()  # 不抛异常即通过


def test_dev_with_placeholder_secrets_is_allowed() -> None:
    settings = Settings(_env_file=None, app_env="dev", secret_key=DEFAULT_SECRET_KEY)
    settings.assert_production_secrets()


def test_runtime_dirs_layout() -> None:
    settings = Settings(_env_file=None, data_dir="./data", sandbox_root="./data/files", chroma_dir="./data/chroma")
    dirs = settings.runtime_dirs()
    assert set(dirs) == {"data", "chroma", "sandbox", "uploads", "reports"}
    assert dirs["uploads"].name == "uploads"
    assert dirs["reports"].name == "reports"
