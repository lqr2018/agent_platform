"""`configs/agents/*.yaml` 的一致性与可用性单测（详细设计 7.9 交付物 / 8.1 / 8.5）。

Phase 8 的三大 Demo 有一条硬约束：**类型差异只能写在 YAML 里**（8.5 的"零专属代码"）。
因此这份单测钉三件事：

1. 三份文件都能被 `agent_config` 装载（`extra="forbid"`：字段名写错立刻报错，不被静默忽略）；
2. YAML 里引用的名字**真实存在**：`tools` ⊆ 内置工具清单、`workflow` ⊆ `configs/workflows`
   （`knowledge_bases` 由 `seed_demo` 按 name 建，这里只断言取值稳定）；
3. 三类 Demo 的差别确实只是"配置字段的组合"：研究型绑图、数据型开危险工具且**不绑图**、
   知识库问答型绑 KB 且**不绑图**（8.2 / 8.3 / 8.4 的口径）。

loader 的失败路径（未知字段 / 保留字段 `strategy` / 重名 / 目录缺失）见文件末尾的用例。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.runtime.tools.registry import builtin_definitions
from app.services import agent_config, workflow_config

EXPECTED_NAMES = ["Data Analysis Agent", "Knowledge Assistant", "Research Agent"]
"""7.9 交付物的三份 Agent 配置（按文件名排序后的 `name` 序列）。"""

MINIMAL_AGENT = """
version: 1
agent:
  name: sample-agent
  model: { provider: sample-provider, name: sample-model }
"""


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_shipped_configs_load_and_validate() -> None:
    configs = agent_config.load_agent_configs()
    assert agent_config.config_names(configs) == EXPECTED_NAMES
    for config in configs:
        assert config.version == 1
        assert config.agent.model.provider and config.agent.model.name
        assert config.agent.tools, "每个 Demo 都至少要有一个工具（8.2 / 8.3 / 8.4）"
        assert len(set(config.agent.tools)) == len(config.agent.tools), "工具不应重复"


def test_shipped_configs_reference_existing_tools() -> None:
    """`tools` 必须是内置清单里的名字：写错就是"演示点不亮"，不该等到运行时才发现。"""
    known = {definition.name for definition in builtin_definitions()}
    for config in agent_config.load_agent_configs():
        assert set(config.agent.tools) <= known, f"{config.agent.name}: {sorted(set(config.agent.tools) - known)}"


def test_shipped_configs_reference_existing_workflows() -> None:
    """`workflow` 必须是 `configs/workflows/*.yaml` 里的名字（`seed_demo` 按 name 解析）。"""
    names = workflow_config.config_names(workflow_config.load_workflow_configs())
    for config in agent_config.load_agent_configs():
        if config.agent.workflow is not None:
            assert config.agent.workflow in names, config.agent.name


def test_demo_types_differ_only_in_config() -> None:
    """8.5 的可验证表述：三类 Demo 的差别是"配置字段的组合"，不是代码分支。"""
    by_name = {config.agent.name: config.agent for config in agent_config.load_agent_configs()}
    research = by_name["Research Agent"]
    data = by_name["Data Analysis Agent"]
    knowledge = by_name["Knowledge Assistant"]

    assert research.workflow == "research-flow" and research.knowledge_bases
    assert data.workflow is None and {"python_execute", "file_write"} <= set(data.tools)
    assert knowledge.workflow is None and knowledge.knowledge_bases == ["product-docs"]

    # 三条 system_prompt 各自表达自己的"行为边界"（引用编号 / 危险工具受限 / 未命中拒答）
    assert "[1]" in research.system_prompt
    assert "TOOL_PERMISSION_DENIED" in data.system_prompt
    assert "不要编造" in knowledge.system_prompt


def test_load_agent_config_rejects_unknown_field(tmp_path: Path) -> None:
    path = _write(tmp_path / "unknown-field.yaml", MINIMAL_AGENT + "  unknown: true\n")
    with pytest.raises(ValidationError):
        agent_config.load_agent_config(path)


def test_load_agent_config_rejects_unsupported_memory_strategy(tmp_path: Path) -> None:
    """SD-15：`strategy` 只接受 `window`，`summary` 必须在**装载期**被拒（不留"运行时才炸"的配置）。"""
    body = MINIMAL_AGENT + "  memory:\n    short_term: { strategy: summary }\n"
    with pytest.raises(ValidationError):
        agent_config.load_agent_config(_write(tmp_path / "bad-memory.yaml", body))


def test_load_agent_config_rejects_missing_agent_section(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        agent_config.load_agent_config(_write(tmp_path / "no-body.yaml", "version: 1\n"))


def test_load_agent_configs_on_missing_directory_returns_empty(tmp_path: Path) -> None:
    assert agent_config.load_agent_configs(tmp_path / "missing") == []


def test_duplicate_agent_names_are_rejected(tmp_path: Path) -> None:
    """同名 = 写库时的幂等键冲突：装载期就报错（否则后一份会悄悄覆盖前一份）。"""
    _write(tmp_path / "a.yaml", MINIMAL_AGENT)
    _write(tmp_path / "b.yaml", MINIMAL_AGENT)
    configs = agent_config.load_agent_configs(tmp_path)
    with pytest.raises(ValueError, match="Duplicate agent name"):
        agent_config.config_names(configs)
