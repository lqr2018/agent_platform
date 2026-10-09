"""统一错误模型（详细设计 1.6 + 附录 A）。

- 所有业务异常继承 `AppError`（`code` / `http_status` / `message` / `details`）；
- `ErrorCode` 只登记**本阶段及之前**会抛出的错误码（附录 A 的"阶段"列）；
  后续 Phase 的错误码在实现对应 Phase 时追加（附录 A 的 Backlog 码只登记、不提供触发路径，SD-14②）。
- HTTP 响应体由 `app/main.py` 的异常处理器组装为 `{"error": {...}, "meta": {...}}`。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """错误码（附录 A 中"Phase 0–1 生效"的部分）。

    后续 Phase 的错误码在实现对应 Phase 时追加；Backlog（`MCP_*` / `EVAL_*` / `APPROVAL_*` /
    `MEMORY_*`）**不登记**（SD-14②：不为未实现的能力留枚举）。
    """

    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    CONFIG_INVALID = "CONFIG_INVALID"

    # ---- 模型 Provider（4.1 / 4.1.2，Phase 1） ----
    MODEL_PROVIDER_NOT_FOUND = "MODEL_PROVIDER_NOT_FOUND"
    MODEL_PROVIDER_UNAVAILABLE = "MODEL_PROVIDER_UNAVAILABLE"
    MODEL_AUTH_FAILED = "MODEL_AUTH_FAILED"
    MODEL_BAD_REQUEST = "MODEL_BAD_REQUEST"
    MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_CONTEXT_OVERFLOW = "MODEL_CONTEXT_OVERFLOW"
    MODEL_MAX_STEPS_EXCEEDED = "MODEL_MAX_STEPS_EXCEEDED"

    # ---- Agent（2.4 / 4.4，Phase 1） ----
    AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
    AGENT_DISABLED = "AGENT_DISABLED"
    AGENT_INVALID_CONFIG = "AGENT_INVALID_CONFIG"

    # ---- 会话与 Run（2.6 / 4.4.3，Phase 1） ----
    CONVERSATION_NOT_FOUND = "CONVERSATION_NOT_FOUND"
    MESSAGE_NOT_FOUND = "MESSAGE_NOT_FOUND"
    RUN_NOT_FOUND = "RUN_NOT_FOUND"
    RUN_ALREADY_FINISHED = "RUN_ALREADY_FINISHED"
    RUN_CANCELED = "RUN_CANCELED"
    RUN_TIMEOUT = "RUN_TIMEOUT"

    # ---- 工具（2.5 / 4.2.3，Phase 2） ----
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_DISABLED = "TOOL_DISABLED"
    TOOL_INVALID_ARGUMENTS = "TOOL_INVALID_ARGUMENTS"
    TOOL_PERMISSION_DENIED = "TOOL_PERMISSION_DENIED"
    TOOL_SANDBOX_VIOLATION = "TOOL_SANDBOX_VIOLATION"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"
    TOOL_OUTPUT_TOO_LARGE = "TOOL_OUTPUT_TOO_LARGE"
    TOOL_REPEATED_FAILURE = "TOOL_REPEATED_FAILURE"

    # ---- Workflow（2.9 / 4.5，Phase 3） ----
    WORKFLOW_NOT_FOUND = "WORKFLOW_NOT_FOUND"
    WORKFLOW_INVALID_GRAPH = "WORKFLOW_INVALID_GRAPH"
    WORKFLOW_RUN_NOT_FOUND = "WORKFLOW_RUN_NOT_FOUND"
    WORKFLOW_RUN_NOT_RESUMABLE = "WORKFLOW_RUN_NOT_RESUMABLE"
    WORKFLOW_NODE_FAILED = "WORKFLOW_NODE_FAILED"
    WORKFLOW_MAX_STEPS_EXCEEDED = "WORKFLOW_MAX_STEPS_EXCEEDED"
    RUN_ABANDONED = "RUN_ABANDONED"
    """6.3 第 3 条：进程重启后收敛的孤儿 Run / WorkflowRun（附录 A 的阶段列为 3）。"""


class AppError(Exception):
    """所有可预期错误的基类（1.6）。

    `details` 只存放安全、可展示的信息（不塞堆栈、不塞密钥）。
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    http_status: int = 500
    message: str = "Internal error"

    def __init__(self, message: str | None = None, *, details: Mapping[str, Any] | None = None) -> None:
        self.message = message or type(self).message
        self.details: dict[str, Any] = dict(details or {})
        super().__init__(self.message)

    def to_body(self) -> dict[str, Any]:
        """`error` 字段的内容（不含 `meta`）。"""
        return {"code": str(self.code), "message": self.message, "details": self.details}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!s}, http_status={self.http_status}, message={self.message!r})"


class ValidationError(AppError):
    """参数 / 请求体不合法（422）。`details.errors[]` 为字段级明细。"""

    code = ErrorCode.VALIDATION_ERROR
    http_status = 422
    message = "Request validation failed"


class NotFoundError(AppError):
    """资源不存在（404）。具体资源在 Phase 1 起用子类（`AGENT_NOT_FOUND` 等）。"""

    code = ErrorCode.NOT_FOUND
    http_status = 404
    message = "Resource not found"


class ConflictError(AppError):
    """唯一约束 / 被引用 / 活跃 Run 冲突（409）。"""

    code = ErrorCode.CONFLICT
    http_status = 409
    message = "Resource conflict"


class RateLimitedError(AppError):
    """平台侧限流（429，见 1.5.4）。"""

    code = ErrorCode.RATE_LIMITED
    http_status = 429
    message = "Too many requests"


class FeatureNotImplementedError(AppError):
    """能力未启用或可选依赖缺失（501）。

    名字不用 `NotImplementedError`，避免与内置异常混淆；错误码仍为 `NOT_IMPLEMENTED`。
    """

    code = ErrorCode.NOT_IMPLEMENTED
    http_status = 501
    message = "Feature not implemented"


class ConfigInvalidError(AppError):
    """配置非法（500，如 prod 下 `SECRET_KEY` 未覆盖，见 6.2）。"""

    code = ErrorCode.CONFIG_INVALID
    http_status = 500
    message = "Invalid configuration"


class InternalError(AppError):
    """未预期异常（500）。响应体不带堆栈，只带 `meta.request_id` 供排查。"""

    code = ErrorCode.INTERNAL_ERROR
    http_status = 500
    message = "Internal error"


class DatabaseBusyError(AppError):
    """SQLite 写锁竞争（503）：请求**可以稍后重试**，不该被当成 500 内部错误。

    错误码沿用 `INTERNAL_ERROR` —— 附录 A 没有 503 专用码，且 SD-14② 不为未实现的能力扩枚举。
    `app/main.py` 的 `OperationalError` 处理器只在驱动原文含 `locked` / `busy` 时用它，
    其余 `OperationalError` 仍按 500 处理（避免把真正的实现缺陷伪装成"重试就好"）。
    """

    code = ErrorCode.INTERNAL_ERROR
    http_status = 503
    message = "Database is busy; please retry"


# ---- Phase 1：模型 Provider（附录 A / 4.1.2） ----
class ModelProviderNotFoundError(NotFoundError):
    """provider id 不存在（404）。"""

    code = ErrorCode.MODEL_PROVIDER_NOT_FOUND
    message = "Model provider not found"


class ModelProviderUnavailableError(AppError):
    """连接失败 / 无法建立会话（502，4.1.2）。"""

    code = ErrorCode.MODEL_PROVIDER_UNAVAILABLE
    http_status = 502
    message = "Model provider is unreachable"


class ModelAuthFailedError(AppError):
    """上游 401 / 403（Key 无效或无权限，502）。"""

    code = ErrorCode.MODEL_AUTH_FAILED
    http_status = 502
    message = "Model provider rejected the credentials"


class ModelBadRequestError(AppError):
    """上游 400（参数 / 模型名不支持，502）。"""

    code = ErrorCode.MODEL_BAD_REQUEST
    http_status = 502
    message = "Model provider rejected the request"


class ModelRateLimitedError(RateLimitedError):
    """上游 429（重试耗尽后，429）。"""

    code = ErrorCode.MODEL_RATE_LIMITED
    message = "Model provider rate limit exceeded"


class ModelTimeoutError(AppError):
    """上游超时（504）。"""

    code = ErrorCode.MODEL_TIMEOUT
    http_status = 504
    message = "Model provider timed out"


class ModelContextOverflowError(ValidationError):
    """上下文超模型窗口（422）。"""

    code = ErrorCode.MODEL_CONTEXT_OVERFLOW
    message = "Context exceeds the model window"


class ModelMaxStepsExceededError(AppError):
    """达到 `agent.max_steps`（500，4.4.3）。"""

    code = ErrorCode.MODEL_MAX_STEPS_EXCEEDED
    http_status = 500
    message = "Agent reached max_steps without a final answer"


# ---- Phase 1：Agent（2.4 / 4.4） ----
class AgentNotFoundError(NotFoundError):
    code = ErrorCode.AGENT_NOT_FOUND
    message = "Agent not found"


class AgentDisabledError(ConflictError):
    """Agent 被禁用后仍发起 Run（409）。"""

    code = ErrorCode.AGENT_DISABLED
    message = "Agent is disabled"


class AgentInvalidConfigError(ValidationError):
    """引用资源不存在或模型不在白名单（422）。"""

    code = ErrorCode.AGENT_INVALID_CONFIG
    message = "Agent configuration is invalid"


# ---- Phase 1：会话与 Run（2.6 / 4.4.3） ----
class ConversationNotFoundError(NotFoundError):
    code = ErrorCode.CONVERSATION_NOT_FOUND
    message = "Conversation not found"


class MessageNotFoundError(NotFoundError):
    code = ErrorCode.MESSAGE_NOT_FOUND
    message = "Message not found"


class RunNotFoundError(NotFoundError):
    code = ErrorCode.RUN_NOT_FOUND
    message = "Run not found"


class RunAlreadyFinishedError(ConflictError):
    """对终态 Run 执行 cancel 等操作（409，2.6）。"""

    code = ErrorCode.RUN_ALREADY_FINISHED
    message = "Run has already finished"


class RunCanceledError(AppError):
    """用户取消（499；同时作为 Run 的 `error_code`，2.6）。"""

    code = ErrorCode.RUN_CANCELED
    http_status = 499
    message = "Run was canceled"


class RunTimeoutError(AppError):
    """超过 `agent.timeout_seconds`（500，4.4.3）。"""

    code = ErrorCode.RUN_TIMEOUT
    http_status = 500
    message = "Run exceeded the configured timeout"


# ---- Phase 2：工具（2.5 / 4.2.3） ----
class ToolNotFoundError(NotFoundError):
    """工具不存在（404，4.2.3 步骤 1）。"""

    code = ErrorCode.TOOL_NOT_FOUND
    message = "Tool not found"


class ToolDisabledError(ConflictError):
    """工具被禁用 / 无外部 Key 而不可用（409，2.5）。"""

    code = ErrorCode.TOOL_DISABLED
    message = "Tool is disabled"


class ToolInvalidArgumentsError(ValidationError):
    """JSON Schema 校验失败（422，4.2.3 步骤 3；错误回填给 LLM，不中断 Run）。"""

    code = ErrorCode.TOOL_INVALID_ARGUMENTS
    message = "Tool arguments failed schema validation"


class ToolPermissionDeniedError(AppError):
    """权限不足 / 需审批但未开启 / 超次数（403，4.2.3 步骤 4/5，SD-17）。"""

    code = ErrorCode.TOOL_PERMISSION_DENIED
    http_status = 403
    message = "Tool call was denied by policy"


class ToolSandboxViolationError(AppError):
    """路径穿越 / 内网 / 非白名单 host（403，4.2.4）。"""

    code = ErrorCode.TOOL_SANDBOX_VIOLATION
    http_status = 403
    message = "Tool call violated sandbox rules"


class ToolTimeoutError(AppError):
    """工具执行超时（504，4.2.3 步骤 6）。"""

    code = ErrorCode.TOOL_TIMEOUT
    http_status = 504
    message = "Tool execution timed out"


class ToolExecutionFailedError(AppError):
    """工具内部异常（500；作为错误结果回填给 LLM，不中断 Run，4.2.3 步骤 7）。"""

    code = ErrorCode.TOOL_EXECUTION_FAILED
    message = "Tool execution failed"


class ToolOutputTooLargeError(AppError):
    """输出被截断（附录 A：HTTP 200 —— 属"正常结果 + `truncated` 标记"，不是失败）。"""

    code = ErrorCode.TOOL_OUTPUT_TOO_LARGE
    http_status = 200
    message = "Tool output was truncated"


class ToolRepeatedFailureError(AppError):
    """同一工具连续失败 3 次 → Run 失败（4.2.3 的错误处理原则）。"""

    code = ErrorCode.TOOL_REPEATED_FAILURE
    message = "The same tool failed repeatedly"


# ---- Phase 3：Workflow（2.9 / 4.5） ----
class WorkflowNotFoundError(NotFoundError):
    """Workflow 不存在（404，附录 A）。"""

    code = ErrorCode.WORKFLOW_NOT_FOUND
    message = "Workflow not found"


class WorkflowInvalidGraphError(ValidationError):
    """图静态校验失败（422，4.5.1）。

    `details["errors"]` 是**错误项清单**（每项 `{code, message, node_id?}`）——
    并行出边的单项 `code=PARALLEL_EDGES_NOT_SUPPORTED`（SD-1）；
    `details["start_node_id"]` 在校验通过时也存在，方便前端直接渲染图。
    """

    code = ErrorCode.WORKFLOW_INVALID_GRAPH
    message = "Workflow definition is not a valid graph"


class WorkflowRunNotFoundError(NotFoundError):
    """WorkflowRun 不存在（404，附录 A）。"""

    code = ErrorCode.WORKFLOW_RUN_NOT_FOUND
    message = "Workflow run not found"


class WorkflowRunNotResumableError(ConflictError):
    """不在 4.5.4 的两类可续跑场景内（409，附录 A）。"""

    code = ErrorCode.WORKFLOW_RUN_NOT_RESUMABLE
    message = "Workflow run is not resumable"


class WorkflowNodeFailedError(AppError):
    """节点失败且 `on_error=fail`（500，4.5.2）。"""

    code = ErrorCode.WORKFLOW_NODE_FAILED
    http_status = 500
    message = "Workflow node failed"


class WorkflowMaxStepsExceededError(AppError):
    """超 `config.max_steps` / `config.recursion_limit`（500，4.5.2）。"""

    code = ErrorCode.WORKFLOW_MAX_STEPS_EXCEEDED
    http_status = 500
    message = "Workflow exceeded the configured step limit"


class WorkflowNodeError(AppError):
    """节点内部失败，**保留原始错误码**上抛给引擎按 `on_error` 处理（4.5.2）。

    例：`agent` 节点里 LLM 超时 → `MODEL_TIMEOUT`；`tool` 节点里工具超时 → `TOOL_TIMEOUT`。
    与 `WorkflowNodeFailedError`（引擎级的"节点失败"）的分工：

    - `WorkflowNodeError`：**服务层**把内部结果（Agent Run 失败 / 工具失败）转成异常时用；
    - `WorkflowNodeFailedError`：引擎发现"节点无法执行"（未注入 runner / 模板非法 / 分支未命中）时用。
    两者都会让 `node_runs.error_code` 记录真实原因。
    """

    http_status = 500

    def __init__(
        self, code: ErrorCode, message: str | None = None, *, details: Mapping[str, Any] | None = None
    ) -> None:
        self.code = code
        super().__init__(message, details=details)


HTTP_STATUS_TO_CODE: Mapping[int, ErrorCode] = {
    400: ErrorCode.VALIDATION_ERROR,
    404: ErrorCode.NOT_FOUND,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.VALIDATION_ERROR,
    429: ErrorCode.RATE_LIMITED,
    500: ErrorCode.INTERNAL_ERROR,
    501: ErrorCode.NOT_IMPLEMENTED,
    # 附录 A 未定义 405：保留原 HTTP 状态码，错误码暂用 NOT_IMPLEMENTED（Phase 3 路由表稳定后复核）
    405: ErrorCode.NOT_IMPLEMENTED,
}
"""Starlette `HTTPException` / 未注册路径 → 本平台错误码的映射（1.6）。"""
