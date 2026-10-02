"""横切能力（详细设计 1.4–1.6）。

- `config.py`：`Settings` 单一配置入口（1.4）
- `logging.py`：structlog 配置 + 密钥脱敏 + 上下文注入（1.5.1）
- `errors.py`：`AppError` 体系与错误码（1.6 / 附录 A）
- `ids.py`：ULID / trace_id / span_id / request_id 生成（0.2.1）
- `enums.py`：基线枚举（0.2.2）
"""

from __future__ import annotations
