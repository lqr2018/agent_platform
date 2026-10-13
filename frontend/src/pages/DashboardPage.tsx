import { useQuery } from "@tanstack/react-query";
import { Card, Col, Descriptions, List, Row, Select, Space, Statistic, Tag, Typography } from "antd";
import { useState } from "react";

import { getHealthz, getPlatformMeta, getReadyz } from "@/api/health";
import { getStatsOverview } from "@/api/stats";

const REFETCH_INTERVAL_MS = 10_000;

/** 指标窗口（分钟；`0` = 不限）。与 `GET /stats/overview` 的 `?from=` 同口径（闭区间）。 */
const WINDOW_OPTIONS = [
  { value: 60, label: "近 1 小时" },
  { value: 60 * 24, label: "近 24 小时" },
  { value: 60 * 24 * 7, label: "近 7 天" },
  { value: 0, label: "全部" },
];

const KIND_OPTIONS = [
  { value: "chat", label: "chat" },
  { value: "workflow", label: "workflow" },
  { value: "eval", label: "eval（评测批次）" },
];

/** 默认窗口：近 24 小时（`WINDOW_OPTIONS` 里那一项）。 */
const DEFAULT_WINDOW_MINUTES = 60 * 24;

function windowStart(minutes: number): string | undefined {
  return minutes > 0 ? new Date(Date.now() - minutes * 60_000).toISOString() : undefined;
}

/** 比率 → 百分比；`null` 表示"没有分母"（4.8.3），显示 `-` 而不是 `0%`。 */
function ratio(value: number | null | undefined): string {
  return value === null || value === undefined ? "-" : `${(value * 100).toFixed(1)}%`;
}

function decimal(value: number | null | undefined, unit = ""): string {
  if (value === null || value === undefined) {
    return "-";
  }
  return `${Number.isInteger(value) ? value : value.toFixed(1)}${unit}`;
}

function featureTag(label: string, enabled: boolean) {
  return <Tag color={enabled ? "green" : "default"}>{`${label}: ${enabled ? "on" : "off"}`}</Tag>;
}

/**
 * 总览页：`/stats` 指标看板（7.8 交付物 → 7.9 前端收口）+ Phase 0 的进程 / 就绪 / 能力开关。
 *
 * 看板只做**数字**（`Statistic` + `Descriptions`）：7.9 的 MVP 口径写明"Stats 图表美化随迭代 A–D"，
 * 因此这里不引入图表库（SD-14 红线①：不出现没有第二调用方的依赖）。
 */
export default function DashboardPage() {
  const [windowMinutes, setWindowMinutes] = useState<number>(DEFAULT_WINDOW_MINUTES);
  const [kind, setKind] = useState<string | undefined>();

  const healthz = useQuery({
    queryKey: ["healthz"],
    queryFn: getHealthz,
    refetchInterval: REFETCH_INTERVAL_MS,
  });
  const readyz = useQuery({ queryKey: ["readyz"], queryFn: getReadyz, refetchInterval: REFETCH_INTERVAL_MS });
  const meta = useQuery({
    queryKey: ["meta"],
    queryFn: getPlatformMeta,
    refetchInterval: REFETCH_INTERVAL_MS,
  });
  const stats = useQuery({
    queryKey: ["stats", windowMinutes, kind],
    queryFn: () => getStatsOverview({ from: windowStart(windowMinutes), kind }),
    refetchInterval: REFETCH_INTERVAL_MS,
  });

  const overview = stats.data;
  const emptyWindow = overview ? overview.terminal_runs === 0 : false;

  return (
    <div style={{ display: "grid", gap: 20 }}>
      <Card
        title="平台指标（/stats/overview）"
        size="small"
        loading={stats.isLoading}
        extra={
          <Space>
            <Select
              style={{ width: 130 }}
              value={windowMinutes}
              onChange={setWindowMinutes}
              options={WINDOW_OPTIONS}
            />
            <Select
              allowClear
              placeholder="kind"
              style={{ width: 150 }}
              value={kind}
              onChange={setKind}
              options={KIND_OPTIONS}
            />
            {stats.isError ? <Tag color="red">{`拉取失败：${stats.error.message}`}</Tag> : null}
            {overview ? (
              <Typography.Text type="secondary">{`截至 ${overview.generated_at}`}</Typography.Text>
            ) : null}
          </Space>
        }
      >
        <Row gutter={16}>
          <Col span={4}>
            <Statistic title="Run 数" value={decimal(overview?.total_runs)} />
          </Col>
          <Col span={4}>
            <Statistic title="成功率" value={ratio(overview?.success_rate)} />
          </Col>
          <Col span={4}>
            <Statistic title="错误率" value={ratio(overview?.error_rate)} />
          </Col>
          <Col span={4}>
            <Statistic title="平均时延" value={decimal(overview?.avg_latency_ms, " ms")} />
          </Col>
          <Col span={4}>
            <Statistic title="平均工具调用" value={decimal(overview?.avg_tool_calls)} />
          </Col>
          <Col span={4}>
            <Statistic title="平均 token" value={decimal(overview?.avg_total_tokens)} />
          </Col>
        </Row>

        <Descriptions
          size="small"
          column={4}
          style={{ marginTop: 16 }}
          items={[
            { key: "terminal", label: "已收敛", children: decimal(overview?.terminal_runs) },
            { key: "running", label: "进行中", children: decimal(overview?.running) },
            { key: "failed", label: "失败", children: decimal(overview?.failed) },
            { key: "canceled", label: "已取消", children: decimal(overview?.canceled) },
            { key: "tokens", label: "token 合计", children: decimal(overview?.total_tokens) },
            {
              key: "cost",
              label: "成本合计（USD）",
              children: overview ? `$${overview.total_cost_usd.toFixed(4)}` : "-",
            },
            {
              key: "tool",
              label: "工具成功率",
              children: `${ratio(overview?.tool_success_rate)}（${decimal(overview?.tool_spans_succeeded)}/${decimal(overview?.tool_spans_total)} 次 tool span）`,
            },
          ]}
        />

        {emptyWindow ? (
          <Typography.Text type="secondary" style={{ display: "block", marginTop: 12 }}>
            窗口内还没有已收敛的 Run：比率与均值显示 `-`（后端返回{" "}
            <Typography.Text code>null</Typography.Text>
            ，不是 0，避免被读成「成功率 0%」）。先去 /chat 或 /workflows 跑一次。
          </Typography.Text>
        ) : null}
      </Card>
      <Card title="进程信息" size="small" loading={healthz.isLoading}>
        <Descriptions
          size="small"
          column={2}
          items={[
            { key: "name", label: "服务", children: healthz.data?.name ?? "-" },
            { key: "version", label: "版本", children: healthz.data?.version ?? "-" },
            { key: "env", label: "环境", children: healthz.data?.app_env ?? "-" },
            { key: "status", label: "存活", children: healthz.data?.status ?? "-" },
          ]}
        />
      </Card>

      <Card title="就绪检查（/readyz）" size="small" loading={readyz.isLoading}>
        <List
          size="small"
          dataSource={readyz.data?.checks ?? []}
          locale={{ emptyText: readyz.isError ? "后端不可达" : "无检查项" }}
          renderItem={(check) => (
            <List.Item>
              <Tag color={check.ok ? "green" : "red"}>{check.ok ? "ok" : "fail"}</Tag>
              <Typography.Text code>{check.name}</Typography.Text>
              <Typography.Text type="secondary" style={{ marginLeft: 8 }}>
                {check.detail}
              </Typography.Text>
            </List.Item>
          )}
        />
      </Card>

      <Card title="能力开关（/api/v1/meta）" size="small" loading={meta.isLoading}>
        <Typography.Paragraph type="secondary" style={{ marginBottom: 12 }}>
          前端据 <Typography.Text code>features</Typography.Text> 隐藏未实现能力的入口（Backlog：Memory / MCP
          / 评测台，SD-14②）。
        </Typography.Paragraph>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {meta.data
            ? Object.entries(meta.data.features).map(([key, enabled]) => featureTag(key, enabled))
            : null}
        </div>
      </Card>
    </div>
  );
}
