"""`llm_judge` 的 judge 实现（详细设计 4.9.3 / 4.9.4 的稳定性对策）。

- **严格 JSON 输出**：提示词要求只输出 `{"score": 0~1, "reason": "..."}`，`temperature=0`；
- **解析失败不抛异常**：`JudgeResult(parsed=False, score=0.0)`，原始输出进 `raw` 并由断言写进
  `detail` —— 报告里能直接看到"judge 没按格式回"，而不是一个空白的 0 分；
- **成本计入**：judge 调用的 `usage` / `cost_usd` 累积在 `JudgeResult` 上，Runner 汇总进
  `total_cost_usd`（4.9.3 末条），但 judge **不作为** answer 断言之外的额外判定项；
- judge 是 Protocol（`Judge`）而非固定实现：单测注入脚本化 judge，实跑由服务层按
  Agent 的 Provider 装配（4.9.4："judge 结果仅作参考，确定性断言优先"）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from app.runtime.llm.base import ChatMessage, LLMProvider
from app.runtime.llm.usage import ZERO_USAGE, TokenUsage

JUDGE_TEMPERATURE = 0.0
"""4.9.4：judge 一律 `temperature=0`（同一输入分数可复现是"参考价值"的前提）。"""

JUDGE_MAX_TOKENS = 512
"""judge 只需要一句理由 + 一个分数，给足即可（省成本）。"""

JUDGE_SYSTEM_PROMPT = (
    "You are a strict evaluation judge. Score how well the ANSWER satisfies the RUBRIC.\n"
    "Reply with ONE JSON object and nothing else, exactly in this shape:\n"
    '{"score": <number between 0 and 1>, "reason": "<one short sentence>"}\n'
    "Rules: score is a number, reason is a single sentence, no markdown, no extra keys."
)

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True, slots=True)
class JudgeResult:
    """judge 的判定（0~1 分 + 理由 + 原始输出 + 成本）。"""

    score: float
    reason: str = ""
    raw: str = ""
    parsed: bool = True
    usage: TokenUsage = ZERO_USAGE
    cost_usd: Decimal = Decimal(0)


class Judge(Protocol):
    """断言的打分器（`assertions.py` 只依赖这个 Protocol；9.2 的可替身化要求）。"""

    async def score(self, *, rubric: str, answer: str, user_input: str = "") -> JudgeResult: ...


class LLMJudge:
    """用一次 LLM 调用给 answer 打分（4.9.3 的 `llm_judge`）。"""

    def __init__(
        self,
        llm: LLMProvider,
        *,
        model: str,
        temperature: float = JUDGE_TEMPERATURE,
        max_tokens: int = JUDGE_MAX_TOKENS,
    ) -> None:
        self._llm = llm
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens

    @property
    def model(self) -> str:
        """judge 用的模型名（报告头展示）。"""
        return self._model

    async def score(self, *, rubric: str, answer: str, user_input: str = "") -> JudgeResult:
        """调一次 LLM → 解析 JSON → `JudgeResult`（解析失败仍返回结果，不抛异常）。"""
        messages = [
            ChatMessage.system(JUDGE_SYSTEM_PROMPT),
            ChatMessage.user(_prompt(rubric=rubric, answer=answer, user_input=user_input)),
        ]
        result = await self._llm.chat(
            messages,
            model=self._model,
            params={"temperature": self._temperature, "max_tokens": self._max_tokens},
        )
        raw = result.message.content or ""
        score, reason, parsed = parse_judge_output(raw)
        return JudgeResult(
            score=score,
            reason=reason,
            raw=raw,
            parsed=parsed,
            usage=result.usage,
            cost_usd=result.cost_usd,
        )


def parse_judge_output(raw: str) -> tuple[float, str, bool]:
    """从模型输出里取 `(score, reason, parsed)`；分数会被夹到 `[0, 1]`。"""
    match = _JSON_OBJECT.search(raw or "")
    if match is None:
        return 0.0, _preview(raw or "empty judge response"), False
    try:
        payload: Any = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return 0.0, _preview(raw), False
    if not isinstance(payload, dict):
        return 0.0, _preview(raw), False
    score_raw = payload.get("score")
    if isinstance(score_raw, bool) or not isinstance(score_raw, (int, float)):
        return 0.0, _preview(raw), False
    score = min(1.0, max(0.0, float(score_raw)))
    reason = str(payload.get("reason") or "").strip() or "(no reason given)"
    return score, reason, True


def _prompt(*, rubric: str, answer: str, user_input: str) -> str:
    """judge 的 user 消息：RUBRIC / QUESTION / ANSWER 三段（与 4.9.3 的 rubric 语义一致）。"""
    return (
        f"RUBRIC:\n{rubric}\n\nQUESTION:\n{user_input or '(not provided)'}\n\nANSWER:\n{answer or '(empty answer)'}\n"
    )


def _preview(text: str, limit: int = 160) -> str:
    """压成单行并截断（进 `detail` 用）。"""
    single_line = " ".join(text.split())
    return single_line if len(single_line) <= limit else f"{single_line[:limit]}…"
