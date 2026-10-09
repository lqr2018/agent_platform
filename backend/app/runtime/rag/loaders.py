"""Loader 实现（详细设计 4.6.1 / 7.6 任务 1）。

MVP 只做**单文件文本类**（7.6 的收窄口径）：

| Loader | 后缀 | 说明 |
|---|---|---|
| `TextLoader` | `.txt` | 编码探测（`utf-8-sig` → `utf-8` → `gb18030` → `latin-1`） |
| `MarkdownLoader` | `.md` / `.markdown` | 在文本基础上把标题层级解析进 `meta["headings"]` |

`PdfLoader`（pypdf 按页）与 `WebLoader`（httpx 抓取）属**迭代 E**（4.6.1 的 Backlog 清单），
本阶段不注册：请求 pdf/其它后缀会得到 `KB_UNSUPPORTED_FORMAT`（422，附录 A），而不是 500。

`LoaderRegistry` **按后缀**选 Loader（不看 MIME）：上传文件名与浏览器猜的 `mime_type` 都不可信，
"后缀白名单 + 编码兜底"是最不容易被文件名绕过的组合（4.6.1 未规定优先级，此处按安全默认实现）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from app.core.errors import KnowledgeBaseIngestFailedError, KnowledgeBaseUnsupportedFormatError
from app.runtime.rag.base import LoadedDocument, Loader, LoaderSource

TEXT_SUFFIXES: Final[tuple[str, ...]] = (".txt",)
MARKDOWN_SUFFIXES: Final[tuple[str, ...]] = (".md", ".markdown")
SUPPORTED_SUFFIXES: Final[tuple[str, ...]] = TEXT_SUFFIXES + MARKDOWN_SUFFIXES
BACKLOG_SUFFIXES: Final[tuple[str, ...]] = (".pdf",)
"""迭代 E 才注册的后缀（4.6.1）：列出来是为了让错误提示能说清"为什么现在不行"。"""

ENCODING_CANDIDATES: Final[tuple[str, ...]] = ("utf-8-sig", "utf-8", "gb18030", "latin-1")
"""解码顺序（4.6.1 的"编码探测"）：最后一项能解出任意字节序列，保证 Loader 永不因编码失败。"""

HEADING_PATTERN: Final = re.compile(r"^(#{1,6})\s+(.*)$")


def source_suffix(source: LoaderSource) -> str:
    """取后缀（以 `filename` 为准，缺省回落到 `path.name`）。"""
    return Path(source.filename or source.path.name).suffix.lower()


def decode_text(raw: bytes) -> tuple[str, str]:
    """按 `ENCODING_CANDIDATES` 依次尝试；返回 `(text, encoding)`（2.8 的 `meta.original_encoding`）。"""
    for encoding in ENCODING_CANDIDATES:
        try:
            return raw.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):  # pragma: no cover - latin-1 之后不再失败
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"  # pragma: no cover


def parse_headings(text: str) -> list[dict[str, Any]]:
    """解析 ATX 标题（`# 标题`）→ `[{"line": 0, "level": 1, "text": "安装"}]`。

    行号从 0 开始，与 `splitters` 里的切分位置同一坐标系（`\n` 计数）。
    """
    headings: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines()):
        match = HEADING_PATTERN.match(line.strip())
        if match is not None:
            headings.append({"line": index, "level": len(match.group(1)), "text": match.group(2).strip()})
    return headings


def _base_meta(source: LoaderSource, *, encoding: str) -> dict[str, Any]:
    """2.8 的 `documents.meta`：上传者 + 原始编码（+ 可选的来源 URL）。"""
    meta: dict[str, Any] = {
        "uploaded_by": "local",  # SD-3：无鉴权，owner_key 固定 local
        "filename": source.filename or source.path.name,
        "original_encoding": encoding,
    }
    if source.source_url:
        meta["source_url"] = source.source_url
    return meta


def _read_bytes(source: LoaderSource) -> bytes:
    """读原始字节；文件缺失属内部故障 → `KB_INGEST_FAILED`（附录 A：`error_message` 含阶段）。"""
    try:
        return source.path.read_bytes()
    except OSError as exc:
        raise KnowledgeBaseIngestFailedError(
            f"Failed to read document during parsing: {exc}",
            details={"stage": "parsing", "filename": source.filename or source.path.name},
        ) from exc


class TextLoader:
    """纯文本（4.6.1）：解码 + meta，不做任何结构解析。"""

    name = "text"

    def supports(self, source: LoaderSource) -> bool:
        return source_suffix(source) in TEXT_SUFFIXES

    def load(self, source: LoaderSource) -> LoadedDocument:
        text, encoding = decode_text(_read_bytes(source))
        return LoadedDocument(text=text, meta=_base_meta(source, encoding=encoding))


class MarkdownLoader(TextLoader):
    """Markdown（4.6.1）：额外把标题层级放进 `meta["headings"]`（供 `MarkdownSplitter` 使用）。"""

    name = "markdown"

    def supports(self, source: LoaderSource) -> bool:
        return source_suffix(source) in MARKDOWN_SUFFIXES

    def load(self, source: LoaderSource) -> LoadedDocument:
        document = super().load(source)
        return LoadedDocument(text=document.text, meta={**document.meta, "headings": parse_headings(document.text)})


class LoaderRegistry:
    """按后缀选择 Loader；不支持的格式抛 `KB_UNSUPPORTED_FORMAT`（422，含可用后缀清单）。"""

    def __init__(self, loaders: Sequence[Loader] | None = None) -> None:
        self._loaders: tuple[Loader, ...] = tuple(loaders) if loaders is not None else (MarkdownLoader(), TextLoader())

    @property
    def suffixes(self) -> tuple[str, ...]:
        return SUPPORTED_SUFFIXES

    def for_source(self, source: LoaderSource) -> Loader:
        for loader in self._loaders:
            if loader.supports(source):
                return loader
        suffix = source_suffix(source)
        details: dict[str, Any] = {
            "filename": source.filename or source.path.name,
            "suffix": suffix,
            "supported": list(SUPPORTED_SUFFIXES),
        }
        if suffix in BACKLOG_SUFFIXES:
            details["backlog"] = "迭代 E（7.6 的收窄口径：MVP 只支持 md/txt）"
        raise KnowledgeBaseUnsupportedFormatError(
            f"Unsupported document format '{suffix or 'unknown'}'; MVP supports md/txt only",
            details=details,
        )

    def load(self, source: LoaderSource) -> LoadedDocument:
        """选 Loader 并解析（服务层在 `parsing` 状态下调用）。"""
        return self.for_source(source).load(source)
