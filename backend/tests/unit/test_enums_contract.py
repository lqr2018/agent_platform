"""文档 ↔ 代码一致性：基线枚举（《详细设计》0.2.2 ↔ `app/core/enums.py`）。

A 段（MVP 生效）必须逐一存在且取值一致；B 段（Backlog，SD-15～SD-18）必须**不存在**，
否则 SD-14② 的"不实现的能力不进 MVP 的枚举"就被破坏了。
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

from app.core import enums as enums_module

DOC_PATH = Path(__file__).resolve().parents[3] / "docs" / "详细设计.md"
ENUM_LINE = re.compile(r"^([A-Za-z]\w*)\s*=\s*(.+)$")
ENUM_VALUE = re.compile(r"^[a-z_][a-z_0-9]*$")


def _fenced_block(anchor: str) -> list[str]:
    text = DOC_PATH.read_text(encoding="utf-8")
    start = text.index(anchor)
    fence_start = text.index("```", start)
    body_start = text.index("\n", fence_start) + 1
    fence_end = text.index("```", body_start)
    return [line.strip() for line in text[body_start:fence_end].splitlines() if line.strip()]


def _documented_a_enums() -> dict[str, list[str]]:
    documented: dict[str, list[str]] = {}
    for line in _fenced_block("**A. MVP 生效**"):
        match = ENUM_LINE.match(line)
        assert match is not None, f"无法解析 0.2.2 A 段的一行：{line!r}"
        name, raw_values = match.groups()
        values = [value.strip() for value in raw_values.split("|")]
        assert values, f"取值列表为空：{line!r}"
        assert all(ENUM_VALUE.match(value) for value in values), f"取值格式异常：{line!r}"
        documented[name] = values
    return documented


def test_documented_phase0_enums_exist_with_same_values() -> None:
    documented = _documented_a_enums()
    for name, values in documented.items():
        enum_cls = getattr(enums_module, name, None)
        assert enum_cls is not None, f"0.2.2 声明了 {name}，但 core/enums.py 未定义"
        assert issubclass(enum_cls, StrEnum)
        assert [member.value for member in enum_cls] == values, f"{name} 取值与文档不一致"


def test_all_enum_classes_are_documented() -> None:
    """反向检查：代码里不得有文档未登记的枚举（只引用、不重定义，0.2.2）。"""
    documented = set(_documented_a_enums())
    defined = {
        name
        for name, obj in vars(enums_module).items()
        if isinstance(obj, type) and issubclass(obj, StrEnum) and obj is not StrEnum
    }
    assert defined == documented


def test_backlog_enums_are_not_defined() -> None:
    block = "\n".join(_fenced_block("**B. 仅 Backlog 保留**"))
    for name in ("MemoryKind", "MemoryScope", "ApprovalStatus"):
        assert name in block, f"0.2.2 B 段应包含 {name}"
        assert not hasattr(enums_module, name), f"{name} 属 Backlog（SD-15/17），MVP 不得定义"


def test_backlog_enum_values_are_not_leaked() -> None:
    assert {"memory", "mcp"}.isdisjoint({member.value for member in enums_module.SpanType})
    assert "mcp" not in {member.value for member in enums_module.ToolType}
    assert "human" not in {member.value for member in enums_module.NodeType}
    assert "approval" not in {member.value for member in enums_module.PermissionDecision}
