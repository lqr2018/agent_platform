"""权限判定与次数闸门（详细设计 4.2.3 步骤 2/4/5、2.5、SD-17）。

分级语义（写死，由 `tests/unit/test_tool_permissions.py` 锁定）：

| level | 步骤 4 的行为 |
|---|---|
| `safe` | 直接 allow |
| `guarded` | 允许执行；`allowed_paths` / `allowed_hosts` 的**具体校验**在执行期由 `sandbox.py` 完成 |
| `dangerous` | 必须有显式开关（`REQUIRED_SWITCHES`）且已开启，否则 deny |

`require_approval=true` 的工具在 MVP 中等价于"**未开启即拒绝**"（SD-17）：不挂起 Run、
不建 `approvals` 行；错误回填给 LLM 后循环继续（4.2.3 步骤 4 括号说明）。

> 为什么 `guarded` 不在本模块做路径 / host 校验：同一份白名单必须对"读取"与"写入"分别施加
> 不同规则（4.2.4 的目录与大小限制），放在 `sandbox.py` 一处实现避免两套语义漂移。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.core.config import Settings
from app.core.enums import PermissionDecision, PermissionLevel
from app.core.errors import AppError, ToolPermissionDeniedError
from app.runtime.tools.base import ToolPermissionConfig

REQUIRED_SWITCHES: Mapping[str, str] = {
    "file_write": "file_write_enabled",
    "python_execute": "python_execute_enabled",
}
"""需显式开启的工具 → `Settings` 字段名（2.5 的 `require_approval` 在 MVP 的落点，SD-17）。"""

MAX_TOOL_FAILURES = 3
"""同一工具连续失败次数上限（4.2.3 的错误处理原则：连续 3 次 → `TOOL_REPEATED_FAILURE`）。"""


@dataclass(frozen=True, slots=True)
class PermissionOutcome:
    """一次权限判定的结果（allow / deny + 可回填的原因）。"""

    decision: PermissionDecision
    reason: str
    message: str = ""
    level: PermissionLevel = PermissionLevel.SAFE

    @property
    def allowed(self) -> bool:
        return self.decision == PermissionDecision.ALLOW


def _allow(level: PermissionLevel, reason: str = "ALLOWED") -> PermissionOutcome:
    return PermissionOutcome(decision=PermissionDecision.ALLOW, reason=reason, level=level)


def _deny(level: PermissionLevel, reason: str, message: str) -> PermissionOutcome:
    return PermissionOutcome(decision=PermissionDecision.DENY, reason=reason, message=message, level=level)


def switch_enabled(settings: Settings, switch: str) -> bool:
    """读取开关（`FILE_WRITE_ENABLED` / `PYTHON_EXECUTE_ENABLED`，附录 C）。"""
    return bool(getattr(settings, switch, False))


def evaluate_policy(*, name: str, config: ToolPermissionConfig, settings: Settings) -> PermissionOutcome:
    """4.2.3 步骤 4：`safe` → allow；`guarded` → allow（细节校验在沙箱）；危险/需审批 → 开关闸门。"""
    switch = REQUIRED_SWITCHES.get(name)

    if config.require_approval:
        if switch is None:
            return _deny(
                config.level,
                "APPROVAL_CHANNEL_UNAVAILABLE",
                f"Tool '{name}' requires approval, which is not available in this build (SD-17)",
            )
        if not switch_enabled(settings, switch):
            return _deny(
                config.level,
                "SWITCH_DISABLED",
                f"Tool '{name}' is disabled (set {switch.upper()}=true to allow it)",
            )

    if config.level == PermissionLevel.DANGEROUS and (switch is None or not switch_enabled(settings, switch)):
        return _deny(
            config.level,
            "DANGEROUS_TOOL_DISABLED",
            f"Tool '{name}' is dangerous and must be enabled explicitly",
        )

    return _allow(config.level)


def check_call_budget(config: ToolPermissionConfig, *, calls_so_far: int) -> PermissionOutcome:
    """4.2.3 步骤 5：本 Run 内该工具调用数 > `max_calls_per_run` → deny。"""
    if calls_so_far >= config.max_calls_per_run:
        return _deny(
            config.level,
            "MAX_CALLS_EXCEEDED",
            f"Tool call budget exceeded ({config.max_calls_per_run} per run)",
        )
    return _allow(config.level, reason="WITHIN_BUDGET")


def deny_error(outcome: PermissionOutcome, *, tool_name: str) -> AppError:
    """把 deny 结论转成可回填的错误（`details` 含 `tool_name` / `permission_level`，1.6 示例）。"""
    return ToolPermissionDeniedError(
        outcome.message or f"Tool '{tool_name}' is not allowed",
        details={
            "tool_name": tool_name,
            "permission_level": str(outcome.level),
            "reason": outcome.reason,
        },
    )
