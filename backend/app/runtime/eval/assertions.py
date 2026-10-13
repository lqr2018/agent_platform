"""7 类断言的实现（详细设计 4.9.3；`type` 取值见 `core/enums.py::EvalExpectType`）。

约定（照 4.9.3 逐条落地）：

- **全部执行、不短路**：`evaluate_assertions()` 永远返回与 `expected[]` 等长的结果列表，
  `detail` 里保留"期望 vs 实际"，便于定位"为什么没过"；
- **断言失败 ≠ 执行异常**：`passed=False` 只是"这条没过"（Run 仍 `succeeded`）；
  只有断言**无法求值**（judge 没配 / judge 调用失败 / YAML 里的正则非法）才 `errored=True`，
  由 Runner 记成 `outcome=error`（4.9.3 的 `failed` / `error` 区分）；
- `llm_judge` 的 judge **注入**（Protocol）而不是自己去 new 一个 LLM：单测注入脚本化 judge，
  实跑由 `services/eval_service.py` 按 Agent 的 Provider 装配（4.9.3：judge 调用计入成本）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.core.enums import EvalExpectType
from app.core.errors import AppError, ConfigInvalidError
from app.runtime.eval.judges import Judge
from app.runtime.eval.suite_source import EvalExpectation

PREVIEW_CHARS = 160
"""`detail` 里回显"实际值"的最大长度（报告是给人看的，不塞整个 answer）。"""

_JSON_PATH_TOKEN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_-]*)|\[(\d+)\]")


@dataclass(frozen=True, slots=True)
class AssertionContext:
    """一次 case 的产出快照（断言的输入；4.9.1 第 4 步"收集产出"）。"""

    answer: str = ""
    tool_calls: tuple[str, ...] = ()
    latency_ms: int = 0
    case_id: str = ""
    user_input: str = ""


@dataclass(frozen=True, slots=True)
class AssertionResult:
    """一条断言的判定结果（进报告、进 `--json`）。"""

    type: str
    passed: bool
    expected: Any = None
    actual: Any = None
    detail: str = ""
    description: str | None = None
    errored: bool = False
    """`True` = 这条断言**没能求值**（见模块 docstring），Runner 据此把 case 记成 `error`。"""

    score: float | None = None
    """`llm_judge` 的分数（其它断言为 `None`）；报告里给 judge 分数分布用。"""

    cost_usd: Decimal = Decimal(0)
    """`llm_judge` 的调用成本（4.9.3：judge 调用计入成本；其它断言恒为 0）。"""

    def as_dict(self) -> dict[str, Any]:
        """`--json` / 报告 JSON 的字段（`expected` / `actual` 已是可序列化值）。"""
        body: dict[str, Any] = {
            "type": self.type,
            "passed": self.passed,
            "expected": _jsonable(self.expected),
            "actual": _jsonable(self.actual),
            "detail": self.detail,
        }
        if self.description:
            body["description"] = self.description
        if self.errored:
            body["errored"] = True
        if self.score is not None:
            body["score"] = self.score
        if self.cost_usd:
            body["cost_usd"] = float(self.cost_usd)
        return body


async def evaluate_assertions(
    expectations: Sequence[EvalExpectation],
    context: AssertionContext,
    *,
    judge: Judge | None = None,
) -> tuple[AssertionResult, ...]:
    """逐条求值（不短路），顺序与 `expected[]` 一致。"""
    return tuple([await evaluate_assertion(item, context, judge=judge) for item in expectations])


async def evaluate_assertion(
    expectation: EvalExpectation,
    context: AssertionContext,
    *,
    judge: Judge | None = None,
) -> AssertionResult:
    """按 `type` 分派（4.9.3 的表格）；`llm_judge` 是唯一的异步分支。"""
    if expectation.type == str(EvalExpectType.LLM_JUDGE):
        return await _llm_judge(expectation, context, judge=judge)
    handler = _SYNC_ASSERTIONS.get(expectation.type)
    if handler is None:  # pragma: no cover - 加载期已校验 type（按定义的兜底分支）
        raise ConfigInvalidError(
            f"Unsupported expect type: {expectation.type!r}",
            details={"type": expectation.type, "allowed": sorted(str(item) for item in EvalExpectType)},
        )
    return handler(expectation, context)


def failed_assertions(results: Sequence[AssertionResult]) -> tuple[AssertionResult, ...]:
    """没过的断言（含 `errored`）—— 报告"失败断言"段的输入。"""
    return tuple(item for item in results if not item.passed)


def assertion_summary(result: AssertionResult) -> str:
    """一行摘要：`expected tool_sequence [search, calculator]`（对齐 4.9.2 的模板）。"""
    expected = result.expected
    if isinstance(expected, (list, tuple)):
        expected_text = "[" + ", ".join(str(item) for item in expected) + "]"
    else:
        expected_text = _preview(str(expected))
    return f"expected {result.type} {expected_text}"


def _contains(expectation: EvalExpectation, context: AssertionContext) -> AssertionResult:
    needle = str(expectation.value)
    hit = needle in context.answer
    return AssertionResult(
        type=expectation.type,
        passed=hit,
        expected=needle,
        actual=_preview(context.answer),
        detail=f"answer contains {needle!r}" if hit else f"answer does not contain {needle!r}",
        description=expectation.description,
    )


def _equals(expectation: EvalExpectation, context: AssertionContext) -> AssertionResult:
    expected = _normalize(str(expectation.value), expectation.normalize)
    actual = _normalize(context.answer, expectation.normalize)
    hit = expected == actual
    steps = f" (normalize={','.join(expectation.normalize)})" if expectation.normalize else ""
    detail = (
        f"answers are equal{steps}" if hit else f"expected {_preview(expected)!r} but got {_preview(actual)!r}{steps}"
    )
    return AssertionResult(
        type=expectation.type,
        passed=hit,
        expected=expected,
        actual=_preview(actual),
        detail=detail,
        description=expectation.description,
    )


def _regex(expectation: EvalExpectation, context: AssertionContext) -> AssertionResult:
    pattern = str(expectation.value)
    try:
        match = re.search(pattern, context.answer, flags=re.MULTILINE | re.DOTALL)
    except re.error as exc:  # YAML 里正则写错 → 记 errored，而不是静默 failed
        return AssertionResult(
            type=expectation.type,
            passed=False,
            errored=True,
            expected=pattern,
            actual=_preview(context.answer),
            detail=f"invalid regex in suite: {exc}",
            description=expectation.description,
        )
    return AssertionResult(
        type=expectation.type,
        passed=match is not None,
        expected=pattern,
        actual=_preview(match.group(0)) if match is not None else _preview(context.answer),
        detail="pattern matched" if match is not None else f"pattern {pattern!r} did not match",
        description=expectation.description,
    )


def _json_path(expectation: EvalExpectation, context: AssertionContext) -> AssertionResult:
    """`answer` 先 `json.loads`，再按简化 JSONPath（`$.a.b[0]`）取值与 `value` 比对。"""
    raw_path = expectation.path or ""
    try:
        tokens = _parse_json_path(raw_path)
    except ConfigInvalidError as exc:
        return AssertionResult(
            type=expectation.type,
            passed=False,
            errored=True,
            expected=raw_path,
            actual=_preview(context.answer),
            detail=exc.message,
            description=expectation.description,
        )
    try:
        payload = json.loads(context.answer)
    except (json.JSONDecodeError, ValueError) as exc:
        return AssertionResult(
            type=expectation.type,
            passed=False,
            expected=expectation.value,
            actual=_preview(context.answer),
            detail=f"answer is not valid JSON: {exc}",
            description=expectation.description,
        )
    try:
        actual = _resolve_json_path(payload, tokens)
    except (KeyError, IndexError, TypeError) as exc:
        return AssertionResult(
            type=expectation.type,
            passed=False,
            expected=expectation.value,
            actual=_preview(json.dumps(payload, ensure_ascii=False)),
            detail=f"path {raw_path!r} is not resolvable: {type(exc).__name__}",
            description=expectation.description,
        )
    hit = actual == expectation.value
    return AssertionResult(
        type=expectation.type,
        passed=hit,
        expected=expectation.value,
        actual=_jsonable(actual),
        detail=(
            f"{raw_path} == {expectation.value!r}"
            if hit
            else f"{raw_path} = {_preview(json.dumps(_jsonable(actual), ensure_ascii=False))} "
            f"but expected {expectation.value!r}"
        ),
        description=expectation.description,
    )


def _tool_sequence(expectation: EvalExpectation, context: AssertionContext) -> AssertionResult:
    """工具名序列比对（4.9.3：`in_order` 子序列 / `exact` 严格相等 / `contains` 集合包含）。"""
    expected = tuple(str(item) for item in expectation.value or ())
    actual = tuple(context.tool_calls)
    mode = expectation.effective_mode
    if mode == "exact":
        hit = actual == expected
        rule = "equals"
    elif mode == "contains":
        hit = set(expected).issubset(set(actual))
        rule = "is a subset of"
    else:
        hit = _is_subsequence(expected, actual)
        rule = "is an in-order subsequence of"
    detail = (
        f"tool calls {list(actual)} {rule} {list(expected)}"
        if hit
        else f"tool calls {list(actual)} do not match expected {list(expected)} (mode={mode})"
    )
    return AssertionResult(
        type=expectation.type,
        passed=hit,
        expected=list(expected),
        actual=list(actual),
        detail=detail,
        description=expectation.description,
    )


def _max_latency(expectation: EvalExpectation, context: AssertionContext) -> AssertionResult:
    limit = int(expectation.value_ms or 0)
    hit = context.latency_ms <= limit
    return AssertionResult(
        type=expectation.type,
        passed=hit,
        expected=limit,
        actual=context.latency_ms,
        detail=(
            f"latency {context.latency_ms}ms <= {limit}ms"
            if hit
            else f"latency {context.latency_ms}ms exceeds {limit}ms"
        ),
        description=expectation.description,
    )


_SYNC_ASSERTIONS: Mapping[str, Callable[[EvalExpectation, AssertionContext], AssertionResult]] = {
    str(EvalExpectType.CONTAINS): _contains,
    str(EvalExpectType.EQUALS): _equals,
    str(EvalExpectType.REGEX): _regex,
    str(EvalExpectType.JSON_PATH): _json_path,
    str(EvalExpectType.TOOL_SEQUENCE): _tool_sequence,
    str(EvalExpectType.MAX_LATENCY): _max_latency,
}
"""同步断言的分派表（`llm_judge` 走 `_llm_judge`）。"""


async def _llm_judge(
    expectation: EvalExpectation,
    context: AssertionContext,
    *,
    judge: Judge | None,
) -> AssertionResult:
    """调一次 LLM 按 rubric 打分（0~1 + 理由），`score >= threshold` 判定。"""
    if judge is None:
        return AssertionResult(
            type=expectation.type,
            passed=False,
            errored=True,
            expected=expectation.effective_threshold,
            detail="llm_judge requires a judge model (Agent provider or --judge)",
            description=expectation.description,
        )
    rubric = expectation.rubric or ""
    try:
        verdict = await judge.score(rubric=rubric, answer=context.answer, user_input=context.user_input)
    except AppError as exc:
        return AssertionResult(
            type=expectation.type,
            passed=False,
            errored=True,
            expected=expectation.effective_threshold,
            detail=f"judge call failed: {exc.code} {exc.message}",
            description=expectation.description,
        )
    threshold = expectation.effective_threshold
    hit = verdict.score >= threshold
    return AssertionResult(
        type=expectation.type,
        passed=hit,
        expected=threshold,
        actual=verdict.score,
        detail=(
            f"judge score {verdict.score:.2f} >= {threshold:.2f}: {verdict.reason}"
            if hit
            else f"judge score {verdict.score:.2f} < {threshold:.2f}: {verdict.reason}"
        ),
        description=expectation.description,
        score=verdict.score,
        cost_usd=verdict.cost_usd,
    )


def _parse_json_path(path: str) -> tuple[str | int, ...]:
    """解析 `$.a.b[0]` 形态（只支持点号取键 + 方括号取下标：够用且不引依赖）。"""
    if not path.startswith("$"):
        raise ConfigInvalidError(f"json_path must start with '$': {path!r}", details={"path": path})
    rest = path[1:]
    tokens: list[str | int] = []
    position = 0
    for match in _JSON_PATH_TOKEN.finditer(rest):
        if match.start() != position:
            raise ConfigInvalidError(f"unsupported json_path syntax: {path!r}", details={"path": path})
        key, index = match.group(1), match.group(2)
        tokens.append(key if key is not None else int(index))
        position = match.end()
    if position != len(rest) or not tokens:
        raise ConfigInvalidError(f"unsupported json_path syntax: {path!r}", details={"path": path})
    return tuple(tokens)


def _resolve_json_path(payload: Any, tokens: Sequence[str | int]) -> Any:
    """按 token 走一遍（dict 取键 / list 取下标），失败抛 `KeyError`/`IndexError`/`TypeError`。"""
    current = payload
    for token in tokens:
        if isinstance(token, int):
            if not isinstance(current, Sequence) or isinstance(current, (str, bytes)):
                raise TypeError(f"cannot index non-list with {token}")
            current = current[token]
        else:
            if not isinstance(current, dict):
                raise TypeError(f"cannot read key {token!r} from non-object")
            current = current[token]
    return current


def _is_subsequence(expected: Sequence[str], actual: Sequence[str]) -> bool:
    """`expected` 是否为 `actual` 的子序列（顺序保持一致，允许中间多调工具）。"""
    iterator = iter(actual)
    return all(any(item == want for item in iterator) for want in expected)


def _normalize(text: str, steps: Sequence[str]) -> str:
    """`equals` 的归一化：`strip` / `lower` / `collapse_ws`（按 YAML 里的顺序依次施加）。"""
    result = text
    for step in steps:
        if step == "strip":
            result = result.strip()
        elif step == "lower":
            result = result.lower()
        else:
            result = re.sub(r"\s+", " ", result)
    return result


def _preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    """压成单行并截断（报告可读性）。"""
    single_line = " ".join(text.split())
    return single_line if len(single_line) <= limit else f"{single_line[:limit]}…"


def _jsonable(value: Any) -> Any:
    """把值收成可 JSON 序列化的形态（报告 JSON 用）。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)
