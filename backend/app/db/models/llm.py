"""`model_providers`（详细设计 2.3）。

- `api_key_encrypted` 存 Fernet 密文（1.4 规则 1），对外只暴露 `api_key_masked`；
- `kind` 取值来自 `ProviderKind`（0.2.2 A 段），MVP 只允许 `openai_compatible`
  （`fake` 仅测试期由 service 直写，见 4.1.3 的 Phase 1 约定）；
- `is_default` 全平台只允许一个 `true`，由 `services/model_provider_service.py` 保证（2.3）；
- `models` 白名单元素结构：`{name, context_window, input_price_per_1k_usd, output_price_per_1k_usd}`（2.3）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ProviderKind
from app.db.base import Base, JSONDict, JSONList, TimestampMixin, UUIDStr

PROVIDER_STATUS_ENABLED = "enabled"
PROVIDER_STATUS_DISABLED = "disabled"


class ModelProvider(Base, TimestampMixin):
    """模型 Provider 行（2.3 的全量列）。"""

    __tablename__ = "model_providers"

    id: Mapped[UUIDStr]
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default=ProviderKind.OPENAI_COMPATIBLE, nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    models: Mapped[JSONList]
    default_params: Mapped[JSONDict]
    headers: Mapped[JSONDict]
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=PROVIDER_STATUS_ENABLED, nullable=False)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_check_error: Mapped[str | None] = mapped_column(Text, nullable=True)
