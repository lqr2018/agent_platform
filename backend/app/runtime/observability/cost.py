"""成本估算（详细设计 4.8.3 / 4.1.2 第 7 条）。

- 价格来自 `model_providers.models[]` 的 `input_price_per_1k_usd` / `output_price_per_1k_usd`（2.3）；
- **价格缺失 → cost 记 0 且 `cost_estimated=false`**（4.1.2 第 7 条：宁可标"没数据"也不编数）；
- `cost_usd` 一律用 `Decimal`，与 `Numeric(12, 6)` 列对齐（2.2：金额 `_usd` 后缀）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

USD_QUANT = Decimal("0.000001")
"""`Numeric(12, 6)` 的精度（2.2）。"""


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """每 1K token 的估算单价（美元）。"""

    input_price_per_1k_usd: Decimal = Decimal(0)
    output_price_per_1k_usd: Decimal = Decimal(0)


def parse_prices(models: Iterable[Mapping[str, Any]]) -> dict[str, ModelPrice]:
    """从 `model_providers.models[]` 解析价格表（键为模型名）。"""
    prices: dict[str, ModelPrice] = {}
    for item in models:
        name = item.get("name")
        if not name:
            continue
        prices[str(name)] = ModelPrice(
            input_price_per_1k_usd=_to_decimal(item.get("input_price_per_1k_usd")),
            output_price_per_1k_usd=_to_decimal(item.get("output_price_per_1k_usd")),
        )
    return prices


def estimate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    prices: Mapping[str, ModelPrice],
) -> Decimal:
    """估算单次调用成本；模型不在价格表里则返回 0（4.8.3）。"""
    price = prices.get(model)
    if price is None:
        return Decimal(0)
    cost = (
        Decimal(prompt_tokens) * price.input_price_per_1k_usd
        + Decimal(completion_tokens) * price.output_price_per_1k_usd
    ) / Decimal(1000)
    return cost.quantize(USD_QUANT, rounding=ROUND_HALF_UP)


def has_price(model: str, prices: Mapping[str, ModelPrice]) -> bool:
    """是否拿到价格（决定 `cost_estimated` 的取值，4.1.2 第 7 条）。"""
    return model in prices


def _to_decimal(value: Any) -> Decimal:
    """把 JSON 里的数值安全转成 `Decimal`（`None` / 非法值 → 0）。"""
    if value is None:
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return Decimal(0)
