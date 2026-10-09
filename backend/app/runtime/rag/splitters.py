"""切片器（详细设计 4.6.1 / 7.6 任务 2）。

MVP 实现两个（7.6 任务 2 要求"三个 Splitter + `chunk_overlap` 边界测试"，第三个 `TokenSplitter`
属迭代 E，故本阶段两个）：

| Splitter | 切法 | `meta` 增量 |
|---|---|---|
| `RecursiveSplitter` | 按 `\\n\\n → \\n → 。！？ → .` 逐级递归，再贪心合并到 `chunk_size` | 继承文档 meta |
| `MarkdownSplitter` | 先按标题分段（`headings`），段内超长再递归 | `heading` / `heading_level` |

两条不变式（被 `tests/unit/test_splitters.py` 钉住）：

1. **每片长度 ≤ `chunk_size`**（硬上限，含 overlap 部分）；
2. **相邻两片有 `chunk_overlap` 个字符重叠**（4.6.1 的"带 overlap"），且 `0 ≤ overlap < chunk_size`。

`ordinal` 在**过滤掉空白片之后**重新编号（`(document_id, ordinal)` 是 UNIQUE，不能跳号太随意，
但也不能把纯空白片写进 `chunks`）。
"""

from __future__ import annotations

from collections.abc import Sequence
from math import ceil
from typing import Final

from app.core.errors import ValidationError
from app.runtime.rag.base import LoadedDocument, Splitter, TextChunk

DEFAULT_SEPARATORS: Final[tuple[str, ...]] = ("\n\n", "\n", "。", "！", "？", ".")
"""4.6.1 的默认分隔符链（从粗到细）；最后一级不再有分隔符时按字符硬切。"""

CJK_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x3000, 0x303F),  # CJK 标点
    (0x4E00, 0x9FFF),  # CJK 统一表意文字
    (0xFF00, 0xFFEF),  # 全角字符
)


def estimate_tokens(text: str) -> int:
    """粗略 token 估计（2.8 的 `chunks.token_count`）。

    CJK 字符按 1:1、其余按 4 字符 = 1 token —— 这只用于"量级展示"（KB 页的切片信息、
    `message_meta` 类的统计），不参与任何计费或截断判断，因此**不引入 tokenizer 依赖**。
    """
    cjk = sum(1 for char in text if any(low <= ord(char) <= high for low, high in CJK_RANGES))
    return max(1, cjk + ceil((len(text) - cjk) / 4))


class RecursiveSplitter:
    """4.6.1 的 `RecursiveSplitter`：逐级分隔符 + 贪心合并 + 重叠。"""

    name = "recursive"

    def __init__(
        self,
        *,
        chunk_size: int,
        chunk_overlap: int,
        separators: Sequence[str] = DEFAULT_SEPARATORS,
    ) -> None:
        if chunk_size <= 0:
            raise ValidationError("chunk_size must be a positive integer", details={"chunk_size": chunk_size})
        if chunk_overlap < 0 or chunk_overlap >= chunk_size:
            raise ValidationError(
                "chunk_overlap must satisfy 0 <= overlap < chunk_size",
                details={"chunk_size": chunk_size, "chunk_overlap": chunk_overlap},
            )
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._separators = tuple(separators)

    @property
    def chunk_size(self) -> int:
        return self._chunk_size

    @property
    def chunk_overlap(self) -> int:
        return self._chunk_overlap

    def split(self, document: LoadedDocument) -> list[TextChunk]:
        pieces = self._merge(self._segments(document.text, 0))
        chunks: list[TextChunk] = []
        for piece in pieces:
            text = piece.strip()
            if not text:
                continue
            chunks.append(
                TextChunk(
                    ordinal=len(chunks),
                    content=text,
                    meta=dict(document.meta),
                    token_count=estimate_tokens(text),
                )
            )
        return chunks

    # ---- 内部：切分与合并 ----

    def _segments(self, text: str, level: int) -> list[str]:
        """把文本切成"每片 ≤ `chunk_size`"的原子片段（尽可能用粗分隔符）。"""
        if len(text) <= self._chunk_size:
            return [text] if text else []
        if level >= len(self._separators):
            return [text[index : index + self._chunk_size] for index in range(0, len(text), self._chunk_size)]
        separator = self._separators[level]
        segments: list[str] = []
        for part in _split_keep_separator(text, separator):
            if len(part) <= self._chunk_size:
                segments.append(part)
            else:
                segments.extend(self._segments(part, level + 1))
        return segments

    def _merge(self, segments: Sequence[str]) -> list[str]:
        """贪心合并到 `chunk_size`，并在新片起点保留上一片的尾部 `chunk_overlap` 字符。"""
        chunks: list[str] = []
        current = ""
        for segment in segments:
            if not current:
                current = segment
            elif len(current) + len(segment) <= self._chunk_size:
                current += segment
            else:
                chunks.append(current)
                current = _overlap_tail(current, self._chunk_overlap) + segment
            while len(current) > self._chunk_size:
                chunks.append(current[: self._chunk_size])
                current = _overlap_tail(current[: self._chunk_size], self._chunk_overlap) + current[self._chunk_size :]
        if current.strip():
            chunks.append(current)
        return chunks


class MarkdownSplitter:
    """4.6.1 的 `MarkdownSplitter`：先按标题分段（用 `MarkdownLoader` 解析的 `headings`），段内超长再递归。

    每片 `meta` 增加 `heading`（所属标题文本 —— 4.6.3 的引用来源靠它拼出 `install.md#2.1`）
    与 `heading_level`（1–6）；首个标题之前的前言部分 `heading=""`。
    """

    name = "markdown"

    def __init__(self, *, chunk_size: int, chunk_overlap: int) -> None:
        self._recursive = RecursiveSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    def split(self, document: LoadedDocument) -> list[TextChunk]:
        chunks: list[TextChunk] = []
        for heading, level, text in self._sections(document):
            meta = dict(document.meta)
            if heading:
                meta = {**meta, "heading": heading, "heading_level": level}
            for chunk in self._recursive.split(LoadedDocument(text=text, meta=meta)):
                chunks.append(
                    TextChunk(
                        ordinal=len(chunks),
                        content=chunk.content,
                        meta=dict(chunk.meta),
                        token_count=chunk.token_count,
                    )
                )
        return chunks

    def _sections(self, document: LoadedDocument) -> list[tuple[str, int, str]]:
        """`(标题, 层级, 正文)`；没有可用的标题信息时整篇作为一节（回落到纯递归切分）。"""
        headings = document.meta.get("headings")
        if not isinstance(headings, list) or not headings:
            return [("", 0, document.text)]
        lines = document.text.splitlines(keepends=True)
        marks: list[tuple[int, str, int]] = []
        for item in headings:
            if not isinstance(item, dict):
                continue
            line = item.get("line")
            if not isinstance(line, int) or not 0 <= line < len(lines):
                continue
            marks.append((line, str(item.get("text") or ""), int(item.get("level") or 0)))
        if not marks:
            return [("", 0, document.text)]
        sections: list[tuple[str, int, str]] = []
        if marks[0][0] > 0:
            sections.append(("", 0, "".join(lines[: marks[0][0]])))
        for index, (line, title, level) in enumerate(marks):
            end = marks[index + 1][0] if index + 1 < len(marks) else len(lines)
            sections.append((title, level, "".join(lines[line:end])))
        return sections


def _split_keep_separator(text: str, separator: str) -> list[str]:
    """按分隔符切分，并把分隔符**并入前一片**（否则句末标点会丢，引用片段读起来是断句）。"""
    if not separator or separator not in text:
        return [text]
    parts = text.split(separator)
    merged: list[str] = []
    for index, part in enumerate(parts):
        merged.append(part + separator if index < len(parts) - 1 else part)
    return [item for item in merged if item]


def _overlap_tail(text: str, overlap: int) -> str:
    return text[-overlap:] if overlap > 0 else ""


def build_splitter(name: str, *, chunk_size: int, chunk_overlap: int) -> Splitter:
    """按 KB 的 `splitter` 列创建（2.8：`recursive` / `markdown`；`token` 属迭代 E）。"""
    if name == "recursive":
        return RecursiveSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    if name == "markdown":
        return MarkdownSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    raise ValidationError(
        f"Unsupported splitter '{name}'",
        details={"supported": ["recursive", "markdown"], "backlog": ["token（迭代 E）"]},
    )
