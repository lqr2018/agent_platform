/**
 * Agent 列表（详细设计 5.2 的 `/agents`，M1）。
 *
 * 卡片 + 状态 + 复制 + 删除；编辑走 `/agents/:id`。
 * Phase 1 不显示工具 / KB / Workflow 列（对应能力未实现，SD-14②）。
 */

import { CopyOutlined, DeleteOutlined, EditOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Card,
  Empty,
  Form,
  Input,
  List,
  message,
  Modal,
  Popconfirm,
  Select,
  Space,
  Tag,
  Typography,
} from "antd";
import { useState } from "react";
import { Link } from "react-router-dom";

import type { FormInstance } from "antd";

import {
  type AgentCreate,
  type AgentRead,
  cloneAgent,
  createAgent,
  deleteAgent,
  listAgents,
} from "@/api/agents";
import { type ProviderRead, listProviders } from "@/api/models";

interface CreateFormValues {
  name: string;
  description?: string;
  model_provider_id: string;
  model_name: string;
  system_prompt?: string;
}

/** 表单 → `AgentCreate`：补齐后端 DTO 的必填项（其余走服务端默认值）。 */
function toCreatePayload(values: CreateFormValues): AgentCreate {
  return {
    name: values.name,
    description: values.description ?? "",
    model_provider_id: values.model_provider_id,
    model_name: values.model_name,
    system_prompt: values.system_prompt ?? "",
    max_steps: 8,
    timeout_seconds: 180,
    status: "enabled",
    is_template: false,
  };
}

export default function AgentsPage() {
  const queryClient = useQueryClient();
  const [keyword, setKeyword] = useState("");
  const [creating, setCreating] = useState(false);
  const [cloning, setCloning] = useState<AgentRead | null>(null);
  const [form] = Form.useForm<CreateFormValues>();
  const [cloneForm] = Form.useForm<{ name: string }>();

  const agents = useQuery({
    queryKey: ["agents", keyword],
    queryFn: () => listAgents({ q: keyword || undefined }),
  });
  const providers = useQuery({ queryKey: ["providers"], queryFn: () => listProviders() });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["agents"] });

  const createMutation = useMutation({
    mutationFn: createAgent,
    onSuccess: async (agent) => {
      message.success(`已创建 ${agent.name}`);
      setCreating(false);
      form.resetFields();
      await refresh();
    },
    onError: (error: Error) => message.error(error.message),
  });

  const cloneMutation = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => cloneAgent(id, { name }),
    onSuccess: async (agent) => {
      message.success(`已复制为 ${agent.name}`);
      setCloning(null);
      cloneForm.resetFields();
      await refresh();
    },
    onError: (error: Error) => message.error(error.message),
  });

  const deleteMutation = useMutation({
    mutationFn: deleteAgent,
    onSuccess: async () => {
      message.success("已删除（软删除，历史 Run 仍可查）");
      await refresh();
    },
    onError: (error: Error) => message.error(error.message),
  });

  const providerList = providers.data ?? [];

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Space wrap>
        <Typography.Title level={4} style={{ margin: 0 }}>
          Agents
        </Typography.Title>
        <Input.Search allowClear placeholder="按名称搜索" style={{ width: 220 }} onSearch={setKeyword} />
        <Button icon={<ReloadOutlined />} onClick={() => void refresh()}>
          刷新
        </Button>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreating(true)}>
          新建 Agent
        </Button>
        {providerList.length === 0 ? (
          <Typography.Text type="warning">
            还没有模型 Provider，先到 <Link to="/models">模型</Link> 页添加。
          </Typography.Text>
        ) : null}
      </Space>

      <AgentList
        agents={agents.data ?? []}
        loading={agents.isLoading}
        onClone={setCloning}
        onDelete={(id) => deleteMutation.mutate(id)}
      />

      <CreateAgentModal
        open={creating}
        form={form}
        providers={providerList}
        loading={createMutation.isPending}
        onCancel={() => setCreating(false)}
        onSubmit={(values) => createMutation.mutate(toCreatePayload(values))}
      />

      <CloneAgentModal
        agent={cloning}
        form={cloneForm}
        loading={cloneMutation.isPending}
        onCancel={() => setCloning(null)}
        onSubmit={(name) => cloning && cloneMutation.mutate({ id: cloning.id, name })}
      />
    </Space>
  );
}

/** Agent 卡片列表（拆成子组件：页面主体只留数据编排）。 */
function AgentList({
  agents,
  loading,
  onClone,
  onDelete,
}: {
  agents: AgentRead[];
  loading: boolean;
  onClone: (agent: AgentRead) => void;
  onDelete: (id: string) => void;
}) {
  return (
    <Card size="small">
      <List
        loading={loading}
        dataSource={agents}
        locale={{ emptyText: <Empty description="还没有 Agent，先建一个" /> }}
        renderItem={(agent) => (
          <List.Item
            actions={[
              <Link key="edit" to={`/agents/${agent.id}`}>
                <Button type="link" icon={<EditOutlined />}>
                  编辑
                </Button>
              </Link>,
              <Button key="clone" type="link" icon={<CopyOutlined />} onClick={() => onClone(agent)}>
                复制
              </Button>,
              <Popconfirm
                key="delete"
                title="删除该 Agent？"
                description="软删除：历史 Run 与 Trace 仍保留。"
                onConfirm={() => onDelete(agent.id)}
              >
                <Button type="link" danger icon={<DeleteOutlined />}>
                  删除
                </Button>
              </Popconfirm>,
            ]}
          >
            <List.Item.Meta
              title={
                <Space size={8} wrap>
                  <Typography.Text strong>{agent.name}</Typography.Text>
                  <Tag color={agent.status === "enabled" ? "green" : "default"}>{agent.status}</Tag>
                  <Tag>prompt v{agent.prompt_version}</Tag>
                  {(agent.tags ?? []).map((tag) => (
                    <Tag key={tag}>{tag}</Tag>
                  ))}
                </Space>
              }
              description={
                <Space direction="vertical" size={2}>
                  <Typography.Text type="secondary">
                    模型：{agent.model_name} · 最大步数 {agent.max_steps} · 超时 {agent.timeout_seconds}s
                  </Typography.Text>
                  <Typography.Text type="secondary">{agent.description || "（无描述）"}</Typography.Text>
                </Space>
              }
            />
          </List.Item>
        )}
      />
    </Card>
  );
}

/** 新建 Agent：Phase 1 只填"名称 / 描述 / Provider / 模型名 / Prompt"。 */
function CreateAgentModal({
  open,
  form,
  providers,
  loading,
  onCancel,
  onSubmit,
}: {
  open: boolean;
  form: FormInstance<CreateFormValues>;
  providers: ProviderRead[];
  loading: boolean;
  onCancel: () => void;
  onSubmit: (values: CreateFormValues) => void;
}) {
  const options = providers.map((provider) => ({
    value: provider.id,
    label: `${provider.name}${provider.is_default ? "（默认）" : ""}`,
  }));
  return (
    <Modal
      title="新建 Agent"
      open={open}
      confirmLoading={loading}
      onCancel={onCancel}
      onOk={() => form.submit()}
      destroyOnClose
    >
      <Form form={form} layout="vertical" onFinish={onSubmit}>
        <Form.Item name="name" label="名称" rules={[{ required: true, message: "必填" }]}>
          <Input placeholder="例如 Research Agent" />
        </Form.Item>
        <Form.Item name="description" label="描述">
          <Input.TextArea rows={2} />
        </Form.Item>
        <Form.Item
          name="model_provider_id"
          label="模型 Provider"
          rules={[{ required: true, message: "必选" }]}
        >
          <Select
            options={options}
            placeholder="选择 Provider"
            onChange={(value: string) => {
              const provider = providers.find((item) => item.id === value);
              if (provider?.default_model) {
                form.setFieldValue("model_name", provider.default_model);
              }
            }}
          />
        </Form.Item>
        <Form.Item name="model_name" label="模型名" rules={[{ required: true, message: "必填" }]}>
          <Input placeholder="须在 Provider 白名单内，例如 qwen-plus" />
        </Form.Item>
        <Form.Item name="system_prompt" label="System Prompt">
          <Input.TextArea rows={4} placeholder="你是一名研究员……" />
        </Form.Item>
      </Form>
      <Typography.Text type="secondary">
        当前版本只支持纯对话型 Agent（工具 / 知识库 / Workflow 属后续阶段）。
      </Typography.Text>
    </Modal>
  );
}

/** 复制 Agent（3.2.2 的 `POST /agents/{id}/clone`）。 */
function CloneAgentModal({
  agent,
  form,
  loading,
  onCancel,
  onSubmit,
}: {
  agent: AgentRead | null;
  form: FormInstance<{ name: string }>;
  loading: boolean;
  onCancel: () => void;
  onSubmit: (name: string) => void;
}) {
  return (
    <Modal
      title={`复制 ${agent?.name ?? ""}`}
      open={Boolean(agent)}
      confirmLoading={loading}
      onCancel={onCancel}
      onOk={() => form.submit()}
      destroyOnClose
    >
      <Form form={form} layout="vertical" onFinish={(values) => onSubmit(values.name)}>
        <Form.Item name="name" label="新名称" rules={[{ required: true, message: "必填" }]}>
          <Input />
        </Form.Item>
      </Form>
    </Modal>
  );
}
