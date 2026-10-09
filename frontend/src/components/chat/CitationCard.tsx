/**
 * 引用来源卡片（详细设计 5.1 的 `components/chat/`、4.6.3、7.6）。
 *
 * Chat 里"这次回答依据了哪些切片"的落点：数据来自 `chatStore.citations`
 * （由事件 10 `retrieval.completed` 归纳，Phase 5）。与 `ToolCallCard` 分开：
 * 工具调用是**过程**，引用来源是**依据**。
 *
 * 事件 10 只带 `kb_ids` / `query` / `hit_count`（3.4 的三字段契约，不含正文），因此
 * `chunks === null` 时这里显示"正在取回引用明细"，明细由 `ChatPage` 用同一个 query 调
 * `POST /knowledge-bases/{id}/query` 补拉 —— 与 `/query`、Workflow `retriever` 节点产物
 * 共用 `RetrievedChunk.citation_source()`，三处口径一致（4.6.3）。
 */

import { Card, Empty, List, Space, Spin, Tag, Typography } from "antd";

import type { RetrievalState } from "@/stores/chatStore";

interface CitationCardProps {
  retrieval: RetrievalState;
  /** 明细还在拉取中（对应 `retrieval.chunks === null`）。 */
  loading?: boolean;
}

/** 单次 Run 的引用来源：命中数 + query + 逐条 `source` / `score` / 正文摘要。 */
export default function CitationCard({ retrieval, loading = false }: CitationCardProps) {
  const chunks = retrieval.chunks ?? [];
  const pending = loading || retrieval.chunks === null;

  return (
    <Card
      size="small"
      title={`引用来源（命中 ${retrieval.hitCount} 条）`}
      style={{ background: "#f6ffed", borderColor: "#b7eb8f" }}
    >
      <Space direction="vertical" size={6} style={{ width: "100%" }}>
        <Space size={8} wrap>
          <Tag color="green">RAG</Tag>
          <Typography.Text type="secondary">query：{retrieval.query || "-"}</Typography.Text>
          {retrieval.kbIds.length > 0 ? (
            <Typography.Text type="secondary">
              知识库：<Typography.Text code>{retrieval.kbIds.join(", ")}</Typography.Text>
            </Typography.Text>
          ) : null}
        </Space>

        {retrieval.hitCount === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="本次未检索到相关内容（知识库为空或问题超出语料）"
          />
        ) : pending ? (
          <Space size={8}>
            <Spin size="small" />
            <Typography.Text type="secondary">正在取回引用明细…</Typography.Text>
          </Space>
        ) : chunks.length === 0 ? (
          <Typography.Text type="warning">
            命中 {retrieval.hitCount} 条，但引用明细获取失败 —— 可到「知识库」页用同一问题做检索试算。
          </Typography.Text>
        ) : (
          <List
            size="small"
            dataSource={chunks}
            renderItem={(chunk, index) => (
              <List.Item style={{ display: "block", paddingInline: 0 }}>
                <Space direction="vertical" size={2} style={{ width: "100%" }}>
                  <Space size={8} wrap>
                    <Typography.Text strong>{`[${index + 1}]`}</Typography.Text>
                    <Typography.Text code>{chunk.source}</Typography.Text>
                    <Tag color="blue">score {chunk.score.toFixed(4)}</Tag>
                  </Space>
                  <Typography.Paragraph
                    style={{ margin: 0, whiteSpace: "pre-wrap", color: "#595959" }}
                    ellipsis={{ rows: 3, expandable: true, symbol: "展开" }}
                  >
                    {chunk.content}
                  </Typography.Paragraph>
                </Space>
              </List.Item>
            )}
          />
        )}
      </Space>
    </Card>
  );
}
