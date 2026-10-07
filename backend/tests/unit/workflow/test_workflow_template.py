"""模板求值单测（详细设计 4.5.2 / 7.4 测试要求：未定义字段、嵌套 path、白名单外函数被拒）。"""

from __future__ import annotations

from typing import Any

import pytest

from app.runtime.workflow import state as state_module
from app.runtime.workflow import template as template_module


def context(**state: Any) -> template_module.TemplateContext:
    """把业务字段包成引擎里的完整 state，再拆出三个根。"""
    return template_module.TemplateContext.from_state(state_module.initial_state(state, run={"run_id": "run-1"}))


def render(text: str, **state: Any) -> Any:
    return template_module.render_text(text, context(**state))


def test_single_placeholder_returns_native_value() -> None:
    assert render("{{state.count}}", count=3) == 3
    assert render("{{state.flag}}", flag=True) is True
    assert render("{{state.items}}", items=[1, 2]) == [1, 2]
    assert render("{{state.plan}}", plan="") == ""


def test_interpolation_and_nested_paths() -> None:
    state = state_module.initial_state({"plan": "P1"}, run={"run_id": "run-1"})
    state_module.set_node_output(state, "search", "hits-1", output_key="hits")
    ctx = template_module.TemplateContext.from_state(state)
    assert template_module.render_text("plan={{state.plan}} hits={{nodes.search.output}}", ctx) == "plan=P1 hits=hits-1"
    assert template_module.render_text("{{nodes.search.output[0]}}", ctx) == "h"
    assert template_module.render_text("{{run.run_id}}", ctx) == "run-1"


def test_scalar_literals_and_arithmetic() -> None:
    assert render("{{1 + 2 * 3}}") == 7
    assert render("{{'a' + 'b'}}") == "ab"
    assert render("{{-state.count if state.count else 0}}", count=5) == -5
    assert render("{{not state.flag}}", flag=False) is True
    assert render("{{[1, 2, 3][2]}}") == 3
    assert render("{{json.dumps({'a': 1})}}") == '{"a": 1}'


def test_undefined_fields_render_as_empty_string_with_warning() -> None:
    """4.5.2：未定义字段 → 空串并记一条 warning（由引擎写进 span attributes）。"""
    warnings: list[str] = []
    ctx = context()
    assert template_module.render_text("{{state.missing}}", ctx, warnings=warnings) == ""
    assert template_module.render_text("{{nodes.ghost.output}}", ctx, warnings=warnings) == ""
    assert template_module.render_text("{{state.missing}}/{{state.also_missing}}", ctx, warnings=warnings) == "/"
    assert any("undefined field: missing" in item for item in warnings)


def test_undefined_fields_degrade_gracefully_in_functions() -> None:
    """`len(state.hits) > 0` 在 hits 缺失时退化为 `0 > 0`（不抛异常）。"""
    warnings: list[str] = []
    ctx = context()
    assert template_module.evaluate_condition("len(state.hits) > 0", ctx, warnings=warnings) is False
    assert template_module.evaluate_condition("len(state.hits) > 0", context(hits=["a"])) is True
    assert template_module.render_text("{{len(state.hits)}}", ctx, warnings=warnings) == 0


def test_whitelist_functions() -> None:
    assert render("{{len(state.items)}}", items=[1, 2, 3]) == 3
    assert render("{{str(state.count)}}", count=7) == "7"
    assert render("{{join(state.items, '-')}}", items=["a", "b"]) == "a-b"
    assert render("{{json.dumps(state.items)}}", items=[1, 2]) == "[1, 2]"


@pytest.mark.parametrize(
    "expression",
    [
        pytest.param("state.plan.upper()", id="method-call"),
        pytest.param("open('file')", id="builtin"),
        pytest.param("payload.query", id="unknown-root"),
        pytest.param("(lambda: 1)()", id="lambda"),
        pytest.param("[x for x in state.items]", id="comprehension"),
        pytest.param("{**state}", id="dict-unpack"),
        pytest.param("state.__class__", id="dunder"),
        pytest.param("eval(state.plan)", id="eval"),
    ],
)
def test_non_whitelisted_expressions_are_rejected(expression: str) -> None:
    """白名单外的函数 / 未知根 / 危险语法一律拒绝（禁 `eval`，4.5.2）。"""
    assert template_module.check_expression(expression) is not None
    with pytest.raises(template_module.TemplateSyntaxError):
        template_module.render_text("{{" + expression + "}}", context(plan="p", items=[1], payload={}))


def test_attribute_access_on_scalars_is_rejected() -> None:
    with pytest.raises(template_module.TemplateSyntaxError):
        render("{{state.count.value}}", count=1)


def test_conditions_compare_in_and_or() -> None:
    assert template_module.evaluate_condition("state.a and state.b", context(a=1, b=2)) is True
    assert template_module.evaluate_condition("state.a or state.b", context(a=0, b=0)) is False
    assert template_module.evaluate_condition("state.mode in ['fast', 'slow']", context(mode="fast")) is True
    assert template_module.evaluate_condition("state.a == '1'", context(a=1)) is False


def test_mismatched_comparison_records_warning_and_is_false() -> None:
    warnings: list[str] = []
    assert template_module.evaluate_condition("state.a > state.b", context(a=1, b="x"), warnings=warnings) is False
    assert any("mismatched types" in item for item in warnings)


def test_arguments_template_renders_recursively() -> None:
    ctx = context(query="qdrant")
    rendered = template_module.render_value({"query": "{{state.query}}", "limit": 3, "tags": ["{{state.query}}"]}, ctx)
    assert rendered == {"query": "qdrant", "limit": 3, "tags": ["qdrant"]}


def test_check_value_reports_nested_problems() -> None:
    problems = template_module.check_value({"query": "{{state.query}}", "bad": ["{{eval(x)}}"]})
    assert len(problems) == 1
    assert "bad.[0]" in problems[0]


def test_check_text_detects_unterminated_placeholder() -> None:
    assert template_module.check_text("{{state.plan") == ["unterminated template placeholder"]
    assert template_module.check_text("plain text") == []


def test_normalize_expression_accepts_both_forms() -> None:
    assert template_module.normalize_expression("len(state.hits) > 0") == "len(state.hits) > 0"
    assert template_module.normalize_expression("{{ len(state.hits) > 0 }}") == " len(state.hits) > 0 "
