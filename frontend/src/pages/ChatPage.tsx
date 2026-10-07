/**
 * Chat 页（详细设计 5.2 的 `/chat`、`/chat/:id`，M1；M2 起含工具调用卡片）。
 *
 * 左侧选 Agent → 建/选会话；右侧流式渲染回答 + 工具调用卡片 + 本轮 Run 的 token / 耗时 / Trace 入口。
 * SSE 事件契约见 3.4，状态归纳见 `stores/chatStore.ts`。
 */

import { PlusOutlined, SendOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Card,
  Descriptions,
  Empty,
  Input,
  List,
  message,
  Select,
  Space,
  Tag,
  Typography,
} from "antd";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { listAgents } from "@/api/agents";
import {
  type ConversationRead,
  createConversation,
  listConversations,
  listMessages,
} from "@/api/conversations";
import MessageBubble from "@/components/chat/MessageBubble";
import ToolCallCard from "@/components/chat/ToolCallCard";
import { useChatStream } from "@/hooks/useChatStream";
import { type RunState, useChatStore } from "@/stores/chatStore";

export default function ChatPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [agentId, setAgentId] = useState<string | undefined>();
  const [draft, setDraft] = useState("");

  const agents = useQuery({ queryKey: ["agents", ""], queryFn: () => listAgents() });
  const conversations = useQuery({
    queryKey: ["conversations", agentId],
    queryFn: () => listConversations({ agentId }),
    enabled: Boolean(agentId),
  });
  const messages = useQuery({
    queryKey: ["messages", id],
    queryFn: () => listMessages(id ?? "", { limit: 50 }),
    enabled: Boolean(id),
  });

  useEffect(() => {
    if (!agentId && agents.data?.length) {
      setAgentId(agents.data[0]?.id);
    }
  }, [agentId, agents.data]);

  const createMutation = useMutation({
    mutationFn: (targetAgentId: string) => createConversation({ agent_id: targetAgentId }),
    onSuccess: async (conversation) => {
      await queryClient.invalidateQueries({ queryKey: ["conversations"] });
      navigate(`/chat/${conversation.id}`);
    },
    onError: (error: Error) => message.error(error.message),
  });

  const chatStream = useChatStream({
    conversationId: id,
    onSettled: async () => {
      await queryClient.invalidateQueries({ queryKey: ["messages", id] });
      await queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });

  const streaming = useChatStore((state) => state.streaming);
  const run = useChatStore((state) => state.run);
  const toolTimeline = useChatStore((state) => state.tools);

  /** 接口返回倒序（最新在前），展示时反转成正序。 */
  const orderedMessages = useMemo(() => [...(messages.data?.data ?? [])].reverse(), [messages.data]);

  const send = async () => {
    const content = draft.trim();
    if (!content) {
      return;
    }
    setDraft("");
    await chatStream.send(content);
    await queryClient.invalidateQueries({ queryKey: ["messages", id] });
  };

  return (
    <div style={{ display: "grid", gridTemplateColumns: "300px 1fr", gap: 16, alignItems: "start" }}>
      <Card size="small" title="会话" styles={{ body: { padding: 12 } }}>
        <Space direction="vertical" style={{ width: "100%" }} size={8}>
          <Select
            style={{ width: "100%" }}
            placeholder="选择 Agent"
            value={agentId}
            onChange={setAgentId}
            options={(agents.data ?? []).map((agent) => ({ value: agent.id, label: agent.name }))}
          />
          <Button
            block
            icon={<PlusOutlined />}
            disabled={!agentId}
            loading={createMutation.isPending}
            onClick={() => agentId && createMutation.mutate(agentId)}
          >
            新建会话
          </Button>
          <ConversationList
            conversations={conversations.data?.data ?? []}
            loading={conversations.isLoading}
            activeId={id}
            onSelect={(conversationId) => navigate(`/chat/${conversationId}`)}
          />
        </Space>
      </Card>

      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <RunStatusCard run={run} toolCount={toolTimeline.length} />
        <Card size="small" title="消息" loading={messages.isLoading} styles={{ body: { padding: 12 } }}>
          {orderedMessages.length === 0 && !streaming ? (
            <Empty description={id ? "开始第一轮对话" : "先在左侧选一个会话"} />
          ) : (
            <Space direction="vertical" size={8} style={{ width: "100%" }}>
              {orderedMessages.map((item) => (
                <MessageBubble key={item.id} message={item} />
              ))}
              {streaming ? (
                <MessageBubble
                  streaming
                  message={{
                    role: streaming.role,
                    content: streaming.content,
                    model_name: streaming.model,
                    finish_reason: streaming.finishReason,
                  }}
                />
              ) : null}
            </Space>
          )}
        </Card>

        {toolTimeline.length > 0 ? (
          <Card size="small" title={`工具调用（${toolTimeline.length}）`} styles={{ body: { padding: 12 } }}>
            <Space direction="vertical" size={8} style={{ width: "100%" }}>
              {toolTimeline.map((item) => (
                <ToolCallCard key={item.toolCallId} item={item} />
              ))}
            </Space>
          </Card>
        ) : null}

        <Space.Compact style={{ width: "100%" }}>
          <Input.TextArea
            autoSize={{ minRows: 2, maxRows: 6 }}
            value={draft}
            placeholder={id ? "输入消息，Enter 发送（Shift+Enter 换行）" : "先新建或选择一个会话"}
            disabled={!id || chatStream.isStreaming}
            onChange={(event) => setDraft(event.target.value)}
            onPressEnter={(event) => {
              if (!event.shiftKey) {
                event.preventDefault();
                void send();
              }
            }}
          />
          <Button
            type="primary"
            icon={<SendOutlined />}
            loading={chatStream.isStreaming}
            disabled={!id}
            onClick={() => void send()}
          >
            发送
          </Button>
        </Space.Compact>
        {chatStream.error ? <Typography.Text type="danger">{chatStream.error}</Typography.Text> : null}
      </Space>
    </div>
  );
}

/** 会话列表（点击切换）。 */
function ConversationList({
  conversations,
  loading,
  activeId,
  onSelect,
}: {
  conversations: ConversationRead[];
  loading: boolean;
  activeId: string | undefined;
  onSelect: (conversationId: string) => void;
}) {
  return (
    <List<ConversationRead>
      size="small"
      loading={loading}
      dataSource={conversations}
      locale={{ emptyText: <Empty description="还没有会话" /> }}
      renderItem={(conversation) => (
        <List.Item
          onClick={() => onSelect(conversation.id)}
          style={{
            cursor: "pointer",
            paddingInline: 8,
            background: conversation.id === activeId ? "#f0f5ff" : undefined,
            borderRadius: 6,
          }}
        >
          <List.Item.Meta
            title={
              <Typography.Text ellipsis style={{ maxWidth: 200 }}>
                {conversation.title || "（新会话）"}
              </Typography.Text>
            }
            description={
              <Typography.Text type="secondary">
                {conversation.message_count} 条 · {conversation.status}
              </Typography.Text>
            }
          />
        </List.Item>
      )}
    />
  );
}

/** 本轮 Run 的 token / 耗时 / Trace 入口（DoD：结束后能看到 token 与耗时）。 */
function RunStatusCard({ run, toolCount }: { run: RunState; toolCount: number }) {
  return (
    <Card size="small" title="本次 Run" styles={{ body: { padding: 12 } }}>
      <Descriptions
        size="small"
        column={4}
        items={[
          {
            key: "status",
            label: "状态",
            children: (
              <Tag
                color={run.status === "succeeded" ? "green" : run.status === "running" ? "blue" : "default"}
              >
                {run.status}
              </Tag>
            ),
          },
          { key: "steps", label: "步数", children: run.steps },
          { key: "tools", label: "工具调用", children: toolCount },
          {
            key: "tokens",
            label: "token",
            children: `${run.usage.promptTokens}→${run.usage.completionTokens}（${run.usage.totalTokens}）`,
          },
          { key: "latency", label: "耗时", children: run.latencyMs === null ? "-" : `${run.latencyMs} ms` },
          { key: "cost", label: "估算成本", children: `$${run.usage.costUsd.toFixed(6)}` },
          {
            key: "trace",
            label: "Trace",
            children: run.traceId ? <Link to={`/traces/${run.traceId}`}>查看链路</Link> : "-",
          },
          {
            key: "error",
            label: "错误",
            children: run.errorCode ? (
              <Typography.Text type="danger">{`${run.errorCode} ${run.errorMessage ?? ""}`}</Typography.Text>
            ) : (
              "-"
            ),
          },
        ]}
      />
    </Card>
  );
}
