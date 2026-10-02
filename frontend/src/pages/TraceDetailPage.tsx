/**
 * Trace 详情（详细设计 5.2 的 `/traces/:traceId`，M1 只读）。
 *
 * 展示 `run → agent → llm` 的树与每层耗时（DoD）；点击 span 打开抽屉看完整 `input` / `output`。
 */

import { useQuery } from "@tanstack/react-query";
import { Button, Card, Descriptions, Drawer, Space, Tag, Typography } from "antd";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { type SpanRead, type SpanSummary, getSpan, getTrace } from "@/api/traces";
import TraceTree from "@/components/trace/TraceTree";

export default function TraceDetailPage() {
  const { traceId = "" } = useParams();
  const navigate = useNavigate();
  const [selected, setSelected] = useState<SpanSummary | null>(null);

  const detail = useQuery({
    queryKey: ["trace", traceId],
    queryFn: () => getTrace(traceId),
    enabled: Boolean(traceId),
  });
  const span = useQuery({
    queryKey: ["span", selected?.id],
    queryFn: () => getSpan(selected?.id ?? ""),
    enabled: Boolean(selected?.id),
  });

  if (detail.isError) {
    return (
      <Space direction="vertical">
        <Typography.Title level={4}>Trace 不存在</Typography.Title>
        <Button onClick={() => navigate("/traces")}>返回列表</Button>
      </Space>
    );
  }

  const trace = detail.data?.trace;

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Space wrap>
        <Typography.Title level={4} style={{ margin: 0 }}>
          Trace 详情
        </Typography.Title>
        <Button onClick={() => navigate("/traces")}>返回列表</Button>
        {trace?.run_id ? <Link to={`/chat`}>相关会话在 Chat 页</Link> : null}
      </Space>

      <Card size="small" loading={detail.isLoading}>
        <Descriptions
          size="small"
          column={3}
          items={[
            { key: "name", label: "名称", children: trace?.name ?? "-" },
            { key: "id", label: "trace_id", children: <Typography.Text code>{traceId}</Typography.Text> },
            {
              key: "status",
              label: "状态",
              children: trace ? (
                <Tag color={trace.status === "succeeded" ? "green" : "red"}>{trace.status}</Tag>
              ) : (
                "-"
              ),
            },
            { key: "run", label: "run_id", children: trace?.run_id ?? "-" },
            { key: "spans", label: "span 数", children: trace?.span_count ?? "-" },
            { key: "tokens", label: "token", children: trace?.total_tokens ?? "-" },
            {
              key: "latency",
              label: "耗时",
              children:
                trace?.latency_ms === null || trace?.latency_ms === undefined
                  ? "-"
                  : `${trace.latency_ms} ms`,
            },
            { key: "cost", label: "估算成本", children: `$${(trace?.total_cost_usd ?? 0).toFixed(6)}` },
            { key: "started", label: "开始", children: trace?.started_at ?? "-" },
          ]}
        />
      </Card>

      <Card size="small" title="Span 树（点击查看 input / output）">
        <TraceTree spans={detail.data?.spans ?? []} onSelect={setSelected} />
      </Card>

      <Drawer
        width={640}
        open={Boolean(selected)}
        onClose={() => setSelected(null)}
        title={selected ? `${selected.span_type} · ${selected.name}` : ""}
      >
        <SpanDetail span={span.data} loading={span.isLoading} />
      </Drawer>
    </Space>
  );
}

/** span 明细：属性 + 截断后的 input / output（4.8.2 的截断策略）。 */
function SpanDetail({ span, loading }: { span: SpanRead | undefined; loading: boolean }) {
  if (loading || !span) {
    return <Typography.Text type="secondary">加载中…</Typography.Text>;
  }
  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Descriptions
        size="small"
        column={1}
        items={[
          { key: "id", label: "span_id", children: <Typography.Text code>{span.id}</Typography.Text> },
          { key: "parent", label: "parent", children: span.parent_span_id ?? "（根）" },
          { key: "status", label: "状态", children: span.status },
          {
            key: "latency",
            label: "耗时",
            children: span.latency_ms === null ? "-" : `${span.latency_ms} ms`,
          },
          {
            key: "tokens",
            label: "token",
            children: `${span.prompt_tokens}→${span.completion_tokens}`,
          },
          { key: "error", label: "错误", children: span.error_code ?? "-" },
        ]}
      />
      <Card size="small" title="attributes">
        <pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>{JSON.stringify(span.attributes, null, 2)}</pre>
      </Card>
      <Card size="small" title="input">
        <pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>{JSON.stringify(span.input, null, 2)}</pre>
      </Card>
      <Card size="small" title="output">
        <pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>{JSON.stringify(span.output, null, 2)}</pre>
      </Card>
    </Space>
  );
}
