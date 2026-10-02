"""Secret 加解密与掩码（详细设计 1.4 规则 1）。"""

from __future__ import annotations

import pytest

from app.core.crypto import SecretCipher, generate_key, mask_secret, normalize_key
from app.core.errors import ConfigInvalidError


def test_mask_secret_keeps_edges() -> None:
    assert mask_secret("sk-abcdef123") == "sk-***123"
    assert mask_secret("short") == "***"
    assert mask_secret("") == ""
    assert mask_secret(None) == ""


def test_roundtrip_with_derived_key_and_real_fernet_key() -> None:
    for key in ("dev-only-change-me", generate_key()):
        cipher = SecretCipher(key)
        token = cipher.encrypt("sk-secret-value")
        assert token is not None and "sk-secret-value" not in token
        assert cipher.decrypt(token) == "sk-secret-value"
        assert cipher.encrypt("") is None
        assert cipher.decrypt(None) == ""


def test_normalize_key_is_stable_and_valid() -> None:
    key = normalize_key("dev-only-change-me")
    assert key == normalize_key("dev-only-change-me")
    assert len(key) == 44


def test_decrypt_with_wrong_key_raises_config_invalid() -> None:
    token = SecretCipher("key-one").encrypt("sk-secret")
    with pytest.raises(ConfigInvalidError) as excinfo:
        SecretCipher("key-two").decrypt(token)
    assert excinfo.value.code == "CONFIG_INVALID"
