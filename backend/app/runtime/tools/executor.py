"""Tool Executor：4.2.3 的九步流水线。

九步**顺序固定**，每一步都有 `tests/unit/test_tool_executor.py` 的分支用例：

```text
1. 查表 → 2. 合并策略 → 3. 参数校验 → 4. 权限判定 → 5. 次数闸门
→ 6. 执行（builtin / api + 限流 + 超时）→ 7. 输出处理
→ 8. 落库（tool_invocations + span）→ 9. 返回 ToolInvocationResult
```

三条贯穿原则：

- **失败不中断 Run**（4.2.3 的错误处理原则）：除 `RunCanceledError` 外，所有失败都转成
  `{"error": ..., "message": ...}` 作为正常结果回填，让模型自行修正或换工具；
- **不静默失败**：每次调用（含被拒绝的）都写一行 `tool_invocations` 与一个 `tool` span
  （4.2.4 审计）；
- **runtime 不 import db / services**（1.2）：落库经注入的 `ToolInvocationSink`，
  由服务层实现 ORM 写入，测试用 `ListInvocationSink`。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

import httpx

from app.core import ids
from app.core.config import Settings
from app.core.enums import PermissionDecision, RunStatus, SpanType
from app.core.errors import (
    AppError,
    ErrorCode,
    RunCanceledError,
    ToolDisabledError,
    ToolExecutionFailedError,
    ToolInvalidArgumentsError,
    ToolNotFoundError,
    ToolOutputTooLargeError,
    ToolTimeoutError,
)
from app.core.events import (
    SseEventType,
    ToolCallCompletedPayload,
    ToolCallFailedPayload,
    ToolCallStartedPayload,
)
from app.core.logging import get_logger
from app.core.ratelimit import TokenBucketLimiter
from app.runtime.agent.emitter import EventEmitter, NullEmitter
from app.runtime.llm.base import ToolCallSpec
from app.runtime.observability.tracer import Span, Tracer
from app.runtime.tools import permissions, sandbox
from app.runtime.tools.base import (
    TOOL_STATUS_ENABLED,
    TOOL_TYPE_MCP,
    BaseTool,
    ToolContext,
    ToolDefinition,
    ToolInvocationResult,
    ToolPermissionConfig,
    ToolResult,
    normalize_arguments,
    schema_digest,
    validate_arguments,
)
from app.runtime.tools.registry import ToolRegistry

logger = get_logger(__name__)

RESULT_PREVIEW_CHARS = 200
"""`tool.call.completed.result_preview` 的字符上限（3.4 事件 7）。"""

API_MAX_RESPONSE_BYTES = 1_048_576
"""`api` 类型工具单次响应体的读取上限（防单页响应打爆内存）。"""

API_RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})


@dataclass(slots=True)
class ToolInvocationRecord:
    """`tool_invocations` 的一行（2.5 的列 ↔ 字段一一对应）。

    由 `ToolExecutor` 组装，`ToolInvocationSink` 负责写入 —— 这样 `runtime/**` 不碰 ORM。
    """

    id: str
    run_id: str
    trace_id: str
    span_id: str
    step_index: int
    call_index: int
    tool_id: str | None
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    normalized_arguments: dict[str, Any] = field(default_factory=dict)
    result: str | None = None
    result_truncated: bool = False
    status: RunStatus = RunStatus.SUCCEEDED
    permission_decision: PermissionDecision = PermissionDecision.ALLOW
    approval_id: str | None = None
    """SD-17：MVP 恒为 `None`（审批属 Backlog 迭代 C）。"""
    attempt: int = 1
    error_code: str | None = None
    error_message: str | None = None
    latency_ms: int = 0

    def as_row(self) -> dict[str, Any]:
        """转成 `tool_invocations` 的列字典（服务层 sink 直接 `insert()`）。"""
        return {
            "id": self.id,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "step_index": self.step_index,
            "call_index": self.call_index,
            "tool_id": self.tool_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "normalized_arguments": self.normalized_arguments,
            "result": self.result,
            "result_truncated": self.result_truncated,
            "status": str(self.status),
            "permission_decision": str(self.permission_decision),
            "approval_id": self.approval_id,
            "attempt": self.attempt,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "latency_ms": self.latency_ms,
        }


class ToolInvocationSink(Protocol):
    """明细落库（4.2.3 步骤 8）；写失败不影响主流程（与 4.8.2 的 SpanSink 同策略）。"""

    async def write_invocation(self, record: ToolInvocationRecord) -> None: ...


class NullInvocationSink:
    """不落库（单测 / `scripts/evaluate.py`）。"""

    async def write_invocation(self, record: ToolInvocationRecord) -> None:
        return None


class ListInvocationSink:
    """把记录收进列表（单元测试断言九步产物）。"""

    def __init__(self) -> None:
        self.records: list[ToolInvocationRecord] = []

    async def write_invocation(self, record: ToolInvocationRecord) -> None:
        self.records.append(record)


@dataclass(slots=True)
class ToolRunContext:
    """一次 Run 内执行工具所需的注入项（4.4.3 的 `tool_calls` 分支提供）。"""

    run_id: str
    definitions: Mapping[str, ToolDefinition]
    """`tools.name` → 定义快照（服务层按 `tools` 表装配）。"""
    settings: Settings
    tracer: Tracer
    emitter: EventEmitter = field(default_factory=NullEmitter)
    cancellation: asyncio.Event = field(default_factory=asyncio.Event)
    sandbox_root: Path = Path("./data/files")
    agent_permission: ToolPermissionConfig | None = None
    """Agent 级 `permission_config`（2.4）；与工具级取更严格者（步骤 2）。"""
    calls_per_tool: dict[str, int] = field(default_factory=dict)
    """本 Run 内每个工具的已调用次数（4.2.3 步骤 5 的闸门依据，跨 step 累积）。"""
    step_index: int = 0
    call_index: int = 0


class ToolExecutor:
    """4.2.3 的 `ToolExecutor`：九步流水线的唯一实现。"""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        settings: Settings,
        limiter: TokenBucketLimiter | None = None,
        sink: ToolInvocationSink | None = None,
        client_factory: Any | None = None,
    ) -> None:
        self._registry = registry
        self._settings = settings
        self._limiter = limiter or TokenBucketLimiter()
        self._sink: ToolInvocationSink = sink or NullInvocationSink()
        self._client_factory = client_factory or _default_client_factory

    async def execute(self, call: ToolCallSpec, *, ctx: ToolRunContext) -> ToolInvocationResult:
        """执行一次工具调用（步骤 1–9）；**除取消外不向外抛错**（4.2.3 错误处理原则）。"""
        arguments = dict(call.arguments or {})
        definition = ctx.definitions.get(call.name)
        normalized: dict[str, Any] = {}
        decision = PermissionDecision.ALLOW
        outcome: ToolResult | None = None
        error: AppError | None = None
        span_id = ""
        started = perf_counter()

        try:
            async with ctx.tracer.span(
                SpanType.TOOL,
                f"tool:{call.name}",
                attributes={
                    "tool_name": call.name,
                    "tool_call_id": call.id,
                    "step_index": ctx.step_index,
                    "call_index": ctx.call_index,
                    "tool_type": definition.tool_type if definition else "unknown",
                },
                input={"arguments": arguments} if arguments else None,
            ) as span:
                span_id = span.span_id
                definition = _require_definition(call.name, ctx)  # 步骤 1
                permission = _merge_permission(definition, ctx)  # 步骤 2
                normalized = _validate_arguments(definition, arguments)  # 步骤 3
                try:
                    decision = _decide(definition, permission, ctx)  # 步骤 4
                    _check_budget(definition, permission, ctx)  # 步骤 5
                except AppError:
                    # 4/5 两步只在被拒时抛错（`TOOL_PERMISSION_DENIED`）；被拒也必须如实记录
                    # `permission_decision=deny`（2.5 的审计列），不能留下 allow 的假象。
                    decision = PermissionDecision.DENY
                    raise
                await _emit_started(ctx, call, normalized)
                outcome = await self._invoke_catching(  # 步骤 6（含限流 / 超时 / 分派）
                    definition, permission, ctx=ctx, span=span, arguments=normalized
                )
                outcome = _finalize(outcome, permission)  # 步骤 7
                span.output = _preview(outcome.as_text())
                span.attributes.update(_output_attributes(outcome))
        except RunCanceledError:
            raise
        except AppError as exc:
            error = exc
        except Exception as exc:  # pragma: no cover - 兜底，避免工具异常打断整轮对话
            logger.error("tool.unexpected_error", tool_name=call.name, error_type=type(exc).__name__, exc_info=True)
            error = ToolExecutionFailedError(
                f"Tool '{call.name}' raised {type(exc).__name__}", details={"tool_name": call.name}
            )

        latency_ms = int((perf_counter() - started) * 1000)
        result = ToolInvocationResult(  # 步骤 9 的返回值（步骤 8 落库后交给 AgentRuntime）
            tool_call_id=call.id,
            tool_name=call.name,
            tool_id=definition.id if definition else None,
            status=RunStatus.SUCCEEDED if error is None else RunStatus.FAILED,
            content=outcome.as_text() if outcome is not None else _error_payload(error),
            is_error=error is not None,
            error_code=str(error.code) if error is not None else None,
            error_message=error.message if error is not None else None,
            permission_decision=decision,
            arguments=arguments,
            normalized_arguments=normalized,
            truncated=bool(outcome.meta.get("truncated")) if outcome is not None else False,
            latency_ms=latency_ms,
            step_index=ctx.step_index,
            call_index=ctx.call_index,
            meta=dict(outcome.meta) if outcome is not None else _failure_meta(error),
        )
        await self._emit_outcome(call, ctx=ctx, result=result)
        await self._write_record(call, ctx=ctx, span_id=span_id, result=result)
        return result

    # ---- 步骤 6：限流 + 超时 + 按 tool_type 分派 ----
    async def _invoke_catching(
        self,
        definition: ToolDefinition,
        permission: ToolPermissionConfig,
        *,
        ctx: ToolRunContext,
        span: Span,
        arguments: dict[str, Any],
    ) -> ToolResult:
        """限流 → `asyncio.wait_for` 超时 → 分派（4.2.3 步骤 6）。"""
        tool_ctx = ToolContext(
            run_id=ctx.run_id,
            trace_span=span,
            sandbox_root=ctx.sandbox_root,
            settings=ctx.settings,
            cancellation=ctx.cancellation,
            allowed_paths=tuple(permission.allowed_paths),
            allowed_hosts=tuple(permission.allowed_hosts),
            allow_network=permission.allow_network,
            max_output_bytes=permission.max_output_bytes,
            timeout_seconds=permission.timeout_seconds,
        )
        async with self._limiter.limit(f"tool:{definition.id}"):
            try:
                return await asyncio.wait_for(
                    self._dispatch(definition, ctx=ctx, tool_ctx=tool_ctx, arguments=arguments),
                    timeout=permission.timeout_seconds,
                )
            except RunCanceledError:
                raise
            except TimeoutError as exc:
                raise ToolTimeoutError(
                    f"Tool '{definition.name}' exceeded {permission.timeout_seconds:g}s",
                    details={"tool_name": definition.name, "timeout_seconds": permission.timeout_seconds},
                ) from exc

    async def _dispatch(
        self, definition: ToolDefinition, *, ctx: ToolRunContext, tool_ctx: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        """4.2.3 步骤 6 的分派点（`mcp` 在迭代 B 接回，SD-16）。"""
        if definition.tool_type == TOOL_TYPE_MCP:
            raise ToolDisabledError(
                f"Tool '{definition.name}' is an MCP tool, which is not supported in this build (SD-16)",
                details={"tool_name": definition.name, "reason": "TOOL_TYPE_NOT_SUPPORTED"},
            )
        if definition.is_builtin:
            return await self._execute_builtin(definition, tool_ctx=tool_ctx, arguments=arguments)
        return await self._execute_api(definition, ctx=ctx, tool_ctx=tool_ctx, arguments=arguments)

    async def _execute_builtin(
        self, definition: ToolDefinition, *, tool_ctx: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        """`builtin` → 源码映射（4.2.2：代码是内置工具定义与实现的单一来源）。"""
        name = definition.builtin_name or definition.name
        tool: BaseTool | None = self._registry.get(name)
        if tool is None:
            raise ToolNotFoundError(
                f"Builtin tool '{name}' is not registered", details={"tool_name": name, "builtin_name": name}
            )
        return await tool.run(tool_ctx, **arguments)

    async def _execute_api(
        self, definition: ToolDefinition, *, ctx: ToolRunContext, tool_ctx: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        """`api` → `http_config` 渲染 + httpx 调用（2.5 的 `http_config` 结构）。"""
        config = dict(definition.http_config or {})
        if not config:
            raise ToolDisabledError(
                f"Tool '{definition.name}' has no http_config", details={"tool_name": definition.name}
            )
        method = str(config.get("method") or "GET").upper()
        url = _render_template(str(config.get("url") or ""), arguments)
        if not url:
            raise ToolInvalidArgumentsError(
                f"Tool '{definition.name}' has an empty URL", details={"tool_name": definition.name}
            )
        sandbox.ensure_network_allowed(
            httpx.URL(url).host or "", allow_network=tool_ctx.allow_network, allowed_hosts=tool_ctx.allowed_hosts
        )

        headers = {
            str(key): _render_template(str(value), arguments)
            for key, value in dict(config.get("headers") or {}).items()
        }
        query = {
            str(key): _render_template(str(value), arguments) for key, value in dict(config.get("query") or {}).items()
        }
        body_template = config.get("body_template")
        body = _render_object(body_template, arguments) if body_template else None
        timeout = float(config.get("timeout_seconds") or tool_ctx.timeout_seconds)
        retry_times = max(0, int(config.get("retry_times") or 0))

        attempts, payload, status_code = await self._request_with_retry(
            method,
            url,
            headers=headers,
            params=query,
            body=body,
            timeout=timeout,
            retry_times=retry_times,
        )
        selected = _json_path(payload, config.get("response_path"))
        return ToolResult(
            content={"status_code": status_code, "data": selected},
            meta={"status_code": status_code, "attempts": attempts, "provider": "api"},
        )

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        body: Any,
        timeout: float,
        retry_times: int,
    ) -> tuple[int, Any, int]:
        """HTTP 调用 + 有限重试（`http_config.retry_times`）；返回 `(attempts, payload, status_code)`。"""
        attempts = 0
        async with self._client_factory(timeout) as client:
            while True:
                attempts += 1
                try:
                    response = await client.request(
                        method,
                        url,
                        headers=dict(headers),
                        # 空 query 传 `None`：否则 httpx 会用空 dict **覆盖掉 URL 模板里自带的查询串**
                        params=dict(params) or None,
                        json=body,
                    )
                except httpx.HTTPError as exc:
                    if attempts > retry_times:
                        raise ToolExecutionFailedError(
                            f"Request to '{url}' failed: {type(exc).__name__}",
                            details={"url": url, "attempts": attempts},
                        ) from exc
                    await asyncio.sleep(_backoff_seconds(attempts))
                    continue
                if response.status_code in API_RETRY_STATUS and attempts <= retry_times:
                    await asyncio.sleep(_backoff_seconds(attempts))
                    continue
                if response.status_code >= 400:
                    raise ToolExecutionFailedError(
                        f"HTTP {response.status_code} from '{url}'",
                        details={"url": url, "status_code": response.status_code, "attempts": attempts},
                    )
                return attempts, _decode_response(response), response.status_code

    # ---- 步骤 8：事件 + 落库 ----
    async def _emit_outcome(self, call: ToolCallSpec, *, ctx: ToolRunContext, result: ToolInvocationResult) -> None:
        """`tool.call.completed` / `tool.call.failed`（3.4 事件 7/8）。"""
        if result.succeeded:
            await ctx.emitter.emit(
                SseEventType.TOOL_CALL_COMPLETED,
                ToolCallCompletedPayload(
                    tool_name=call.name,
                    tool_call_id=call.id,
                    status=str(result.status),
                    latency_ms=result.latency_ms,
                    result_preview=_preview(result.content),
                ),
            )
            return
        await ctx.emitter.emit(
            SseEventType.TOOL_CALL_FAILED,
            ToolCallFailedPayload(
                tool_name=call.name,
                tool_call_id=call.id,
                error_code=result.error_code or str(ErrorCode.TOOL_EXECUTION_FAILED),
                error_message=result.error_message or "",
            ),
        )

    async def _write_record(
        self, call: ToolCallSpec, *, ctx: ToolRunContext, span_id: str, result: ToolInvocationResult
    ) -> None:
        """`tool_invocations` 一行（2.5）；写库失败不影响主流程（与 4.8.2 同策略）。"""
        run_trace = ctx.tracer.current_run()
        record = ToolInvocationRecord(
            id=ids.new_ulid(),
            run_id=ctx.run_id,
            trace_id=run_trace.trace_id if run_trace is not None else "",
            span_id=span_id,
            step_index=result.step_index,
            call_index=result.call_index,
            tool_id=result.tool_id,
            tool_name=result.tool_name,
            arguments=result.arguments,
            normalized_arguments=result.normalized_arguments,
            result=result.content,
            result_truncated=result.truncated,
            status=result.status,
            permission_decision=result.permission_decision,
            error_code=result.error_code,
            error_message=result.error_message,
            latency_ms=result.latency_ms,
        )
        try:
            await self._sink.write_invocation(record)
        except Exception:
            logger.warning("tool.invocation_sink_failed", tool_name=result.tool_name, exc_info=True)


# --------------------------------------------------------------------------------------
# 九步流水线的纯函数辅助（步骤 1–7、9 的规约；拆出来便于单测）
# --------------------------------------------------------------------------------------
_TEMPLATE_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")


def _require_definition(name: str, ctx: ToolRunContext) -> ToolDefinition:
    """步骤 1：查表 + 可用性。"""
    definition = ctx.definitions.get(name)
    if definition is None:
        raise ToolNotFoundError(f"Tool '{name}' is not available in this Run", details={"tool_name": name})
    if definition.status != TOOL_STATUS_ENABLED:
        raise ToolDisabledError(
            f"Tool '{name}' is disabled", details={"tool_name": name, "status": str(definition.status)}
        )
    if definition.tool_type == TOOL_TYPE_MCP:
        raise ToolDisabledError(
            f"Tool '{name}' is an MCP tool, which is not supported in this build (SD-16)",
            details={"tool_name": name, "reason": "TOOL_TYPE_NOT_SUPPORTED"},
        )
    return definition


def _merge_permission(definition: ToolDefinition, ctx: ToolRunContext) -> ToolPermissionConfig:
    """步骤 2：工具级 `permission_config` 与 Agent 级配置取严格合并。"""
    return definition.permission_config.merged_with(ctx.agent_permission)


def _validate_arguments(definition: ToolDefinition, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """步骤 3：JSON Schema 子集校验 + 默认值/类型归一。"""
    errors = validate_arguments(definition.input_schema, arguments)
    if errors:
        raise ToolInvalidArgumentsError(
            f"Invalid arguments for tool '{definition.name}'",
            details={"tool_name": definition.name, "errors": errors, "schema": schema_digest(definition.input_schema)},
        )
    return normalize_arguments(definition.input_schema, arguments)


def _decide(definition: ToolDefinition, permission: ToolPermissionConfig, ctx: ToolRunContext) -> PermissionDecision:
    """步骤 4：权限判定（`permission_policy`）。"""
    outcome = permissions.evaluate_policy(name=definition.name, config=permission, settings=ctx.settings)
    if not outcome.allowed:
        raise permissions.deny_error(outcome, tool_name=definition.name)
    return PermissionDecision.ALLOW


def _check_budget(definition: ToolDefinition, permission: ToolPermissionConfig, ctx: ToolRunContext) -> None:
    """步骤 5：调用闸门（单次 Run 内每工具调用上限）；通过后立即占用一次配额。"""
    outcome = permissions.check_call_budget(permission, calls_so_far=ctx.calls_per_tool.get(definition.name, 0))
    if not outcome.allowed:
        raise permissions.deny_error(outcome, tool_name=definition.name)
    ctx.calls_per_tool[definition.name] = ctx.calls_per_tool.get(definition.name, 0) + 1


async def _emit_started(ctx: ToolRunContext, call: ToolCallSpec, arguments: Mapping[str, Any]) -> None:
    """`tool.call.started`（3.4 事件 6）——仅在权限与闸门通过后发出。"""
    await ctx.emitter.emit(
        SseEventType.TOOL_CALL_STARTED,
        ToolCallStartedPayload(tool_name=call.name, tool_call_id=call.id, arguments=dict(arguments)),
    )


def _finalize(outcome: ToolResult, permission: ToolPermissionConfig) -> ToolResult:
    """步骤 7：输出大小控制（超限截断并把 `result_truncated` 置真）。"""
    text = outcome.as_text()
    truncated_text, truncated = sandbox.truncate_text(text, max_bytes=permission.max_output_bytes)
    if not truncated:
        return outcome
    outcome.content = truncated_text
    outcome.meta["truncated"] = True
    outcome.meta["original_bytes"] = len(text.encode("utf-8"))
    return outcome


def _output_attributes(outcome: ToolResult) -> dict[str, Any]:
    """把工具自报 meta 中可序列化的部分写进 span attributes。"""
    return {str(key): value for key, value in outcome.meta.items() if isinstance(value, (str, int, float, bool))}


def _preview(text: str) -> str:
    """SSE 事件与 span 用的短预览。"""
    if len(text) <= RESULT_PREVIEW_CHARS:
        return text
    return text[:RESULT_PREVIEW_CHARS]


def _error_payload(error: AppError | None) -> str:
    """失败时回填给模型的内容（4.2.3 错误处理原则）。"""
    if error is None:  # pragma: no cover - 正常分支不会走到
        return json.dumps({"error": str(ErrorCode.TOOL_EXECUTION_FAILED), "message": "unknown error"})
    body: dict[str, Any] = {"error": str(error.code), "message": error.message}
    details = getattr(error, "details", None)
    if details:
        body["details"] = details
    return json.dumps(body, ensure_ascii=False)


def _failure_meta(error: AppError | None) -> dict[str, Any]:
    """失败路径写进 span attributes 的可序列化字段。"""
    if error is None:  # pragma: no cover - 正常分支不会走到
        return {}
    meta: dict[str, Any] = {"error_code": str(error.code)}
    details = getattr(error, "details", None)
    if isinstance(details, Mapping):
        meta.update({str(key): value for key, value in details.items() if isinstance(value, (str, int, float, bool))})
    return meta


def _default_client_factory(timeout: float) -> httpx.AsyncClient:
    """`api` 工具的 httpx 客户端工厂（测试可注入 `httpx.MockTransport`）。"""
    return httpx.AsyncClient(timeout=timeout)


def _backoff_seconds(attempt: int) -> float:
    """指数退避（0.2s、0.4s、0.8s…，上限 2s）。"""
    return min(0.2 * (2.0 ** max(attempt - 1, 0)), 2.0)


def _lookup(arguments: Mapping[str, Any], path: str) -> Any:
    cursor: Any = arguments
    for part in path.split("."):
        if isinstance(cursor, Mapping) and part in cursor:
            cursor = cursor[part]
        else:
            return None
    return cursor


def _render_template(template: str, arguments: Mapping[str, Any]) -> str:
    """把 `{{arg}}` 占位符替换成调用参数（缺失的参数渲染为空串）。"""

    def _replace(match: re.Match[str]) -> str:
        value = _lookup(arguments, match.group(1))
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    return _TEMPLATE_PATTERN.sub(_replace, template)


def _render_object(template: Any, arguments: Mapping[str, Any]) -> Any:
    """递归渲染 `http_config.body_template`。"""
    if isinstance(template, str):
        return _render_template(template, arguments)
    if isinstance(template, Mapping):
        return {str(key): _render_object(value, arguments) for key, value in template.items()}
    if isinstance(template, (list, tuple)):
        return [_render_object(item, arguments) for item in template]
    return template


def _decode_response(response: httpx.Response) -> Any:
    """响应体硬上限（`API_MAX_RESPONSE_BYTES`）→ JSON 优先，其次文本。"""
    size = len(response.content)
    if size > API_MAX_RESPONSE_BYTES:
        raise ToolOutputTooLargeError(
            f"Tool response exceeds {API_MAX_RESPONSE_BYTES} bytes",
            details={"size_bytes": size, "limit_bytes": API_MAX_RESPONSE_BYTES},
        )
    try:
        return response.json()
    except ValueError:
        return response.text


def _json_path(payload: Any, path: Any) -> Any:
    """简化 JSONPath（2.5 `response_path`）：支持 `$.a.b` / `a.b` / 空（整体）。"""
    if not isinstance(path, str) or not path.strip():
        return payload
    cursor: Any = payload
    for part in path.strip().lstrip("$").strip(".").split("."):
        if not part:
            continue
        if isinstance(cursor, Mapping) and part in cursor:
            cursor = cursor[part]
        elif (
            isinstance(cursor, Sequence)
            and not isinstance(cursor, (str, bytes))
            and part.isdigit()
            and int(part) < len(cursor)
        ):
            cursor = cursor[int(part)]
        else:
            return None
    return cursor
