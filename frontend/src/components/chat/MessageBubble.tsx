/**
 * 消息气泡（详细设计 5.1 的 `components/chat/MessageBubble.tsx`）。
 *
 * 只渲染文本 + 元信息（token / 耗时 / finish_reason）；工具调用用独立组件
 * `components/chat/ToolCallCard.tsx`（M2 起，事件 6/7/8 驱动），引用来源卡片在 Phase 5 接入
 * （SD-14②：不预先堆空组件）。
 */

import { Card, Space, Tag, Typography } from "antd";

const ROLE_LABEL: Record<string, string> = {
  system: "system",
  user: "我",
  assistant: "助手",
  tool: "工具",
};

const ROLE_COLOR: Record<string, string> = {
  user: "blue",
  assistant: "green",
  tool: "orange",
  system: "default",
};

/** 渲染所需的最小字段集合：`MessageRead` 与流式中的临时消息都能喂进来。 */
export interface MessageView {
  role: string;
  content: string;
  model_name?: string | null;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  latency_ms?: number | null;
  finish_reason?: string | null;
}

interface MessageBubbleProps {
  message: MessageView;
  /** 流式生成中：内容后追加光标。 */
  streaming?: boolean;
}

/** 单条消息：左侧角色标签 + 正文。 */
export default function MessageBubble({ message, streaming = false }: MessageBubbleProps) {
  const role = message.role ?? "assistant";
  return (
    <Card size="small" style={{ background: role === "user" ? "#f5f8ff" : "#fff" }}>
      <Space direction="vertical" size={6} style={{ width: "100%" }}>
        <Space size={8} wrap>
          <Tag color={ROLE_COLOR[role] ?? "default"}>{ROLE_LABEL[role] ?? role}</Tag>
          {message.model_name ? (
            <Typography.Text type="secondary">{message.model_name}</Typography.Text>
          ) : null}
          {message.total_tokens ? (
            <Typography.Text type="secondary">
              {message.prompt_tokens ?? 0}→{message.completion_tokens ?? 0} tok
            </Typography.Text>
          ) : null}
          {typeof message.latency_ms === "number" ? (
            <Typography.Text type="secondary">{message.latency_ms} ms</Typography.Text>
          ) : null}
          {message.finish_reason ? <Tag>{message.finish_reason}</Tag> : null}
        </Space>
        <Typography.Paragraph style={{ margin: 0, whiteSpace: "pre-wrap" }}>
          {message.content}
          {streaming ? <span style={{ opacity: 0.4 }}>▌</span> : null}
        </Typography.Paragraph>
      </Space>
    </Card>
  );
}
