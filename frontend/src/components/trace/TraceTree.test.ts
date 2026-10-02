import { describe, expect, it } from "vitest";

import { buildSpanTree } from "@/components/trace/TraceTree";
import type { SpanSummary } from "@/api/traces";

/**
 * Trace 建树（3.2.9：后端返回扁平 span，前端按 `parent_span_id` 组树）。
 * 断言覆盖 4.8.1 的固定层级 `run → agent → llm`。
 */
function span(id: string, parent: string | null, type: string, seq: number): SpanSummary {
  return {
    id,
    trace_id: "t1",
    parent_span_id: parent,
    run_id: "r1",
    span_type: type,
    name: `${type}:x`,
    status: "ok",
    attributes: {},
    prompt_tokens: 0,
    completion_tokens: 0,
    cost_usd: 0,
    latency_ms: 1,
    seq,
    error_code: null,
    error_message: null,
    started_at: "2026-10-01T00:00:00.000Z",
    ended_at: "2026-10-01T00:00:00.001Z",
  };
}

describe("buildSpanTree", () => {
  it("按 parent_span_id 组树，且按 seq 稳定排序", () => {
    const nodes = buildSpanTree([
      span("llm1", "agent1", "llm", 3),
      span("run1", null, "run", 1),
      span("agent1", "run1", "agent", 2),
    ]);

    expect(nodes).toHaveLength(1);
    expect(nodes[0]?.key).toBe("run1");
    expect(nodes[0]?.children).toHaveLength(1);
    expect(nodes[0]?.children?.[0]?.key).toBe("agent1");
    expect(nodes[0]?.children?.[0]?.children?.[0]?.key).toBe("llm1");
  });

  it("父 span 缺失时把子节点当根，避免丢节点", () => {
    const nodes = buildSpanTree([span("llm1", "missing", "llm", 2), span("run1", null, "run", 1)]);
    expect(nodes.map((node) => node.key)).toEqual(["run1", "llm1"]);
  });

  it("空列表返回空数组", () => {
    expect(buildSpanTree([])).toEqual([]);
  });
});
