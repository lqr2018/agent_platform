import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import MessageBubble from "@/components/chat/MessageBubble";

/**
 * 消息气泡（7.9「测试要求」点名的 `MessageBubble` 渲染；3.4 的事件 3/4/5 → UI）。
 *
 * 覆盖：角色标签（assistant / user / tool）、正文、元信息（模型 / token / 耗时 /
 * `finish_reason`）、流式光标、换行保留（`white-space: pre-wrap`）。
 * 元信息缺省时**不渲染空占位** —— 后端没给数字就不假装有（与 `/stats` 空窗口显示 `-` 同一口径）。
 */
describe("MessageBubble", () => {
  it("assistant 消息：角色标签 + 正文；缺省的元信息不渲染空占位", () => {
    render(<MessageBubble message={{ role: "assistant", content: "你好" }} />);

    expect(screen.getByText("助手")).toBeInTheDocument();
    expect(screen.getByText("你好")).toBeInTheDocument();
    // 没有 total_tokens / latency_ms / finish_reason 时不留占位
    expect(screen.queryByText(/tok/)).not.toBeInTheDocument();
    expect(screen.queryByText(/ms/)).not.toBeInTheDocument();
  });

  it("user / tool 用各自的中文角色标签，user 气泡有独立底色", () => {
    render(<MessageBubble message={{ role: "user", content: "帮我算一下 12 万的 15%" }} />);
    render(<MessageBubble message={{ role: "tool", content: "2+2 = 4" }} />);

    expect(screen.getByText("我")).toBeInTheDocument();
    expect(screen.getByText("工具")).toBeInTheDocument();

    const userCard = screen.getByText("帮我算一下 12 万的 15%").closest(".ant-card");
    const toolCard = screen.getByText("2+2 = 4").closest(".ant-card");
    expect(userCard?.getAttribute("style")).not.toBe(toolCard?.getAttribute("style"));
  });

  it("元信息齐全：模型名、token 明细、耗时与 finish_reason 逐个显示", () => {
    render(
      <MessageBubble
        message={{
          role: "assistant",
          content: "答案",
          model_name: "fake-model",
          prompt_tokens: 10,
          completion_tokens: 5,
          total_tokens: 15,
          latency_ms: 42,
          finish_reason: "stop",
        }}
      />,
    );

    expect(screen.getByText("fake-model")).toBeInTheDocument();
    expect(screen.getByText("10→5 tok")).toBeInTheDocument();
    expect(screen.getByText("42 ms")).toBeInTheDocument();
    expect(screen.getByText("stop")).toBeInTheDocument();
  });

  it("流式中在正文末尾追加光标，结束后不残留", () => {
    const { unmount } = render(
      <MessageBubble message={{ role: "assistant", content: "生成中" }} streaming />,
    );

    expect(screen.getByText("生成中")).toBeInTheDocument();
    expect(screen.getByText("▌")).toBeInTheDocument();

    unmount();
    render(<MessageBubble message={{ role: "assistant", content: "生成中" }} />);
    expect(screen.queryByText("▌")).not.toBeInTheDocument();
  });

  it("正文保留换行（pre-wrap），不把多行答案压成一行", () => {
    render(<MessageBubble message={{ role: "assistant", content: "第一行\n第二行" }} />);

    expect(screen.getByText(/第一行/).style.whiteSpace).toBe("pre-wrap");
  });
});
