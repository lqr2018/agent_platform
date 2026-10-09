"""向量库单测（详细设计 4.3.3 / 4.6.1 / 7.6 任务 4）。

`InMemoryVectorStore` 是测试与"离线演示"用的实现，这里把它的 CRUD、排序、阈值行为钉住；
`ChromaVectorStore` 只测"缺依赖时的错误语义"（真实读写由 `tests/integration` 覆盖）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import ConfigInvalidError
from app.runtime.rag.base import VectorItem
from app.runtime.rag.vectorstores import (
    ChromaVectorStore,
    InMemoryVectorStore,
    build_vector_store,
    clean_metadata,
    cosine_similarity,
)


def _item(item_id: str, vector: list[float], content: str = "", **meta: object) -> VectorItem:
    return VectorItem(id=item_id, vector=vector, content=content, meta=dict(meta))


async def test_in_memory_store_upsert_query_delete_count() -> None:
    store = InMemoryVectorStore()
    await store.upsert("kb_1", [_item("c1", [1.0, 0.0], "安装说明"), _item("c2", [0.0, 1.0], "配置说明")])

    assert await store.count("kb_1") == 2

    hits = await store.query("kb_1", [1.0, 0.0], top_k=5)
    assert [hit.id for hit in hits] == ["c1", "c2"]
    assert hits[0].score == pytest.approx(1.0)
    assert hits[0].content == "安装说明"

    await store.delete("kb_1", ["c1"])
    assert await store.count("kb_1") == 1
    assert [hit.id for hit in await store.query("kb_1", [1.0, 0.0], top_k=5)] == ["c2"]

    await store.drop("kb_1")
    assert await store.count("kb_1") == 0


async def test_in_memory_store_applies_top_k_and_returns_empty_for_unknown_collection() -> None:
    store = InMemoryVectorStore()
    await store.upsert("kb_1", [_item(str(index), [1.0, float(index) / 10]) for index in range(5)])

    assert len(await store.query("kb_1", [1.0, 0.0], top_k=2)) == 2
    assert await store.query("kb_1", [1.0, 0.0], top_k=0) == []
    assert await store.query("kb_missing", [1.0, 0.0], top_k=5) == []


async def test_in_memory_store_upsert_overwrites_same_id() -> None:
    store = InMemoryVectorStore()
    await store.upsert("kb_1", [_item("c1", [1.0, 0.0], "旧内容")])
    await store.upsert("kb_1", [_item("c1", [1.0, 0.0], "新内容")])

    hits = await store.query("kb_1", [1.0, 0.0], top_k=5)
    assert [hit.content for hit in hits] == ["新内容"]


def test_clean_metadata_drops_none_and_serializes_composites() -> None:
    cleaned = clean_metadata({"page": 2, "heading": "安装", "score": 0.83, "nested": {"a": 1}, "none": None})

    assert cleaned["page"] == 2
    assert cleaned["heading"] == "安装"
    assert cleaned["nested"] == '{"a": 1}'
    assert "none" not in cleaned


def test_cosine_similarity_edge_cases() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([1.0, 0.0], [1.0]) == 0.0
    assert cosine_similarity([], []) == 0.0
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_build_vector_store_selects_kind() -> None:
    assert isinstance(build_vector_store(kind="memory", chroma_dir=Path("unused")), InMemoryVectorStore)
    assert isinstance(build_vector_store(kind="chroma", chroma_dir=Path("unused")), ChromaVectorStore)


async def test_chroma_store_reports_missing_dependency() -> None:
    """缺 `chromadb` 时给 `CONFIG_INVALID`（含安装提示），而不是裸 `ImportError`（7.6 的依赖约定）。"""
    try:
        import chromadb  # noqa: F401
    except ImportError:
        store = ChromaVectorStore(Path("unused-chroma-dir"))
        with pytest.raises(ConfigInvalidError) as excinfo:
            await store.count("kb_missing")
        assert excinfo.value.details["vector_store_kind"] == "chroma"
    else:
        pytest.skip("chromadb 已安装：缺依赖分支不适用")
