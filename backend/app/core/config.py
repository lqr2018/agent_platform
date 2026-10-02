"""`Settings`：平台配置的**单一入口**（详细设计 1.4 / 6.2 / 附录 C）。

- 字段与《详细设计》附录 C 全量清单**逐项对应**（由 `tests/unit/test_config_docs_consistency.py` 断言）；
- 三层优先级：进程环境变量 > `.env` > 本类默认值；
- 除本模块外**任何地方不得直接读 `os.environ`**（1.4）；
- `prod` 环境下 `secret_key` / `encryption_key` 仍是默认值 → 启动即失败（`CONFIG_INVALID`，6.2）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.errors import ConfigInvalidError

DEFAULT_SECRET_KEY = "dev-only-change-me"
DEFAULT_ENCRYPTION_KEY = "dev-only-change-me"
PLACEHOLDER_SECRETS = frozenset({DEFAULT_SECRET_KEY, DEFAULT_ENCRYPTION_KEY, ""})


class Settings(BaseSettings):
    """全部配置项（字段顺序与附录 C 一致，便于对照）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- 基础 ----
    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:///./data/app.db"
    secret_key: str = DEFAULT_SECRET_KEY
    encryption_key: str = DEFAULT_ENCRYPTION_KEY
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    enable_docs: bool = True

    # ---- LLM 默认值 ----
    llm_default_base_url: str = "https://api.openai.com/v1"
    llm_default_api_key: str = ""
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2
    llm_stream: bool = True

    # ---- 工具与沙箱 ----
    sandbox_root: str = "./data/files"
    tool_default_timeout_seconds: float = 15.0
    tool_max_output_bytes: int = 65_536
    python_execute_enabled: bool = False
    file_write_enabled: bool = False

    # ---- Agent Runtime ----
    agent_max_steps: int = 8
    agent_run_timeout_seconds: float = 180.0

    # ---- 向量库（SD-11：只实现 chroma 与 memory，不预留 qdrant） ----
    vector_store_kind: Literal["chroma", "memory"] = "chroma"
    chroma_dir: str = "./data/chroma"
    embed_allow_fake: bool = False
    embedding_batch_size: int = 32

    # ---- Trace / 并发 ----
    trace_store_io: bool = True
    trace_max_payload_bytes: int = 8_192
    max_concurrent_runs: int = 4
    eval_concurrency: int = 2

    # ---- 长任务与上传 ----
    detach_cancel: bool = False
    task_runner_concurrency: int = 1
    upload_max_bytes: int = 10_485_760

    # ---- 外部工具（为空则该工具禁用） ----
    web_search_provider: str = ""
    web_search_api_key: str = ""

    # ---- 数据根目录 ----
    data_dir: str = "./data"

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def sandbox_path(self) -> Path:
        return Path(self.sandbox_root)

    @property
    def chroma_path(self) -> Path:
        return Path(self.chroma_dir)

    def runtime_dirs(self) -> dict[str, Path]:
        """启动时确保存在的运行时目录（`DATA_DIR` 下的完整内容，附录 C 说明）。"""
        return {
            "data": self.data_path,
            "chroma": self.chroma_path,
            "sandbox": self.sandbox_path,
            "uploads": self.data_path / "uploads",
            "reports": self.data_path / "reports",
        }

    def assert_production_secrets(self) -> None:
        """6.2：`prod` 下密钥仍为默认值 → `CONFIG_INVALID`（启动即失败）。"""
        if not self.is_prod:
            return
        offenders = [
            name
            for name, value in (("SECRET_KEY", self.secret_key), ("ENCRYPTION_KEY", self.encryption_key))
            if value in PLACEHOLDER_SECRETS
        ]
        if offenders:
            raise ConfigInvalidError(
                "Refusing to start in prod with placeholder secrets",
                details={"env_vars": offenders},
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """配置单例（1.4）。测试中可用 `get_settings.cache_clear()` 重载。"""
    return Settings()
