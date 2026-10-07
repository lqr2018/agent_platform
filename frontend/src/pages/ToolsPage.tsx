/**
 * 工具管理（详细设计 5.2 的 `/tools`，M2）。
 *
 * 页面职责（对应 3.2.3 的七个端点）：
 *
 * - 列表 + 筛选（`?tool_type=&status=&q=`）与详情抽屉（schema / `http_config` / `permission_config`）；
 * - 内置工具的**编辑边界**（2.5：禁删、可禁用 / 可改权限）—— 编辑表单对 `is_system` 行只放开
 *   `status` / `permission_config` / `tags`，其余字段是代码属地（4.2.2），前端也不给入口；
 * - 新建 / 编辑 `api` 类型工具（`http_config` 与 `input_schema` 用 JSON 文本框，避免为自由结构造一套 UI）；
 * - "测试"：`POST /tools/{id}/test` 直接执行一次（跳过 LLM），把九步流水线的结论显示出来。
 */

import {
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Drawer,
  Form,
  Input,
  InputNumber,
  message,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
} from "antd";
import type { FormInstance } from "antd";
import { useState } from "react";

import {
  type PermissionLevel,
  type ToolDetail,
  type ToolPermissionConfig,
  type ToolRead,
  type ToolTestResult,
  createTool,
  deleteTool,
  getTool,
  listTools,
  testTool,
  updateTool,
} from "@/api/tools";

/** 权限配置的表单形态（与 `ToolPermissionConfigDTO` 同构，去掉可选性便于表单绑定）。 */
interface PermissionForm {
  level: PermissionLevel;
  require_approval: boolean;
  allowed_paths: string[];
  allow_network: boolean;
  allowed_hosts: string[];
  max_output_bytes: number;
  max_calls_per_run: number;
  timeout_seconds: number;
}

const DEFAULT_PERMISSION: PermissionForm = {
  level: "safe",
  require_approval: false,
  allowed_paths: [],
  allow_network: false,
  allowed_hosts: [],
  max_output_bytes: 65536,
  max_calls_per_run: 20,
  timeout_seconds: 15,
};

const DEFAULT_HTTP_CONFIG = JSON.stringify(
  {
    method: "GET",
    url: "https://api.example.com/search",
    query: { q: "{{query}}" },
    response_path: "$.data.items",
  },
  null,
  2,
);

interface ToolFormValues extends PermissionForm {
  name: string;
  display_name: string;
  description: string;
  input_schema_text: string;
  http_config_text: string;
  tags?: string[];
  status: "enabled" | "disabled";
}

const LEVEL_COLOR: Record<string, string> = { safe: "green", guarded: "orange", dangerous: "red" };

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function asNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asBoolean(value: unknown): boolean {
  return value === true;
}

/** 服务端 `permission_config`（`Record<string, unknown>`）→ 表单值（缺字段回落到 2.5 的默认）。 */
function toPermissionForm(config: Record<string, unknown> | undefined): PermissionForm {
  const level = config?.["level"];
  return {
    level: level === "guarded" || level === "dangerous" ? level : "safe",
    require_approval: asBoolean(config?.["require_approval"]),
    allowed_paths: asStringArray(config?.["allowed_paths"]),
    allow_network: asBoolean(config?.["allow_network"]),
    allowed_hosts: asStringArray(config?.["allowed_hosts"]),
    max_output_bytes: asNumber(config?.["max_output_bytes"], DEFAULT_PERMISSION.max_output_bytes),
    max_calls_per_run: asNumber(config?.["max_calls_per_run"], DEFAULT_PERMISSION.max_calls_per_run),
    timeout_seconds: asNumber(config?.["timeout_seconds"], DEFAULT_PERMISSION.timeout_seconds),
  };
}

/** 表单值 → 请求体（后端 `ToolPermissionConfigDTO` 会补默认值）。 */
function toPermissionPayload(values: PermissionForm): ToolPermissionConfig {
  return {
    level: values.level,
    require_approval: values.require_approval,
    allowed_paths: values.allowed_paths,
    allow_network: values.allow_network,
    allowed_hosts: values.allowed_hosts,
    max_output_bytes: values.max_output_bytes,
    max_calls_per_run: values.max_calls_per_run,
    timeout_seconds: values.timeout_seconds,
  };
}

/** JSON 文本框 → 对象（`any` 被 1.3 禁止，这里统一收窄为 `Record<string, unknown>`）。 */
function parseJsonObject(text: string, label: string): Record<string, unknown> | null {
  const trimmed = text.trim();
  if (!trimmed) {
    return {};
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    message.error(`${label} 不是合法 JSON`);
    return null;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    message.error(`${label} 必须是 JSON 对象`);
    return null;
  }
  return parsed as Record<string, unknown>;
}

function jsonBlock(value: unknown): string {
  return JSON.stringify(value ?? {}, null, 2);
}

export default function ToolsPage() {
  const queryClient = useQueryClient();
  const [toolType, setToolType] = useState<string | undefined>();
  const [status, setStatus] = useState<string | undefined>();
  const [keyword, setKeyword] = useState("");
  const [editing, setEditing] = useState<ToolRead | null>(null);
  const [creating, setCreating] = useState(false);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [testing, setTesting] = useState<ToolRead | null>(null);
  const [form] = Form.useForm<ToolFormValues>();

  const tools = useQuery({
    queryKey: ["tools", toolType, status, keyword],
    queryFn: () => listTools({ tool_type: toolType, status, q: keyword || undefined }),
  });
  const detail = useQuery({
    queryKey: ["tool", detailId],
    queryFn: () => getTool(detailId ?? ""),
    enabled: Boolean(detailId),
  });

  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ["tools"] });
    if (detailId) {
      await queryClient.invalidateQueries({ queryKey: ["tool", detailId] });
    }
  };
  const notifyError = (error: Error) => message.error(error.message);

  const saveMutation = useMutation({
    mutationFn: async (payload: { values: ToolFormValues; target: ToolRead | null }) => {
      const { values, target } = payload;
      const permission = toPermissionPayload(values);
      const inputSchema = parseJsonObject(values.input_schema_text, "input_schema");
      const httpConfig = parseJsonObject(values.http_config_text, "http_config");
      if (!inputSchema || !httpConfig) {
        throw new Error("JSON 字段不合法，请修正后重试");
      }
      if (target) {
        if (target.is_system) {
          // 2.5：内置行只放开 status / permission_config / tags（其余列以代码为准，4.2.2）
          return updateTool(target.id, {
            status: values.status,
            permission_config: permission,
            tags: values.tags ?? [],
          });
        }
        return updateTool(target.id, {
          name: values.name,
          display_name: values.display_name,
          description: values.description,
          status: values.status,
          input_schema: inputSchema,
          http_config: httpConfig,
          permission_config: permission,
          tags: values.tags ?? [],
        });
      }
      return createTool({
        name: values.name,
        display_name: values.display_name,
        description: values.description,
        tool_type: "api",
        status: values.status,
        input_schema: inputSchema,
        http_config: httpConfig,
        permission_config: permission,
        tags: values.tags ?? [],
      });
    },
    onSuccess: async (saved) => {
      message.success(`已保存 ${saved.name}`);
      setCreating(false);
      setEditing(null);
      form.resetFields();
      await refresh();
    },
    onError: notifyError,
  });

  const toggleMutation = useMutation({
    mutationFn: (payload: { id: string; next: "enabled" | "disabled" }) =>
      updateTool(payload.id, { status: payload.next }),
    onSuccess: async (saved) => {
      message.success(`${saved.name} → ${saved.status}`);
      await refresh();
    },
    onError: notifyError,
  });

  const deleteMutation = useMutation({
    mutationFn: deleteTool,
    onSuccess: async () => {
      message.success("已删除");
      await refresh();
    },
    onError: notifyError,
  });

  const testMutation = useMutation({
    mutationFn: (payload: { id: string; toolArguments: Record<string, unknown> }) =>
      testTool(payload.id, payload.toolArguments),
    onError: notifyError,
  });

  const openCreate = () => {
    form.resetFields();
    form.setFieldsValue({
      status: "enabled",
      input_schema_text: jsonBlock({ type: "object", properties: {}, additionalProperties: false }),
      http_config_text: DEFAULT_HTTP_CONFIG,
      ...DEFAULT_PERMISSION,
    });
    setCreating(true);
  };

  const openEdit = async (tool: ToolRead) => {
    // `http_config` 只在详情里返回（列表刻意不含请求头，3.2.3）→ 编辑 `api` 工具时补一次详情
    const full = tool.is_system
      ? null
      : await queryClient.fetchQuery({ queryKey: ["tool", tool.id], queryFn: () => getTool(tool.id) });
    form.resetFields();
    form.setFieldsValue({
      name: tool.name,
      display_name: tool.display_name,
      description: tool.description,
      status: tool.status === "disabled" ? "disabled" : "enabled",
      input_schema_text: jsonBlock(tool.input_schema),
      http_config_text: jsonBlock(full?.http_config),
      tags: tool.tags ?? [],
      ...toPermissionForm(tool.permission_config),
    });
    setEditing(tool);
  };

  const columns = [
    {
      title: "名称",
      dataIndex: "name",
      render: (value: string, row: ToolRead) => (
        <Space size={6}>
          <Typography.Text strong>{value}</Typography.Text>
          {row.is_system ? <Tag color="blue">内置</Tag> : <Tag>api</Tag>}
        </Space>
      ),
    },
    { title: "展示名", dataIndex: "display_name" },
    {
      title: "权限",
      dataIndex: "permission_config",
      render: (value: Record<string, unknown> | undefined) => {
        const config = toPermissionForm(value);
        return (
          <Space size={4} wrap>
            <Tag color={LEVEL_COLOR[config.level]}>{config.level}</Tag>
            {config.require_approval ? <Tag color="volcano">需显式开启</Tag> : null}
            {config.allow_network ? <Tag color="geekblue">可出网</Tag> : null}
            <Typography.Text type="secondary">≤{config.max_calls_per_run} 次/Run</Typography.Text>
          </Space>
        );
      },
    },
    {
      title: "状态",
      dataIndex: "status",
      render: (value: string, row: ToolRead) => (
        <Switch
          checked={value === "enabled"}
          checkedChildren="启用"
          unCheckedChildren="禁用"
          loading={toggleMutation.isPending}
          onChange={(checked) =>
            toggleMutation.mutate({ id: row.id, next: checked ? "enabled" : "disabled" })
          }
        />
      ),
    },
    {
      title: "更新时间",
      dataIndex: "updated_at",
      render: (value: string) => <Typography.Text type="secondary">{value}</Typography.Text>,
    },
    {
      title: "操作",
      render: (_: unknown, row: ToolRead) => (
        <Space size={4} wrap>
          <Button type="link" onClick={() => setDetailId(row.id)}>
            详情
          </Button>
          <Button type="link" icon={<EditOutlined />} onClick={() => void openEdit(row)}>
            编辑
          </Button>
          <Button type="link" icon={<ThunderboltOutlined />} onClick={() => setTesting(row)}>
            测试
          </Button>
          {row.is_system ? (
            <Typography.Text type="secondary">禁删</Typography.Text>
          ) : (
            <Popconfirm title="删除该工具？" onConfirm={() => deleteMutation.mutate(row.id)}>
              <Button type="link" danger icon={<DeleteOutlined />}>
                删除
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Card
        size="small"
        title="工具"
        extra={
          <Space size={8} wrap>
            <Input.Search
              allowClear
              placeholder="按名称筛选"
              style={{ width: 200 }}
              onSearch={(value) => setKeyword(value.trim())}
            />
            <Select
              allowClear
              placeholder="类型"
              style={{ width: 120 }}
              value={toolType}
              onChange={setToolType}
              options={[
                { value: "builtin", label: "builtin" },
                { value: "api", label: "api" },
              ]}
            />
            <Select
              allowClear
              placeholder="状态"
              style={{ width: 120 }}
              value={status}
              onChange={setStatus}
              options={[
                { value: "enabled", label: "enabled" },
                { value: "disabled", label: "disabled" },
              ]}
            />
            <Button icon={<ReloadOutlined />} onClick={() => void refresh()}>
              刷新
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
              新建 api 工具
            </Button>
          </Space>
        }
      >
        <Table<ToolRead>
          size="small"
          rowKey="id"
          loading={tools.isLoading}
          dataSource={tools.data ?? []}
          columns={columns}
          pagination={false}
          locale={{ emptyText: "没有工具（内置工具由迁移 0003 / 启动对齐写入）" }}
        />
      </Card>

      <ToolModal
        open={creating || editing !== null}
        editing={editing}
        form={form}
        loading={saveMutation.isPending}
        onCancel={() => {
          setCreating(false);
          setEditing(null);
          form.resetFields();
        }}
        onSubmit={(values) => saveMutation.mutate({ values, target: editing })}
      />

      <ToolTestModal
        tool={testing}
        result={testMutation.data}
        loading={testMutation.isPending}
        onClose={() => {
          setTesting(null);
          testMutation.reset();
        }}
        onRun={(toolArguments) => {
          if (testing) {
            testMutation.mutate({ id: testing.id, toolArguments });
          }
        }}
      />

      <ToolDetailDrawer tool={detail.data} loading={detail.isLoading} onClose={() => setDetailId(null)} />
    </Space>
  );
}

/** 新建 / 编辑表单；`editing` 为内置工具时只放开 `status` / `permission_config` / `tags`（2.5）。 */
function ToolModal({
  open,
  editing,
  form,
  loading,
  onCancel,
  onSubmit,
}: {
  open: boolean;
  editing: ToolRead | null;
  form: FormInstance<ToolFormValues>;
  loading: boolean;
  onCancel: () => void;
  onSubmit: (values: ToolFormValues) => void;
}) {
  const builtin = editing?.is_system === true;
  return (
    <Modal
      title={editing ? `编辑 ${editing.name}` : "新建 api 工具"}
      open={open}
      width={760}
      confirmLoading={loading}
      onCancel={onCancel}
      onOk={() => form.submit()}
      destroyOnClose
    >
      <Form form={form} layout="vertical" onFinish={onSubmit}>
        {builtin ? (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="内置工具的定义以代码为准（4.2.2）"
            description="只有 status / 权限配置 / 标签可以改；description、input_schema 等列由迁移 0003 与启动对齐维护。"
          />
        ) : null}

        <Form.Item name="name" label="名称（LLM 的函数名）" rules={[{ required: true, message: "必填" }]}>
          <Input placeholder="weather_now" disabled={builtin} />
        </Form.Item>
        <Form.Item name="display_name" label="展示名">
          <Input placeholder="实时天气" disabled={builtin} />
        </Form.Item>
        <Form.Item
          name="description"
          label="描述（直接进入 function schema，需精写）"
          extra="把 function schema 的 description 写好，模型选对工具的概率会显著提高"
        >
          <Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} disabled={builtin} />
        </Form.Item>
        <Form.Item name="status" label="状态">
          <Select
            options={[
              { value: "enabled", label: "enabled" },
              { value: "disabled", label: "disabled" },
            ]}
          />
        </Form.Item>
        <Form.Item name="input_schema_text" label="input_schema（JSON Schema 子集）">
          <Input.TextArea autoSize={{ minRows: 4, maxRows: 10 }} disabled={builtin} />
        </Form.Item>
        <Form.Item name="http_config_text" label="http_config（api 工具；{{arg}} 为调用参数占位符）">
          <Input.TextArea autoSize={{ minRows: 4, maxRows: 10 }} disabled={builtin} />
        </Form.Item>
        <Form.Item name="tags" label="标签">
          <Select mode="tags" open={false} placeholder="demo / prod" disabled={builtin} />
        </Form.Item>

        <Form.Item
          name="level"
          label="权限等级（level）"
          extra="内置工具的权限是运营侧地盘：改了不会被重启抹掉"
        >
          <Select
            options={[
              { value: "safe", label: "safe —— 直接允许" },
              { value: "guarded", label: "guarded —— 允许，细节由沙箱校验" },
              { value: "dangerous", label: "dangerous —— 必须显式开启开关" },
            ]}
          />
        </Form.Item>
        <Form.Item name="require_approval" label="require_approval" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item name="allow_network" label="allow_network" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item name="allowed_paths" label="allowed_paths">
          <Select mode="tags" open={false} placeholder="uploads/ / ../docs" />
        </Form.Item>
        <Form.Item name="allowed_hosts" label="allowed_hosts">
          <Select mode="tags" open={false} placeholder="api.example.com / .example.com" />
        </Form.Item>
        <Space size={12} wrap>
          <Form.Item name="max_output_bytes" label="max_output_bytes">
            <InputNumber min={1} step={1024} style={{ width: 160 }} />
          </Form.Item>
          <Form.Item name="timeout_seconds" label="timeout_seconds">
            <InputNumber min={0.1} step={1} style={{ width: 160 }} />
          </Form.Item>
          <Form.Item name="max_calls_per_run" label="max_calls_per_run">
            <InputNumber min={1} style={{ width: 160 }} />
          </Form.Item>
        </Space>
      </Form>
    </Modal>
  );
}

/** 试跑：`POST /tools/{id}/test`（跳过 LLM），把九步流水线的结论原样显示。 */
function ToolTestModal({
  tool,
  result,
  loading,
  onClose,
  onRun,
}: {
  tool: ToolRead | null;
  result: ToolTestResult | undefined;
  loading: boolean;
  onClose: () => void;
  onRun: (toolArguments: Record<string, unknown>) => void;
}) {
  const [argumentsText, setArgumentsText] = useState("{}");

  const run = () => {
    const parsed = parseJsonObject(argumentsText, "arguments");
    if (parsed) {
      onRun(parsed);
    }
  };

  return (
    <Modal
      title={tool ? `测试 ${tool.name}` : "测试"}
      open={tool !== null}
      width={720}
      footer={null}
      onCancel={onClose}
      destroyOnClose
    >
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Typography.Text type="secondary">
          直接执行一次（跳过 LLM），走的是运行期同一套九步流水线：参数校验 → 权限 → 沙箱 → 超时 → 截断。
        </Typography.Text>
        <Input.TextArea
          autoSize={{ minRows: 4, maxRows: 10 }}
          value={argumentsText}
          onChange={(event) => setArgumentsText(event.target.value)}
          placeholder='{"expression": "2+2"}'
        />
        <Button type="primary" icon={<ThunderboltOutlined />} loading={loading} onClick={run}>
          执行一次
        </Button>
        {result ? (
          <>
            <Alert
              type={result.ok ? "success" : "error"}
              showIcon
              message={
                result.ok
                  ? `执行成功 · ${result.latency_ms} ms`
                  : `${result.error_code}：${result.error_message}`
              }
            />
            <Descriptions
              size="small"
              column={2}
              items={[
                { key: "status", label: "status", children: result.status },
                { key: "permission", label: "权限判定", children: result.permission_decision },
                { key: "latency", label: "耗时", children: `${result.latency_ms} ms` },
                { key: "truncated", label: "输出截断", children: result.truncated ? "是" : "否" },
                {
                  key: "result",
                  label: "结果",
                  span: 2,
                  children: <Typography.Text code>{result.result ?? "-"}</Typography.Text>,
                },
                ...(Object.keys(result.details ?? {}).length > 0
                  ? [
                      {
                        key: "details",
                        label: "details",
                        span: 2,
                        children: <Typography.Text code>{JSON.stringify(result.details)}</Typography.Text>,
                      },
                    ]
                  : []),
              ]}
            />
          </>
        ) : null}
      </Space>
    </Modal>
  );
}

/** 详情抽屉：schema / `http_config` / `permission_config` 的原样 JSON（运营判断配置是否符合预期）。 */
function ToolDetailDrawer({
  tool,
  loading,
  onClose,
}: {
  tool: ToolDetail | undefined;
  loading: boolean;
  onClose: () => void;
}) {
  return (
    <Drawer
      title={tool ? `工具详情 · ${tool.name}` : "工具详情"}
      width={640}
      open={tool !== undefined}
      onClose={onClose}
    >
      {tool ? (
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Descriptions
            size="small"
            column={2}
            items={[
              { key: "id", label: "id", children: <Typography.Text code>{tool.id}</Typography.Text> },
              { key: "type", label: "tool_type", children: tool.tool_type },
              { key: "status", label: "status", children: tool.status },
              { key: "builtin", label: "builtin_name", children: tool.builtin_name ?? "-" },
              { key: "tags", label: "tags", children: (tool.tags ?? []).join(", ") || "-" },
              { key: "updated", label: "updated_at", children: tool.updated_at },
            ]}
          />
          <Typography.Paragraph style={{ margin: 0, whiteSpace: "pre-wrap" }}>
            {tool.description}
          </Typography.Paragraph>
          <JsonBlock title="input_schema" value={tool.input_schema} />
          <JsonBlock title="output_schema" value={tool.output_schema} />
          <JsonBlock title="http_config" value={tool.http_config} />
          <JsonBlock title="permission_config" value={tool.permission_config} />
        </Space>
      ) : (
        <Typography.Text type="secondary">{loading ? "加载中…" : "未选择工具"}</Typography.Text>
      )}
    </Drawer>
  );
}

function JsonBlock({ title, value }: { title: string; value: unknown }) {
  return (
    <Space direction="vertical" size={4} style={{ width: "100%" }}>
      <Typography.Text strong>{title}</Typography.Text>
      <Typography.Paragraph style={{ margin: 0 }}>
        <pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>{jsonBlock(value)}</pre>
      </Typography.Paragraph>
    </Space>
  );
}
