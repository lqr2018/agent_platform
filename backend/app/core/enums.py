"""基线枚举（一致性契约 0.2.2，全文只引用、不重定义）。

**A 段（MVP 生效）** 在这里定义；**B 段（Backlog，SD-15～SD-18）不解冻**：
`MemoryKind` / `MemoryScope` / `ApprovalStatus` 不定义，`SpanType.memory`、`SpanType.mcp`、
`ToolType.mcp`、`NodeType.human`、`PermissionDecision.approval` 等取值一律不出现。

代码落点约定：`db/models/**` 与 `runtime/**` 只 import 本模块，不各自重定义取值。

一致性由 `tests/unit/test_enums_contract.py` 断言（解析《详细设计》0.2.2 的 A/B 两段文本）。
"""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    """Run / WorkflowRun / NodeRun 的状态（2.6 / 2.9）。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class RunKind(StrEnum):
    """`traces.kind`：一次执行实例的来源。"""

    CHAT = "chat"
    WORKFLOW = "workflow"
    EVAL = "eval"


class SpanType(StrEnum):
    """Span 类型（2.11）；`memory` / `mcp` 属 Backlog，不在此列出。"""

    RUN = "run"
    AGENT = "agent"
    LLM = "llm"
    TOOL = "tool"
    RETRIEVER = "retriever"
    EMBEDDING = "embedding"
    WORKFLOW = "workflow"
    NODE = "node"
    EVAL = "eval"


class SpanStatus(StrEnum):
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"


class ToolType(StrEnum):
    """`mcp` 属 Backlog（SD-16），不在此列出。"""

    BUILTIN = "builtin"
    API = "api"


class PermissionLevel(StrEnum):
    SAFE = "safe"
    GUARDED = "guarded"
    DANGEROUS = "dangerous"


class PermissionDecision(StrEnum):
    """`approval`（挂起等审批）属 Backlog（SD-17）。"""

    ALLOW = "allow"
    DENY = "deny"


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class NodeType(StrEnum):
    """Workflow 节点类型（4.5.2）；`human` 属 Backlog（SD-17）。"""

    START = "start"
    AGENT = "agent"
    TOOL = "tool"
    RETRIEVER = "retriever"
    CONDITION = "condition"
    END = "end"


class DocumentStatus(StrEnum):
    """文档摄取状态机（4.6.2）。"""

    PENDING = "pending"
    PARSING = "parsing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    READY = "ready"
    FAILED = "failed"


class WorkflowStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class ProviderKind(StrEnum):
    """LLM Provider 类型（4.1.2）。"""

    OPENAI_COMPATIBLE = "openai_compatible"


class EvalExpectType(StrEnum):
    """评测断言的 `expect.type`（4.9.3，MVP 由脚本读取，不落库）。"""

    CONTAINS = "contains"
    REGEX = "regex"
    EQUALS = "equals"
    JSON_PATH = "json_path"
    TOOL_SEQUENCE = "tool_sequence"
    MAX_LATENCY = "max_latency"
    LLM_JUDGE = "llm_judge"


class EvalOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
