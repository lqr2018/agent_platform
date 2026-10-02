/**
 * Agent 编辑（详细设计 5.2 的 `/agents/:id`，M1→M3）。
 *
 * Phase 1 的字段：Prompt / 模型 / 步数与超时 / 短期记忆窗口；工具 / KB / Workflow 属后续阶段。
 * 改 `system_prompt` 会由后端落 `agent_prompt_versions` 并自增 `prompt_version`（2.4）。
 */

import { SaveOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Card, Form, Input, InputNumber, message, Select, Space, Table, Tag, Typography } from "antd";
import { useEffect } from "react";
import { useNavigate, useParams } from "react-router-dom";

import {
  type AgentPromptVersionRead,
  type AgentUpdate,
  getAgent,
  listPromptVersions,
  updateAgent,
} from "@/api/agents";
import { listProviders } from "@/api/models";

interface AgentFormValues {
  name: string;
  description: string;
  status: "enabled" | "disabled";
  model_provider_id: string;
  model_name: string;
  system_prompt: string;
  prompt_note?: string;
  max_steps: number;
  timeout_seconds: number;
  max_turns: number;
  max_tokens: number;
}

/** 表单 → `AgentUpdate`（`prompt_note` 只在 prompt 变化时才有意义）。 */
function toUpdatePayload(values: AgentFormValues): AgentUpdate {
  return {
    name: values.name,
    description: values.description,
    status: values.status,
    model_provider_id: values.model_provider_id,
    model_name: values.model_name,
    system_prompt: values.system_prompt,
    prompt_note: values.prompt_note ?? null,
    max_steps: values.max_steps,
    timeout_seconds: values.timeout_seconds,
    memory_config: {
      short_term: {
        strategy: "window",
        max_turns: values.max_turns,
        max_tokens: values.max_tokens,
        summarize_threshold_tokens: 4000,
      },
      long_term: { enabled: false, scope: "agent", retrieve_top_k: 5, min_score: 0.35, write_policy: "auto" },
    },
  };
}

export default function AgentEditPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [form] = Form.useForm<AgentFormValues>();

  const agent = useQuery({ queryKey: ["agent", id], queryFn: () => getAgent(id), enabled: Boolean(id) });
  const providers = useQuery({ queryKey: ["providers"], queryFn: () => listProviders() });
  const versions = useQuery({
    queryKey: ["prompt-versions", id],
    queryFn: () => listPromptVersions(id),
    enabled: Boolean(id),
  });

  useEffect(() => {
    if (!agent.data) {
      return;
    }
    const shortTerm = agent.data.memory_config?.["short_term"] as
      { max_turns?: number; max_tokens?: number } | undefined;
    form.setFieldsValue({
      name: agent.data.name,
      description: agent.data.description ?? "",
      status: agent.data.status === "disabled" ? "disabled" : "enabled",
      model_provider_id: agent.data.model_provider_id,
      model_name: agent.data.model_name,
      system_prompt: agent.data.system_prompt ?? "",
      max_steps: agent.data.max_steps,
      timeout_seconds: agent.data.timeout_seconds,
      max_turns: shortTerm?.max_turns ?? 12,
      max_tokens: shortTerm?.max_tokens ?? 6000,
    });
  }, [agent.data, form]);

  const saveMutation = useMutation({
    mutationFn: (values: AgentFormValues) => updateAgent(id, toUpdatePayload(values)),
    onSuccess: async (updated) => {
      message.success(`已保存（prompt v${updated.prompt_version}）`);
      await queryClient.invalidateQueries({ queryKey: ["agent", id] });
      await queryClient.invalidateQueries({ queryKey: ["prompt-versions", id] });
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: (error: Error) => message.error(error.message),
  });

  if (agent.isError) {
    return (
      <Space direction="vertical">
        <Typography.Title level={4}>Agent 不存在</Typography.Title>
        <Button onClick={() => navigate("/agents")}>返回列表</Button>
      </Space>
    );
  }

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Space wrap>
        <Typography.Title level={4} style={{ margin: 0 }}>
          编辑 Agent
        </Typography.Title>
        <Tag>prompt v{agent.data?.prompt_version ?? "-"}</Tag>
        <Tag color={agent.data?.status === "enabled" ? "green" : "default"}>{agent.data?.status ?? "-"}</Tag>
        <Button onClick={() => navigate("/agents")}>返回列表</Button>
      </Space>

      <Card size="small" loading={agent.isLoading}>
        <Form form={form} layout="vertical" onFinish={(values) => saveMutation.mutate(values)}>
          <Space size={16} wrap style={{ width: "100%" }}>
            <Form.Item
              name="name"
              label="名称"
              rules={[{ required: true, message: "必填" }]}
              style={{ minWidth: 240 }}
            >
              <Input />
            </Form.Item>
            <Form.Item name="status" label="状态" style={{ minWidth: 200 }}>
              <Select
                options={[
                  { value: "enabled", label: "enabled" },
                  { value: "disabled", label: "disabled（不接受新 Run）" },
                ]}
              />
            </Form.Item>
          </Space>

          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} />
          </Form.Item>

          <Space size={16} wrap style={{ width: "100%" }}>
            <Form.Item name="model_provider_id" label="Provider" style={{ minWidth: 260 }}>
              <Select
                options={(providers.data ?? []).map((provider) => ({
                  value: provider.id,
                  label: `${provider.name}${provider.is_default ? "（默认）" : ""}`,
                }))}
              />
            </Form.Item>
            <Form.Item name="model_name" label="模型名" style={{ minWidth: 220 }}>
              <Input />
            </Form.Item>
            <Form.Item name="max_steps" label="max_steps" style={{ minWidth: 120 }}>
              <InputNumber min={1} max={50} />
            </Form.Item>
            <Form.Item name="timeout_seconds" label="超时（秒）" style={{ minWidth: 140 }}>
              <InputNumber min={1} max={3600} />
            </Form.Item>
          </Space>

          <Form.Item
            name="system_prompt"
            label="System Prompt"
            extra="修改后会先把旧值写入 agent_prompt_versions，并自增 prompt_version（2.4）"
          >
            <Input.TextArea rows={6} />
          </Form.Item>
          <Form.Item name="prompt_note" label="本次 Prompt 变更说明">
            <Input placeholder="例如：加强语气 / 补充输出格式要求" />
          </Form.Item>

          <Space size={16} wrap>
            <Form.Item name="max_turns" label="短期记忆：最大轮数" style={{ minWidth: 200 }}>
              <InputNumber min={1} max={100} />
            </Form.Item>
            <Form.Item name="max_tokens" label="短期记忆：token 预算" style={{ minWidth: 200 }}>
              <InputNumber min={256} max={200000} step={256} />
            </Form.Item>
          </Space>

          <Button type="primary" htmlType="submit" icon={<SaveOutlined />} loading={saveMutation.isPending}>
            保存
          </Button>
        </Form>
      </Card>

      <Card size="small" title="Prompt 历史版本">
        <Table<AgentPromptVersionRead>
          rowKey="id"
          size="small"
          loading={versions.isLoading}
          dataSource={versions.data ?? []}
          pagination={false}
          columns={[
            { title: "版本", dataIndex: "version", width: 80 },
            { title: "说明", dataIndex: "note" },
            { title: "内容", dataIndex: "system_prompt" },
            { title: "时间", dataIndex: "created_at", width: 220 },
          ]}
        />
      </Card>
    </Space>
  );
}
