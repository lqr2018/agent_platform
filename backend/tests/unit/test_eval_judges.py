"""`llm_judge` 的 judge 契约（详细设计 4.9.3 / 4.9.4；`app/runtime/eval/judges.py`）。

两个 DoD：

- judge **必须** `temperature=0` + 严格 JSON 提示词（4.9.4：分数可复现才叫"参考"）；
- 解析失败**不抛异常**，而是 `parsed=False, score=0.0` + 原始输出进 `raw`
  （报告里要看得到"judge 没按格式回"，而不是一个无解释的 0 分）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.core.errors import ModelTimeoutError
from app.runtime.eval import LLMJudge, parse_judge_output
from app.runtime.llm.base import ChatMessage, LLMResult, ProviderConfig
from app.runtime.llm.fake import FakeLLMProvider
from app.runtime.llm.usage import TokenUsage


def _provider() -> FakeLLMProvider:
    return FakeLLMProvider(ProviderConfig(id="p1", name="fake", kind="fake", base_url="fake://"))


class _ScriptedProvider:
    """只回一条固定内容的 LLM（覆盖 judge 的解析成功路径，不碰真实网络）。"""

    name = "scripted"

    def __init__(self, content: str, *, error: Exception | None = None) -> None:
        self._content = content
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        params: Any = None,
        **_: Any,
    ) -> LLMResult:
        self.calls.append({"messages": messages, "model": model, "params": params})
        if self._error is not None:
            raise self._error
        return LLMResult(
            message=ChatMessage.assistant(self._content),
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=11, completion_tokens=3),
            model_name=model,
            latency_ms=7,
            cost_usd=Decimal("0.0007"),
        )


def test_parse_judge_output_accepts_plain_and_fenced_json() -> None:
    assert parse_judge_output('{"score": 0.8, "reason": "good"}') == (0.8, "good", True)
    fenced = '```json\n{"score": 0.25, "reason": "partly"}\n```'
    assert parse_judge_output(fenced) == (0.25, "partly", True)
    assert parse_judge_output('{"score": 1, "reason": ""}') == (1.0, "(no reason given)", True)


def test_parse_judge_output_clamps_and_rejects_bad_scores() -> None:
    assert parse_judge_output('{"score": 1.5, "reason": "too generous"}')[0] == 1.0
    assert parse_judge_output('{"score": -0.2, "reason": "harsh"}')[0] == 0.0
    assert parse_judge_output('{"score": true, "reason": "bool" }')[2] is False, "布尔不是分数"
    assert parse_judge_output('{"reason": "no score"}')[2] is False
    assert parse_judge_output("not json at all")[0] == 0.0
    assert parse_judge_output("not json at all")[2] is False
    assert parse_judge_output("")[1] == "empty judge response"


async def test_llm_judge_parses_scripted_json_and_counts_cost() -> None:
    provider = _ScriptedProvider('{"score": 0.9, "reason": "satisfies the rubric"}')
    judge = LLMJudge(provider, model="judge-model")  # type: ignore[arg-type]

    result = await judge.score(rubric="必须给出 72", answer="72", user_input="12 * (3 + 3)")

    assert (result.score, result.reason, result.parsed) == (0.9, "satisfies the rubric", True)
    assert result.cost_usd == Decimal("0.0007")
    assert result.usage.total_tokens == 14
    assert judge.model == "judge-model"

    call = provider.calls[0]
    assert call["model"] == "judge-model"
    assert call["params"] == {"temperature": 0.0, "max_tokens": 512}, "4.9.4：judge 必须 temperature=0"
    system, user = call["messages"]
    assert system.role == "system" and "JSON" in system.content
    assert "必须给出 72" in user.content and "ANSWER:\n72" in user.content


async def test_llm_judge_unparsable_response_does_not_raise() -> None:
    """fake Provider 回的是 `FAKE_RESPONSE: ...`（不是 JSON）→ 0 分 + `parsed=False`，异常不外抛。"""
    judge = LLMJudge(_provider(), model="fake-model")
    result = await judge.score(rubric="随便", answer="答案")

    assert result.parsed is False
    assert result.score == 0.0
    assert result.raw.startswith("FAKE_RESPONSE:")
    assert result.reason, "原始输出要留在 reason/raw 里，报告才能解释为什么是 0 分"


async def test_llm_judge_propagates_app_error() -> None:
    """judge 调用失败要抛给断言层（那边记 `errored`），而不是吞成 0 分。"""
    provider = _ScriptedProvider("", error=ModelTimeoutError("judge timed out"))
    judge = LLMJudge(provider, model="judge-model")  # type: ignore[arg-type]

    with pytest.raises(ModelTimeoutError):
        await judge.score(rubric="r", answer="a")
