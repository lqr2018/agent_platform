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
  appliedEvents: number;
  /** 把服务端事件应用到本地状态（`useChatStream` 负责解析，store 只做归纳）。 */
  applyEvent: (event: SseEventType, data: Record<string, unknown>) => void;
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

  reset: () => set({ streaming: null, tools: [], run: IDLE_RUN, appliedEvents: 0 }),
}));
