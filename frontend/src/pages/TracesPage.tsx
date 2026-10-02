/**
 * Trace 列表（详细设计 5.2 的 `/traces`，M1）：列表 + 游标分页 + 跳详情。
 */

import { ReloadOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Button, Card, Select, Space, Table, Tag, Typography } from "antd";
import { useState } from "react";
import { Link } from "react-router-dom";

import { type TraceRead, listTraces } from "@/api/traces";

const STATUS_COLOR: Record<string, string> = {
  succeeded: "green",
  running: "blue",
  failed: "red",
  canceled: "default",
};

export default function TracesPage() {
  const [kind, setKind] = useState<string | undefined>();
  const [status, setStatus] = useState<string | undefined>();
  const [cursor, setCursor] = useState<string | undefined>();
  const [cursors, setCursors] = useState<string[]>([]);

  const traces = useQuery({
    queryKey: ["traces", kind, status, cursor],
    queryFn: () => listTraces({ kind, status, cursor }),
  });

  const goNext = () => {
    const next = traces.data?.meta?.next_cursor ?? undefined;
    if (next) {
      setCursors((prev) => [...prev, cursor ?? ""]);
      setCursor(next);
    }
  };

  const goPrev = () => {
    const prev = [...cursors];
    const previous = prev.pop();
    setCursors(prev);
    setCursor(previous || undefined);
  };

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Space wrap>
        <Typography.Title level={4} style={{ margin: 0 }}>
          Trace
        </Typography.Title>
        <Select
          allowClear
          placeholder="kind"
          style={{ width: 140 }}
          value={kind}
          onChange={(value) => {
            setKind(value);
            setCursor(undefined);
            setCursors([]);
          }}
          options={[
            { value: "chat", label: "chat" },
            { value: "workflow", label: "workflow（Phase 3）" },
            { value: "eval", label: "eval（Phase 7）" },
          ]}
        />
        <Select
          allowClear
          placeholder="status"
          style={{ width: 160 }}
          value={status}
          onChange={(value) => {
            setStatus(value);
            setCursor(undefined);
            setCursors([]);
          }}
          options={["running", "succeeded", "failed", "canceled"].map((value) => ({ value, label: value }))}
        />
        <Button icon={<ReloadOutlined />} onClick={() => void traces.refetch()}>
          刷新
        </Button>
        <Button disabled={cursors.length === 0} onClick={goPrev}>
          上一页
        </Button>
        <Button disabled={!traces.data?.meta?.next_cursor} onClick={goNext}>
          下一页
        </Button>
      </Space>

      <Card size="small">
        <Table<TraceRead>
          rowKey="id"
          size="small"
          loading={traces.isLoading}
          dataSource={traces.data?.data ?? []}
          pagination={false}
          columns={[
            {
              title: "名称",
              dataIndex: "name",
              render: (name: string, row) => <Link to={`/traces/${row.id}`}>{name}</Link>,
            },
            { title: "kind", dataIndex: "kind", width: 90 },
            {
              title: "状态",
              dataIndex: "status",
              width: 110,
              render: (value: string) => <Tag color={STATUS_COLOR[value] ?? "default"}>{value}</Tag>,
            },
            { title: "span", dataIndex: "span_count", width: 80 },
            { title: "token", dataIndex: "total_tokens", width: 90 },
            {
              title: "耗时",
              dataIndex: "latency_ms",
              width: 100,
              render: (value: number | null) => (value === null ? "-" : `${value} ms`),
            },
            { title: "开始时间", dataIndex: "started_at", width: 230 },
            {
              title: "错误",
              dataIndex: "error_code",
              render: (value: string | null) =>
                value ? <Typography.Text type="danger">{value}</Typography.Text> : "-",
            },
          ]}
        />
      </Card>

      <Typography.Text type="secondary">
        第 {cursors.length + 1} 页；游标分页（3.2.9），`next_cursor` 为空表示到底了。
      </Typography.Text>
    </Space>
  );
}
