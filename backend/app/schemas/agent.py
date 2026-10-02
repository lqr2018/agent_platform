"""Agent 的 DTO（详细设计 3.2.2 / 2.4 / 3.3.1）。

`memory_config` 用 Pydantic 强校验（2.4）：

- `short_term.strategy` 在 MVP 只接受 `window`（`summary` / `window+summary` → 迭代 A，SD-15）；
- `long_term` 字段保留但**不生效**（可以传，运行时不读；README 的"已知限制"里如实写明）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import UtcDatetime

AgentStatus = Literal["enabled", "disabled"]


class ShortTermConfigDTO(BaseModel):
    """`memory_config.short_term`（2.4）。"""

    strategy: Literal["window"] = "window"
    max_turns: int = Field(default=12, ge=1, le=100)
    max_tokens: int = Field(default=6000, ge=256, le=200_000)
    summarize_threshold_tokens: int = Field(default=4000, ge=256)


class LongTermConfigDTO(BaseModel):
    """`memory_config.long_term`：**MVP 不生效**（SD-15），字段保留以便迭代 A 不改数据。"""

    enabled: bool = False
    scope: Literal["session", "agent", "user"] = "agent"
    retrieve_top_k: int = Field(default=5, ge=1, le=50)
    min_score: float = Field(default=0.35, ge=0.0, le=1.0)
    write_policy: Literal["off", "explicit", "auto"] = "auto"
    ttl_seconds: int | None = Field(default=None, ge=1)


class MemoryConfigDTO(BaseModel):
    short_term: ShortTermConfigDTO = Field(default_factory=ShortTermConfigDTO)
    long_term: LongTermConfigDTO = Field(default_factory=LongTermConfigDTO)


class AgentCreate(BaseModel):
    """3.3.1 的请求体（`tool_ids` / `knowledge_base_ids` / `workflow_id` 在 Phase 1 必须为空）。"""

    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    model_provider_id: str = Field(min_length=26, max_length=26)
    model_name: str = Field(min_length=1, max_length=120)
    model_params: dict[str, object] = Field(default_factory=dict)
    system_prompt: str = ""
    tool_ids: list[str] = Field(default_factory=list)
    knowledge_base_ids: list[str] = Field(default_factory=list)
    memory_config: MemoryConfigDTO = Field(default_factory=MemoryConfigDTO)
    workflow_id: str | None = None
    max_steps: int = Field(default=8, ge=1, le=50)
    timeout_seconds: int = Field(default=180, ge=1, le=3600)
    tags: list[str] = Field(default_factory=list)
    is_template: bool = False
    status: AgentStatus = "enabled"


class AgentUpdate(BaseModel):
    """`PATCH`：只传需要改的字段；`system_prompt` 变化会写 `agent_prompt_versions`（2.4）。"""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    model_provider_id: str | None = Field(default=None, min_length=26, max_length=26)
    model_name: str | None = Field(default=None, min_length=1, max_length=120)
    model_params: dict[str, object] | None = None
    system_prompt: str | None = None
    memory_config: MemoryConfigDTO | None = None
    max_steps: int | None = Field(default=None, ge=1, le=50)
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    tags: list[str] | None = None
    status: AgentStatus | None = None
    prompt_note: str | None = Field(default=None, max_length=200)


class AgentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str = ""
    status: str = "enabled"
    model_provider_id: str
    model_name: str
    model_params: dict[str, object] = Field(default_factory=dict)
    system_prompt: str = ""
    prompt_version: int = 1
    tool_ids: list[str] = Field(default_factory=list)
    knowledge_base_ids: list[str] = Field(default_factory=list)
    memory_config: dict[str, object] = Field(default_factory=dict)
    workflow_id: str | None = None
    max_steps: int = 8
    timeout_seconds: int = 180
    tags: list[str] = Field(default_factory=list)
    is_template: bool = False
    created_at: UtcDatetime
    updated_at: UtcDatetime
    deleted_at: UtcDatetime | None = None


class AgentCloneRequest(BaseModel):
    """`POST /agents/{id}/clone`（3.2.2）。"""

    name: str = Field(min_length=1, max_length=120)
    description: str | None = None


class AgentPromptVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    version: int
    system_prompt: str
    note: str = ""
    created_at: UtcDatetime
