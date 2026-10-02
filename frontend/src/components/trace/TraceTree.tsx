/**
 * Trace 树（详细设计 5.1 的 `components/trace/TraceTree.tsx`）。
 *
 * 后端返回**扁平 span 列表**（带 `parent_span_id`），这里在客户端建树并按 `seq` 稳定排序，
 * 渲染成可折叠的时间轴（对齐 5.2 Trace 详情的呈现示意）。
 */

import { Space, Tag, Tree, Typography } from "antd";
import type { DataNode } from "antd/es/tree";

import type { SpanSummary } from "@/api/traces";

const SPAN_COLOR: Record<string, string> = {
  run: "purple",
  agent: "geekblue",
  llm: "green",
  tool: "orange",
  retriever: "cyan",
  workflow: "magenta",
  node: "gold",
};

/** 把扁平 span 建成 `DataNode[]`（`parent_span_id` 为空的作为根）。
 *
 * 与组件放在同一文件是刻意的（5.1 的目录约定只有 `TraceTree.tsx`）；
 * react-refresh 的告警只影响该文件的 HMR 粒度，故在此显式豁免并说明原因。
 */
// eslint-disable-next-line react-refresh/only-export-components
export function buildSpanTree(spans: SpanSummary[]): DataNode[] {
  const ordered = [...spans].sort((left, right) => (left.seq ?? 0) - (right.seq ?? 0));
  const childrenOf = new Map<string | null, SpanSummary[]>();
  for (const span of ordered) {
    const parent = span.parent_span_id ?? null;
    const bucket = childrenOf.get(parent) ?? [];
    bucket.push(span);
    childrenOf.set(parent, bucket);
  }

  const toNode = (span: SpanSummary): DataNode => ({
    key: span.id,
    title: <SpanTitle span={span} />,
    children: (childrenOf.get(span.id) ?? []).map(toNode),
  });

  const roots = ordered.filter(
    (span) => !span.parent_span_id || !spans.some((s) => s.id === span.parent_span_id),
  );
  return roots.map(toNode);
}

function SpanTitle({ span }: { span: SpanSummary }) {
  return (
    <Space size={8} wrap>
      <Tag color={SPAN_COLOR[span.span_type] ?? "default"}>{span.span_type}</Tag>
      <Typography.Text strong>{span.name}</Typography.Text>
      {typeof span.latency_ms === "number" ? (
        <Typography.Text type="secondary">{span.latency_ms} ms</Typography.Text>
      ) : null}
      {span.prompt_tokens || span.completion_tokens ? (
        <Typography.Text type="secondary">
          {span.prompt_tokens}→{span.completion_tokens} tok
        </Typography.Text>
      ) : null}
      {span.status !== "ok" ? <Tag color="red">{span.status}</Tag> : null}
      {span.error_code ? <Tag color="red">{span.error_code}</Tag> : null}
    </Space>
  );
}

interface TraceTreeProps {
  spans: SpanSummary[];
  onSelect?: (span: SpanSummary) => void;
}

/** 可折叠的 Trace 树；点击节点回调交给页面打开 span 抽屉。 */
export default function TraceTree({ spans, onSelect }: TraceTreeProps) {
  if (spans.length === 0) {
    return <Typography.Text type="secondary">该 Trace 暂无 span</Typography.Text>;
  }
  return (
    <Tree
      treeData={buildSpanTree(spans)}
      defaultExpandAll
      selectable={Boolean(onSelect)}
      onSelect={(keys) => {
        const span = spans.find((item) => item.id === keys[0]);
        if (span) {
          onSelect?.(span);
        }
      }}
    />
  );
}
