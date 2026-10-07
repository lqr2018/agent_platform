/**
 * Workflow 只读图（详细设计 5.2：M2 的 "JSON 编辑 + 校验 + 只读图"，SD-6 不做拖拽编辑器）。
 *
 * 设计取舍（避免"伪造的可视化"）：
 *
 * - **按定义顺序**列出节点（`definition.nodes` 的顺序就是作者的意图），不自己做图布局；
 * - 每个节点显示 `type` 徽标 + 出边（`next` / `branches[].when → next` / `default_next → next`），
 *   由 `buildGraphSteps()` 这个纯函数算出，便于单测；
 * - 运行中传入 `nodeRuns` 时，用**每个节点最后一行** `node_runs` 给节点染色（4.5.3 的轮询结果）。
 */

import { Space, Tag, Typography } from "antd";

import type { GraphNode, NodeRunRead, WorkflowGraphProjection } from "@/api/workflows";

const TYPE_COLOR: Record<string, string> = {
  start: "default",
  agent: "blue",
  tool: "geekblue",
  retriever: "purple",
  condition: "orange",
  end: "default",
};

const STATUS_COLOR: Record<string, string> = {
  pending: "default",
  running: "processing",
  succeeded: "success",
  failed: "error",
  canceled: "warning",
};

export interface GraphStep {
  id: string;
  type: string;
  name: string;
  isStart: boolean;
  /** 出边的人类可读描述（`next` / `when → next` / `default → next`）。 */
  edges: string[];
  /** 该节点最近一次 `node_runs.status`（未运行时为 `undefined`）。 */
  status?: string;
}

function edgesOf(node: GraphNode): string[] {
  const edges: string[] = [];
  if (node.next) {
    edges.push(`→ ${node.next}`);
  }
  for (const branch of node.branches) {
    edges.push(`${branch.when} → ${branch.next}`);
  }
  if (node.default_next) {
    edges.push(`default → ${node.default_next}`);
  }
  return edges;
}

/**
 * 定义顺序 + 节点最后状态 → 只读图的步骤列表（纯函数，5.2）。
 *
 * 与组件放在同一文件是刻意的（5.2 的目录约定只有 `WorkflowGraphView.tsx`）；
 * react-refresh 的告警只影响该文件的 HMR 粒度，故在此显式豁免并说明原因（同 `TraceTree.buildSpanTree`）。
 */
// eslint-disable-next-line react-refresh/only-export-components
export function buildGraphSteps(
  graph: WorkflowGraphProjection | undefined,
  nodeRuns: NodeRunRead[] = [],
): GraphStep[] {
  if (!graph) {
    return [];
  }
  const latestStatus = new Map<string, string>();
  for (const row of nodeRuns) {
    latestStatus.set(row.node_id, row.status);
  }
  return graph.nodes.map((node) => ({
    id: node.id,
    type: node.type,
    name: node.name,
    isStart: node.id === graph.start_node_id,
    edges: edgesOf(node),
    status: latestStatus.get(node.id),
  }));
}

export default function WorkflowGraphView({
  graph,
  nodeRuns = [],
}: {
  graph: WorkflowGraphProjection | undefined;
  nodeRuns?: NodeRunRead[];
}) {
  const steps = buildGraphSteps(graph, nodeRuns);
  if (steps.length === 0) {
    return <Typography.Text type="secondary">暂无图（先点"校验"或保存一次即可看到只读图）</Typography.Text>;
  }
  return (
    <Space direction="vertical" size={6} style={{ width: "100%" }}>
      {steps.map((step) => (
        <Space key={step.id} size={8} wrap>
          <Typography.Text code>{step.id}</Typography.Text>
          <Tag color={TYPE_COLOR[step.type] ?? "default"}>{step.type}</Tag>
          {step.isStart ? <Tag>start</Tag> : null}
          {step.status ? <Tag color={STATUS_COLOR[step.status] ?? "default"}>{step.status}</Tag> : null}
          <Typography.Text type="secondary">{step.name}</Typography.Text>
          {step.edges.length > 0 ? (
            <Typography.Text type="secondary">{step.edges.join(" | ")}</Typography.Text>
          ) : (
            <Typography.Text type="secondary">（终点）</Typography.Text>
          )}
        </Space>
      ))}
    </Space>
  );
}
