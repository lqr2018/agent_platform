"""评测用例集（详细设计 4.9.1 / 4.9.3；SD-18：MVP 的用例来自 YAML 文件）。

- `EvalSuite` / `EvalCase` / `EvalExpectation` 是**纯数据**（runtime 不 import ORM、不碰 DB，1.2）；
- `YamlSuiteSource` 读 `configs/eval/{name}.yaml`；Backlog 迭代 D 的 `DbSuiteSource`
  只要实现 `SuiteSource.load()`，Runner / 指标 / 断言 / 报告**一行不改**（4.9.1）；
- 用例文件写错（字段缺失、`type` 未知、`expected` 为空）一律在**加载期**报
  `ConfigInvalidError`：否则报告里的 `error` 分不清是"模型跑挂了"还是"文件写错了"。

YAML 形状（`cases[].expected[]` 的字段与 4.9.3 的断言语义一一对应）::

    name: demo-suite                 # 用例集名（报告头 Suite 列）
    agent: research-agent            # 可选：Agent id 或 name（命令行 --agent 优先）
    description: 三个 Demo Agent 的主路径用例
    cases:
      - id: task-1-calculator        # 唯一、进报告与 conversation 标题
        input: 计算 12 * (3 + 3)
        weight: 1.0                  # 默认 1.0
        enabled: true                # 默认 true；false = "先留着但不跑"
        timeout_seconds: 120         # 可选，默认 EvalRunner.default_timeout_seconds
        expected:
          - type: tool_sequence
            value: [calculator]
            mode: exact
          - type: contains
            value: "12 * (3 + 3) = 72"
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

import yaml

from app.core.enums import EvalExpectType
from app.core.errors import ConfigInvalidError

DEFAULT_SUITE_DIR = Path(__file__).resolve().parents[3] / "configs" / "eval"
"""`backend/configs/eval`（`app/runtime/eval/suite_source.py` 的 `parents[3]` = `backend/`）。"""

DEFAULT_CASE_WEIGHT = 1.0
DEFAULT_TOOL_SEQUENCE_MODE = "in_order"
TOOL_SEQUENCE_MODES: tuple[str, ...] = ("in_order", "exact", "contains")
"""4.9.3：`in_order` = 期望序列是实际的**子序列**；`exact` = 严格相等；`contains` = 集合包含。"""

NORMALIZE_MODES: tuple[str, ...] = ("strip", "lower", "collapse_ws")
DEFAULT_JUDGE_THRESHOLD = 0.7
"""`llm_judge` 的默认通过阈值（4.9.3：`score >= threshold`）。"""

_VALUE_TYPES = frozenset(
    {
        str(EvalExpectType.CONTAINS),
        str(EvalExpectType.REGEX),
        str(EvalExpectType.EQUALS),
        str(EvalExpectType.JSON_PATH),
    }
)


@dataclass(frozen=True, slots=True)
class EvalExpectation:
    """`expected[]` 的一条断言（4.9.3 的七种 `type`）。"""

    type: str
    value: Any = None
    """`contains` / `equals` / `regex` / `json_path` 的期望值；`tool_sequence` 为工具名列表。"""

    mode: str | None = None
    """`tool_sequence` 的比对模式（见 `TOOL_SEQUENCE_MODES`）。"""

    normalize: tuple[str, ...] = ()
    """`equals` 的归一化步骤（见 `NORMALIZE_MODES`）。"""

    value_ms: int | None = None
    """`max_latency` 的上限（毫秒）。"""

    path: str | None = None
    """`json_path` 的简化 JSONPath（如 `$.a.b[0]`）。"""

    rubric: str | None = None
    """`llm_judge` 的评分标准（写死在用例里，4.9.4 的稳定性要求）。"""

    threshold: float | None = None
    """`llm_judge` 的通过阈值（默认 `DEFAULT_JUDGE_THRESHOLD`）。"""

    description: str | None = None
    """人读说明（原样进断言结果，便于定位"为什么没过"）。"""

    @property
    def effective_mode(self) -> str:
        """`tool_sequence` 的生效模式。"""
        return self.mode or DEFAULT_TOOL_SEQUENCE_MODE

    @property
    def effective_threshold(self) -> float:
        """`llm_judge` 的生效阈值。"""
        return self.threshold if self.threshold is not None else DEFAULT_JUDGE_THRESHOLD

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, where: str) -> EvalExpectation:
        """校验并构造一条断言；任何结构问题 → `CONFIG_INVALID`（加载期失败）。"""
        expect_type = str(raw.get("type") or "").strip()
        if expect_type not in {str(item) for item in EvalExpectType}:
            raise ConfigInvalidError(
                f"{where}: unknown expect type {expect_type!r}",
                details={"where": where, "type": expect_type, "allowed": sorted(str(i) for i in EvalExpectType)},
            )
        description = _optional_str(raw, "description", where=where)
        if expect_type in _VALUE_TYPES:
            return cls(
                type=expect_type,
                value=_require(raw, "value", where=where),
                path=_optional_str(raw, "path", where=where),
                normalize=_normalize_modes(raw, where=where),
                description=description,
            )
        if expect_type == str(EvalExpectType.TOOL_SEQUENCE):
            return cls(
                type=expect_type,
                value=_tool_names(raw, where=where),
                mode=_tool_sequence_mode(raw, where=where),
                description=description,
            )
        if expect_type == str(EvalExpectType.MAX_LATENCY):
            return cls(
                type=expect_type,
                value_ms=_positive_int(raw, "value_ms", where=where),
                description=description,
            )
        return cls(
            type=expect_type,
            rubric=_require_str(raw, "rubric", where=where),
            threshold=_optional_float(raw, "threshold", where=where),
            description=description,
        )


@dataclass(frozen=True, slots=True)
class EvalCase:
    """一条测试任务（4.9.1：`input` + `expected[]` + 权重/开关/超时）。"""

    id: str
    input: str
    expected: tuple[EvalExpectation, ...]
    weight: float = DEFAULT_CASE_WEIGHT
    enabled: bool = True
    timeout_seconds: float | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, where: str) -> EvalCase:
        """校验并构造一条用例。"""
        case_id = _require_str(raw, "id", where=where)
        expected_raw = raw.get("expected")
        if not isinstance(expected_raw, Sequence) or isinstance(expected_raw, (str, bytes)) or not expected_raw:
            raise ConfigInvalidError(
                f"{where}: `expected` must be a non-empty list",
                details={"where": where, "case_id": case_id},
            )
        expectations = tuple(
            EvalExpectation.from_mapping(item, where=f"{case_id}.expected[{index}]")
            for index, item in enumerate(expected_raw)
        )
        timeout = _optional_float(raw, "timeout_seconds", where=where)
        weight = _optional_float(raw, "weight", where=where)
        if weight is not None and weight <= 0:
            raise ConfigInvalidError(f"{where}: `weight` must be > 0", details={"case_id": case_id})
        return cls(
            id=case_id,
            input=_require_str(raw, "input", where=where),
            expected=expectations,
            weight=weight if weight is not None else DEFAULT_CASE_WEIGHT,
            enabled=_optional_bool(raw, "enabled", where=where, default=True),
            timeout_seconds=timeout,
        )


@dataclass(frozen=True, slots=True)
class EvalSuite:
    """一个用例集（MVP：一份 `configs/eval/*.yaml`）。"""

    name: str
    cases: tuple[EvalCase, ...]
    agent: str | None = None
    description: str = ""
    source_path: Path | None = None

    @property
    def enabled_cases(self) -> tuple[EvalCase, ...]:
        """`enabled: false` 的用例不参与本次评测（也不算进分母，4.9.2）。"""
        return tuple(case for case in self.cases if case.enabled)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, source_path: Path | None = None) -> EvalSuite:
        """校验并构造用例集；`cases[].id` 必须唯一。"""
        name = _require_str(raw, "name", where="suite")
        cases_raw = raw.get("cases")
        if not isinstance(cases_raw, Sequence) or isinstance(cases_raw, (str, bytes)) or not cases_raw:
            raise ConfigInvalidError(f"suite {name!r}: `cases` must be a non-empty list", details={"suite": name})
        cases = tuple(
            EvalCase.from_mapping(item, where=f"{name}.cases[{index}]") for index, item in enumerate(cases_raw)
        )
        seen: set[str] = set()
        duplicates: list[str] = []
        for case in cases:
            if case.id in seen and case.id not in duplicates:
                duplicates.append(case.id)
            seen.add(case.id)
        if duplicates:
            raise ConfigInvalidError(
                f"suite {name!r}: duplicated case ids {sorted(duplicates)}",
                details={"suite": name, "case_ids": sorted(duplicates)},
            )
        return cls(
            name=name,
            cases=cases,
            agent=_optional_str(raw, "agent", where="suite"),
            description=_optional_str(raw, "description", where="suite") or "",
            source_path=source_path,
        )


class SuiteSource(Protocol):
    """用例来源（4.9.1）；MVP 只有 `YamlSuiteSource`。"""

    def load(self) -> EvalSuite: ...


class YamlSuiteSource:
    """从 `configs/eval/{name}.yaml`（或直接给路径）载入用例集。"""

    def __init__(self, suite: str | Path, *, suite_dir: Path | None = None) -> None:
        self._requested = str(suite)
        self._suite_dir = Path(suite_dir) if suite_dir is not None else DEFAULT_SUITE_DIR

    @property
    def path(self) -> Path:
        """解析后的 YAML 路径（含 `.yaml`/包含分隔符时按路径，否则按集名在 `suite_dir` 下补后缀）。"""
        candidate = Path(self._requested)
        if candidate.suffix in {".yaml", ".yml"} or candidate.parent != Path():
            return candidate if candidate.is_absolute() else (Path.cwd() / candidate)
        return self._suite_dir / f"{self._requested}.yaml"

    def load(self) -> EvalSuite:
        """读文件 → 校验 → `EvalSuite`；文件不存在或结构非法 → `CONFIG_INVALID`。"""
        path = self.path
        if not path.exists():
            raise ConfigInvalidError(
                f"Eval suite file not found: {path}",
                details={"suite": self._requested, "path": str(path)},
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise ConfigInvalidError(
                f"Eval suite file must contain a mapping: {path}",
                details={"suite": self._requested, "path": str(path)},
            )
        return EvalSuite.from_mapping(raw, source_path=path)


def load_suite(suite: str | Path, *, suite_dir: Path | None = None) -> EvalSuite:
    """便捷函数：`YamlSuiteSource(...).load()`。"""
    return YamlSuiteSource(suite, suite_dir=suite_dir).load()


def select_cases(suite: EvalSuite, case_ids: Sequence[str] | None = None) -> tuple[EvalCase, ...]:
    """按 `--cases` 过滤启用用例；未知 id → `CONFIG_INVALID`（避免"静默少跑"）。"""
    enabled = suite.enabled_cases
    if not case_ids:
        return enabled
    wanted = set(case_ids)
    unknown = sorted(wanted - {case.id for case in enabled})
    if unknown:
        raise ConfigInvalidError(
            f"Unknown case ids for suite {suite.name!r}: {unknown}",
            details={"suite": suite.name, "case_ids": unknown},
        )
    return tuple(case for case in enabled if case.id in wanted)


def with_cases(suite: EvalSuite, cases: Sequence[EvalCase]) -> EvalSuite:
    """换一批用例（`--cases` 过滤后仍保留 suite 的元信息）。"""
    return replace(suite, cases=tuple(cases))


def _require(raw: Mapping[str, Any], key: str, *, where: str) -> Any:
    if key not in raw or raw[key] is None:
        raise ConfigInvalidError(f"{where}: missing `{key}`", details={"where": where, "key": key})
    return raw[key]


def _require_str(raw: Mapping[str, Any], key: str, *, where: str) -> str:
    value = _require(raw, key, where=where)
    if not isinstance(value, str) or not value.strip():
        raise ConfigInvalidError(f"{where}: `{key}` must be a non-empty string", details={"where": where, "key": key})
    return value.strip()


def _optional_str(raw: Mapping[str, Any], key: str, *, where: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigInvalidError(f"{where}: `{key}` must be a string", details={"where": where, "key": key})
    return value.strip() or None


def _optional_float(raw: Mapping[str, Any], key: str, *, where: str) -> float | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigInvalidError(f"{where}: `{key}` must be a number", details={"where": where, "key": key})
    return float(value)


def _positive_int(raw: Mapping[str, Any], key: str, *, where: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigInvalidError(f"{where}: `{key}` must be a positive integer", details={"where": where, "key": key})
    return int(value)


def _optional_bool(raw: Mapping[str, Any], key: str, *, where: str, default: bool) -> bool:
    value = raw.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigInvalidError(f"{where}: `{key}` must be a boolean", details={"where": where, "key": key})
    return value


def _tool_names(raw: Mapping[str, Any], *, where: str) -> tuple[str, ...]:
    value = _require(raw, "value", where=where)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ConfigInvalidError(
            f"{where}: `tool_sequence.value` must be a list of tool names", details={"where": where}
        )
    names = tuple(str(item).strip() for item in value if str(item).strip())
    if not names:
        raise ConfigInvalidError(f"{where}: `tool_sequence.value` must not be empty", details={"where": where})
    return names


def _tool_sequence_mode(raw: Mapping[str, Any], *, where: str) -> str:
    mode = _optional_str(raw, "mode", where=where) or DEFAULT_TOOL_SEQUENCE_MODE
    if mode not in TOOL_SEQUENCE_MODES:
        raise ConfigInvalidError(
            f"{where}: unknown `tool_sequence.mode` {mode!r}",
            details={"where": where, "mode": mode, "allowed": list(TOOL_SEQUENCE_MODES)},
        )
    return mode


def _normalize_modes(raw: Mapping[str, Any], *, where: str) -> tuple[str, ...]:
    value = raw.get("normalize")
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ConfigInvalidError(
            f"{where}: `normalize` must be a list of {list(NORMALIZE_MODES)}", details={"where": where}
        )
    modes = tuple(str(item).strip() for item in value)
    unknown = [item for item in modes if item not in NORMALIZE_MODES]
    if unknown:
        raise ConfigInvalidError(
            f"{where}: unknown `normalize` steps {unknown}",
            details={"where": where, "normalize": list(modes), "allowed": list(NORMALIZE_MODES)},
        )
    return modes
