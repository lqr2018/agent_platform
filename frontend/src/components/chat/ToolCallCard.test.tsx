import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import ToolCallCard from "@/components/chat/ToolCallCard";
import type { ToolCallTimelineItem } from "@/stores/chatStore";

/**
 * 工具调用卡片（DoD 1：前端展示"调用了什么、参数、结果、耗时"）。
 *
 * 数据形态与 `chatStore` 对事件 6/7/8 的归纳一致（`tool.call.started` → `completed` / `failed`）。
 */

const STARTED: ToolCallTimelineItem = {
  toolCallId: "call_1",
  toolName: "calculator",
  status: "started",
  arguments: { expression: "2+2" },
  resultPreview: "",
  errorCode: null,
  latencyMs: null,
};

describe("ToolCallCard", () => {
  it("执行中：显示名称、参数与状态，未完成时不显示耗时", () => {
    render(<ToolCallCard item={STARTED} />);

    expect(screen.getByText("calculator")).toBeInTheDocument();
    expect(screen.getByText("执行中")).toBeInTheDocument();
    expect(screen.getByText(/2\+2/)).toBeInTheDocument();
    expect(screen.queryByText(/ms/)).not.toBeInTheDocument();
  });

  it("成功：显示结果与耗时", () => {
    render(
      <ToolCallCard item={{ ...STARTED, status: "succeeded", resultPreview: "2+2 = 4", latencyMs: 12 }} />,
    );

    expect(screen.getByText("成功")).toBeInTheDocument();
    expect(screen.getByText("2+2 = 4")).toBeInTheDocument();
    expect(screen.getByText("12 ms")).toBeInTheDocument();
    expect(screen.queryByText("TOOL_TIMEOUT")).not.toBeInTheDocument();
  });

  it("失败：显示错误码（容错循环的可见性）", () => {
    render(
      <ToolCallCard
        item={{ ...STARTED, status: "failed", errorCode: "TOOL_PERMISSION_DENIED", latencyMs: 3 }}
      />,
    );

    expect(screen.getByText("失败")).toBeInTheDocument();
    expect(screen.getByText("TOOL_PERMISSION_DENIED")).toBeInTheDocument();
  });
});
