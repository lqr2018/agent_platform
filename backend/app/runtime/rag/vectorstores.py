"""向量库实现（详细设计 4.3.3 / 4.6.1 / SD-11）。

两个实现，按 `VECTOR_STORE_KIND` 选择：

| 实现 | 用途 | 说明 |
|---|---|---|
| `InMemoryVectorStore` | 测试 / `kind=memory` | 纯 Python 余弦相似度；进程内、不落盘 |
| `ChromaVectorStore` | 生产（**唯一**，SD-11） | `chromadb.PersistentClient(path=CHROMA_DIR)`；集合名 `kb_{id}` |

两个工程约定：

1. **`chromadb` 在构造函数内部 import**：它是运行期依赖（0.2.3 的 `Chroma 0.5+`），但导入会连带
   加载 `onnxruntime` 等重依赖，而 `/readyz`、`/api/v1/meta` 与不碰知识库的 Chat 路径都不该为它
   付启动成本。未安装时抛 `CONFIG_INVALID`（500，附安装提示）而不是 `ImportError`。
2. **同步的 Chroma 调用一律走 `asyncio.to_thread`**：chromadb 是同步库，直接在事件循环里调用会
   阻塞整个进程（摄取 worker 与 Chat 共用同一个 loop）。`InMemoryVectorStore` 是纯计算，
   保持同步实现即可（它本来就只在测试里用）。
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.core.errors import ConfigInvalidError
from app.core.logging import get_logger
from app.runtime.rag.base import VectorHit, VectorItem, VectorStore

logger = get_logger(__name__)

CHROMA_DISTANCE_SPACE = "cosine"
"""Chroma 集合的距离度量（4.6.1：余弦排序）；返回的 `distance = 1 - cosine`。"""


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """余弦相似度（`4.3.3` 的 `query` 返回值语义；长度不一致 / 零向量 → 0.0）。"""
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def clean_metadata(meta: dict[str, Any]) -> dict[str, str | int | float | bool]:
    """把 `chunks.meta` 压成 Chroma 能接受的字面量（None 丢弃、复合值转 JSON 文本）。"""
    cleaned: dict[str, str | int | float | bool] = {}
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            cleaned[str(key)] = value
        else:
            cleaned[str(key)] = json.dumps(value, ensure_ascii=False)
    return cleaned


class InMemoryVectorStore:
    """进程内向量库（测试用；`VECTOR_STORE_KIND=memory` 时也可用于离线演示）。"""

    def __init__(self) -> None:
        self._collections: dict[str, dict[str, VectorItem]] = {}

    async def upsert(self, collection: str, items: list[VectorItem]) -> None:
        bucket = self._collections.setdefault(collection, {})
        for item in items:
            bucket[item.id] = item

    async def query(self, collection: str, vector: list[float], top_k: int) -> list[VectorHit]:
        if top_k <= 0:
            return []
        hits = [
            VectorHit(
                id=item.id,
                score=cosine_similarity(vector, item.vector),
                content=item.content,
                meta=dict(item.meta),
            )
            for item in (self._collections.get(collection) or {}).values()
        ]
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:top_k]

    async def delete(self, collection: str, ids: list[str]) -> None:
        bucket = self._collections.get(collection)
        if not bucket:
            return
        for item_id in ids:
            bucket.pop(item_id, None)

    async def drop(self, collection: str) -> None:
        self._collections.pop(collection, None)

    async def count(self, collection: str) -> int:
        return len(self._collections.get(collection) or {})


class ChromaVectorStore:
    """Chroma 持久化实现（SD-11：默认且唯一的生产向量库）。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._client: Any | None = None

    async def upsert(self, collection: str, items: list[VectorItem]) -> None:
        if not items:
            return
        await asyncio.to_thread(self._upsert_sync, collection, items)

    async def query(self, collection: str, vector: list[float], top_k: int) -> list[VectorHit]:
        if top_k <= 0:
            return []
        return await asyncio.to_thread(self._query_sync, collection, vector, top_k)

    async def delete(self, collection: str, ids: list[str]) -> None:
        if not ids:
            return
        await asyncio.to_thread(self._delete_sync, collection, ids)

    async def drop(self, collection: str) -> None:
        await asyncio.to_thread(self._drop_sync, collection)

    async def count(self, collection: str) -> int:
        return await asyncio.to_thread(self._count_sync, collection)

    # ---- 同步实现（一律在 `asyncio.to_thread` 里执行） ----

    def _client_or_create(self) -> Any:
        if self._client is None:
            try:
                import chromadb  # noqa: PLC0415 - 见模块 docstring 的"惰性 import"约定
            except ImportError as exc:  # pragma: no cover - 缺依赖属部署错误
                raise ConfigInvalidError(
                    "chromadb is not installed; run `pip install chromadb` or set VECTOR_STORE_KIND=memory",
                    details={"vector_store_kind": "chroma"},
                ) from exc
            self._path.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self._path))
        return self._client

    def _collection(self, name: str) -> Any:
        return self._client_or_create().get_or_create_collection(
            name=name, metadata={"hnsw:space": CHROMA_DISTANCE_SPACE}
        )

    def _upsert_sync(self, collection: str, items: list[VectorItem]) -> None:
        self._collection(collection).upsert(
            ids=[item.id for item in items],
            embeddings=[list(item.vector) for item in items],
            documents=[item.content for item in items],
            metadatas=[clean_metadata(item.meta) for item in items],
        )

    def _query_sync(self, collection: str, vector: list[float], top_k: int) -> list[VectorHit]:
        result = self._collection(collection).query(
            query_embeddings=[list(vector)],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        ids = _first(result.get("ids"))
        documents = _first(result.get("documents"))
        metadatas = _first(result.get("metadatas"))
        distances = _first(result.get("distances"))
        hits: list[VectorHit] = []
        for index, item_id in enumerate(ids):
            distance = _at(distances, index, default=1.0)
            hits.append(
                VectorHit(
                    id=str(item_id),
                    # cosine 空间：`distance = 1 - similarity`（4.6.1 的排序键）
                    score=1.0 - float(distance),
                    content=str(_at(documents, index, default="") or ""),
                    meta=dict(_at(metadatas, index, default={}) or {}),
                )
            )
        return hits

    def _delete_sync(self, collection: str, ids: list[str]) -> None:
        self._collection(collection).delete(ids=list(ids))

    def _drop_sync(self, collection: str) -> None:
        client = self._client_or_create()
        try:
            client.delete_collection(name=collection)
        except Exception as exc:  # 版本差异：集合不存在时抛的异常类型不固定
            if type(exc).__name__ not in {"NotFoundError", "InvalidCollectionException", "ValueError"}:
                raise
            logger.info("rag.chroma_drop_skipped", collection=collection, reason=type(exc).__name__)

    def _count_sync(self, collection: str) -> int:
        return int(self._collection(collection).count())


_memory_store: InMemoryVectorStore | None = None


def build_vector_store(*, kind: str, chroma_dir: Path) -> VectorStore:
    """按 `VECTOR_STORE_KIND` 创建（SD-11：只有 `chroma` / `memory` 两个取值）。

    `memory` 返回**进程级单例**：`InMemoryVectorStore` 的数据只存在实例里，每次 new 一个会让
    "摄取写入的向量"与"检索读的向量"落在两个不同的库上（`VECTOR_STORE_KIND=memory` 时表现为
    "永远检索不到"）——共享单例是 `kind=memory` 能工作的唯一语义。
    """
    global _memory_store  # noqa: PLW0603 - 模块级单例
    if kind == "memory":
        if _memory_store is None:
            _memory_store = InMemoryVectorStore()
        return _memory_store
    return ChromaVectorStore(chroma_dir)


def reset_memory_vector_store() -> None:
    """清掉进程内单例（测试用）。"""
    global _memory_store  # noqa: PLW0603 - 模块级单例
    _memory_store = None


def _first(value: Any) -> list[Any]:
    """Chroma 的 `query` 把每列结果包一层（按 query 分组）→ 取出第一个 query 的结果。"""
    if not isinstance(value, list) or not value:
        return []
    head = value[0]
    return head if isinstance(head, list) else []


def _at(items: list[Any], index: int, *, default: Any) -> Any:
    return items[index] if 0 <= index < len(items) else default
