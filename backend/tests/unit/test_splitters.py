"""切片器单测（详细设计 4.6.1 / 7.6 测试要求）。

两条不变式被钉住：**每片 ≤ `chunk_size`**、**相邻片有 `chunk_overlap` 字符重叠**；
外加三个边界：空文档、超长单段（无分隔符）、非法参数。
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from app.core.errors import ValidationError
from app.runtime.rag.base import LoadedDocument
from app.runtime.rag.loaders import parse_headings
from app.runtime.rag.splitters import (
    MarkdownSplitter,
    RecursiveSplitter,
    build_splitter,
    estimate_tokens,
)


def _document(text: str, **meta: object) -> LoadedDocument:
    return LoadedDocument(text=text, meta=dict(meta))


def test_recursive_splitter_respects_chunk_size_and_overlap() -> None:
    text = "\n\n".join(f"第{i}段：" + "内容" * 40 for i in range(6))
    chunks = RecursiveSplitter(chunk_size=120, chunk_overlap=30).split(_document(text))

    assert len(chunks) > 1
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    assert all(len(chunk.content) <= 120 for chunk in chunks)
    for previous, current in pairwise(chunks):
        tail = previous.content.strip()[-20:]
        assert tail and tail in current.content, "相邻切片必须带 chunk_overlap 的重叠"


def test_recursive_splitter_hard_splits_long_text_without_separators() -> None:
    """超长单段（没有任何分隔符）按字符硬切，仍然每片 ≤ `chunk_size`。"""
    chunks = RecursiveSplitter(chunk_size=100, chunk_overlap=20).split(_document("a" * 300))

    assert len(chunks) >= 3
    assert all(len(chunk.content) <= 100 for chunk in chunks)


def test_recursive_splitter_returns_empty_for_blank_document() -> None:
    assert RecursiveSplitter(chunk_size=100, chunk_overlap=10).split(_document("   \n\n  ")) == []


def test_recursive_splitter_sets_token_count_and_meta() -> None:
    chunks = RecursiveSplitter(chunk_size=50, chunk_overlap=10).split(_document("你好世界" * 10, filename="a.md"))

    assert chunks
    assert all(chunk.token_count > 0 for chunk in chunks)
    assert all(chunk.meta["filename"] == "a.md" for chunk in chunks)


def test_markdown_splitter_keeps_heading_in_chunk_meta() -> None:
    text = "# 安装\n\n安装步骤说明\n\n## 配置\n\n配置内容说明\n"
    document = LoadedDocument(
        text=text,
        meta={"filename": "install.md", "headings": parse_headings(text)},
    )
    chunks = MarkdownSplitter(chunk_size=200, chunk_overlap=40).split(document)

    headings = [chunk.meta.get("heading") for chunk in chunks]
    assert "安装" in headings
    assert "配置" in headings
    assert all(chunk.meta.get("filename") == "install.md" for chunk in chunks)


def test_markdown_splitter_falls_back_to_recursive_without_headings() -> None:
    chunks = MarkdownSplitter(chunk_size=60, chunk_overlap=10).split(_document("没有标题的正文。" * 20))

    assert len(chunks) > 1
    assert all(len(chunk.content) <= 60 for chunk in chunks)


def test_splitter_rejects_invalid_parameters() -> None:
    with pytest.raises(ValidationError):
        RecursiveSplitter(chunk_size=100, chunk_overlap=100)
    with pytest.raises(ValidationError):
        RecursiveSplitter(chunk_size=100, chunk_overlap=-1)
    with pytest.raises(ValidationError):
        RecursiveSplitter(chunk_size=0, chunk_overlap=0)


def test_build_splitter_supports_documented_names_only() -> None:
    assert isinstance(build_splitter("recursive", chunk_size=100, chunk_overlap=10), RecursiveSplitter)
    assert isinstance(build_splitter("markdown", chunk_size=100, chunk_overlap=10), MarkdownSplitter)
    with pytest.raises(ValidationError):  # `token` 属迭代 E（4.6.1 的 Backlog）
        build_splitter("token", chunk_size=100, chunk_overlap=10)


def test_estimate_tokens_counts_cjk_one_to_one() -> None:
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("") == 1
