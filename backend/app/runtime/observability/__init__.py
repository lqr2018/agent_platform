"""可观测性：Trace 上下文、Tracer、成本聚合（详细设计 4.8 / 1.5.1–1.5.2）。

Phase 0 只有 `context.py` 与 `tracer.py`（骨架）；`cost.py` 属 Phase 7。
"""

from __future__ import annotations
