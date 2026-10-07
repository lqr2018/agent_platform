"""Checkpoint 序列化与 resume 判定（详细设计 4.5.4 / SD-10）。

结构与引擎无关（4.5.4）：

```json
{ "engine": "simple", "engine_version": "1",
  "state": { ... }, "current_node_id": "router", "pending_branch": null }
```

- 目前只有 `simple` 一种引擎（SD-10），`engine` / `engine_version` 是为"未来可能加第二实现"
  预留的处理位：`resume` 遇到版本不匹配时**降级为从头重跑**（保留已保存的 state）；
- `current_node_id` 的语义 = **接下来要执行的节点**：节点开始前就写成本节点（崩溃后从本节点续跑），
  节点成功后改写成"下一个节点"，恰好落在 `end` 时保持为 `end`。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

ENGINE_NAME = "simple"
ENGINE_VERSION = "1"

PENDING_BRANCH_KEY = "pending_branch"


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """解析后的检查点（`resume` 的输入）。"""

    engine: str
    engine_version: str
    state: dict[str, Any]
    current_node_id: str | None
    pending_branch: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "engine_version": self.engine_version,
            "state": dict(self.state),
            "current_node_id": self.current_node_id,
            PENDING_BRANCH_KEY: self.pending_branch,
        }

    def is_compatible(self) -> bool:
        """引擎与版本都匹配才可续跑（SD-10；否则降级从头重跑）。"""
        return self.engine == ENGINE_NAME and self.engine_version == ENGINE_VERSION


def build_checkpoint(
    state: Mapping[str, Any],
    current_node_id: str | None,
    *,
    pending_branch: str | None = None,
) -> dict[str, Any]:
    """节点结束时构造 checkpoint（写入 `workflow_runs.checkpoint`，4.5.4）。"""
    return Checkpoint(
        engine=ENGINE_NAME,
        engine_version=ENGINE_VERSION,
        state=dict(state),
        current_node_id=current_node_id,
        pending_branch=pending_branch,
    ).as_dict()


def parse_checkpoint(raw: Mapping[str, Any] | None) -> Checkpoint:
    """从 `workflow_runs.checkpoint`（JSON）还原；字段缺失/类型不符时按"无断点"处理。"""
    payload = dict(raw or {})
    state = payload.get("state")
    engine = payload.get("engine")
    version = payload.get("engine_version")
    current = payload.get("current_node_id")
    pending = payload.get(PENDING_BRANCH_KEY)
    return Checkpoint(
        engine=engine if isinstance(engine, str) else "",
        engine_version=version if isinstance(version, str) else "",
        state=dict(state) if isinstance(state, Mapping) else {},
        current_node_id=current if isinstance(current, str) and current else None,
        pending_branch=pending if isinstance(pending, str) and pending else None,
    )


def resume_start_node(checkpoint: Checkpoint, *, fallback_start_node_id: str) -> tuple[str, bool]:
    """`resume` 的起始节点：返回 `(node_id, restarted)`。

    - checkpoint 与当前引擎兼容且有 `current_node_id` → 从该节点续跑（4.5.4 的两类场景）；
    - 不兼容（SD-10）或没有断点 → 从 `start` 重跑，`restarted=True`（调用方记 warning）。
    """
    if checkpoint.is_compatible() and checkpoint.current_node_id:
        return checkpoint.current_node_id, False
    return fallback_start_node_id, True
