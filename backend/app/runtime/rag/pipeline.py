"""摄取流水线（详细设计 4.6.2 / 7.6 任务 5）。

```text
pending → parsing → chunking → embedding → ready
                     ↘ 任一步失败 → failed（记 error_code / error_message，可 reingest）
```

本模块只做**纯计算编排**（loader → splitter → embedder + 阶段耗时），状态机与落库在
`services/kb_service.py`：这样 pipeline 能在单测里脱离 DB / 事件循环跑（7.6 的测试要求）。

**它不写向量库**：向量写入必须由服务层在"先写 `chunks` 行、再 upsert 向量"的顺序里完成 ——
`documents.status` 是进度的唯一判据（7.6 的风险与对策），把向量写入藏在 pipeline 里会让
"哪一步崩了"变得不可判定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter

from app.core.errors import KnowledgeBaseIngestFailedError
from app.runtime.rag.base import Embedder, LoadedDocument, LoaderSource, TextChunk
from app.runtime.rag.loaders import LoaderRegistry
from app.runtime.rag.splitters import build_splitter

STAGE_PARSING = "parsing"
STAGE_CHUNKING = "chunking"
STAGE_EMBEDDING = "embedding"
"""4.6.2 状态机的三个阶段名（`documents.status` 与 `details.stage` 共用）。"""


@dataclass(frozen=True, slots=True)
class IngestResult:
    """一次摄取的计算结果（服务层据此落 `chunks` + 写向量）。"""

    document: LoadedDocument
    chunks: list[TextChunk]
    vectors: list[list[float]]
    stage_latency_ms: dict[str, int] = field(default_factory=dict)


class IngestPipeline:
    """4.6.2 的流水线（同步的 load/split + 异步的 embed）。"""

    def __init__(self, *, loaders: LoaderRegistry | None = None) -> None:
        self._loaders = loaders or LoaderRegistry()

    @property
    def supported_suffixes(self) -> tuple[str, ...]:
        return self._loaders.suffixes

    def load(self, source: LoaderSource) -> LoadedDocument:
        """`parsing` 阶段：读文件 + 解码（`KB_UNSUPPORTED_FORMAT` / `KB_INGEST_FAILED` 从这里抛）。"""
        return self._loaders.load(source)

    def split(
        self,
        document: LoadedDocument,
        *,
        splitter: str,
        chunk_size: int,
        chunk_overlap: int,
    ) -> list[TextChunk]:
        """`chunking` 阶段：切片（空白文档得到空列表，不算失败）。"""
        return build_splitter(splitter, chunk_size=chunk_size, chunk_overlap=chunk_overlap).split(document)

    async def embed(self, chunks: list[TextChunk], *, embedder: Embedder) -> list[list[float]]:
        """`embedding` 阶段：批量向量化，并校验"一个切片一个向量"。"""
        if not chunks:
            return []
        vectors = await embedder.embed([chunk.content for chunk in chunks])
        if len(vectors) != len(chunks):
            raise KnowledgeBaseIngestFailedError(
                "Embedding provider returned a wrong number of vectors",
                details={"stage": STAGE_EMBEDDING, "expected": len(chunks), "got": len(vectors)},
            )
        return vectors

    async def run(
        self,
        source: LoaderSource,
        *,
        embedder: Embedder,
        splitter: str,
        chunk_size: int,
        chunk_overlap: int,
    ) -> IngestResult:
        """三个阶段串起来（每段耗时写进 `stage_latency_ms`，供 span 的 `attributes` 用）。"""
        started = perf_counter()
        document = self.load(source)
        parsing_ms = _elapsed_ms(started)

        started = perf_counter()
        chunks = self.split(document, splitter=splitter, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        chunking_ms = _elapsed_ms(started)

        started = perf_counter()
        vectors = await self.embed(chunks, embedder=embedder)
        embedding_ms = _elapsed_ms(started)

        return IngestResult(
            document=document,
            chunks=chunks,
            vectors=vectors,
            stage_latency_ms={
                STAGE_PARSING: parsing_ms,
                STAGE_CHUNKING: chunking_ms,
                STAGE_EMBEDDING: embedding_ms,
            },
        )


def _elapsed_ms(started: float) -> int:
    return int((perf_counter() - started) * 1000)
