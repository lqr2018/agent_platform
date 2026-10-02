"""用量与成本（详细设计 4.1.2 第 6/7 条、4.8.3）。"""

from __future__ import annotations

from decimal import Decimal

from app.runtime.llm.usage import ZERO_USAGE, TokenUsage, estimate_tokens
from app.runtime.observability.cost import estimate_cost, has_price, parse_prices


def test_token_usage_total_and_add() -> None:
    usage = TokenUsage(prompt_tokens=10, completion_tokens=5)
    assert usage.total_tokens == 15
    assert usage.as_dict() == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert (usage + usage) == TokenUsage(prompt_tokens=20, completion_tokens=10)
    assert ZERO_USAGE.total_tokens == 0
    assert usage + ZERO_USAGE == usage


def test_estimate_tokens_ascii_and_cjk() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1  # ASCII 4 字符 1 token
    assert estimate_tokens("你好") == 2  # 中文按字符计
    assert estimate_tokens("你好hello") == 3


def test_parse_prices_and_estimate_cost() -> None:
    prices = parse_prices(
        [
            {"name": "qwen-plus", "input_price_per_1k_usd": 0.0004, "output_price_per_1k_usd": 0.0012},
            {"name": "no-price"},
        ]
    )
    assert has_price("qwen-plus", prices) and not has_price("gpt-4o", prices)

    cost = estimate_cost("qwen-plus", prompt_tokens=1000, completion_tokens=500, prices=prices)
    # 1000/1000*0.0004 + 500/1000*0.0012 = 0.001
    assert cost == Decimal("0.001000")

    # 模型不在价格表（或根本没配价格）→ 0，且 `has_price` 为假（4.1.2 第 7 条）
    assert estimate_cost("gpt-4o", 1000, 1000, prices) == Decimal(0)
    assert estimate_cost("no-price", 1000, 1000, prices) == Decimal(0)


def test_estimate_cost_handles_bad_values() -> None:
    prices = parse_prices([{"name": "weird", "input_price_per_1k_usd": "oops", "output_price_per_1k_usd": None}])
    assert estimate_cost("weird", 100, 100, prices) == Decimal(0)
