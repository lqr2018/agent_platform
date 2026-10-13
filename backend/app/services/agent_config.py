"""`configs/agents/*.yaml` 的读取与结构校验（详细设计 7.9 交付物 / 8.1）。

Phase 8 的范围：**读取 + 结构校验**（`extra="forbid"`，写错字段名立刻报错，而不是被静默忽略）。
文件里的 `model.provider` / `tools[]` / `knowledge_bases[]` / `workflow` 都是**按 name 引用**，
解析成真实 id 由 `app/scripts/seed_demo.py` 在写库时完成 —— 引用合法性则复用
`agent_service.create_agent` / `upsert_agent` 的服务层校验（工具必须 enabled、KB 必须未软删、
Workflow 必须 published），不在这里重复实现。

文件 schema（`AgentConfigFile`，8.1）：

```yaml
version: 1
agent:
  name: "Research Agent"
  model: { provider: "default-openai-compatible", name: "qwen-plus", params: { temperature: 0.3 } }
  tools: ["web_search", "calculator"]
  knowledge_bases: ["product-docs"]
  memory: { short_term: { strategy: window, max_turns: 12 }, long_term: { enabled: false } }
  workflow: "research-flow"
```
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs" / "agents"
"""仓库内默认目录（`backend/configs/agents`，7.9 的交付物）。"""

CONFIG_SUFFIXES = (".yaml", ".yml")


class AgentModelConfigFile(BaseModel):
    """`agent.model`（8.1）：`provider` 按 `model_providers.name` 引用。"""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    params: dict[str, Any] = Field(default_factory=dict)


class AgentShortTermConfigFile(BaseModel):
    """`agent.memory.short_term`（2.4 的可配置子集；`strategy` 只接受 `window`，SD-15）。"""

    model_config = ConfigDict(extra="forbid")

    strategy: Literal["window"] = "window"
    max_turns: int = Field(default=12, ge=1, le=100)
    max_tokens: int = Field(default=6000, ge=256, le=200_000)
    summarize_threshold_tokens: int = Field(default=4000, ge=256)


class AgentLongTermConfigFile(BaseModel):
    """`agent.memory.long_term`：MVP 不生效（SD-15），字段保留以便迭代 A 不改 YAML。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


class AgentMemoryConfigFile(BaseModel):
    """`agent.memory`（落库形态见 2.4 的 `memory_config`）。"""

    model_config = ConfigDict(extra="forbid")

    short_term: AgentShortTermConfigFile = Field(default_factory=AgentShortTermConfigFile)
    long_term: AgentLongTermConfigFile = Field(default_factory=AgentLongTermConfigFile)


class AgentConfigBody(BaseModel):
    """`agent:` 段（8.1 的字段全集；写库时翻译成 `AgentCreate`）。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4_000)
    is_template: bool = False
    tags: list[str] = Field(default_factory=list)
    model: AgentModelConfigFile
    system_prompt: str = Field(default="", max_length=20_000)
    tools: list[str] = Field(default_factory=list)
    knowledge_bases: list[str] = Field(default_factory=list)
    memory: AgentMemoryConfigFile = Field(default_factory=AgentMemoryConfigFile)
    workflow: str | None = None
    max_steps: int = Field(default=8, ge=1, le=50)
    timeout_seconds: int = Field(default=180, ge=1, le=3600)
    status: Literal["enabled", "disabled"] = "enabled"

    @field_validator("name", "workflow", mode="before")
    @classmethod
    def _strip_names(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class AgentConfigFile(BaseModel):
    """一个 YAML 文件 = 一个 Agent（`version` 预留给后续格式演进）。"""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    agent: AgentConfigBody


def load_agent_config(path: Path) -> AgentConfigFile:
    """读取单个文件；schema 不合法时抛 `pydantic.ValidationError`（不吞错）。"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return AgentConfigFile.model_validate(raw)


def list_agent_config_paths(directory: Path | None = None) -> list[Path]:
    """目录内的配置文件（按文件名排序，便于测试与幂等装载的稳定性）。"""
    base = directory or CONFIG_DIR
    if not base.is_dir():
        return []
    return sorted(path for path in base.iterdir() if path.suffix in CONFIG_SUFFIXES)


def load_agent_configs(directory: Path | None = None) -> list[AgentConfigFile]:
    """装载目录内的全部 Agent 配置（空目录 → 空列表）。"""
    return [load_agent_config(path) for path in list_agent_config_paths(directory)]


def config_names(configs: Sequence[AgentConfigFile]) -> list[str]:
    """便捷取值（`seed_demo` 与测试都要用 `name` 做幂等键）；重复 name 直接报错。"""
    names = [config.agent.name for config in configs]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"Duplicate agent name(s) in configs: {', '.join(duplicates)}")
    return names
