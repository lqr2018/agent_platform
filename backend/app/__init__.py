"""Agent Platform 后端（FastAPI + SQLite + Chroma）。

分层与依赖方向（不可逆，详细设计 1.2）：

    api  →  services  →  db/models
                     ↘  runtime/*  →  runtime/observability
"""

from __future__ import annotations

__version__ = "0.1.0"
APP_NAME = "agent-platform"
