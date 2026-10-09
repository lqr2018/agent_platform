"""就绪探针的单元级校验与 features 快照（6.3 第 4–5 条、3.2.1）。"""

from __future__ import annotations

from pathlib import Path

from app.api.v1.health import MetaFeatures, _check_vector_store, build_features
from app.core.config import Settings


def test_features_are_false_for_backlog_capabilities() -> None:
    """mcp / memory / eval_platform 在 MVP 恒为 false（SD-15/16/18）。"""
    features = build_features(Settings(_env_file=None))
    assert features.mcp is False
    assert features.memory is False
    assert features.eval_platform is False


def test_features_follow_settings_for_implemented_toggles() -> None:
    features = build_features(
        Settings(
            _env_file=None,
            python_execute_enabled=True,
            file_write_enabled=True,
            web_search_provider="tavily",
            web_search_api_key="sk-abcdefghijklmn",
        )
    )
    assert features.python_execute is True
    assert features.file_write is True
    assert features.web_search is True


def test_web_search_requires_both_provider_and_key() -> None:
    assert build_features(Settings(_env_file=None, web_search_provider="tavily")).web_search is False
    assert build_features(Settings(_env_file=None, web_search_api_key="sk-x")).web_search is False


def test_features_schema_is_stable() -> None:
    assert set(MetaFeatures.model_fields) == {
        "mcp",
        "memory",
        "eval_platform",
        "python_execute",
        "file_write",
        "web_search",
    }


async def test_vector_store_check_for_memory_kind() -> None:
    check = await _check_vector_store(Settings(_env_file=None, vector_store_kind="memory"))
    assert check.ok is True
    assert check.detail == "memory (in-process)"


async def test_vector_store_check_for_missing_chroma_dir(tmp_path: Path) -> None:
    check = await _check_vector_store(Settings(_env_file=None, chroma_dir=(tmp_path / "nope").as_posix()))
    assert check.ok is False
    assert "directory missing" in check.detail


async def test_vector_store_check_for_writable_chroma_dir(tmp_path: Path) -> None:
    """Phase 5 起 `chroma` 是**真实探测**（`PersistentClient.heartbeat()`），不再只是"目录可写"。"""
    chroma = tmp_path / "chroma"
    chroma.mkdir()
    check = await _check_vector_store(Settings(_env_file=None, chroma_dir=chroma.as_posix()))
    assert check.ok is True
    assert check.detail.startswith("chroma ok:")
