/**
 * 知识库（详细设计 5.2 的 `/knowledge`，M3）。
 *
 * 页面职责（对应 3.2.5 的 12 个端点 + 1 个运维端点）：
 *
 * - **KB**：列表 + 搜索 + 新建（后端探测 `embedding_dim`）/ 编辑配置 / 删除（软删 + 删向量集合）；
 * - **文档**：上传（multipart，`accept=".md,.txt"`）→ 202 `pending` → **每 2s 轮询**到 `ready` / `failed`
 *   （4.6.2 的状态流转）；失败显示 `error_code` / `error_message`，可 `reingest`；
 * - **切片预览**：`GET /{doc_id}/chunks` 分页（`offset` / `limit`）；
 * - **检索试算**：`POST /query`，支持多 KB 合并 —— 与 Chat 引用卡片、Workflow `retriever` 节点共用
 *   `RetrievedChunk.citation_source()`，三处口径一致（4.6.3）；
 * - **运维**：`GET /maintenance/knowledge-bases/{id}/verify-index` 对账（向量条数 vs DB 切片数）。
 */

import {
  DeleteOutlined,
  EditOutlined,
  ExperimentOutlined,
  FileSearchOutlined,
  PlusOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  List,
  message,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  Upload,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";

import {
  type ChunkRead,
  type DocumentRead,
  type KnowledgeBaseRead,
  DOCUMENT_POLL_INTERVAL_MS,
  UPLOAD_ACCEPT,
  createKnowledgeBase,
  deleteDocument,
  deleteKnowledgeBase,
  getKnowledgeBase,
  isDocumentSettled,
  listDocumentChunks,
  listDocuments,
  listKnowledgeBases,
  queryKnowledgeBase,
  reingestDocument,
  updateKnowledgeBase,
  uploadDocument,
  verifyKnowledgeBaseIndex,
} from "@/api/kb";
import { listProviders } from "@/api/models";

/** 4.6.2 的文档状态 → 颜色（`pending/parsing/chunking/embedding` 都算"在跑"）。 */
const STATUS_COLOR: Record<string, string> = {
  pending: "default",
  parsing: "processing",
  chunking: "processing",
  embedding: "processing",
  ready: "success",
  failed: "error",
};

/** 切片预览每页条数（后端上限 `MAX_CHUNK_PAGE_SIZE` = 200）。 */
const CHUNK_PAGE_SIZE = 20;

/** `knowledge_bases.stats`（`Record<string, unknown>`）里取一个数值。 */
function statNumber(stats: Record<string, unknown> | undefined, key: string): number {
  const value = stats?.[key];
  return typeof value === "number" ? value : 0;
}

function formatBytes(size: number): string {
  if (size < 1024) {
    return `${size} B`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  return `${(size / 1024 / 1024).toFixed(2)} MB`;
}

const notifyError = (error: Error) => message.error(error.message);

export default function KnowledgePage() {
  const queryClient = useQueryClient();
  const [keyword, setKeyword] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<KnowledgeBaseRead | null>(null);
  const [detailKbId, setDetailKbId] = useState<string | null>(null);

  const list = useQuery({
    queryKey: ["knowledge-bases", keyword],
    queryFn: () => listKnowledgeBases({ q: keyword || undefined }),
  });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["knowledge-bases"] });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteKnowledgeBase(id),
    onSuccess: async () => {
      message.success("已删除（软删 KB + 删除向量集合）");
      await refresh();
    },
    onError: notifyError,
  });

  const columns: ColumnsType<KnowledgeBaseRead> = [
    {
      title: "名称",
      dataIndex: "name",
      render: (value: string, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{value}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {row.description || row.collection_name}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 100,
      render: (value: string) => <Tag color={STATUS_COLOR[value] ?? "default"}>{value}</Tag>,
    },
    {
      title: "embedding",
      dataIndex: "embedding_model",
      render: (_value: string, row) => (
        <Typography.Text type="secondary">
          {row.embedding_model}（dim={row.embedding_dim} · {row.vector_store_kind}）
        </Typography.Text>
      ),
    },
    {
      title: "切片 / 检索",
      key: "split",
      render: (_value: unknown, row) => (
        <Typography.Text type="secondary">
          {row.splitter} · {row.chunk_size}/{row.chunk_overlap} · top_k={row.top_k} · thr=
          {row.score_threshold}
        </Typography.Text>
      ),
    },
    {
      title: "文档 / 切片",
      key: "stats",
      width: 130,
      render: (_value: unknown, row) => (
        <Typography.Text>
          {statNumber(row.stats, "document_count")} / {statNumber(row.stats, "chunk_count")}
        </Typography.Text>
      ),
    },
    { title: "更新时间", dataIndex: "updated_at", width: 200 },
    {
      title: "操作",
      key: "actions",
      width: 150,
      render: (_value: unknown, row) => (
        <Space size={0}>
          <Button size="small" type="link" onClick={() => setDetailKbId(row.id)}>
            详情
          </Button>
          <Button
            size="small"
            type="link"
            icon={<EditOutlined />}
            onClick={() => {
              setEditing(row);
              setFormOpen(true);
            }}
          />
          <Popconfirm
            title="删除该知识库？"
            description="软删 KB 并删除向量集合（documents / chunks 行保留供审计）"
            onConfirm={() => deleteMutation.mutate(row.id)}
          >
            <Button size="small" type="link" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Space wrap>
        <Typography.Title level={4} style={{ margin: 0 }}>
          知识库
        </Typography.Title>
        <Typography.Text type="secondary">
          上传 md / txt → 每 2s 轮询摄取进度（pending → parsing → chunking → embedding → ready）
        </Typography.Text>
        <Input.Search
          allowClear
          placeholder="按名称搜索"
          style={{ width: 220 }}
          onSearch={(value) => setKeyword(value.trim())}
        />
        <Button icon={<ReloadOutlined />} onClick={() => void refresh()}>
          刷新
        </Button>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => {
            setEditing(null);
            setFormOpen(true);
          }}
        >
          新建
        </Button>
      </Space>

      <Table<KnowledgeBaseRead>
        rowKey="id"
        size="small"
        loading={list.isLoading}
        dataSource={list.data ?? []}
        columns={columns}
        locale={{ emptyText: <Empty description="还没有知识库（上传 md/txt 后即可检索）" /> }}
      />

      <KnowledgeBaseFormModal
        open={formOpen}
        editing={editing}
        onClose={() => setFormOpen(false)}
        onSaved={async (kb) => {
          await refresh();
          setDetailKbId(kb.id);
        }}
      />

      <KbDetailDrawer
        kbId={detailKbId}
        onClose={() => setDetailKbId(null)}
        onEdit={(kb) => {
          setEditing(kb);
          setFormOpen(true);
        }}
      />
    </Space>
  );
}

/** 新建 / 编辑 KB 的表单值（`embedding_dim` / `collection_name` 由后端决定，不进表单）。 */
interface KnowledgeBaseFormValues {
  name: string;
  description?: string;
  embedding_provider_id: string;
  embedding_model: string;
  splitter: "recursive" | "markdown";
  chunk_size: number;
  chunk_overlap: number;
  top_k: number;
  score_threshold: number;
}

const FORM_DEFAULTS: KnowledgeBaseFormValues = {
  name: "",
  description: "",
  embedding_provider_id: "",
  embedding_model: "",
  splitter: "recursive",
  chunk_size: 800,
  chunk_overlap: 120,
  top_k: 5,
  score_threshold: 0.3,
};

/** 新建（探测 `embedding_dim`）/ 编辑配置（2.8：有文档后 `embedding_model` 会被后端拒绝改）。 */
function KnowledgeBaseFormModal({
  open,
  editing,
  onClose,
  onSaved,
}: {
  open: boolean;
  editing: KnowledgeBaseRead | null;
  onClose: () => void;
  onSaved: (kb: KnowledgeBaseRead) => Promise<void> | void;
}) {
  const [form] = Form.useForm<KnowledgeBaseFormValues>();
  const providers = useQuery({ queryKey: ["providers"], queryFn: () => listProviders(), enabled: open });
  const providerId = Form.useWatch("embedding_provider_id", form);
  const provider = (providers.data ?? []).find((item) => item.id === providerId);
  const modelOptions = (provider?.models ?? [])
    .filter((entry) => entry.name)
    .map((entry) => ({ value: entry.name, label: entry.name }));

  useEffect(() => {
    if (!open) {
      return;
    }
    form.setFieldsValue(
      editing
        ? {
            name: editing.name,
            description: editing.description,
            embedding_provider_id: editing.embedding_provider_id,
            embedding_model: editing.embedding_model,
            splitter: editing.splitter === "markdown" ? "markdown" : "recursive",
            chunk_size: editing.chunk_size,
            chunk_overlap: editing.chunk_overlap,
            top_k: editing.top_k,
            score_threshold: editing.score_threshold,
          }
        : FORM_DEFAULTS,
    );
  }, [editing, form, open]);

  const saveMutation = useMutation({
    mutationFn: (values: KnowledgeBaseFormValues) =>
      editing
        ? updateKnowledgeBase(editing.id, {
            name: values.name,
            description: values.description ?? "",
            embedding_model: values.embedding_model,
            splitter: values.splitter,
            chunk_size: values.chunk_size,
            chunk_overlap: values.chunk_overlap,
            top_k: values.top_k,
            score_threshold: values.score_threshold,
          })
        : createKnowledgeBase({
            name: values.name,
            description: values.description ?? "",
            embedding_provider_id: values.embedding_provider_id,
            embedding_model: values.embedding_model,
            splitter: values.splitter,
            chunk_size: values.chunk_size,
            chunk_overlap: values.chunk_overlap,
            top_k: values.top_k,
            score_threshold: values.score_threshold,
          }),
    onSuccess: async (kb) => {
      message.success(
        editing ? `已更新 ${kb.name}` : `已创建 ${kb.name}（embedding_dim=${kb.embedding_dim}）`,
      );
      onClose();
      await onSaved(kb);
    },
    onError: notifyError,
  });

  return (
    <Modal
      title={editing ? `编辑知识库 · ${editing.name}` : "新建知识库"}
      open={open}
      width={760}
      confirmLoading={saveMutation.isPending}
      onCancel={onClose}
      onOk={() => form.submit()}
    >
      <Form<KnowledgeBaseFormValues>
        form={form}
        layout="vertical"
        onFinish={(values) => saveMutation.mutate(values)}
      >
        <Space size={16} wrap style={{ width: "100%" }}>
          <Form.Item
            name="name"
            label="名称"
            rules={[{ required: true, message: "必填" }]}
            style={{ minWidth: 280 }}
          >
            <Input placeholder="handbook" maxLength={120} />
          </Form.Item>
          <Form.Item name="splitter" label="切片器" style={{ minWidth: 200 }}>
            <Select
              options={[
                { value: "recursive", label: "recursive" },
                { value: "markdown", label: "markdown（按标题切）" },
              ]}
            />
          </Form.Item>
        </Space>

        <Form.Item name="description" label="描述">
          <Input.TextArea rows={2} maxLength={2000} />
        </Form.Item>

        <Space size={16} wrap style={{ width: "100%" }}>
          <Form.Item
            name="embedding_provider_id"
            label="Embedding Provider"
            rules={[{ required: true, message: "必选" }]}
            style={{ minWidth: 280 }}
            extra={editing ? "建库后不可改（换 Provider 请新建知识库）" : undefined}
          >
            <Select
              showSearch
              disabled={Boolean(editing)}
              optionFilterProp="label"
              options={(providers.data ?? []).map((item) => ({
                value: item.id,
                label: `${item.name}（${item.kind}）`,
              }))}
            />
          </Form.Item>
          <Form.Item
            name="embedding_model"
            label="Embedding 模型"
            rules={[{ required: true, message: "必填" }]}
            style={{ minWidth: 280 }}
            extra={editing ? "有文档后禁改（2.8）" : "建库时会探测 embedding_dim"}
          >
            {modelOptions.length > 0 ? (
              <Select showSearch options={modelOptions} />
            ) : (
              <Input placeholder="text-embedding-3-small" />
            )}
          </Form.Item>
        </Space>

        <Space size={16} wrap>
          <Form.Item name="chunk_size" label="chunk_size">
            <InputNumber min={100} max={4000} />
          </Form.Item>
          <Form.Item name="chunk_overlap" label="chunk_overlap">
            <InputNumber min={0} max={1000} />
          </Form.Item>
          <Form.Item name="top_k" label="top_k">
            <InputNumber min={1} max={20} />
          </Form.Item>
          <Form.Item name="score_threshold" label="score_threshold">
            <InputNumber min={0} max={1} step={0.05} />
          </Form.Item>
        </Space>
      </Form>
    </Modal>
  );
}

/** 详情抽屉：KB 配置 + 文档列表（2s 轮询）+ 上传 + 切片预览 + 检索试算 + 运维对账。 */
function KbDetailDrawer({
  kbId,
  onClose,
  onEdit,
}: {
  kbId: string | null;
  onClose: () => void;
  onEdit: (kb: KnowledgeBaseRead) => void;
}) {
  const queryClient = useQueryClient();
  const [chunkDocument, setChunkDocument] = useState<DocumentRead | null>(null);

  const kb = useQuery({
    queryKey: ["knowledge-base", kbId],
    queryFn: () => getKnowledgeBase(kbId ?? ""),
    enabled: Boolean(kbId),
  });
  const documents = useQuery({
    queryKey: ["kb-documents", kbId],
    queryFn: () => listDocuments(kbId ?? ""),
    enabled: Boolean(kbId),
    // 4.6.2：只要还有非终态文档就每 2s 轮询；全部 ready / failed 即停。
    refetchInterval: (query) =>
      (query.state.data ?? []).some((doc) => !isDocumentSettled(doc.status))
        ? DOCUMENT_POLL_INTERVAL_MS
        : false,
  });

  /** 文档状态变化会同时影响列表 stats（`knowledge_bases.stats` 由服务层重算）。 */
  const refreshAll = async () => {
    await queryClient.invalidateQueries({ queryKey: ["kb-documents", kbId] });
    await queryClient.invalidateQueries({ queryKey: ["knowledge-base", kbId] });
    await queryClient.invalidateQueries({ queryKey: ["knowledge-bases"] });
  };

  const uploadMutation = useMutation({
    mutationFn: (file: File) => uploadDocument(kbId ?? "", file),
    onSuccess: async (document) => {
      message.success(`已上传 ${document.filename}（${document.status}）—— 摄取进度每 2s 刷新`);
      await refreshAll();
    },
    onError: notifyError,
  });

  const reingestMutation = useMutation({
    mutationFn: (documentId: string) => reingestDocument(kbId ?? "", documentId),
    onSuccess: async () => {
      message.success("已重新摄取（pending）");
      await refreshAll();
    },
    onError: notifyError,
  });

  const deleteDocumentMutation = useMutation({
    mutationFn: (documentId: string) => deleteDocument(kbId ?? "", documentId),
    onSuccess: async () => {
      message.success("已删除文档（含切片与向量）");
      await refreshAll();
    },
    onError: notifyError,
  });

  const verifyMutation = useMutation({
    mutationFn: () => verifyKnowledgeBaseIndex(kbId ?? ""),
    onError: notifyError,
  });

  const documentColumns: ColumnsType<DocumentRead> = [
    { title: "文件名", dataIndex: "filename" },
    {
      title: "状态",
      dataIndex: "status",
      width: 110,
      render: (value: string) => <Tag color={STATUS_COLOR[value] ?? "default"}>{value}</Tag>,
    },
    { title: "切片", dataIndex: "chunk_count", width: 70 },
    {
      title: "大小",
      dataIndex: "size_bytes",
      width: 100,
      render: (value: number) => formatBytes(value),
    },
    {
      title: "错误",
      dataIndex: "error_code",
      render: (value: string | null, row) =>
        value ? (
          <Typography.Text type="danger">{`${value}：${row.error_message ?? ""}`}</Typography.Text>
        ) : (
          "-"
        ),
    },
    {
      title: "操作",
      key: "actions",
      width: 210,
      render: (_value: unknown, row) => (
        <Space size={0}>
          <Button
            size="small"
            type="link"
            icon={<FileSearchOutlined />}
            disabled={row.chunk_count === 0}
            onClick={() => setChunkDocument(row)}
          >
            切片
          </Button>
          <Button size="small" type="link" onClick={() => reingestMutation.mutate(row.id)}>
            重新摄取
          </Button>
          <Popconfirm title="删除该文档？" onConfirm={() => deleteDocumentMutation.mutate(row.id)}>
            <Button size="small" type="link" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const running = (documents.data ?? []).some((doc) => !isDocumentSettled(doc.status));

  return (
    <Drawer
      title={kb.data ? `知识库详情 · ${kb.data.name}` : "知识库详情"}
      width={960}
      open={Boolean(kbId)}
      onClose={onClose}
      destroyOnHidden
    >
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Card
          size="small"
          title="配置"
          extra={
            <Space size={4}>
              <Button
                size="small"
                icon={<EditOutlined />}
                disabled={!kb.data}
                onClick={() => kb.data && onEdit(kb.data)}
              >
                编辑
              </Button>
              <Button
                size="small"
                icon={<SafetyCertificateOutlined />}
                loading={verifyMutation.isPending}
                disabled={!kbId}
                onClick={() => verifyMutation.mutate()}
              >
                校验索引
              </Button>
            </Space>
          }
        >
          {kb.data ? (
            <Descriptions
              size="small"
              column={3}
              items={[
                {
                  key: "id",
                  label: "id",
                  children: <Typography.Text code>{kb.data.id}</Typography.Text>,
                },
                {
                  key: "status",
                  label: "status",
                  children: <Tag color={STATUS_COLOR[kb.data.status] ?? "default"}>{kb.data.status}</Tag>,
                },
                {
                  key: "collection",
                  label: "collection",
                  children: <Typography.Text code>{kb.data.collection_name}</Typography.Text>,
                },
                {
                  key: "model",
                  label: "embedding_model",
                  children: `${kb.data.embedding_model}（dim=${kb.data.embedding_dim}）`,
                },
                { key: "store", label: "vector_store", children: kb.data.vector_store_kind },
                {
                  key: "splitter",
                  label: "splitter",
                  children: `${kb.data.splitter} · ${kb.data.chunk_size}/${kb.data.chunk_overlap}`,
                },
                {
                  key: "retriever",
                  label: "retriever",
                  children: `${kb.data.retriever_kind} · top_k=${kb.data.top_k} · thr=${kb.data.score_threshold}`,
                },
                {
                  key: "rerank",
                  label: "rerank_enabled",
                  children: kb.data.rerank_enabled ? "true" : "false（MVP 预留）",
                },
                {
                  key: "stats",
                  label: "文档 / 切片",
                  children: `${statNumber(kb.data.stats, "document_count")} / ${statNumber(kb.data.stats, "chunk_count")}`,
                },
              ]}
            />
          ) : (
            <Typography.Text type="secondary">{kb.isLoading ? "加载中…" : "未选择知识库"}</Typography.Text>
          )}

          {verifyMutation.data ? (
            <Alert
              style={{ marginTop: 8 }}
              showIcon
              type={verifyMutation.data.ok ? "success" : "warning"}
              message={`索引对账：${verifyMutation.data.detail}`}
              description={<Typography.Text code>{JSON.stringify(verifyMutation.data.data)}</Typography.Text>}
            />
          ) : null}
        </Card>

        <Card
          size="small"
          title={`文档（${documents.data?.length ?? 0}）`}
          extra={running ? <Tag color="processing">摄取中 · 每 2s 刷新</Tag> : null}
        >
          <Space direction="vertical" size={8} style={{ width: "100%" }}>
            <Upload.Dragger
              accept={UPLOAD_ACCEPT}
              multiple={false}
              showUploadList={false}
              disabled={!kbId || uploadMutation.isPending}
              beforeUpload={(file) => {
                // 返回 false 阻止 antd 自己发请求：上传交给 api/kb.ts（走统一信封与错误类型）。
                uploadMutation.mutate(file);
                return false;
              }}
            >
              <p className="ant-upload-drag-icon">
                <UploadOutlined />
              </p>
              <p className="ant-upload-text">点击或拖拽 md / txt 到此处上传</p>
              <p className="ant-upload-hint">
                上传即 202 `pending`；同 checksum 的文件不会重复 embedding（4.6.2）
              </p>
            </Upload.Dragger>

            <Table<DocumentRead>
              rowKey="id"
              size="small"
              pagination={false}
              loading={documents.isLoading}
              dataSource={documents.data ?? []}
              columns={documentColumns}
              locale={{ emptyText: <Empty description="还没有文档" /> }}
            />
          </Space>
        </Card>

        <RetrievalProbeCard kbId={kbId} />
      </Space>

      <DocumentChunksModal kbId={kbId} document={chunkDocument} onClose={() => setChunkDocument(null)} />
    </Drawer>
  );
}

/** 切片预览（分页）：`GET /{doc_id}/chunks`（7.6 的"上传后能看到切片"）。 */
function DocumentChunksModal({
  kbId,
  document,
  onClose,
}: {
  kbId: string | null;
  document: DocumentRead | null;
  onClose: () => void;
}) {
  const [offset, setOffset] = useState(0);

  useEffect(() => {
    setOffset(0); // 换文档时回到第一页
  }, [document?.id]);

  const chunks = useQuery({
    queryKey: ["chunks", kbId, document?.id, offset],
    queryFn: () => listDocumentChunks(kbId ?? "", document?.id ?? "", { offset, limit: CHUNK_PAGE_SIZE }),
    enabled: Boolean(kbId && document),
  });
  const rows = chunks.data ?? [];

  return (
    <Modal
      title={document ? `切片预览 · ${document.filename}（共 ${document.chunk_count} 条）` : "切片预览"}
      open={Boolean(document)}
      width={860}
      onCancel={onClose}
      footer={
        <Space>
          <Button
            size="small"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - CHUNK_PAGE_SIZE))}
          >
            上一页
          </Button>
          <Typography.Text type="secondary">
            {offset + 1} - {offset + rows.length}
          </Typography.Text>
          <Button
            size="small"
            disabled={rows.length < CHUNK_PAGE_SIZE}
            onClick={() => setOffset(offset + CHUNK_PAGE_SIZE)}
          >
            下一页
          </Button>
          <Button size="small" type="primary" onClick={onClose}>
            关闭
          </Button>
        </Space>
      }
    >
      <List<ChunkRead>
        size="small"
        loading={chunks.isLoading}
        dataSource={rows}
        locale={{ emptyText: <Empty description="这一页没有切片" /> }}
        renderItem={(chunk) => (
          <List.Item style={{ display: "block" }}>
            <Space direction="vertical" size={2} style={{ width: "100%" }}>
              <Space size={8} wrap>
                <Tag>#{chunk.ordinal}</Tag>
                <Typography.Text type="secondary">{chunk.token_count} tok</Typography.Text>
                <Typography.Text type="secondary">
                  {chunk.vector_id ? "已入向量库" : "无向量（切片 id 即向量 id）"}
                </Typography.Text>
              </Space>
              <Typography.Paragraph style={{ margin: 0, whiteSpace: "pre-wrap" }}>
                {chunk.content}
              </Typography.Paragraph>
            </Space>
          </List.Item>
        )}
      />
    </Modal>
  );
}

/**
 * 检索试算（`POST /knowledge-bases/{id}/query`）。
 *
 * 与 Chat 引用卡片、Workflow `retriever` 节点走**同一条** `Retriever`：返回里的 `source`
 * 由 `RetrievedChunk.citation_source()` 生成，因此三处显示的来源串逐字一致（4.6.3）。
 * `hit_count = 0` 是正常结果（未命中），不是错误。
 */
function RetrievalProbeCard({ kbId }: { kbId: string | null }) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const bases = useQuery({ queryKey: ["knowledge-bases", ""], queryFn: () => listKnowledgeBases() });

  useEffect(() => {
    setSelected(kbId ? [kbId] : []);
  }, [kbId]);

  const probe = useMutation({
    // 主 KB（路径）取第一个选中项；`kb_ids` 带上全部选中项做多库合并重排（2.8）。
    mutationFn: () => queryKnowledgeBase(selected[0] ?? "", { query, kb_ids: selected }),
    onError: notifyError,
  });
  const result = probe.data;
  const ready = selected.length > 0 && query.trim().length > 0;

  return (
    <Card size="small" title="检索试算（与 Chat 引用同一条 Retriever）">
      <Space direction="vertical" size={8} style={{ width: "100%" }}>
        <Space.Compact style={{ width: "100%" }}>
          <Input
            value={query}
            placeholder="输入一个问题，例如：怎么安装？"
            onChange={(event) => setQuery(event.target.value)}
            onPressEnter={() => {
              if (ready) {
                probe.mutate();
              }
            }}
          />
          <Button
            type="primary"
            icon={<ExperimentOutlined />}
            loading={probe.isPending}
            disabled={!ready}
            onClick={() => probe.mutate()}
          >
            检索
          </Button>
        </Space.Compact>

        <Select
          mode="multiple"
          style={{ width: "100%" }}
          placeholder="选择知识库（可多选：多库合并后按分数排序）"
          value={selected}
          onChange={setSelected}
          options={(bases.data ?? []).map((kb) => ({ value: kb.id, label: kb.name }))}
        />

        {result ? (
          result.hit_count === 0 ? (
            <Alert
              type="info"
              showIcon
              message="未检索到相关内容（知识库为空，或分数低于 score_threshold）"
            />
          ) : (
            <List
              size="small"
              dataSource={result.chunks ?? []}
              renderItem={(hit, index) => (
                <List.Item style={{ display: "block" }}>
                  <Space direction="vertical" size={2} style={{ width: "100%" }}>
                    <Space size={8} wrap>
                      <Typography.Text strong>{`[${index + 1}]`}</Typography.Text>
                      <Typography.Text code>{hit.source}</Typography.Text>
                      <Tag color="blue">score {hit.score.toFixed(4)}</Tag>
                      <Typography.Text type="secondary">kb={hit.kb_id}</Typography.Text>
                    </Space>
                    <Typography.Paragraph style={{ margin: 0, whiteSpace: "pre-wrap" }}>
                      {hit.content}
                    </Typography.Paragraph>
                  </Space>
                </List.Item>
              )}
            />
          )
        ) : null}
      </Space>
    </Card>
  );
}
