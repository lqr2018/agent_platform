/**
 * Workflow 编辑 + 试跑（详细设计 5.2 的 `/workflows/:id`，M2）。
 *
 * 页面职责（对应 3.2.7 的全部端点）：
 *
 * - **JSON 编辑器**：保存（`PATCH`）/ 校验（`POST /validate`，可校验未保存草稿）/ 发布（`version+1`）；
 * - **校验反馈**：错误清单（`details.errors` 的 code/message/node_id）+ 只读图（SD-6）；
 * - **试跑**：`POST /workflows/{id}/runs` 返回 202 → 每 2s 轮询 `node-runs` 与运行详情（3.4）；
 * - **续跑 / 取消**：`resume`（4.5.4 的两类场景）/ `cancel`，失败原因直接显示在运行详情里。
 */

import {
  CaretRightOutlined,
  DeleteOutlined,
  PauseCircleOutlined,
  ReloadOutlined,
  SaveOutlined,
  SendOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Input,
  List,
  message,
  Popconfirm,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import {
  type NodeRunRead,
  type WorkflowRunRead,
  type WorkflowValidateResult,
  cancelWorkflowRun,
  deleteWorkflow,
  getWorkflow,
  getWorkflowRun,
  listNodeRuns,
  listWorkflowRuns,
  publishWorkflow,
  resumeWorkflowRun,
  startWorkflowRun,
  updateWorkflow,
  validateWorkflow,
} from "@/api/workflows";
import WorkflowGraphView from "@/components/workflow/WorkflowGraphView";

const STATUS_TAG: Record<string, string> = {
  running: "processing",
  succeeded: "success",
  failed: "error",
  canceled: "warning",
  pending: "default",
};

const POLL_INTERVAL_MS = 2000;
/** 3.4：Workflow 运行不返回 SSE，前端按 2s 轮询节点进度。 */

function isTerminal(status: string | undefined): boolean {
  return status === "succeeded" || status === "failed" || status === "canceled";
}

function parseJsonObject(text: string, label: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(text);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      message.error(`${label} 必须是 JSON 对象`);
      return null;
    }
    return parsed as Record<string, unknown>;
  } catch {
    message.error(`${label} 不是合法 JSON`);
    return null;
  }
}

export default function WorkflowEditPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [definitionText, setDefinitionText] = useState("");
  const [runInputText, setRunInputText] = useState('{\n  "input": "你好"\n}');
  const [validation, setValidation] = useState<WorkflowValidateResult | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);

  const workflow = useQuery({
    queryKey: ["workflow", id],
    queryFn: () => getWorkflow(id),
    enabled: Boolean(id),
  });
  useEffect(() => {
    if (workflow.data) {
      setDefinitionText(JSON.stringify(workflow.data.definition, null, 2));
    }
  }, [workflow.data]);

  const notifyError = (error: Error) => message.error(error.message);
  const refreshAll = async () => {
    await queryClient.invalidateQueries({ queryKey: ["workflow", id] });
    await queryClient.invalidateQueries({ queryKey: ["workflows"] });
  };

  const runQuery = useQuery({
    queryKey: ["workflow-run", activeRunId],
    queryFn: () => getWorkflowRun(activeRunId ?? ""),
    enabled: Boolean(activeRunId),
    refetchInterval: (query) =>
      isTerminal((query.state.data as WorkflowRunRead | undefined)?.status) ? false : POLL_INTERVAL_MS,
  });
  const nodeRunsQuery = useQuery({
    queryKey: ["workflow-run-nodes", activeRunId],
    queryFn: () => listNodeRuns(activeRunId ?? ""),
    enabled: Boolean(activeRunId),
    refetchInterval: () => (isTerminal(runQuery.data?.status) ? false : POLL_INTERVAL_MS),
  });
  const historyQuery = useQuery({
    queryKey: ["workflow-runs", id],
    queryFn: () => listWorkflowRuns({ workflow_id: id }),
    enabled: Boolean(id),
  });

  const saveMutation = useMutation({
    mutationFn: async (text: string) => {
      const definition = parseJsonObject(text, "图定义");
      if (!definition) {
        throw new Error("图定义不合法，请修正后重试");
      }
      return updateWorkflow(id, { definition });
    },
    onSuccess: async (row) => {
      message.success(`已保存（status=${row.status}，v${row.version}）`);
      setValidation(null);
      await refreshAll();
    },
    onError: notifyError,
  });

  const validateMutation = useMutation({
    mutationFn: async (text: string) => {
      const definition = parseJsonObject(text, "图定义");
      if (!definition) {
        throw new Error("图定义不合法，请修正后重试");
      }
      return validateWorkflow(id, definition);
    },
    onSuccess: (result) => {
      setValidation(result);
      message[result.valid ? "success" : "warning"](
        result.valid ? "校验通过" : `发现 ${result.errors.length} 个问题`,
      );
    },
    onError: notifyError,
  });

  const publishMutation = useMutation({
    mutationFn: () => publishWorkflow(id),
    onSuccess: async (row) => {
      message.success(`已发布到 v${row.version}`);
      await refreshAll();
    },
    onError: notifyError,
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteWorkflow(id),
    onSuccess: async () => {
      message.success("已删除");
      await refreshAll();
      navigate("/workflows");
    },
    onError: notifyError,
  });

  const startMutation = useMutation({
    mutationFn: async (text: string) => {
      const input = parseJsonObject(text, "运行输入");
      if (!input) {
        throw new Error("运行输入不合法，请修正后重试");
      }
      return startWorkflowRun(id, input);
    },
    onSuccess: async (started) => {
      message.success("已启动运行（进度每 2s 刷新）");
      setActiveRunId(started.id);
      await queryClient.invalidateQueries({ queryKey: ["workflow-runs", id] });
    },
    onError: notifyError,
  });

  const resumeMutation = useMutation({
    mutationFn: () => resumeWorkflowRun(activeRunId ?? ""),
    onSuccess: async () => {
      message.success("已从断点续跑");
      await queryClient.invalidateQueries({ queryKey: ["workflow-run", activeRunId] });
      await queryClient.invalidateQueries({ queryKey: ["workflow-runs", id] });
    },
    onError: notifyError,
  });

  const cancelMutation = useMutation({
    mutationFn: () => cancelWorkflowRun(activeRunId ?? ""),
    onSuccess: async () => {
      message.success("已请求取消");
      await queryClient.invalidateQueries({ queryKey: ["workflow-run", activeRunId] });
      await queryClient.invalidateQueries({ queryKey: ["workflow-runs", id] });
    },
    onError: notifyError,
  });

  const run = runQuery.data;
  const nodeRuns = nodeRunsQuery.data ?? [];
  const running = Boolean(run) && !isTerminal(run?.status);

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Space wrap>
        <Typography.Title level={4} style={{ margin: 0 }}>
          {workflow.data ? workflow.data.name : "Workflow"}
        </Typography.Title>
        {workflow.data ? (
          <Tag color={workflow.data.status === "published" ? "green" : "orange"}>{workflow.data.status}</Tag>
        ) : null}
        {workflow.data ? <Typography.Text type="secondary">v{workflow.data.version}</Typography.Text> : null}
        <Button
          icon={<SaveOutlined />}
          loading={saveMutation.isPending}
          onClick={() => saveMutation.mutate(definitionText)}
        >
          保存（定义变更回 draft）
        </Button>
        <Button icon={<ReloadOutlined />} onClick={() => validateMutation.mutate(definitionText)}>
          校验
        </Button>
        <Button
          icon={<SendOutlined />}
          loading={publishMutation.isPending}
          onClick={() => publishMutation.mutate()}
        >
          发布
        </Button>
        <Popconfirm
          title="删除该 Workflow？（有运行中的实例时会被拒绝）"
          onConfirm={() => deleteMutation.mutate()}
        >
          <Button danger icon={<DeleteOutlined />} />
        </Popconfirm>
      </Space>

      {validation ? <ValidationPanel result={validation} /> : null}

      <Card title="图定义（JSON）" size="small">
        <Input.TextArea
          rows={16}
          value={definitionText}
          onChange={(event) => setDefinitionText(event.target.value)}
          style={{ fontFamily: "monospace" }}
        />
        <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
          模板只支持 {"{{state.x}}"} / {"{{nodes.<id>.output}}"} / {"{{run.run_id}}"} 与白名单函数 （len / str
          / join / json.dumps）；`on_error` 取 fail / continue / retry(n)。
        </Typography.Paragraph>
      </Card>

      <Card size="small" title="只读图">
        <WorkflowGraphView graph={validation?.graph ?? undefined} nodeRuns={nodeRuns} />
      </Card>

      <Card
        size="small"
        title="试跑"
        extra={
          <Space>
            {running ? (
              <Button size="small" icon={<PauseCircleOutlined />} onClick={() => cancelMutation.mutate()}>
                取消
              </Button>
            ) : null}
            {run && run.status === "failed" ? (
              <Button size="small" onClick={() => resumeMutation.mutate()}>
                续跑（resume）
              </Button>
            ) : null}
          </Space>
        }
      >
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Input.TextArea
            rows={3}
            value={runInputText}
            onChange={(event) => setRunInputText(event.target.value)}
            style={{ fontFamily: "monospace" }}
          />
          <Button
            type="primary"
            icon={<CaretRightOutlined />}
            loading={startMutation.isPending}
            onClick={() => startMutation.mutate(runInputText)}
          >
            运行（202 + 每 2s 轮询）
          </Button>
          {run ? (
            <Descriptions
              size="small"
              column={2}
              items={[
                { key: "id", label: "run_id", children: <Typography.Text code>{run.id}</Typography.Text> },
                {
                  key: "status",
                  label: "status",
                  children: <Tag color={STATUS_TAG[run.status]}>{run.status}</Tag>,
                },
                { key: "node", label: "当前节点", children: run.current_node_id ?? "-" },
                {
                  key: "latency",
                  label: "耗时",
                  children: run.latency_ms === null ? "-" : `${run.latency_ms} ms`,
                },
                { key: "trigger", label: "trigger", children: run.trigger },
                { key: "trace", label: "trace_id", children: run.trace_id ?? "-" },
                {
                  key: "error",
                  label: "错误",
                  span: 2,
                  children: run.error_code ? `${run.error_code}：${run.error_message ?? ""}` : "-",
                },
                {
                  key: "state",
                  label: "state",
                  span: 2,
                  children: (
                    <Typography.Paragraph style={{ margin: 0 }}>
                      <pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>
                        {JSON.stringify(run.state, null, 2)}
                      </pre>
                    </Typography.Paragraph>
                  ),
                },
              ]}
            />
          ) : (
            <Typography.Text type="secondary">尚未运行</Typography.Text>
          )}
        </Space>
      </Card>

      <Card size="small" title={`节点执行记录（${nodeRuns.length}）`}>
        <Table<NodeRunRead>
          rowKey="id"
          size="small"
          pagination={false}
          dataSource={nodeRuns}
          columns={NODE_RUN_COLUMNS}
        />
      </Card>

      <Card size="small" title="历史运行">
        <List
          size="small"
          dataSource={historyQuery.data ?? []}
          renderItem={(item) => (
            <List.Item
              actions={[
                <Button key="view" size="small" type="link" onClick={() => setActiveRunId(item.id)}>
                  查看
                </Button>,
              ]}
            >
              <Space wrap>
                <Tag color={STATUS_TAG[item.status]}>{item.status}</Tag>
                <Typography.Text code>{item.id}</Typography.Text>
                <Typography.Text type="secondary">
                  {item.trigger} · v{item.workflow_version} · 当前节点 {item.current_node_id ?? "-"}
                </Typography.Text>
                {item.error_code ? <Typography.Text type="danger">{item.error_code}</Typography.Text> : null}
              </Space>
            </List.Item>
          )}
        />
      </Card>
    </Space>
  );
}

/** 校验面板：错误清单 + 结论（3.2.7 的 `/validate` 不合法也回 200，编辑器直接渲染）。 */
function ValidationPanel({ result }: { result: WorkflowValidateResult }) {
  if (result.valid) {
    return <Alert type="success" showIcon message="图校验通过" />;
  }
  return (
    <Alert
      type="warning"
      showIcon
      message={`图校验未通过（${result.errors.length} 个问题）`}
      description={
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          {result.errors.map((issue, index) => (
            <li key={`${issue.code}-${index}`}>
              <Typography.Text code>{issue.code}</Typography.Text> {issue.message}
              {issue.node_id ? `（node=${issue.node_id}）` : ""}
            </li>
          ))}
        </ul>
      }
    />
  );
}

const NODE_RUN_COLUMNS: ColumnsType<NodeRunRead> = [
  { title: "seq", dataIndex: "seq", width: 60 },
  { title: "node_id", dataIndex: "node_id", width: 140 },
  { title: "type", dataIndex: "node_type", width: 100 },
  {
    title: "status",
    dataIndex: "status",
    width: 100,
    render: (value: string) => <Tag color={STATUS_TAG[value] ?? "default"}>{value}</Tag>,
  },
  { title: "iter", dataIndex: "iteration", width: 60 },
  { title: "attempt", dataIndex: "attempt", width: 80 },
  {
    title: "耗时",
    dataIndex: "latency_ms",
    width: 90,
    render: (value: number | null) => (value === null ? "-" : `${value} ms`),
  },
  {
    title: "输出摘要",
    dataIndex: "output",
    render: (value: Record<string, unknown>) => (
      <Typography.Text type="secondary" style={{ whiteSpace: "pre-wrap" }}>
        {JSON.stringify(value)}
      </Typography.Text>
    ),
  },
  {
    title: "错误",
    dataIndex: "error_code",
    width: 200,
    render: (value: string | null, row) => (value ? `${value}：${row.error_message ?? ""}` : "-"),
  },
];
