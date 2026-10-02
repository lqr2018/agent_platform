"""SSE 事件契约测试（详细设计 3.4 / 附录 B / 7.2）。

断言三处一致：

1. 《详细设计》3.4 表格的 21 个事件名 == `core/events.py::SseEventType` 的取值集合；
2. 每个**非 Backlog** 事件在表格里列出的"关键字段"都能在 `SSE_PAYLOAD_MODELS` 的模型里找到
   （MVP 的 18 个事件全部有模型；Backlog 的 3 个事件按 SD-14② 不定义 payload）；
3. `frontend/src/types/events.ts` 的联合类型字面量与之相同（附录 B 的第三条约束）。

新增事件必须**同时**改这三处 + 3.4 表 + 附录 B，否则本测试失败。
"""

from __future__ import annotations

import re
from pathlib import Path as FsPath

import pytest
from pydantic import BaseModel

from app.core.events import SSE_PAYLOAD_MODELS, SseEventType

REPO_ROOT = FsPath(__file__).resolve().parents[3]
DOC_PATH = REPO_ROOT / "docs" / "详细设计.md"
EVENTS_TS_PATH = REPO_ROOT / "frontend" / "src" / "types" / "events.ts"

BACKLOG_MARKER = "Backlog"
EVENT_ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*`([a-z.]+)`\s*\|(?P<stage>[^|]*)\|(?P<payload>[^|]*)\|")
"""匹配 3.4 表格的数据行：`| # | event | 阶段 | payload（关键字段） | 触发时机 |`。"""


def _doc_rows() -> list[tuple[str, str, str]]:
    """解析 3.4 表，返回 `[(event, stage, payload_cell)]`。"""
    text = DOC_PATH.read_text(encoding="utf-8")
    section = text.split("### 3.4 SSE 事件协议")[1].split("\n### 3.5")[0]
    rows: list[tuple[str, str, str]] = []
    for line in section.splitlines():
        match = EVENT_ROW.match(line.strip())
        if match:
            rows.append((match.group(2), match.group("stage").strip(), match.group("payload").strip()))
    return rows


def _documented_keys(payload_cell: str) -> set[str]:
    """把 payload 单元格里的"关键字段"解析成字段名集合。

    规则：去反引号与括号；`[]` 去掉；`/` 表示"共享后缀的并列字段"（`prompt/completion_tokens`
    → `prompt_tokens` + `completion_tokens`）；`error.code` 这类点号路径保留（与模型的嵌套结构对齐）。
    """
    cell = payload_cell.replace("`", "").strip()
    cell = re.sub(r"（[^）]*）", "", cell)  # 去掉中文括号（如 `items[]（id/kind/score）`）
    keys: set[str] = set()
    for item in cell.split(","):
        entry = item.strip().replace("[]", "")
        if not entry:
            continue
        parts = [part.strip() for part in entry.split("/") if part.strip()]
        if not parts:
            continue
        last = parts[-1]
        keys.add(last)
        if len(parts) > 1 and "_" in last:
            suffix = last[last.index("_") :]  # `completion_tokens` → `_tokens`
            keys.update(f"{part}{suffix}" for part in parts[:-1])
    return {key for key in keys if key and key != "{}"}


def _flatten(model: type[BaseModel]) -> set[str]:
    """模型的可达字段路径（顶层 + 一层嵌套，如 `error.code`）。"""
    paths: set[str] = set()
    for name, field in model.model_fields.items():
        paths.add(name)
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            paths |= {f"{name}.{child}" for child in annotation.model_fields}
    return paths


def _satisfied(key: str, paths: set[str]) -> bool:
    """文档里的简写也算命中：`message` 可对应 `error.message`（1.6 错误信封的字段）。"""
    if key in paths:
        return True
    return any(path.rsplit(".", 1)[-1] == key for path in paths)


def test_doc_event_names_match_enum() -> None:
    """3.4 表的 21 个事件名 == `SseEventType` 的取值集合（附录 B 约束 1）。"""
    rows = _doc_rows()
    assert len(rows) == 21, f"3.4 表应有 21 行事件，实际 {len(rows)}"
    assert {event for event, _, _ in rows} == {str(member) for member in SseEventType}


def test_backlog_events_have_no_payload_model() -> None:
    """Backlog 的 3 个事件不定义 payload（SD-14②：MVP 不产生它们）。"""
    backlog = [event for event, stage, _ in _doc_rows() if BACKLOG_MARKER in stage]
    assert backlog == ["approval.required", "memory.recalled", "memory.updated"]
    for event in backlog:
        assert SseEventType(event) not in SSE_PAYLOAD_MODELS


def test_mvp_events_have_payload_models() -> None:
    """MVP 的 18 个事件都必须有 payload 模型。"""
    mvp = [event for event, stage, _ in _doc_rows() if BACKLOG_MARKER not in stage]
    assert len(mvp) == 18
    for event in mvp:
        assert SseEventType(event) in SSE_PAYLOAD_MODELS, f"{event} 缺少 payload 模型"


@pytest.mark.parametrize(("payload_cell", "event"), [(row[2], row[0]) for row in _doc_rows()])
def test_documented_payload_keys_exist_in_models(payload_cell: str, event: str) -> None:
    """3.4 表列出的关键字段必须都在模型的字段（含一层嵌套路径）里。"""
    member = SseEventType(event)
    model = SSE_PAYLOAD_MODELS.get(member)
    if model is None:
        pytest.skip(f"{event} 属 Backlog（无 payload 模型）")
    documented = _documented_keys(payload_cell)
    paths = _flatten(model)
    missing = sorted(key for key in documented if not _satisfied(key, paths))
    assert not missing, f"{event} 的 payload 模型缺少字段：{missing}"


def test_frontend_event_types_match_enum() -> None:
    """前端 `types/events.ts` 的联合类型字面量 == 后端枚举（附录 B 约束 3）。"""
    source = EVENTS_TS_PATH.read_text(encoding="utf-8")
    body = source.split("export type SseEventType =", 1)[1].split(";", 1)[0]
    declared = set(re.findall(r'"([a-z.]+)"', body))
    assert declared == {str(member) for member in SseEventType}
