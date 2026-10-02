/**
 * 消费 Chat SSE 流（详细设计 5.1 的 `hooks/useChatStream.ts`）。
 *
 * `fetch` + `ReadableStream` → 按 3.4 的事件名分发给 `chatStore`；
 * 结束时把最终 assistant 消息交给 `onCompleted`（由页面写入 react-query 缓存）。
 */

import { useCallback, useRef, useState } from "react";

import { postMessage } from "@/api/chat";
import { readSse } from "@/api/sse";
import { useChatStore } from "@/stores/chatStore";
import type { SseEventType } from "@/types/events";

export interface ChatStreamResult {
  content: string;
  messageId: string | null;
  finishReason: string | null;
  ok: boolean;
  errorCode: string | null;
}

interface UseChatStreamOptions {
  conversationId: string | undefined;
  onSettled?: (result: ChatStreamResult) => void;
}

interface UseChatStreamState {
  isStreaming: boolean;
  error: string | null;
}

/** 返回 `send()` 与流状态；同一时刻只允许一个活跃流（后端也会用 409 兜底）。 */
export function useChatStream({ conversationId, onSettled }: UseChatStreamOptions) {
  const applyEvent = useChatStore((state) => state.applyEvent);
  const reset = useChatStore((state) => state.reset);
  const [state, setState] = useState<UseChatStreamState>({ isStreaming: false, error: null });
  const abortRef = useRef<AbortController | null>(null);

  const send = useCallback(
    async (content: string) => {
      if (!conversationId || state.isStreaming) {
        return;
      }
      reset();
      const controller = new AbortController();
      abortRef.current = controller;
      setState({ isStreaming: true, error: null });

      let answer = "";
      let messageId: string | null = null;
      let finishReason: string | null = null;
      let ok = true;
      let errorCode: string | null = null;

      try {
        const response = await postMessage(conversationId, { content, stream: true }, controller.signal);
        for await (const frame of readSse(response)) {
          applyEvent(frame.event as SseEventType, frame.data);
          if (frame.event === "message.delta") {
            answer += typeof frame.data["delta"] === "string" ? frame.data["delta"] : "";
          } else if (frame.event === "message.started") {
            messageId = typeof frame.data["message_id"] === "string" ? frame.data["message_id"] : null;
          } else if (frame.event === "message.completed") {
            finishReason =
              typeof frame.data["finish_reason"] === "string" ? frame.data["finish_reason"] : null;
            answer =
              typeof frame.data["content"] === "string" && frame.data["content"]
                ? frame.data["content"]
                : answer;
          } else if (frame.event === "run.failed") {
            ok = false;
            errorCode =
              typeof frame.data["error_code"] === "string" ? frame.data["error_code"] : "INTERNAL_ERROR";
          }
        }
      } catch (error) {
        ok = false;
        errorCode = error instanceof Error ? error.name : "UNKNOWN";
        setState({ isStreaming: false, error: error instanceof Error ? error.message : "请求失败" });
        onSettled?.({ content: answer, messageId, finishReason, ok, errorCode });
        abortRef.current = null;
        return;
      }

      setState({ isStreaming: false, error: null });
      onSettled?.({ content: answer, messageId, finishReason, ok, errorCode });
      abortRef.current = null;
    },
    [applyEvent, conversationId, onSettled, reset, state.isStreaming],
  );

  /** 停止本轮流（3.4：默认不取消 Run，仅断开连接）。 */
  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setState({ isStreaming: false, error: null });
  }, []);

  return { send, stop, ...state };
}
