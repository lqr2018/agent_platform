"""检索器（详细设计 4.6.1 / 4.6.3 / 7.6 任务 6）。

把"query 文本"变成"可直接注入 system 的引用块"：

```text
Retriever.retrieve(query, config) ->
    embed(query) → 逐 KB 查向量库（collection = kb_{id}）→ 合并
    → 过滤 score >= score_threshold → 按 score 降序 → 取前 top_k → RetrievedChunk[]
```

- **多 KB 合并**（2.8："多 KB 结果合并后重排"）：各 KB **各取 `top_k`** 再合并排序 —— 若先合并再截断，
  命中数多的大 KB 会挤掉其它 KB 的结果；
- **无命中**（`hit_count == 0`）返回空列表：由调用方注入 4.6.3 的"未检索到相关内容"提示，
  **不注入空段落**（避免模型凭空编造）;
- **rerank 位置预留**（4.6.1：MVP 不实现）：`_rerank()` 目前是"按分数降序"的恒等实现，迭代 E 在此插入。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.runtime.agent.prompt import format_retrieved_chunk
from app.runtime.rag.base import Embedder, RetrievalConfig, RetrievedChunk, VectorHit, VectorStore

EMPTY_RETRIEVAL_NOTICE = "当前知识库未检索到相关内容，请明确说明"
"""4.6.3：无命中时追加到 system 的那句话（替代"注入空段落"）。"""


class Retriever:
    """向量检索 + 多 KB 合并（4.6.1 的 `Retriever.retrieve(...)`）。"""

    def __init__(self, *, embedder: Embedder, store: VectorStore) -> None:
        self._embedder = embedder
        self._store = store

    async def retrieve(self, query: str, config: RetrievalConfig) -> list[RetrievedChunk]:
        """检索并排序；`kb_ids` 为空或 query 为空白 → 空列表（不触发 embedding 调用）。"""
        text = query.strip()
        if not text or not config.kb_ids or config.top_k <= 0:
            return []
        vectors = await self._embedder.embed([text])
        if not vectors or not vectors[0]:
            return []
        vector = list(vectors[0])
        merged: list[RetrievedChunk] = []
        for kb_id in config.kb_ids:
            collection = config.collection_names.get(kb_id) or f"kb_{kb_id}"
            hits = await self._store.query(collection, vector, config.top_k)
            merged.extend(_to_chunks(hits, kb_id=kb_id))
        above_threshold = [chunk for chunk in merged if chunk.score >= config.score_threshold]
        return self._rerank(above_threshold, query=text)[: config.top_k]

    def _rerank(self, chunks: list[RetrievedChunk], *, query: str) -> list[RetrievedChunk]:
        """**迭代 E** 的 rerank 插点（4.6.1）：MVP 只按余弦分数降序，`query` 参数先占位。"""
        return sorted(chunks, key=lambda chunk: chunk.score, reverse=True)


def _to_chunks(hits: Sequence[VectorHit], *, kb_id: str) -> list[RetrievedChunk]:
    chunks: list[RetrievedChunk] = []
    for hit in hits:
        meta = dict(hit.meta)
        chunks.append(
            RetrievedChunk(
                chunk_id=hit.id,
                document_id=str(meta.get("document_id") or ""),
                kb_id=kb_id,
                score=float(hit.score),
                content=hit.content,
                meta=meta,
            )
        )
    return chunks


def build_context_blocks(chunks: Sequence[RetrievedChunk]) -> list[str]:
    """4.4.2 的 system 注入块：`<chunk id="…" source="…">`，`source` 用 4.6.3 的引用串。

    两个格式口径的统一：4.6.3 的 `[1] source=install.md#2.1 page=2 score=0.83` 与 4.4.2 的
    `<chunk id source>` 描述的是同一件事 —— 本实现把前者作为后者的 `source` 属性值，
    这样 system 里既有稳定可解析的标签，又保留了 4.6.3 要求的"文件名 / 页码 / 分数"三要素。
    """
    return [
        format_retrieved_chunk(chunk_id=chunk.chunk_id, source=chunk.citation_source(), content=chunk.content)
        for chunk in chunks
    ]


def format_citation_list(chunks: Sequence[RetrievedChunk]) -> str:
    """4.6.3 的可读引用清单（日志 / 前端"检索试算"展示用）。"""
    return "\n".join(f"[{index}] source={chunk.citation_source()}" for index, chunk in enumerate(chunks, start=1))
