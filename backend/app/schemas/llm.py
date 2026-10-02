"""模型 Provider 的 DTO（详细设计 3.2.2 / 2.3 / 1.4 规则 1）。

- **请求**可以带 `api_key` 明文；**响应只返回 `api_key_masked`**（1.4 规则 1）；
- `kind` 用 `ProviderKind`（0.2.2 A 段：MVP 只有 `openai_compatible`），因此 `fake` 无法经 API 创建；
- `models[]` 的结构见 2.3（`name` / `context_window` / 两个价格字段）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import ProviderKind
from app.schemas.common import UtcDatetime

ProviderStatus = Literal["enabled", "disabled"]


class ModelEntry(BaseModel):
    """`models[]` 的一个白名单元素（2.3）。"""

    name: str = Field(min_length=1, max_length=120)
    context_window: int | None = Field(default=None, ge=1)
    input_price_per_1k_usd: float | None = Field(default=None, ge=0)
    output_price_per_1k_usd: float | None = Field(default=None, ge=0)


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: ProviderKind = ProviderKind.OPENAI_COMPATIBLE
    base_url: str = Field(min_length=1, max_length=512)
    api_key: str | None = None
    default_model: str | None = Field(default=None, max_length=120)
    models: list[ModelEntry] = Field(default_factory=list)
    default_params: dict[str, object] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    is_default: bool = False
    status: ProviderStatus = "enabled"

    @field_validator("base_url")
    @classmethod
    def _strip_base_url(cls, value: str) -> str:
        return value.strip()


class ProviderUpdate(BaseModel):
    """`PATCH`：只传需要改的字段；`api_key` 传空串表示清除。"""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    base_url: str | None = Field(default=None, min_length=1, max_length=512)
    api_key: str | None = None
    default_model: str | None = Field(default=None, max_length=120)
    models: list[ModelEntry] | None = None
    default_params: dict[str, object] | None = None
    headers: dict[str, str] | None = None
    is_default: bool | None = None
    status: ProviderStatus | None = None


class ProviderRead(BaseModel):
    """响应 DTO（**不含**任何明文密钥）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    kind: str
    base_url: str
    api_key_masked: str = ""
    has_api_key: bool = False
    default_model: str | None = None
    models: list[ModelEntry] = Field(default_factory=list)
    default_params: dict[str, object] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    is_default: bool = False
    status: str = "enabled"
    last_check_at: UtcDatetime | None = None
    last_check_error: str | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class ProviderTestResult(BaseModel):
    """`POST /model-providers/{id}/test` 的结果（3.2.2：连通性检测，1-token）。"""

    ok: bool
    provider_id: str
    model: str
    latency_ms: int = 0
    error_code: str | None = None
    error_message: str | None = None
