"""Embedder（详细设计 4.6.1 / 4.1.3 / 7.6 任务 3）。

| Embedder | 用途 | 行为 |
|---|---|---|
| `OpenAICompatibleEmbedder` | 生产 | 复用 `LLMProvider.embed`（4.1.2 的 `POST /embeddings`）+ 分批 + 重试 |
| `FakeEmbedder` | 测试 / 离线演示 | 词元级 hashing trick + L2 归一化，确定性、**不联网** |

**维度探测**（2.8："创建 KB 时探测并固化"）：`probe_embedding_dim()` 调一次 `embed()` 取长度，
调用方把它写进 `knowledge_bases.embedding_dim`；返回 0 / 空向量 → `VALIDATION_ERROR`（7.6 测试要求）。

`FakeEmbedder` 为什么用词元级哈希而不是整段哈希：整段哈希下"共享词汇的两段文本"余弦相似度
近似 0，7.6 的集成用例（上传文档 → 用自然语言 query 命中预期 chunk）就没法离线验证；
词元级 hashing trick 保留"共享词越多越相似"的性质，同时保持跨进程确定（无随机种子）。
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterator, Sequence
from typing import Final

from app.core.errors import AppError, KnowledgeBaseIngestFailedError, ValidationError
from app.core.retry import call_with_retry
from app.runtime.llm.base import LLMProvider
from app.runtime.rag.base import DEFAULT_FAKE_EMBEDDING_DIM, Embedder

EMBEDDING_PROBE_TEXT: Final = "embedding dimension probe"
"""维度探测用的固定文本（内容不重要，只关心返回向量的长度）。"""

RETRYABLE_MODEL_CODES: Final[frozenset[str]] = frozenset(
    {"MODEL_RATE_LIMITED", "MODEL_TIMEOUT", "MODEL_PROVIDER_UNAVAILABLE"}
)
"""4.1.2 的重试口径：只重试限流 / 超时 / 上游不可用，参数类错误立即失败。"""

TOKEN_PATTERN: Final = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """词元：连续的英文数字串 + 单个 CJK 字符（无需词表，见模块 docstring）。"""
    return TOKEN_PATTERN.findall(text.lower())


class FakeEmbedder:
    """确定性 embedding（4.1.3 / 7.6 任务 3）：同文本恒定、共享词元越多越相似。"""

    def __init__(self, *, model: str = "fake-embedding", dim: int = DEFAULT_FAKE_EMBEDDING_DIM) -> None:
        if dim <= 0:
            raise ValidationError("Embedding dimension must be positive", details={"dim": dim})
        self.model = model
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self._dim
        for token in tokenize(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self._dim
            vector[index] += 1.0 if digest[4] % 2 == 0 else -1.0
        return _normalize(vector)


class OpenAICompatibleEmbedder:
    """生产实现：分批 + 重试（1.5.3 的 `call_with_retry`，不在业务代码里裸写重试循环）。"""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        model: str,
        batch_size: int = 32,
        max_retries: int = 2,
    ) -> None:
        if batch_size <= 0:
            raise ValidationError("Embedding batch size must be positive", details={"batch_size": batch_size})
        self.model = model
        self._provider = provider
        self._batch_size = batch_size
        self._max_retries = max(0, max_retries)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for batch in _batched(texts, self._batch_size):
            vectors.extend(await self._embed_batch(batch))
        return vectors

    async def _embed_batch(self, batch: Sequence[str]) -> list[list[float]]:
        async def call() -> list[list[float]]:
            return await self._provider.embed(list(batch), model=self.model)

        try:
            vectors = await call_with_retry(
                call,
                times=self._max_retries,
                retry_on=(AppError,),
                should_retry=_should_retry,
                event="rag.embed",
            )
        except AppError as exc:
            raise KnowledgeBaseIngestFailedError(
                f"Embedding failed: {exc.message}",
                details={"stage": "embedding", "code": str(exc.code)},
            ) from exc
        if len(vectors) != len(batch):
            raise KnowledgeBaseIngestFailedError(
                "Embedding provider returned a wrong number of vectors",
                details={"stage": "embedding", "expected": len(batch), "got": len(vectors)},
            )
        if any(not vector for vector in vectors):
            raise KnowledgeBaseIngestFailedError(
                "Embedding provider returned an empty vector",
                details={"stage": "embedding", "model": self.model},
            )
        return vectors


def build_embedder(
    *,
    provider: LLMProvider | None,
    model: str,
    batch_size: int,
    max_retries: int,
    allow_fake: bool,
    fake_dim: int = DEFAULT_FAKE_EMBEDDING_DIM,
) -> Embedder:
    """按 Provider 选择实现；`fake` 必须显式开启 `EMBED_ALLOW_FAKE=true`（4.1.3）。"""
    if provider is None or getattr(provider, "name", "") == "fake":
        if not allow_fake:
            raise ValidationError(
                "The fake embedding provider requires EMBED_ALLOW_FAKE=true",
                details={"setting": "EMBED_ALLOW_FAKE"},
            )
        return FakeEmbedder(model=model or "fake-embedding", dim=fake_dim)
    return OpenAICompatibleEmbedder(provider, model=model, batch_size=batch_size, max_retries=max_retries)


async def probe_embedding_dim(embedder: Embedder) -> int:
    """2.8：创建 KB 时探测 `embedding_dim`（拿不到有效维度 → `VALIDATION_ERROR`）。"""
    vectors = await embedder.embed([EMBEDDING_PROBE_TEXT])
    dim = len(vectors[0]) if vectors and vectors[0] else 0
    if dim <= 0:
        raise ValidationError(
            "Embedding provider did not return a usable dimension",
            details={"stage": "probe", "model": embedder.model},
        )
    return dim


def _should_retry(error: BaseException, attempt: int) -> bool:
    return str(getattr(error, "code", "")) in RETRYABLE_MODEL_CODES


def _batched(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _normalize(vector: list[float]) -> list[float]:
    """L2 归一化（余弦相似度才等价于点积）；全零输入回退为 `[1, 0, …]`（空文本不会进 pipeline）。"""
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        fallback = [0.0] * len(vector)
        fallback[0] = 1.0
        return fallback
    return [value / norm for value in vector]
