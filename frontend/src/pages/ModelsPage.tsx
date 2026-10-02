/**
 * 模型管理（详细设计 5.2 的 `/models`，M1）：Provider CRUD + 连通性测试。
 *
 * 列表只展示 `api_key_masked`（1.4 规则 1）；"测试"对应 3.2.2 的 `POST /model-providers/{id}/test`。
 */

import { ApiOutlined, DeleteOutlined, EditOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Card,
  Form,
  Input,
  message,
  Modal,
  Popconfirm,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
} from "antd";
import type { FormInstance } from "antd";
import { useState } from "react";

import {
  type ProviderCreate,
  type ProviderRead,
  createProvider,
  deleteProvider,
  listProviders,
  testProvider,
  updateProvider,
} from "@/api/models";

interface ProviderFormValues {
  name: string;
  base_url: string;
  api_key?: string;
  default_model?: string;
  model_name?: string;
  is_default: boolean;
}

export default function ModelsPage() {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<ProviderRead | null>(null);
  const [creating, setCreating] = useState(false);
  const [form] = Form.useForm<ProviderFormValues>();

  const providers = useQuery({ queryKey: ["providers"], queryFn: () => listProviders() });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["providers"] });
  const notifyError = (error: Error) => message.error(error.message);

  const saveMutation = useMutation({
    mutationFn: async (values: ProviderFormValues) => {
      const models = values.model_name ? [{ name: values.model_name }] : [];
      const defaultModel = values.default_model ?? values.model_name ?? null;
      if (editing) {
        return updateProvider(editing.id, {
          name: values.name,
          base_url: values.base_url,
          api_key: values.api_key ?? undefined,
          default_model: defaultModel,
          models,
          is_default: values.is_default,
        });
      }
      const payload: ProviderCreate = {
        name: values.name,
        base_url: values.base_url,
        api_key: values.api_key ?? null,
        default_model: defaultModel,
        models,
        is_default: values.is_default,
        status: "enabled",
        kind: "openai_compatible",
      };
      return createProvider(payload);
    },
    onSuccess: async (provider) => {
      message.success(`已保存 ${provider.name}`);
      setCreating(false);
      setEditing(null);
      form.resetFields();
      await refresh();
    },
    onError: notifyError,
  });

  const deleteMutation = useMutation({
    mutationFn: deleteProvider,
    onSuccess: async () => {
      message.success("已删除");
      await refresh();
    },
    onError: notifyError,
  });

  const testMutation = useMutation({
    mutationFn: testProvider,
    onSuccess: async (result) => {
      if (result.ok) {
        message.success(`连通正常：${result.model} · ${result.latency_ms} ms`);
      } else {
        message.error(`${result.error_code}：${result.error_message ?? ""}`);
      }
      await refresh();
    },
    onError: notifyError,
  });

  const openCreate = () => {
    form.resetFields();
    form.setFieldsValue({ is_default: false, base_url: "https://api.openai.com/v1" });
    setCreating(true);
  };

  const openEdit = (provider: ProviderRead) => {
    const firstModel = provider.models?.[0]?.name ?? "";
    form.setFieldsValue({
      name: provider.name,
      base_url: provider.base_url,
      default_model: provider.default_model ?? "",
      model_name: provider.default_model ?? firstModel,
      is_default: provider.is_default,
    });
    setEditing(provider);
  };

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Space wrap>
        <Typography.Title level={4} style={{ margin: 0 }}>
          模型 Provider
        </Typography.Title>
        <Button icon={<ReloadOutlined />} onClick={() => void refresh()}>
          刷新
        </Button>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          新建 Provider
        </Button>
        <Typography.Text type="secondary">
          切换模型只改这里的 base_url / 白名单，不需要改代码（统一封装的验证点）。
        </Typography.Text>
      </Space>

      <Card size="small">
        <ProviderTable
          providers={providers.data ?? []}
          loading={providers.isLoading}
          testing={testMutation.isPending}
          onTest={(id) => testMutation.mutate(id)}
          onEdit={openEdit}
          onDelete={(id) => deleteMutation.mutate(id)}
        />
      </Card>

      <ProviderModal
        open={creating || Boolean(editing)}
        editing={editing}
        form={form}
        loading={saveMutation.isPending}
        onCancel={() => {
          setCreating(false);
          setEditing(null);
        }}
        onSubmit={(values) => saveMutation.mutate(values)}
      />
    </Space>
  );
}

/** Provider 表格：名称 / 地址 / 掩码密钥 / 最近检测 / 操作。 */
function ProviderTable({
  providers,
  loading,
  testing,
  onTest,
  onEdit,
  onDelete,
}: {
  providers: ProviderRead[];
  loading: boolean;
  testing: boolean;
  onTest: (id: string) => void;
  onEdit: (provider: ProviderRead) => void;
  onDelete: (id: string) => void;
}) {
  return (
    <Table<ProviderRead>
      rowKey="id"
      loading={loading}
      dataSource={providers}
      pagination={false}
      columns={[
        {
          title: "名称",
          dataIndex: "name",
          render: (name: string, row) => (
            <Space size={6}>
              <Typography.Text strong>{name}</Typography.Text>
              {row.is_default ? <Tag color="blue">默认</Tag> : null}
              <Tag color={row.status === "enabled" ? "green" : "default"}>{row.status}</Tag>
            </Space>
          ),
        },
        { title: "base_url", dataIndex: "base_url" },
        { title: "默认模型", dataIndex: "default_model", render: (value: string | null) => value ?? "-" },
        {
          title: "api_key",
          dataIndex: "api_key_masked",
          render: (value: string) => <Typography.Text code>{value || "（未设置）"}</Typography.Text>,
        },
        {
          title: "最近检测",
          dataIndex: "last_check_error",
          render: (value: string | null, row) =>
            row.last_check_at ? (
              <Typography.Text type={value ? "danger" : "success"}>
                {value ? `失败：${value}` : "成功"}
              </Typography.Text>
            ) : (
              <Typography.Text type="secondary">未检测</Typography.Text>
            ),
        },
        {
          title: "操作",
          render: (_, row) => (
            <Space size={4}>
              <Button type="link" icon={<ApiOutlined />} loading={testing} onClick={() => onTest(row.id)}>
                测试
              </Button>
              <Button type="link" icon={<EditOutlined />} onClick={() => onEdit(row)}>
                编辑
              </Button>
              <Popconfirm title="删除该 Provider？" onConfirm={() => onDelete(row.id)}>
                <Button type="link" danger icon={<DeleteOutlined />}>
                  删除
                </Button>
              </Popconfirm>
            </Space>
          ),
        },
      ]}
    />
  );
}

/** 新建 / 编辑表单；`editing` 为空即新建模式（`api_key` 留空表示不修改）。 */
function ProviderModal({
  open,
  editing,
  form,
  loading,
  onCancel,
  onSubmit,
}: {
  open: boolean;
  editing: ProviderRead | null;
  form: FormInstance<ProviderFormValues>;
  loading: boolean;
  onCancel: () => void;
  onSubmit: (values: ProviderFormValues) => void;
}) {
  return (
    <Modal
      title={editing ? `编辑 ${editing.name}` : "新建 Provider"}
      open={open}
      confirmLoading={loading}
      onCancel={onCancel}
      onOk={() => form.submit()}
      destroyOnClose
    >
      <Form form={form} layout="vertical" onFinish={onSubmit}>
        <Form.Item name="name" label="名称" rules={[{ required: true, message: "必填" }]}>
          <Input placeholder="default-openai-compatible" />
        </Form.Item>
        <Form.Item
          name="base_url"
          label="base_url"
          rules={[{ required: true, message: "必填" }]}
          extra="OpenAI 兼容端点：缺 /v1 会自动补；DashScope / DeepSeek / vLLM / Ollama 都可用"
        >
          <Input placeholder="https://api.deepseek.com" />
        </Form.Item>
        <Form.Item
          name="api_key"
          label="api_key"
          extra={editing ? "留空表示不修改，填值则覆盖" : "写入前加密存储；响应只回掩码"}
        >
          <Input.Password placeholder="sk-..." autoComplete="new-password" />
        </Form.Item>
        <Form.Item name="default_model" label="默认模型">
          <Input placeholder="deepseek-chat" />
        </Form.Item>
        <Form.Item name="model_name" label="白名单模型" extra="MVP 只填一个；Agent 只能引用白名单内的模型">
          <Input placeholder="deepseek-chat" />
        </Form.Item>
        <Form.Item name="is_default" label="设为平台默认" valuePropName="checked">
          <Switch />
        </Form.Item>
      </Form>
    </Modal>
  );
}
