"""/api/v1 路由汇总（详细设计 1.1 / 3.2）。

随 Phase 增长：`agents.py` / `model_providers.py` / `conversations.py` / `chat.py` / `runs.py` /
`traces.py`（Phase 1）、`tools.py`（Phase 2）、`workflows.py` + `runs.workflow_router`（Phase 3）…
Backlog 能力（审批 / MCP / 评测台 / 记忆）**不注册路由**（SD-14②）。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    agents,
    chat,
    conversations,
    health,
    knowledge_bases,
    model_providers,
    runs,
    tools,
    traces,
    workflows,
)

api_router = APIRouter()
api_router.include_router(health.meta_router)
api_router.include_router(model_providers.router)
api_router.include_router(agents.router)
api_router.include_router(conversations.router)
api_router.include_router(chat.router)
api_router.include_router(runs.router)
api_router.include_router(runs.workflow_router)
api_router.include_router(traces.router)
api_router.include_router(tools.router)
api_router.include_router(workflows.router)
api_router.include_router(knowledge_bases.router)
api_router.include_router(knowledge_bases.maintenance_router)

__all__ = ["api_router"]
