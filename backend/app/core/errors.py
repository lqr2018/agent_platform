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
