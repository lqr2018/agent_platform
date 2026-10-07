"""受限模板求值（详细设计 4.5.2 / 2.9）。

只支持 `{{ path.to.value }}` 与**白名单只读函数**（`len` / `str` / `join` / `json.dumps`）：

- **不使用 `eval` / `exec`**：表达式用 `ast.parse(mode="eval")` 解析后由本模块的迷你解释器执行，
  非白名单节点（lambda、推导式、导入、`getattr` 等）一律拒绝；
- 命名空间只有三个根：`state.<业务字段>`、`nodes.<node_id>.output`、`run.<run_id>`（4.5.2）；
- **未定义字段 → 空串并记一条 warning**（由调用方写进 span attributes），
  `_Undefined` 实现了 `__len__` / `__bool__` / `__str__` / `__getitem__`，
  因此 `len(state.hits) > 0` 这类表达式在字段缺失时优雅退化为 `0 > 0`；
- 整段模板恰好是一个 `{{ ... }}` 时返回**原生值**（int / bool / list），
  其余情况按字符串插值（`{{a}}-{{b}}` → `"1-2"`）。
"""

from __future__ import annotations

import ast
import json
import operator
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.runtime.workflow import state as state_module

TOKEN_PATTERN = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
"""模板占位：`{{ 表达式 }}`（不支持嵌套大括号）。"""


class TemplateSyntaxError(ValueError):
    """模板不被支持（非白名单函数 / 未知根 / 不允许的语法）。"""


class _Undefined:
    """未定义字段的哨兵：等价于空串、空序列、假值。"""

    __slots__ = ()

    def __len__(self) -> int:
        return 0

    def __bool__(self) -> bool:
        return False

    def __str__(self) -> str:
        return ""

    def __iter__(self) -> Iterator[Any]:
        return iter(())

    def __getitem__(self, _key: Any) -> _Undefined:
        return self

    def __getattr__(self, _name: str) -> _Undefined:
        return self

    def __eq__(self, other: object) -> bool:
        return other is UNDEFINED or other is None or other == ""

    def __hash__(self) -> int:
        return hash("<undefined>")

    def __add__(self, other: Any) -> _Undefined:
        return self

    def __radd__(self, other: Any) -> _Undefined:
        return self

    def __repr__(self) -> str:
        return "<undefined>"


UNDEFINED = _Undefined()


def _join(sequence: Any, separator: str = "") -> str:
    """`join(seq, sep="")`：与 `str.join` 同名同义，但入参顺序对模板作者更直观。"""
    if isinstance(sequence, (str, bytes)) or not isinstance(sequence, Sequence):
        return str(sequence)
    return str(separator).join(str(item) for item in sequence)


def _json_dumps(value: Any, *, ensure_ascii: bool = False, indent: int | None = None) -> str:
    """`json.dumps(value, ensure_ascii=False)` 的**白名单包装**（关键字受限）。"""
    return json.dumps(value, ensure_ascii=ensure_ascii, indent=indent, default=str)


json_dumps = _json_dumps
"""公开别名：节点执行器（`nodes._as_text`）把结构化入参转文本时复用同一实现。"""


WHITELIST_FUNCTIONS: Mapping[str, Callable[..., Any]] = {
    "len": len,
    "str": str,
    "join": _join,
    "json.dumps": _json_dumps,
}
"""白名单只读函数（4.5.2）；名字是**点号形式**，解释器按 `_dotted_name` 还原后查表。"""

ROOT_NAMES: frozenset[str] = frozenset({"state", "nodes", "run"})
"""模板只允许这三个根（4.5.2 的命名空间约定）。"""

_BIN_OPS: Mapping[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_COMPARE_OPS: Mapping[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda left, right: left in right,
    ast.NotIn: lambda left, right: left not in right,
}


@dataclass(frozen=True, slots=True)
class TemplateContext:
    """模板可见的三个命名空间（4.5.2）。"""

    state: Mapping[str, Any] = field(default_factory=dict)
    nodes: Mapping[str, Any] = field(default_factory=dict)
    run: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_state(cls, state: Mapping[str, Any]) -> TemplateContext:
        """从引擎维护的 state 拆出三个根（业务字段 / nodes / run）。"""
        return cls(
            state=state_module.business_fields(state),
            nodes=state_module.node_scope_view(state),
            run=state_module.run_scope(state),
        )

    def namespaces(self) -> dict[str, Any]:
        return {"state": self.state, "nodes": self.nodes, "run": self.run}


def _dotted_name(node: ast.AST) -> str | None:
    """`Name` / `Attribute` 还原为点号名（`json.dumps` → `"json.dumps"`；其它 → `None`）。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _guard_numeric(node: ast.AST, value: Any) -> Any:
    """一元 `+` / `-` 只接受数值，避免模板作者写出 `-state.text` 这类无意义表达式。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, _Undefined):
        return value
    raise TemplateSyntaxError(f"unary operator expects a number, got {type(value).__name__}")


class _Interpreter:
    """迷你解释器：只处理只读表达式（4.5.2），非白名单节点直接报错。"""

    def __init__(self, context: TemplateContext, warnings: list[str] | None = None) -> None:
        self._namespaces = context.namespaces()
        self._warnings = warnings if warnings is not None else []

    # ---- 入口 ----
    def evaluate(self, expression: str) -> Any:
        try:
            parsed = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise TemplateSyntaxError(f"invalid expression syntax: {exc.msg}") from exc
        return self._eval(parsed.body)

    # ---- 求值 ----
    def _eval(self, node: ast.AST) -> Any:
        method = getattr(self, f"_eval_{type(node).__name__.lower()}", None)
        if method is None:
            raise TemplateSyntaxError(f"unsupported expression: {type(node).__name__}")
        return method(node)

    def _eval_constant(self, node: ast.Constant) -> Any:
        return node.value

    def _eval_name(self, node: ast.Name) -> Any:
        if node.id not in ROOT_NAMES:
            raise TemplateSyntaxError(f"unknown root name '{node.id}' (allowed: state/nodes/run)")
        return self._namespaces[node.id]

    def _eval_attribute(self, node: ast.Attribute) -> Any:
        dotted = _dotted_name(node)
        if dotted is not None and dotted in WHITELIST_FUNCTIONS:
            return WHITELIST_FUNCTIONS[dotted]
        if node.attr.startswith("_"):
            raise TemplateSyntaxError(f"attribute '{node.attr}' is not readable")
        return self._lookup(self._eval(node.value), node.attr)

    def _eval_subscript(self, node: ast.Subscript) -> Any:
        container = self._eval(node.value)
        key = self._eval(node.slice)
        if isinstance(container, _Undefined):
            return container
        if isinstance(container, Mapping):
            return container.get(key, UNDEFINED)
        if isinstance(container, (list, tuple, str)) and isinstance(key, int) and not isinstance(key, bool):
            try:
                return container[key]
            except IndexError:
                return UNDEFINED
        raise TemplateSyntaxError(f"unsupported subscript on {type(container).__name__}")

    def _eval_call(self, node: ast.Call) -> Any:
        name = _dotted_name(node.func)
        func = WHITELIST_FUNCTIONS.get(name) if name else None
        if func is None:
            raise TemplateSyntaxError(f"function '{name or '<expression>'}' is not in the read-only whitelist")
        if any(keyword.arg is None for keyword in node.keywords):
            raise TemplateSyntaxError("`**kwargs` is not allowed in templates")
        args = [self._eval(argument) for argument in node.args]
        kwargs = {keyword.arg: self._eval(keyword.value) for keyword in node.keywords if keyword.arg}
        try:
            return func(*args, **kwargs)
        except TemplateSyntaxError:
            raise
        except TypeError as exc:
            raise TemplateSyntaxError(f"{name}(): {exc}") from exc

    def _eval_boolop(self, node: ast.BoolOp) -> Any:
        """`and` / `or` 短路求值（返回操作数本身，与 Python 语义一致）。"""
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in node.values:
                result = self._eval(value)
                if not result:
                    return result
            return result
        result = False
        for value in node.values:
            result = self._eval(value)
            if result:
                return result
        return result

    def _eval_unaryop(self, node: ast.UnaryOp) -> Any:
        operand = self._eval(node.operand)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return _guard_numeric(node, -operand if not isinstance(operand, _Undefined) else operand)
        if isinstance(node.op, ast.UAdd):
            return _guard_numeric(node, +operand if not isinstance(operand, _Undefined) else operand)
        raise TemplateSyntaxError(f"unsupported unary operator: {type(node.op).__name__}")

    def _eval_binop(self, node: ast.BinOp) -> Any:
        handler = _BIN_OPS.get(type(node.op))
        if handler is None:
            raise TemplateSyntaxError(f"unsupported binary operator: {type(node.op).__name__}")
        left = self._eval(node.left)
        right = self._eval(node.right)
        if isinstance(left, _Undefined) or isinstance(right, _Undefined):
            return UNDEFINED
        try:
            return handler(left, right)
        except TypeError as exc:
            raise TemplateSyntaxError(f"operator {type(node.op).__name__} failed: {exc}") from exc

    def _eval_compare(self, node: ast.Compare) -> bool:
        left = self._eval(node.left)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            handler = _COMPARE_OPS.get(type(op))
            if handler is None:
                raise TemplateSyntaxError(f"unsupported comparison: {type(op).__name__}")
            right = self._eval(comparator)
            try:
                if not handler(left, right):
                    return False
            except TypeError:
                # 类型不匹配（如 int 与 str）视为"条件不成立"，与"未定义字段"同向退化
                self._warnings.append(
                    f"comparison skipped for mismatched types: {type(left).__name__}/{type(right).__name__}"
                )
                return False
            left = right
        return True

    def _eval_ifexp(self, node: ast.IfExp) -> Any:
        return self._eval(node.body) if self._eval(node.test) else self._eval(node.orelse)

    def _eval_list(self, node: ast.List) -> list[Any]:
        return [self._eval(element) for element in node.elts]

    def _eval_tuple(self, node: ast.Tuple) -> tuple[Any, ...]:
        return tuple(self._eval(element) for element in node.elts)

    def _eval_dict(self, node: ast.Dict) -> dict[Any, Any]:
        result: dict[Any, Any] = {}
        for key, value in zip(node.keys, node.values, strict=True):
            if key is None:
                raise TemplateSyntaxError("`**` unpacking is not allowed in templates")
            result[self._eval(key)] = self._eval(value)
        return result

    # ---- 内部 ----
    def _lookup(self, parent: Any, attr: str) -> Any:
        """`state.topic` / `nodes.search.output`：Mapping 走键，其余类型只允许映射式读取。"""
        if isinstance(parent, _Undefined):
            return parent
        if isinstance(parent, Mapping):
            if attr in parent:
                return parent[attr]
            self._warnings.append(f"undefined field: {attr}")
            return UNDEFINED
        raise TemplateSyntaxError(f"attribute access is not supported on {type(parent).__name__}")


# --------------------------------------------------------------------------------------
# 对外 API（引擎 / 图校验 / 节点执行器共用）
# --------------------------------------------------------------------------------------
def _native(value: Any) -> Any:
    """整段模板的原生值：未定义 / None → 空串（4.5.2）。"""
    return "" if isinstance(value, _Undefined) or value is None else value


def _stringify(value: Any) -> str:
    return "" if isinstance(value, _Undefined) or value is None else str(value)


def render_text(text: str, context: TemplateContext, *, warnings: list[str] | None = None) -> Any:
    """渲染一段模板。

    - 整段恰好是一个 `{{ expr }}` → 返回**原生值**（供 `input_template` 传 int/bool/list）；
    - 其它情况 → 字符串插值（未定义字段渲染成空串）。
    """
    matches = list(TOKEN_PATTERN.finditer(text))
    if not matches:
        return text
    if len(matches) == 1 and matches[0].start() == 0 and matches[0].end() == len(text):
        return _native(_Interpreter(context, warnings).evaluate(matches[0].group(1)))
    parts: list[str] = []
    cursor = 0
    for match in matches:
        parts.append(text[cursor : match.start()])
        parts.append(_stringify(_Interpreter(context, warnings).evaluate(match.group(1))))
        cursor = match.end()
    parts.append(text[cursor:])
    return "".join(parts)


def render_value(value: Any, context: TemplateContext, *, warnings: list[str] | None = None) -> Any:
    """递归渲染（`arguments_template` 的 dict / list 走这里）。"""
    if isinstance(value, str):
        return render_text(value, context, warnings=warnings)
    if isinstance(value, Mapping):
        return {key: render_value(item, context, warnings=warnings) for key, item in value.items()}
    if isinstance(value, list):
        return [render_value(item, context, warnings=warnings) for item in value]
    if isinstance(value, tuple):
        return tuple(render_value(item, context, warnings=warnings) for item in value)
    return value


def evaluate_condition(expression: str, context: TemplateContext, *, warnings: list[str] | None = None) -> bool:
    """`condition` 节点的 `when` 求值（结果按真值判断）。"""
    return bool(_Interpreter(context, warnings).evaluate(expression))


def to_jsonable(value: Any) -> Any:
    """模板 / 节点产物 → 可写进 JSON 列的值（4.5.3 的落库前置处理）。

    - `_Undefined` → `""`（与"未定义字段渲染成空串"一致），`None` 原样；
    - `Decimal` → `float`、`datetime`/`date` → ISO 字符串；
    - Mapping / list / tuple / set 递归；其余原样（`str` / `int` / `float` / `bool`）。
    """
    if isinstance(value, _Undefined):
        return ""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return _to_jsonable_complex(value)


def _to_jsonable_complex(value: Any) -> Any:
    """复杂类型分支（拆出来只为让 `to_jsonable` 的分支数与返回点数保持可读）。"""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    return str(value)


def normalize_expression(text: str) -> str:
    """`condition.when` 允许两种写法：裸表达式（4.5.2 的 `len(state.hits) > 0`）或 `{{ ... }}`。"""
    stripped = text.strip()
    match = TOKEN_PATTERN.fullmatch(stripped)
    return match.group(1) if match else stripped


def check_expression(expression: str) -> str | None:
    """静态检查一个表达式：返回错误消息（合法 → `None`）。

    图校验（4.5.1）在**保存时**就跑一遍：空命名空间下求值，凡"未知根 / 非白名单函数 /
    不支持的语法"都会在这里暴露，避免运行期才发现模板写错。
    """
    try:
        _Interpreter(TemplateContext()).evaluate(expression)
    except TemplateSyntaxError as exc:
        return str(exc)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def check_text(text: str) -> list[str]:
    """静态检查一段模板文本（每个 `{{ }}` 占位各查一次）。"""
    problems: list[str] = []
    matches = list(TOKEN_PATTERN.finditer(text))
    if "{{" in text and not matches:
        problems.append("unterminated template placeholder")
    for match in matches:
        error = check_expression(match.group(1))
        if error:
            problems.append(f"{{{{{match.group(1).strip()}}}}}: {error}")
    return problems


def check_value(value: Any) -> list[str]:
    """静态检查模板值（str / list / dict 递归）。"""
    if isinstance(value, str):
        return check_text(value)
    if isinstance(value, Mapping):
        problems: list[str] = []
        for key, item in value.items():
            problems.extend(f"{key}.{problem}" if problem else problem for problem in check_value(item) if problem)
        return problems
    if isinstance(value, (list, tuple)):
        problems = []
        for index, item in enumerate(value):
            problems.extend(f"[{index}] {problem}" for problem in check_value(item))
        return problems
    return []
