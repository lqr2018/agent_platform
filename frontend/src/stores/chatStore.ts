/**
 * Chat 局部状态（详细设计 5.1 的 `stores/chatStore.ts`）。
 *
 * 用 zustand 保存"必须跨组件共享且高频变化"的部分：流式增量缓冲、Run 状态、工具调用时间线。
 * 服务端数据（会话列表、历史消息）交给 react-query 缓存，不重复放进 store。
 */

import { create } from "zustand";

import type { SseEventType } from "@/types/events";

/** 一条正在流式生成中的 assistant 消息。 */
export interface StreamingMessage {
  messageId: string;
  role: string;
  model: string;
  content: string;
  finishReason: string | null;
}

/** 工具调用时间线的一项（Phase 2 起才会有内容，Phase 1 只保留结构与渲染）。 */
export interface ToolCallTimelineItem {
  toolCallId: string;
  toolName: string;
  status: "started" | "succeeded" | "failed";
  arguments: Record<string, unknown>;
  resultPreview: string;
  errorCode: string | null;
  latencyMs: number | null;
}

export interface RunState {
  runId: string | null;
  traceId: string | null;
  status: "idle" | "running" | "succeeded" | "failed" | "canceled";
  steps: number;
  toolCallCount: number;
  usage: { promptTokens: number; completionTokens: number; totalTokens: number; costUsd: number };
  latencyMs: number | null;
  errorCode: string | null;
  errorMessage: string | null;
}

/** 引用来源卡片的一项（`POST /knowledge-bases/{id}/query` 的 `QueryHitRead`，3.2.5 / 4.6.3）。 */
export interface CitationChunk {
  chunkId: string;
  documentId: string;
  kbId: string;
  score: number;
  source: string;
  content: string;
}

/**
 * 本轮 Run 的检索态（事件 10 `retrieval.completed`，Phase 5）。
 *
 * 事件本身只带 `kb_ids` / `query` / `hit_count`（3.4 的三字段契约），**不含切片正文**；
 * 明细由页面在收到事件后用**同一个 query**调 `POST /knowledge-bases/{kb_ids[0]}/query`
 * （带 `kb_ids` 多库）补拉 —— `chunks === null` 表示"还没拉回来"，`[]` 表示"确实没命中"。
 */
export interface RetrievalState {
  kbIds: string[];
  query: string;
  hitCount: number;
  chunks: CitationChunk[] | null;
}

const IDLE_RUN: RunState = {
  runId: null,
  traceId: null,
  status: "idle",
  steps: 0,
  toolCallCount: 0,
  usage: { promptTokens: 0, completionTokens: 0, totalTokens: 0, costUsd: 0 },
  latencyMs: null,
  errorCode: null,
  errorMessage: null,
};

interface ChatStoreState {
  streaming: StreamingMessage | null;
  tools: ToolCallTimelineItem[];
  run: RunState;
  /** 本轮 Run 的检索态（Phase 5，事件 10；`null` = 这一轮没触发检索）。 */
  citations: RetrievalState | null;
  appliedEvents: number;
  /** 把服务端事件应用到本地状态（`useChatStream` 负责解析，store 只做归纳）。 */
  applyEvent: (event: SseEventType, data: Record<string, unknown>) => void;
  /** 补拉回来的引用明细（事件 10 不含正文，见 `RetrievalState`）。 */
  setCitationChunks: (chunks: CitationChunk[]) => void;
  reset: () => void;
}

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" ? value : fallback;
}

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

export const useChatStore = create<ChatStoreState>((set) => ({
  streaming: null,
  tools: [],
  run: IDLE_RUN,
  citations: null,
  appliedEvents: 0,

  applyEvent: (event, data) =>
    set((state) => {
      const next = { ...state, appliedEvents: state.appliedEvents + 1 };
      switch (event) {
        case "run.started":
          return {
            ...next,
            streaming: null,
            tools: [],
            citations: null,
            run: {
              ...IDLE_RUN,
              runId: str(data["run_id"]) || null,
              traceId: str(data["trace_id"]) || null,
              status: "running",
            },
          };
        case "message.started":
          return {
            ...next,
            streaming: {
              messageId: str(data["message_id"]),
              role: str(data["role"], "assistant"),
              model: str(data["model"]),
              content: "",
              finishReason: null,
            },
          };
        case "message.delta":
          return {
            ...next,
            streaming: state.streaming
              ? { ...state.streaming, content: state.streaming.content + str(data["delta"]) }
              : state.streaming,
          };
        case "message.completed":
          return {
            ...next,
            streaming: state.streaming
              ? {
                  ...state.streaming,
                  content: str(data["content"]) || state.streaming.content,
                  finishReason: str(data["finish_reason"]) || null,
                }
              : state.streaming,
          };
        case "tool.call.started":
          return {
            ...next,
            tools: [
              ...state.tools,
              {
                toolCallId: str(data["tool_call_id"]),
                toolName: str(data["tool_name"]),
                status: "started",
                arguments: (data["arguments"] as Record<string, unknown> | undefined) ?? {},
                resultPreview: "",
                errorCode: null,
                latencyMs: null,
              },
            ],
          };
        case "tool.call.completed":
          return {
            ...next,
            tools: state.tools.map((item) =>
              item.toolCallId === str(data["tool_call_id"])
                ? {
                    ...item,
                    status: "succeeded",
                    resultPreview: str(data["result_preview"]),
                    latencyMs: num(data["latency_ms"]),
                  }
                : item,
            ),
          };
        case "tool.call.failed":
          return {
            ...next,
            tools: state.tools.map((item) =>
              item.toolCallId === str(data["tool_call_id"])
                ? { ...item, status: "failed", errorCode: str(data["error_code"]) }
                : item,
            ),
          };
        case "retrieval.completed": {
          const hitCount = num(data["hit_count"]);
          return {
            ...next,
            citations: {
              kbIds: Array.isArray(data["kb_ids"])
                ? data["kb_ids"].filter((item): item is string => typeof item === "string")
                : [],
              query: str(data["query"]),
              hitCount,
              // 事件只带计数（3.4 三字段契约）：命中时先占位，正文由页面用同一 query 补拉。
              chunks: hitCount === 0 ? [] : null,
            },
          };
        }
        case "usage.updated":
          return {
            ...next,
            run: {
              ...state.run,
              usage: {
                promptTokens: num(data["prompt_tokens"]),
                completionTokens: num(data["completion_tokens"]),
                totalTokens: num(data["total_tokens"]),
                costUsd: num(data["cost_usd"]),
              },
            },
          };
        case "run.completed":
          return {
            ...next,
            run: {
              ...state.run,
              status: "succeeded",
              steps: num(data["steps"]),
              toolCallCount: num(data["tool_call_count"]),
              latencyMs: num(data["latency_ms"]),
            },
          };
        case "run.failed":
          return {
            ...next,
            run: {
              ...state.run,
              status: str(data["error_code"]) === "RUN_CANCELED" ? "canceled" : "failed",
              errorCode: str(data["error_code"]) || "INTERNAL_ERROR",
              errorMessage: str(data["error_message"]),
            },
          };
        default:
          return next;
      }
    }),

  setCitationChunks: (chunks) =>
    set((state) => (state.citations ? { ...state, citations: { ...state.citations, chunks } } : state)),

  reset: () => set({ streaming: null, tools: [], citations: null, run: IDLE_RUN, appliedEvents: 0 }),
}));
