"""ORM 模型包（详细设计 1.1 / 2.14）。

`alembic/env.py` 通过 `from app.db import models` 让 autogenerate 看到全部表；
各 Phase 只导入本阶段存在的模型模块（Backlog 的 `memory.py` / `mcp.py` / `eval.py` /
`approval.py` **不存在**，SD-14②）。
"""

from __future__ import annotations

from app.db.models.agent import Agent, AgentPromptVersion
from app.db.models.conversation import Conversation, Message, Run
from app.db.models.llm import ModelProvider
from app.db.models.tool import Tool, ToolInvocation
from app.db.models.trace import Span, Trace

__all__ = [
    "Agent",
    "AgentPromptVersion",
    "Conversation",
    "Message",
    "ModelProvider",
    "Run",
    "Span",
    "Tool",
    "ToolInvocation",
    "Trace",
]
