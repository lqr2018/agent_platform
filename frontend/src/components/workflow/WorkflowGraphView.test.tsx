import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { NodeRunRead, WorkflowGraphProjection } from "@/api/workflows";

import WorkflowGraphView, { buildGraphSteps } from "./WorkflowGraphView";

const GRAPH: WorkflowGraphProjection = {
  start_node_id: "start",
  end_node_ids: ["end"],
  config: { max_steps: 10 },
  nodes: [
    { id: "start", type: "start", name: "开始", next: "planner", branches: [], default_next: null },
    { id: "planner", type: "agent", name: "规划", next: "router", branches: [], default_next: null },
    {
      id: "router",
      type: "condition",
      name: "是否命中",
      next: null,
      branches: [{ when: "len(state.hits) > 0", next: "writer" }],
      default_next: "end",
    },
    { id: "writer", type: "agent", name: "撰写", next: "end", branches: [], default_next: null },
    { id: "end", type: "end", name: "结束", next: null, branches: [], default_next: null },
  ],
};

function nodeRun(nodeId: string, status: string, seq: number): NodeRunRead {
  return {
    id: `nr-${seq}`,
    run_id: "run-1",
    node_id: nodeId,
    node_type: "agent",
    name: nodeId,
    seq,
    iteration: 1,
    attempt: 1,
    status,
    input: {},
    output: {},
    started_at: "2026-10-07T00:00:00Z",
    created_at: "2026-10-07T00:00:00Z",
  };
}

describe("buildGraphSteps", () => {
  it("keeps definition order and renders edges", () => {
    const steps = buildGraphSteps(GRAPH);
    expect(steps.map((step) => step.id)).toEqual(["start", "planner", "router", "writer", "end"]);
    expect(steps[0]).toMatchObject({ isStart: true, edges: ["→ planner"] });
    expect(steps[2]?.edges).toEqual(["len(state.hits) > 0 → writer", "default → end"]);
    expect(steps[4]?.edges).toEqual([]);
  });

  it("uses the latest node run status per node", () => {
    const steps = buildGraphSteps(GRAPH, [
      nodeRun("planner", "failed", 1),
      nodeRun("planner", "succeeded", 2),
      nodeRun("router", "succeeded", 3),
    ]);
    const byId = new Map(steps.map((step) => [step.id, step.status]));
    expect(byId.get("planner")).toBe("succeeded");
    expect(byId.get("router")).toBe("succeeded");
    expect(byId.get("end")).toBeUndefined();
  });

  it("returns an empty list without a graph", () => {
    expect(buildGraphSteps(undefined)).toEqual([]);
  });
});

describe("WorkflowGraphView", () => {
  it("renders nodes with status tags", () => {
    render(<WorkflowGraphView graph={GRAPH} nodeRuns={[nodeRun("planner", "succeeded", 1)]} />);
    expect(screen.getByText("planner")).toBeInTheDocument();
    expect(screen.getByText("condition")).toBeInTheDocument();
    expect(screen.getByText("succeeded")).toBeInTheDocument();
    expect(screen.getByText(/len\(state\.hits\) > 0 → writer/)).toBeInTheDocument();
  });

  it("shows a hint when there is no graph yet", () => {
    render(<WorkflowGraphView graph={undefined} />);
    expect(screen.getByText(/暂无图/)).toBeInTheDocument();
  });
});
