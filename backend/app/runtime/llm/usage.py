"""Token 用量与粗估（详细设计 4.1.2 第 6 条）。

- `TokenUsage` 是 LLM / Run / Span 三层唯一的用量类型（`prompt` + `completion`，`total` 派生）；
- 上游没回 `usage` 时用 `estimate_tokens()` 兜底，并在 `LLMResult.usage_estimated=true`
  与 `spans.attributes.usage_estimated` 标注（4.1.2 / 2.11）。
"""

from __future__ import annotations

from dataclasses import dataclass

ASCII_CHARS_PER_TOKEN = 4
"""英文/代码的常见经验值（1 token ≈ 4 字符）。"""


def estimate_tokens(text: str) -> int:
    """粗估 token 数：ASCII 按 4 字符 1 token，非 ASCII（中文等）按 1 字符 1 token。"""
    if not text:
        return 0
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    wide_chars = len(text) - ascii_chars
    return max(1, ascii_chars // ASCII_CHARS_PER_TOKEN + wide_chars)


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """用量（`total` 由 `prompt` + `completion` 派生，避免三处不一致）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )

    def as_dict(self) -> dict[str, int]:
        """SSE `usage.updated` / Trace attributes 用的扁平结构（3.4 事件 16）。"""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


ZERO_USAGE = TokenUsage()
"""单位元（`AgentState.usage` 初值，4.4.1）。"""
