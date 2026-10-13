"""7 类断言的行为契约（详细设计 4.9.3；`app/runtime/eval/assertions.py`）。

覆盖四件事：

1. 每类断言的**通过 / 不通过**两侧与边界（`max_latency` 相等即通过）；
2. 三条"不抛异常"的约定 —— 非法正则 → `errored`；`json_path` 取不到 / answer 不是 JSON → 普通 `failed`；
   `path` 写法不支持 → `errored`；judge 没配或 judge 调用失败 → `errored`（4.9.3 的 `failed` / `error` 之分）；
3. **不短路**：`evaluate_assertions()` 与 `expected[]` 等长、同序，且失败点之后的断言确实被求值过；
4. 加载期校验：断言结构写错在**载入用例集**时就 `CONFIG_INVALID`，而不是跑到一半才炸。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest

from app.core.errors import AppError, ConfigInvalidError, ModelTimeoutError
from app.runtime.eval import (
    AssertionContext,
    EvalExpectation,
    assertion_summary,
    evaluate_assertion,
    evaluate_assertions,
    failed_assertions,
)
from app.runtime.eval.judges import JudgeResult

ANSWER = "结果：12 * (3 + 3) = 72"
USER_INPUT = "计算 12 * (3 + 3)"
CONTEXT = AssertionContext(
    answer=ANSWER, tool_calls=("calculator",), latency_ms=1200, case_id="c1", user_input=USER_INPUT
)


def _expect(**raw: Any) -> EvalExpectation:
    """从 YAML 形态的 mapping 造断言（顺带覆盖加载期校验）。"""
    return EvalExpectation.from_mapping(raw, where="unit")


@dataclass
class ScriptedJudge:
    """脚本化 judge（9.2：`llm_judge` 的打分器可替身，单测不碰真实模型）。"""

    score_value: float = 0.9
    reason: str = "covers the rubric"
    cost: Decimal = Decimal("0.0004")
    error: AppError | None = None
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    async def score(self, *, rubric: str, answer: str, user_input: str = "") -> JudgeResult:
        self.calls.append((rubric, answer, user_input))
        if self.error is not None:
            raise self.error
        return JudgeResult(
            score=self.score_value,
            reason=self.reason,
            raw=json.dumps({"score": self.score_value, "reason": self.reason}),
            cost_usd=self.cost,
        )


# ---------------------------------------------------------------- contains


async def test_contains_both_sides() -> None:
    hit = await evaluate_assertion(_expect(type="contains", value="12 * (3 + 3) = 72"), CONTEXT)
    assert hit.passed is True
    assert hit.errored is False
    assert hit.expected == "12 * (3 + 3) = 72"
    assert hit.actual == ANSWER
    assert hit.detail == "answer contains '12 * (3 + 3) = 72'"

    miss = await evaluate_assertion(_expect(type="contains", value="73"), CONTEXT)
    assert miss.passed is False
    assert miss.errored is False
    assert miss.detail == "answer does not contain '73'"


async def test_assertions_do_not_short_circuit() -> None:
    """第 2 条失败后第 3 条仍要跑（4.9.3：全部执行，方便一次看清所有问题）。"""
    expectations = (
        _expect(type="contains", value="72"),
        _expect(type="contains", value="nope"),
        _expect(type="max_latency", value_ms=100),
    )
    results = await evaluate_assertions(expectations, CONTEXT)

    assert [item.type for item in results] == ["contains", "contains", "max_latency"]
    assert [item.passed for item in results] == [True, False, False]
    assert results[2].detail == "latency 1200ms exceeds 100ms", "第 3 条真的被求值了"
    assert len(failed_assertions(results)) == 2
    assert assertion_summary(results[1]) == "expected contains nope"
    assert assertion_summary(results[2]) == "expected max_latency 100"


# ------------------------------------------------------------------ equals


async def test_equals_applies_normalize_in_order() -> None:
    padded = AssertionContext(answer="  结果：8 / 2 = 4  \n")
    hit = await evaluate_assertion(_expect(type="equals", value="结果：8 / 2 = 4", normalize=["strip"]), padded)
    assert hit.passed is True
    assert hit.expected == "结果：8 / 2 = 4"

    loose = _expect(type="equals", value="  A   B ", normalize=["strip", "collapse_ws", "lower"])
    assert (await evaluate_assertion(loose, AssertionContext(answer="a\n  b"))).passed is True

    strict = _expect(type="equals", value="a b")
    assert (await evaluate_assertion(strict, AssertionContext(answer="a\n  b"))).passed is False


async def test_equals_failure_detail_spells_out_normalize_steps() -> None:
    result = await evaluate_assertion(
        _expect(type="equals", value="x", normalize=["strip"]),
        AssertionContext(answer="y"),
    )
    assert result.passed is False
    assert result.detail == "expected 'x' but got 'y' (normalize=strip)"


# ------------------------------------------------------------------- regex


async def test_regex_is_multiline_and_invalid_pattern_errors() -> None:
    context = AssertionContext(answer="hint line\nTOOL_EXECUTION_FAILED: division by zero")

    hit = await evaluate_assertion(_expect(type="regex", value="TOOL_EXECUTION_FAILED"), context)
    assert hit.passed is True
    assert hit.actual == "TOOL_EXECUTION_FAILED"

    multiline = await evaluate_assertion(_expect(type="regex", value="^hint line$"), context)
    assert multiline.passed is True, "`re.MULTILINE` 必须生效（answer 里带换行是常态）"

    broken = await evaluate_assertion(_expect(type="regex", value="("), context)
    assert broken.passed is False
    assert broken.errored is True
    assert broken.detail.startswith("invalid regex in suite:")


# --------------------------------------------------------------- json_path


async def test_json_path_hit_miss_and_error_kinds() -> None:
    context = AssertionContext(answer=json.dumps({"result": {"values": [2, 4]}, "ok": True}))

    hit = await evaluate_assertion(_expect(type="json_path", path="$.result.values[1]", value=4), context)
    assert hit.passed is True
    assert hit.actual == 4
    assert hit.detail == "$.result.values[1] == 4"

    out_of_range = await evaluate_assertion(_expect(type="json_path", path="$.result.values[9]", value=4), context)
    assert out_of_range.passed is False
    assert out_of_range.errored is False, "取不到值只是这条没过，不是求值不了"
    assert out_of_range.detail.startswith("path '$.result.values[9]' is not resolvable: IndexError")

    not_json = await evaluate_assertion(
        _expect(type="json_path", path="$.ok", value=True),
        AssertionContext(answer="不是 JSON"),
    )
    assert not_json.passed is False
    assert not_json.errored is False
    assert not_json.detail.startswith("answer is not valid JSON")

    bad_path = await evaluate_assertion(_expect(type="json_path", path="result.values", value=4), context)
    assert bad_path.passed is False
    assert bad_path.errored is True, "用例文件写错（缺 `$`）→ error，而不是静默 failed"
    assert bad_path.detail == "json_path must start with '$': 'result.values'"


# ----------------------------------------------------------- tool_sequence


async def test_tool_sequence_modes() -> None:
    context = AssertionContext(answer="ok", tool_calls=("kb_search", "kb_search", "calculator"))

    exact = await evaluate_assertion(
        _expect(type="tool_sequence", value=["kb_search", "kb_search", "calculator"], mode="exact"),
        context,
    )
    assert exact.passed is True
    assert exact.actual == ["kb_search", "kb_search", "calculator"]

    not_exact = await evaluate_assertion(_expect(type="tool_sequence", value=["kb_search"], mode="exact"), context)
    assert not_exact.passed is False
    assert "mode=exact" in not_exact.detail

    subset = await evaluate_assertion(
        _expect(type="tool_sequence", value=["calculator", "kb_search"], mode="contains"),
        context,
    )
    assert subset.passed is True, "contains 只看集合包含，不要求顺序"

    not_subset = await evaluate_assertion(_expect(type="tool_sequence", value=["mcp_echo"], mode="contains"), context)
    assert not_subset.passed is False

    default_mode = _expect(type="tool_sequence", value=["kb_search", "calculator"])
    assert default_mode.effective_mode == "in_order"
    in_order = await evaluate_assertion(default_mode, context)
    assert in_order.passed is True, "in_order 允许中间多调工具"

    out_of_order = await evaluate_assertion(_expect(type="tool_sequence", value=["calculator", "kb_search"]), context)
    assert out_of_order.passed is False


# ------------------------------------------------------------ max_latency


async def test_max_latency_boundary_is_inclusive() -> None:
    at_limit = await evaluate_assertion(_expect(type="max_latency", value_ms=1200), CONTEXT)
    assert at_limit.passed is True
    assert at_limit.detail == "latency 1200ms <= 1200ms"

    over = await evaluate_assertion(_expect(type="max_latency", value_ms=1199), CONTEXT)
    assert over.passed is False
    assert (over.expected, over.actual) == (1199, 1200)


# -------------------------------------------------------------- llm_judge


async def test_llm_judge_without_judge_errors_instead_of_failing() -> None:
    result = await evaluate_assertion(_expect(type="llm_judge", rubric="必须给出 72"), CONTEXT)
    assert result.passed is False
    assert result.errored is True, "没配 judge 是求值不了（error），不是模型答错（failed）"
    assert result.expected == 0.7  # DEFAULT_JUDGE_THRESHOLD
    assert result.detail == "llm_judge requires a judge model (Agent provider or --judge)"


async def test_llm_judge_threshold_score_and_cost() -> None:
    judge = ScriptedJudge(score_value=0.8)
    expectation = _expect(type="llm_judge", rubric="给出正确结果", threshold=0.6)

    hit = await evaluate_assertion(expectation, CONTEXT, judge=judge)
    assert hit.passed is True
    assert hit.errored is False
    assert hit.score == 0.8
    assert hit.cost_usd == Decimal("0.0004")
    assert hit.detail == "judge score 0.80 >= 0.60: covers the rubric"
    assert judge.calls == [("给出正确结果", ANSWER, USER_INPUT)], "rubric / answer / question 都要进 judge 提示词"

    miss = await evaluate_assertion(expectation, CONTEXT, judge=ScriptedJudge(score_value=0.4))
    assert miss.passed is False
    assert miss.errored is False
    assert miss.detail.startswith("judge score 0.40 < 0.60")
    assert miss.cost_usd == Decimal("0.0004"), "分数没过也要计成本（钱已经花了）"


async def test_llm_judge_call_failure_is_errored() -> None:
    judge = ScriptedJudge(error=ModelTimeoutError("judge timed out"))
    result = await evaluate_assertion(_expect(type="llm_judge", rubric="x"), CONTEXT, judge=judge)
    assert result.passed is False
    assert result.errored is True
    assert result.detail == "judge call failed: MODEL_TIMEOUT judge timed out"
    assert result.score is None
    assert result.cost_usd == Decimal(0)


# ------------------------------------------------------- 加载期结构校验


@pytest.mark.parametrize(
    "raw",
    [
        {"type": "contains"},  # 缺 value
        {"type": "equial", "value": "x"},  # type 拼错
        {"type": "tool_sequence", "value": []},  # 空工具序列
        {"type": "tool_sequence", "value": "calculator"},  # 不是列表
        {"type": "tool_sequence", "value": ["calculator"], "mode": "loose"},
        {"type": "equals", "value": "x", "normalize": ["utf8"]},
        {"type": "max_latency", "value_ms": 0},
        {"type": "max_latency", "value_ms": True},
        {"type": "llm_judge", "rubric": "   "},
    ],
)
def test_broken_expectation_is_rejected_at_load_time(raw: dict[str, Any]) -> None:
    with pytest.raises(ConfigInvalidError) as error:
        EvalExpectation.from_mapping(raw, where="unit")
    assert error.value.code == "CONFIG_INVALID"
    assert "unit" in str(error.value.details["where"])
