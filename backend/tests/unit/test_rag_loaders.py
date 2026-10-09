"""Loader 单测（详细设计 4.6.1 / 7.6 测试要求）。

覆盖：编码探测（utf-8 / gb18030）、Markdown 标题层级、后缀白名单与 `KB_UNSUPPORTED_FORMAT`
的错误细节（含"pdf 属迭代 E"的提示）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import KnowledgeBaseUnsupportedFormatError
from app.runtime.rag.base import LoaderSource
from app.runtime.rag.loaders import (
    LoaderRegistry,
    MarkdownLoader,
    TextLoader,
    decode_text,
    parse_headings,
    source_suffix,
)


def _source(path: Path, *, filename: str | None = None) -> LoaderSource:
    return LoaderSource(path=path, filename=filename or path.name)


def test_decode_text_prefers_utf8(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("你好，世界", encoding="utf-8")

    document = TextLoader().load(_source(path))

    assert document.text == "你好，世界"
    assert document.meta["original_encoding"] == "utf-8-sig"
    assert document.meta["uploaded_by"] == "local"


def test_decode_text_falls_back_to_gb18030() -> None:
    text, encoding = decode_text("你好，世界".encode("gb18030"))

    assert text == "你好，世界"
    assert encoding == "gb18030"


def test_markdown_loader_parses_headings(tmp_path: Path) -> None:
    path = tmp_path / "install.md"
    path.write_text("# 安装\n\n正文\n\n## 2.1 前置\n\n更多正文\n", encoding="utf-8")

    document = MarkdownLoader().load(_source(path))

    assert [item["text"] for item in document.meta["headings"]] == ["安装", "2.1 前置"]
    assert [item["level"] for item in document.meta["headings"]] == [1, 2]
    assert document.meta["filename"] == "install.md"


def test_parse_headings_ignores_code_fence_free_text() -> None:
    assert parse_headings("没有标题\n正文") == []
    assert parse_headings("#### 四级") == [{"line": 0, "level": 4, "text": "四级"}]


def test_loader_registry_selects_by_suffix(tmp_path: Path) -> None:
    md = tmp_path / "guide.markdown"
    md.write_text("# 标题\n", encoding="utf-8")

    registry = LoaderRegistry()

    assert registry.for_source(_source(md)).name == "markdown"
    assert isinstance(registry.for_source(_source(md)), MarkdownLoader)


def test_loader_registry_rejects_pdf_with_backlog_hint(tmp_path: Path) -> None:
    pdf = tmp_path / "manual.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    with pytest.raises(KnowledgeBaseUnsupportedFormatError) as excinfo:
        LoaderRegistry().load(_source(pdf))

    details = excinfo.value.details
    assert details["suffix"] == ".pdf"
    assert ".md" in details["supported"]
    assert "迭代 E" in details["backlog"]


def test_loader_registry_rejects_unknown_suffix_without_backlog_hint(tmp_path: Path) -> None:
    weird = tmp_path / "data.bin"
    weird.write_bytes(b"\x00\x01")

    with pytest.raises(KnowledgeBaseUnsupportedFormatError) as excinfo:
        LoaderRegistry().load(_source(weird))

    assert "backlog" not in excinfo.value.details
    assert source_suffix(_source(weird)) == ".bin"
