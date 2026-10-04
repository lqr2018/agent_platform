"""`calculator` 内置工具（详细设计 2.5 / 4.2.4）。

- 白名单运算 `+ - * / ** // %` 与一元正负号；
- 白名单数学函数与常量（`MATH_FUNCTIONS` / `MATH_CONSTANTS`）；
- 用 `ast` 解析 + **自带求值器**（绝不 `eval`）：属性访问、下标、lambda、推导式、变量等
  一律拒绝，从语法层面断掉 `__class__` 之类的绕过（4.2.4 "拒 `__` 与属性访问"）；
- 指数与操作数上限（4.2.4 "指数上限"），避免 `9**9**9` 拖死进程。
"""

from __future__ import annotations

import ast
import math
import operator
from collections.abc import Callable, Mapping
from typing import Any

from app.core.enums import PermissionLevel
from app.core.errors import ToolExecutionFailedError, ToolInvalidArgumentsError
from app.runtime.tools.base import BaseTool, ToolContext, ToolPermissionConfig, ToolResult

MAX_EXPRESSION_CHARS = 500
MAX_OPERAND = 1e12
MAX_EXPONENT = 1_000

MATH_FUNCTIONS: Mapping[str, Callable[..., Any]] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "pow": pow,
    "sqrt": math.sqrt,
    "exp": math.exp,
    "log": math.log,
    "log10": math.log10,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "floor": math.floor,
    "ceil": math.ceil,
}
MATH_CONSTANTS: Mapping[str, float] = {"pi": math.pi, "e": math.e, "tau": math.tau}

_BINARY_OPS: Mapping[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


class _UnsafeExpression(Exception):
    """表达式越出白名单（统一转 `TOOL_INVALID_ARGUMENTS` 回填给 LLM）。"""


class CalculatorTool(BaseTool):
    """4.2.4 的 `calculator`。"""

    name = "calculator"
    display_name = "计算器"
    description = (
        "计算数学表达式并返回结果。只支持算术运算（+ - * / ** // %）与常用数学函数"
        "（sqrt/log/exp/sin/cos/tan/floor/ceil/abs/round/min/max/pow）以及常量 pi/e/tau；"
        "不支持变量、字符串、文件或网络访问。"
    )
    input_schema: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "要计算的数学表达式，例如 (12+3)*4/sqrt(9)",
                "minLength": 1,
                "maxLength": MAX_EXPRESSION_CHARS,
            }
        },
        "required": ["expression"],
        "additionalProperties": False,
    }
    output_schema: Mapping[str, Any] = {"type": "object", "properties": {"result": {"type": "number"}}}
    default_permission = ToolPermissionConfig(
        level=PermissionLevel.SAFE,
        timeout_seconds=5.0,
        max_output_bytes=65_536,
        max_calls_per_run=50,
    )

    async def run(self, ctx: ToolContext, *, expression: str = "", **_: Any) -> ToolResult:
        text = (expression or "").strip()
        if not text:
            raise ToolInvalidArgumentsError("`expression` must not be empty", details={"expression": expression})
        if len(text) > MAX_EXPRESSION_CHARS:
            raise ToolInvalidArgumentsError(
                f"`expression` must be at most {MAX_EXPRESSION_CHARS} characters",
                details={"length": len(text)},
            )
        try:
            tree = ast.parse(text, mode="eval")
        except SyntaxError as exc:
            raise ToolInvalidArgumentsError(f"Invalid expression: {exc.msg}", details={"expression": text}) from exc

        try:
            value = _eval_node(tree)
        except _UnsafeExpression as exc:
            raise ToolInvalidArgumentsError(
                f"Unsupported or unsafe expression: {exc}", details={"expression": text}
            ) from exc
        except ZeroDivisionError as exc:
            raise ToolExecutionFailedError("Division by zero", details={"expression": text}) from exc
        except (OverflowError, ValueError) as exc:
            raise ToolExecutionFailedError(f"Math error: {exc}", details={"expression": text}) from exc

        if isinstance(value, float) and (math.isinf(value) or math.isnan(value)):
            raise ToolExecutionFailedError("Result is not a finite number", details={"expression": text})
        return ToolResult(content=f"{text} = {_format_number(value)}", meta={"result": value})


def _eval_node(node: ast.AST) -> Any:
    """白名单求值器（只认识数字、四则运算、白名单函数与常量）。"""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise _UnsafeExpression("only numbers are allowed")
        if abs(node.value) > MAX_OPERAND:
            raise _UnsafeExpression("operand is too large")
        return node.value

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_node(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value

    if isinstance(node, ast.BinOp):
        func = _BINARY_OPS.get(type(node.op))
        if func is None:
            raise _UnsafeExpression(f"operator {type(node.op).__name__} is not allowed")
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if isinstance(node.op, ast.Pow):
            if not isinstance(right, (int, float)) or abs(right) > MAX_EXPONENT:
                raise _UnsafeExpression(f"exponent must be <= {MAX_EXPONENT}")
            if left != 0 and abs(left) > 1 and right > 0 and abs(right) * math.log10(abs(left)) > 100:
                raise _UnsafeExpression("result would be too large")
        return func(left, right)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in MATH_FUNCTIONS:
            raise _UnsafeExpression("only whitelisted math functions can be called")
        if node.keywords:
            raise _UnsafeExpression("keyword arguments are not supported")
        if len(node.args) > 3:
            raise _UnsafeExpression("too many arguments")
        args = [_eval_node(argument) for argument in node.args]
        return MATH_FUNCTIONS[node.func.id](*args)

    if isinstance(node, ast.Name):
        if node.id in MATH_CONSTANTS:
            return MATH_CONSTANTS[node.id]
        raise _UnsafeExpression(f"unknown name {node.id!r}")

    raise _UnsafeExpression(f"syntax {type(node).__name__} is not allowed")


def _format_number(value: Any) -> str:
    """整数原样输出；浮点去掉多余尾零（便于 LLM 与前端展示）。"""
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e16:
        return str(int(value))
    return f"{value:g}" if isinstance(value, float) else str(value)
