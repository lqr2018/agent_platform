"""权限合并与判定（详细设计 4.2.3 步骤 2/4/5、2.5、SD-17）。

锁定三类规则：`merged_with` 的"取更严格"、`evaluate_policy` 的开关闸门、
`check_call_budget` 的次数闸门（含"未开启即拒绝，且不产生 approvals"的 SD-17 回归点）。
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.enums import PermissionDecision, PermissionLevel
from app.core.errors import ErrorCode, ToolPermissionDeniedError
from app.runtime.tools import permissions
from app.runtime.tools.base import ToolPermissionConfig


def _settings(**overrides: object) -> Settings:
    """显式给全开关，避免本机 `.env` 影响断言（1.4：配置只经 Settings 读取）。"""
    base: dict[str, object] = {
        "app_env": "test",
        "python_execute_enabled": False,
        "file_write_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)


def test_from_mapping_defaults_and_unknown_keys() -> None:
    config = ToolPermissionConfig.from_mapping({"level": "guarded", "unknown": 1})
    assert config.level is PermissionLevel.GUARDED
    assert config.require_approval is False
    assert config.max_output_bytes == 65_536
    assert config.timeout_seconds == 15.0
    assert config.max_calls_per_run == 20
    assert ToolPermissionConfig.from_mapping(None) == ToolPermissionConfig()


def test_merged_with_takes_the_stricter_level() -> None:
    assert (
        ToolPermissionConfig(level=PermissionLevel.SAFE)
        .merged_with(ToolPermissionConfig(level=PermissionLevel.DANGEROUS))
        .level
        is PermissionLevel.DANGEROUS
    )
    assert (
        ToolPermissionConfig(level=PermissionLevel.GUARDED)
        .merged_with(ToolPermissionConfig(level=PermissionLevel.SAFE))
        .level
        is PermissionLevel.GUARDED
    )


def test_merged_with_or_and_and_intersection_rules() -> None:
    tool_level = ToolPermissionConfig(
        level=PermissionLevel.SAFE,
        require_approval=True,
        allow_network=True,
        allowed_paths=["uploads", "outputs"],
        allowed_hosts=["api.example.com", "other.example.com"],
    )
    agent_level = ToolPermissionConfig(
        require_approval=False,
        allow_network=True,
        allowed_paths=["outputs"],
        allowed_hosts=["api.example.com"],
    )
    merged = tool_level.merged_with(agent_level)

    assert merged.require_approval is True  # or
    assert merged.allow_network is True  # and（两方都允许才允许）
    assert merged.allowed_paths == ["outputs"]  # 交集
    assert merged.allowed_hosts == ["api.example.com"]  # 交集
    assert tool_level.merged_with(ToolPermissionConfig(allow_network=False)).allow_network is False


def test_merged_with_empty_whitelist_means_no_extra_restriction() -> None:
    restricted = ToolPermissionConfig(allowed_hosts=["api.example.com"], allowed_paths=["outputs"])
    assert restricted.merged_with(ToolPermissionConfig()).allowed_hosts == ["api.example.com"]
    assert ToolPermissionConfig().merged_with(restricted).allowed_paths == ["outputs"]


def test_merged_with_takes_smaller_numeric_limits() -> None:
    merged = ToolPermissionConfig(max_output_bytes=65_536, timeout_seconds=15.0, max_calls_per_run=20).merged_with(
        ToolPermissionConfig(max_output_bytes=1_024, timeout_seconds=5.0, max_calls_per_run=3)
    )
    assert (merged.max_output_bytes, merged.timeout_seconds, merged.max_calls_per_run) == (1_024, 5.0, 3)
    assert ToolPermissionConfig().merged_with(None) == ToolPermissionConfig()


@pytest.mark.parametrize("level", [PermissionLevel.SAFE, PermissionLevel.GUARDED])
def test_policy_allows_safe_and_guarded(level: PermissionLevel) -> None:
    """`guarded` 在步骤 4 直接放行；路径 / host 的细节校验由 `sandbox.py` 在执行期做。"""
    outcome = permissions.evaluate_policy(
        name="calculator", config=ToolPermissionConfig(level=level), settings=_settings()
    )
    assert outcome.allowed and outcome.decision is PermissionDecision.ALLOW


def test_policy_denies_dangerous_without_explicit_switch() -> None:
    config = ToolPermissionConfig(level=PermissionLevel.DANGEROUS)
    outcome = permissions.evaluate_policy(name="python_execute", config=config, settings=_settings())

    assert outcome.decision is PermissionDecision.DENY
    # 已登记开关的工具（`REQUIRED_SWITCHES`）与未登记的工具给出不同 reason，便于前端区分
    assert outcome.reason == "DANGEROUS_TOOL_DISABLED"
    assert "must be enabled explicitly" in outcome.message
    assert permissions.evaluate_policy(name="custom_tool", config=config, settings=_settings()).reason == (
        "DANGEROUS_TOOL_DISABLED"
    )


def test_policy_allows_dangerous_when_switch_is_on() -> None:
    config = ToolPermissionConfig(level=PermissionLevel.DANGEROUS)
    outcome = permissions.evaluate_policy(
        name="python_execute", config=config, settings=_settings(python_execute_enabled=True)
    )
    assert outcome.allowed


def test_approval_without_channel_is_denied_not_suspended() -> None:
    """SD-17：`require_approval=true` 在 MVP 等价于"未开启即拒绝"，不挂起 Run、不建 approvals 行。"""
    outcome = permissions.evaluate_policy(
        name="custom_tool", config=ToolPermissionConfig(require_approval=True), settings=_settings()
    )
    assert outcome.decision is PermissionDecision.DENY
    assert outcome.reason == "APPROVAL_CHANNEL_UNAVAILABLE"
    assert "SD-17" in outcome.message


def test_deny_error_maps_to_tool_permission_denied() -> None:
    outcome = permissions.evaluate_policy(
        name="file_write", config=ToolPermissionConfig(require_approval=True), settings=_settings()
    )
    error = permissions.deny_error(outcome, tool_name="file_write")

    assert isinstance(error, ToolPermissionDeniedError)
    assert str(error.code) == str(ErrorCode.TOOL_PERMISSION_DENIED)
    assert error.details == {
        "tool_name": "file_write",
        "permission_level": "safe",
        "reason": "SWITCH_DISABLED",
    }


def test_check_call_budget_blocks_at_the_limit() -> None:
    config = ToolPermissionConfig(max_calls_per_run=2)
    assert permissions.check_call_budget(config, calls_so_far=1).allowed
    assert permissions.check_call_budget(config, calls_so_far=2).reason == "MAX_CALLS_EXCEEDED"
    assert permissions.check_call_budget(config, calls_so_far=3).decision is PermissionDecision.DENY


def test_switch_enabled_reads_settings_flag() -> None:
    assert permissions.switch_enabled(_settings(file_write_enabled=True), "file_write_enabled") is True
    assert permissions.switch_enabled(_settings(), "file_write_enabled") is False
    assert permissions.switch_enabled(_settings(), "nope") is False
    assert permissions.REQUIRED_SWITCHES == {
        "file_write": "file_write_enabled",
        "python_execute": "python_execute_enabled",
    }
