"""Secret 加解密（详细设计 1.4 Secrets 规则 1；落点见 1.1 的 `core/crypto.py`）。

约定：

- `model_providers.api_key_encrypted` 写入前用本模块加密（Fernet），读取后仅在内存中解密；
- **API 响应永远只返回 `api_key_masked`**（如 `sk-***abc`），由 `mask_secret()` 生成；
- `ENCRYPTION_KEY` 若是合法 Fernet key（`Fernet.generate_key()` 的输出）则直接使用；
  若是普通口令（dev 默认值 `dev-only-change-me`）、不能用 32 字节做派生输入时，
  按其 SHA-256 派生 Fernet key —— 保证开发环境开箱可用，生产用真 key（6.2 会拒绝占位密钥）。
- 解密失败（如换了 `ENCRYPTION_KEY`）抛 `CONFIG_INVALID`，而不是静默返回空值。
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.errors import ConfigInvalidError

MASK = "***"
MASK_VISIBLE_CHARS = 3
"""掩码保留的首尾字符数（`sk-***abc`，1.4 规则 1）。"""


def normalize_key(raw_key: str) -> bytes:
    """把 `ENCRYPTION_KEY` 归一为合法的 Fernet key。"""
    candidate = raw_key.encode("utf-8")
    try:
        Fernet(candidate)
    except (ValueError, TypeError):
        # 非 Fernet key（口令形态）：按 SHA-256 派生 32 字节密钥
        return base64.urlsafe_b64encode(hashlib.sha256(candidate).digest())
    return candidate


def mask_secret(value: str | None) -> str:
    """生成 `api_key_masked`（1.4 规则 1：响应里只出现掩码）。"""
    if not value:
        return ""
    if len(value) <= MASK_VISIBLE_CHARS * 2:
        return MASK
    return f"{value[:MASK_VISIBLE_CHARS]}{MASK}{value[-MASK_VISIBLE_CHARS:]}"


class SecretCipher:
    """Fernet 加解密封装（每次读写都新建实例，避免把密钥长期留在对象里）。"""

    def __init__(self, encryption_key: str) -> None:
        self._fernet = Fernet(normalize_key(encryption_key))

    def encrypt(self, plaintext: str | None) -> str | None:
        """空值原样返回（`api_key` 允许为空 = 走 `LLM_DEFAULT_API_KEY`）。"""
        if not plaintext:
            return None
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str | None) -> str:
        """解密；空值返回空串。"""
        if not token:
            return ""
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise ConfigInvalidError(
                "Cannot decrypt stored secret; ENCRYPTION_KEY may have changed",
                details={"reason": type(exc).__name__},
            ) from exc


def generate_key() -> str:
    """生成一个合法的 Fernet key（附录 C：`ENCRYPTION_KEY` 用 `Fernet.generate_key()`）。"""
    return Fernet.generate_key().decode("ascii")
