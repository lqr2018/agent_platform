import { useQuery } from "@tanstack/react-query";
import { Card, Descriptions, List, Tag, Typography } from "antd";

import { getHealthz, getPlatformMeta, getReadyz } from "@/api/health";

const REFETCH_INTERVAL_MS = 10_000;

function featureTag(label: string, enabled: boolean) {
  return <Tag color={enabled ? "green" : "default"}>{`${label}: ${enabled ? "on" : "off"}`}</Tag>;
}

/**
 * 总览页（Phase 0）：展示进程信息、就绪检查明细与能力开关。
 *
 * 后续里程碑会在此扩展为看板（`/stats`，M3）；当前只做"基础设施可见"这一件事。
 */
export default function DashboardPage() {
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

  return (
    <div style={{ display: "grid", gap: 20 }}>
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
