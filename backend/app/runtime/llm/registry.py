"""LLM Provider 工厂注册表（详细设计 4.1.3）。

- 按 `ProviderConfig.kind` 实例化 Provider，业务代码只持有 `LLMProvider` 抽象（1.2 / 大纲原则 3）；
- `fake` **只在 `APP_ENV=test` 时注册**（4.1.3），生产/开发环境用它 → `AGENT_INVALID_CONFIG`；
- 需要注入 `httpx.AsyncBaseTransport` 的测试可直接构造 Provider，或把 transport 传给本类。
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from app.core.config import Settings
from app.core.errors import AgentInvalidConfigError
from app.runtime.llm.base import LLMProvider, ProviderConfig
from app.runtime.llm.fake import FakeLLMProvider
from app.runtime.llm.openai_compatible import OpenAICompatibleProvider

ProviderFactory = Callable[[ProviderConfig, Settings], LLMProvider]


class LLMRegistry:
    """按 kind 创建 Provider 的注册表（4.1.3 的收敛版：入参是快照而非 ORM 行）。"""

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport
        self._factories: dict[str, ProviderFactory] = {"openai_compatible": self._openai_compatible}
        if settings.app_env == "test":
            self._factories["fake"] = FakeLLMProvider.from_settings

    @property
    def kinds(self) -> tuple[str, ...]:
        """已注册的 kind（`app_env=test` 时含 `fake`）。"""
        return tuple(sorted(self._factories))

    def create(self, config: ProviderConfig) -> LLMProvider:
        """按 `config.kind` 创建 Provider；未注册的 kind 抛 `AGENT_INVALID_CONFIG`（422）。"""
        factory = self._factories.get(config.kind)
        if factory is None:
            raise AgentInvalidConfigError(
                f"Provider kind '{config.kind}' is not available in this environment",
                details={"provider_id": config.id, "kind": config.kind, "available": list(self.kinds)},
            )
        return factory(config, self._settings)

    def _openai_compatible(self, config: ProviderConfig, settings: Settings) -> LLMProvider:
        return OpenAICompatibleProvider.from_settings(config, settings, transport=self._transport)
