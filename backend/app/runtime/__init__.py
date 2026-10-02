"""平台核心（runtime）：纯执行逻辑，**禁止 import `services` / `api`**（详细设计 1.2）。

数据通过入参 dataclass 传入，保证可单测、可脱离 DB 运行。
Phase 0 只落地 `observability/`（Tracer 骨架）。
"""

from __future__ import annotations
