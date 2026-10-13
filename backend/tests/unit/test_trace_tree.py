"""服务端建树（详细设计 3.2.9 的 `/tree` / 7.8 任务 6）的纯函数边界。

不连库：直接构造 `Span` ORM 对象喂给 `trace_service.build_span_tree`，钉住四条边界 ——
孤儿当根、兄弟按 `seq` 排序、父指针成环不递归爆炸、超上限截断
（真实链路的证据在 `tests/integration/test_runs_and_traces_api.py`）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.enums import SpanStatus, SpanType
from app.db.base import to_naive_utc
from app.db.models import Span
from app.services.trace_service import build_span_tree

TRACE_ID = "b" * 32


def _span(span_id: str, *, seq: int, parent: str | None = None, span_type: SpanType = SpanType.LLM) -> Span:
    """内存里的 span 行（字段足够建树与序列化用）。"""
    stamp = to_naive_utc(datetime(2026, 10, 13, 12, 0, tzinfo=UTC))
    return Span(
        id=span_id,
        trace_id=TRACE_ID,
        parent_span_id=parent,
        span_type=str(span_type),
        name=f"{span_type}:{span_id}",
        status=str(SpanStatus.OK),
        attributes={},
        seq=seq,
        started_at=stamp,
        ended_at=stamp,
    )


def test_empty_input_builds_an_empty_tree() -> None:
    tree = build_span_tree([])
    assert (tree.roots, tree.node_count, tree.truncated) == ([], 0, False)


def test_tree_keeps_hierarchy_and_sorts_siblings_by_seq() -> None:
    """`run → agent → {llm, tool}`；入参乱序也要按 `seq` 排好（4.8.1 的稳定顺序）。"""
    spans = [
        _span("span-run", seq=1, span_type=SpanType.RUN),
        _span("span-agent", seq=2, parent="span-run", span_type=SpanType.AGENT),
        _span("span-tool", seq=4, parent="span-agent", span_type=SpanType.TOOL),
        _span("span-llm", seq=3, parent="span-agent"),
    ]
    tree = build_span_tree(spans)

    assert (tree.node_count, tree.truncated) == (4, False)
    root = tree.roots[0]
    assert root.span.id == "span-run"
    agent = root.children[0]
    assert agent.span.id == "span-agent"
    assert [child.span.id for child in agent.children] == ["span-llm", "span-tool"]


def test_orphan_and_self_parent_become_roots() -> None:
    """父 span 不在结果里（例如被截断）或指向自己 → 当根，节点不丢（与前端同口径）。"""
    spans = [_span("span-lost", seq=1, parent="span-missing"), _span("span-self", seq=2, parent="span-self")]
    tree = build_span_tree(spans)

    assert [node.span.id for node in tree.roots] == ["span-lost", "span-self"]
    assert (tree.node_count, tree.truncated) == (2, False)


def test_parent_cycle_is_dropped_without_recursion() -> None:
    """环（脏数据）上的节点不可达 → 丢弃并置 `truncated`，而不是递归到爆栈。"""
    spans = [_span("span-a", seq=1, parent="span-b"), _span("span-b", seq=2, parent="span-a")]
    tree = build_span_tree(spans)

    assert (tree.roots, tree.node_count, tree.truncated) == ([], 0, True)


def test_max_nodes_caps_the_tree_and_flags_truncated() -> None:
    spans = [_span("span-run", seq=1, span_type=SpanType.RUN)] + [
        _span(f"span-c{index}", seq=index + 1, parent="span-run") for index in range(1, 5)
    ]
    tree = build_span_tree(spans, max_nodes=3)

    assert (tree.node_count, tree.truncated) == (3, True)
    assert [child.span.id for child in tree.roots[0].children] == ["span-c1", "span-c2"]


def test_deep_chain_does_not_hit_the_recursion_limit() -> None:
    """1000 深的链也要能建树：实现用显式栈（递归实现会撞 Python 的 1000 层限制）。"""
    spans = [_span("span-0", seq=1, span_type=SpanType.RUN)]
    spans.extend(_span(f"span-{index}", seq=index + 1, parent=f"span-{index - 1}") for index in range(1, 1000))

    tree = build_span_tree(spans, max_nodes=2000)
    assert (tree.node_count, tree.truncated) == (1000, False)
    assert tree.roots[0].span.id == "span-0"
