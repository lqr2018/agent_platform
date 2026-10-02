"""服务层：事务边界、实体 CRUD、跨实体编排（详细设计 1.2）。

依赖方向：`api → services → db/models`；services 可调 `runtime/*`，但 runtime 不得反向 import services。
"""
