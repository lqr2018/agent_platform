/**
 * Workflow 列表（详细设计 5.2 的 `/workflows`，M2）。
 *
 * 页面职责（对应 3.2.7 的定义类端点）：列表 + 状态筛选、新建（JSON 图定义）、发布、删除。
 * 编辑与试跑走 `/workflows/:id`（`WorkflowEditPage`）。
 */

import { DeleteOutlined, EditOutlined, PlusOutlined, ReloadOutlined, SendOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Form, Input, message, Modal, Popconfirm, Select, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import {
  type WorkflowCreate,
  type WorkflowRead,
  createWorkflow,
  deleteWorkflow,
  listWorkflows,
  publishWorkflow,
} from "@/api/workflows";

const STATUS_COLOR: Record<string, string> = { draft: "orange", published: "green", archived: "default" };

/** 新建时的起步模板：`agent_id` / `tool_name` 必须引用**已存在**的 Agent / 工具（4.5.1）。 */
const STARTER_DEFINITION = JSON.stringify(
  {
    nodes: [
      { id: "start", type: "start", name: "开始", next: "planner" },
      {
        id: "planner",
        type: "agent",
        name: "规划",
        agent_id: "<agent_id>",
        input_template: "{{state.input}}",
        output_key: "plan",
        next: "end",
      },
      { id: "end", type: "end", name: "结束" },
    ],
    config: { max_steps: 30, recursion_limit: 50, timeout_seconds: 600 },
  },
  null,
  2,
);

interface CreateFormValues {
  name: string;
  description?: string;
  status: "draft" | "published";
  definition_text: string;
}

function parseDefinition(text: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(text);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      message.error("图定义必须是 JSON 对象");
      return null;
    }
    return parsed as Record<string, unknown>;
  } catch {
    message.error("图定义不是合法 JSON");
    return null;
  }
}

function toCreatePayload(values: CreateFormValues): WorkflowCreate {
  const definition = parseDefinition(values.definition_text);
  if (!definition) {
    throw new Error("图定义不合法，请修正后重试");
  }
  return {
    name: values.name,
    description: values.description ?? "",
    status: values.status,
    definition,
    state_schema: {},
  };
}

export default function WorkflowsPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [statusFilter, setStatusFilter] = useState<string | undefined>(undefined);
  const [creating, setCreating] = useState(false);
  const [form] = Form.useForm<CreateFormValues>();

  const workflows = useQuery({
    queryKey: ["workflows", statusFilter ?? ""],
    queryFn: () => listWorkflows({ status: statusFilter }),
  });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["workflows"] });
  const notifyError = (error: Error) => message.error(error.message);

  const createMutation = useMutation({
    mutationFn: async (values: CreateFormValues) => createWorkflow(toCreatePayload(values)),
    onSuccess: async (workflow) => {
      message.success(`已创建 ${workflow.name}`);
      setCreating(false);
      form.resetFields();
      await refresh();
      navigate(`/workflows/${workflow.id}`);
    },
    onError: notifyError,
  });

  const publishMutation = useMutation({
    mutationFn: publishWorkflow,
    onSuccess: async (workflow) => {
      message.success(`${workflow.name} 已发布到 v${workflow.version}`);
      await refresh();
    },
    onError: notifyError,
  });

  const deleteMutation = useMutation({
    mutationFn: deleteWorkflow,
    onSuccess: async () => {
      message.success("已删除");
      await refresh();
    },
    onError: notifyError,
  });

  const columns: ColumnsType<WorkflowRead> = [
    {
      title: "名称",
      dataIndex: "name",
      render: (_value, row) => <Link to={`/workflows/${row.id}`}>{row.name}</Link>,
    },
    {
      title: "状态",
      dataIndex: "status",
      render: (value: string) => <Tag color={STATUS_COLOR[value] ?? "default"}>{value}</Tag>,
    },
    { title: "版本", dataIndex: "version", width: 80 },
    { title: "节点数", dataIndex: "node_count", width: 90 },
    { title: "起点", dataIndex: "start_node_id", width: 120 },
    { title: "更新时间", dataIndex: "updated_at", width: 200 },
    {
      title: "操作",
      key: "actions",
      width: 240,
      render: (_value, row) => (
        <Space size={4}>
          <Button size="small" icon={<EditOutlined />} onClick={() => navigate(`/workflows/${row.id}`)}>
            编辑
          </Button>
          <Button
            size="small"
            icon={<SendOutlined />}
            loading={publishMutation.isPending}
            onClick={() => publishMutation.mutate(row.id)}
          >
            发布
          </Button>
          <Popconfirm title="删除该 Workflow？" onConfirm={() => deleteMutation.mutate(row.id)}>
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Space wrap>
        <Typography.Title level={4} style={{ margin: 0 }}>
          Workflow
        </Typography.Title>
        <Typography.Text type="secondary">
          JSON 定义图（SD-1：串行；SD-6：只做编辑 + 校验 + 只读图）
        </Typography.Text>
        <Select
          allowClear
          placeholder="按状态筛选"
          style={{ width: 160 }}
          value={statusFilter}
          onChange={setStatusFilter}
          options={[
            { value: "draft", label: "draft" },
            { value: "published", label: "published" },
            { value: "archived", label: "archived" },
          ]}
        />
        <Button icon={<ReloadOutlined />} onClick={() => void refresh()}>
          刷新
        </Button>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => {
            form.setFieldsValue({
              name: "",
              description: "",
              status: "draft",
              definition_text: STARTER_DEFINITION,
            });
            setCreating(true);
          }}
        >
          新建
        </Button>
      </Space>

      <Table<WorkflowRead>
        rowKey="id"
        size="small"
        loading={workflows.isLoading}
        dataSource={workflows.data ?? []}
        columns={columns}
      />

      <Modal
        title="新建 Workflow"
        open={creating}
        confirmLoading={createMutation.isPending}
        onCancel={() => setCreating(false)}
        onOk={() => form.submit()}
        width={720}
      >
        <Form<CreateFormValues>
          form={form}
          layout="vertical"
          onFinish={(values) => createMutation.mutate(values)}
        >
          <Form.Item name="name" label="名称" rules={[{ required: true, message: "请输入名称" }]}>
            <Input placeholder="research-flow" maxLength={120} />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} maxLength={4000} />
          </Form.Item>
          <Form.Item name="status" label="初始状态">
            <Select
              options={[
                { value: "draft", label: "draft（先试跑再发布）" },
                { value: "published", label: "published（可被 Agent 绑定）" },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="definition_text"
            label="图定义（JSON）"
            rules={[{ required: true, message: "请输入图定义" }]}
            extra="节点类型：start / agent / tool / retriever / condition / end；agent_id 与 tool_name 必须是已存在的引用"
          >
            <Input.TextArea rows={14} style={{ fontFamily: "monospace" }} />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  );
}
