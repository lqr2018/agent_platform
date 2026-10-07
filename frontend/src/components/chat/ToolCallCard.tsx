/**
 * 工具调用卡片（详细设计 5.1 的 `components/chat/`、7.3 任务 7）。
 *
 * Chat 里"调用了什么、参数、结果、耗时"的落点：数据来自 `chatStore.tools`
 * （由 `tool.call.started` / `tool.call.completed` / `tool.call.failed` 三个事件归纳，3.4 事件 6/7/8）。
 * 与 `MessageBubble` 分开：消息是"内容"，工具调用是"过程"，两者在页面上属于不同层。
 */

import { Alert, Card, Space, Tag, Typography } from "antd";

import type { ToolCallTimelineItem } from "@/stores/chatStore";

const STATUS_LABEL: Record<ToolCallTimelineItem["status"], string> = {
  started: "执行中",
  succeeded: "成功",
  failed: "失败",
};

const STATUS_COLOR: Record<ToolCallTimelineItem["status"], string> = {
  started: "processing",
  succeeded: "green",
  failed: "red",
};

interface ToolCallCardProps {
  item: ToolCallTimelineItem;
}

/** 单次工具调用：名称 + 状态 + 耗时 + 参数 + 结果（或错误码）。 */
export default function ToolCallCard({ item }: ToolCallCardProps) {
  const argumentKeys = Object.keys(item.arguments);
  return (
    <Card size="small" style={{ background: "#fffbe6", borderColor: "#ffe58f" }}>
      <Space direction="vertical" size={6} style={{ width: "100%" }}>
        <Space size={8} wrap>
          <Tag color="orange">工具</Tag>
          <Typography.Text strong>{item.toolName || "（未命名）"}</Typography.Text>
          <Tag color={STATUS_COLOR[item.status]}>{STATUS_LABEL[item.status]}</Tag>
          {item.latencyMs === null ? null : (
            <Typography.Text type="secondary">{item.latencyMs} ms</Typography.Text>
          )}
        </Space>
        {argumentKeys.length > 0 ? (
          <Typography.Text type="secondary">
            参数：<Typography.Text code>{JSON.stringify(item.arguments)}</Typography.Text>
          </Typography.Text>
        ) : null}
        {item.resultPreview ? (
          <Typography.Paragraph style={{ margin: 0, whiteSpace: "pre-wrap" }}>
            {item.resultPreview}
          </Typography.Paragraph>
        ) : null}
        {item.errorCode ? <Alert type="error" showIcon message={item.errorCode} /> : null}
      </Space>
    </Card>
  );
}
