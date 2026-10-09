"""Embedder 单测（详细设计 4.6.1 / 4.1.3 / 7.6 任务 3）。

覆盖：`FakeEmbedder` 的确定性（7.6 的集成用例依赖它离线跑通）、`OpenAICompatibleEmbedder`
的分批与重试（429 重试一次成功、重试耗尽包成 `KB_INGEST_FAILED`）、维度探测与 `EMBED_ALLOW_FAKE` 闸门。
"""

from __future__ import annotations

import pytest

from app.core.errors import KnowledgeBaseIngestFailedError, ModelRateLimitedError, ValidationError
from app.runtime.rag.embedders import (
    FakeEmbedder,
    OpenAICompatibleEmbedder,
    build_embedder,
    probe_embedding_dim,
)
from app.runtime.rag.vectorstores import cosine_similarity


class _StubProvider:
    """最小 Provider 桩：只实现 `embed`，并记录每次调用的批量大小。"""

    name = "stub"

    def __init__(self, *, dim: int = 8, fail_times: int = 0) -> None:
        self.calls: list[list[str]] = []
        self._dim = dim
        self._fail_times = fail_times

    async def chat(self, *args: object, **kwargs: object) -> None:  # pragma: no cover - 本测试不用
        raise NotImplementedError

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        self.calls.append(list(texts))
        if len(self.calls) <= self._fail_times:
            raise ModelRateLimitedError("rate limited")
        return [[0.5] * self._dim for _ in texts]


async def test_fake_embedder_is_deterministic_and_unit_length() -> None:
    embedder = FakeEmbedder(dim=32)

    first = (await embedder.embed(["安装 步骤"]))[0]
    second = (await embedder.embed(["安装 步骤"]))[0]

    assert first == second
    assert cosine_similarity(first, first) == pytest.approx(1.0, abs=1e-6)


async def test_fake_embedder_scores_shared_tokens_higher() -> None:
    embedder = FakeEmbedder(dim=64)
    base, same_topic, other = await embedder.embed(["知识库 检索 命中", "知识库 检索 排序", "python 单元测试"])

    assert cosine_similarity(base, same_topic) > cosine_similarity(base, other)


async def test_fake_embedder_rejects_non_positive_dim() -> None:
    with pytest.raises(ValidationError):
        FakeEmbedder(dim=0)


async def test_openai_embedder_batches_requests() -> None:
    provider = _StubProvider(dim=4)
    embedder = OpenAICompatibleEmbedder(provider, model="embed-1", batch_size=2)

    vectors = await embedder.embed(["a", "b", "c"])

    assert len(vectors) == 3
    assert [len(call) for call in provider.calls] == [2, 1]


async def test_openai_embedder_retries_rate_limit_then_succeeds() -> None:
    provider = _StubProvider(dim=4, fail_times=1)
    embedder = OpenAICompatibleEmbedder(provider, model="embed-1", batch_size=2, max_retries=1)

    vectors = await embedder.embed(["a"])

    assert len(vectors) == 1
    assert len(provider.calls) == 2


async def test_openai_embedder_wraps_exhausted_retries() -> None:
    provider = _StubProvider(dim=4, fail_times=5)
    embedder = OpenAICompatibleEmbedder(provider, model="embed-1", max_retries=0)

    with pytest.raises(KnowledgeBaseIngestFailedError) as excinfo:
        await embedder.embed(["a"])

    assert excinfo.value.details["stage"] == "embedding"
    assert excinfo.value.details["code"] == "MODEL_RATE_LIMITED"


async def test_openai_embedder_rejects_wrong_vector_count() -> None:
    class _ShortProvider(_StubProvider):
        async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
            self.calls.append(list(texts))
            return []

    embedder = OpenAICompatibleEmbedder(_ShortProvider(), model="embed-1", batch_size=4)

    with pytest.raises(KnowledgeBaseIngestFailedError) as excinfo:
        await embedder.embed(["a", "b"])

    assert excinfo.value.details["expected"] == 2


async def test_probe_embedding_dim_returns_vector_length() -> None:
    assert await probe_embedding_dim(FakeEmbedder(dim=48)) == 48


async def test_build_embedder_gates_fake_provider() -> None:
    with pytest.raises(ValidationError) as excinfo:
        build_embedder(
            provider=None,
            model="fake-embedding",
            batch_size=32,
            max_retries=2,
            allow_fake=False,
        )
    assert excinfo.value.details["setting"] == "EMBED_ALLOW_FAKE"

    embedder = build_embedder(
        provider=None,
        model="fake-embedding",
        batch_size=32,
        max_retries=2,
        allow_fake=True,
        fake_dim=16,
    )
    assert isinstance(embedder, FakeEmbedder)
    assert await probe_embedding_dim(embedder) == 16


async def test_build_embedder_uses_openai_compatible_for_real_provider() -> None:
    embedder = build_embedder(
        provider=_StubProvider(dim=4),
        model="text-embedding-3-small",
        batch_size=8,
        max_retries=1,
        allow_fake=False,
    )

    assert isinstance(embedder, OpenAICompatibleEmbedder)
