import { useQuery } from "@tanstack/react-query";
import { Badge, Spin, Tooltip, Typography } from "antd";

import { getReadyz, type ReadyzResponse } from "@/api/health";

const REFETCH_INTERVAL_MS = 10_000;

function describe(readyz: ReadyzResponse | undefined): {
  status: "success" | "error" | "processing";
  text: string;
  detail: string;
} {
  if (!readyz) {
    return { status: "processing", text: "检查中", detail: "正在请求 /readyz…" };
  }
  const lines = readyz.checks.map((check) => `${check.ok ? "✅" : "❌"} ${check.name}: ${check.detail}`);
  if (readyz.status === "ok") {
    return { status: "success", text: "就绪", detail: lines.join("\n") };
  }
  return { status: "error", text: "不可用", detail: lines.join("\n") };
}

/**
 * 顶部 `/readyz` 状态条（7.1）。
 *
 * 每 10s 轮询一次；后端 503 时 `getReadyz` 抛错，这里用 `error` 分支兜底展示原因。
 */
export default function ReadinessBadge() {
  const query = useQuery({
    queryKey: ["readyz"],
    queryFn: getReadyz,
    refetchInterval: REFETCH_INTERVAL_MS,
  });

  const { status, text, detail } = query.isError
    ? {
        status: "error" as const,
        text: "后端不可达",
        detail: query.error instanceof Error ? query.error.message : "未知错误",
      }
    : describe(query.data);

  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
      {query.isLoading ? <Spin size="small" /> : null}
      <Tooltip title={<pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>{detail}</pre>}>
        <Badge status={status} text={<Typography.Text type="secondary">/readyz {text}</Typography.Text>} />
      </Tooltip>
    </span>
  );
}
