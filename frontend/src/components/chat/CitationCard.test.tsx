import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import CitationCard from "@/components/chat/CitationCard";
import type { RetrievalState } from "@/stores/chatStore";

/**
 * 引用来源卡片（7.6 的 DoD：Chat 中引用的来源与 `/query` 一致，4.6.3）。
 *
 * 四种态：未命中（`hit_count = 0`）、命中但明细在拉（`chunks === null`）、
 * 命中且明细已到、命中但明细拉取失败（降级，不报错）。
 */

const BASE: RetrievalState = { kbIds: ["kb_1"], query: "怎么安装", hitCount: 0, chunks: [] };

describe("CitationCard", () => {
  it("未命中：明确说未检索到相关内容（不编造）", () => {
    render(<CitationCard retrieval={BASE} />);

    expect(screen.getByText(/命中 0 条/)).toBeInTheDocument();
    expect(screen.getByText(/未检索到相关内容/)).toBeInTheDocument();
    expect(screen.getByText(/怎么安装/)).toBeInTheDocument();
  });

  it("命中但明细未到：显示计数与加载态", () => {
    render(<CitationCard retrieval={{ ...BASE, hitCount: 2, chunks: null }} />);

    expect(screen.getByText(/命中 2 条/)).toBeInTheDocument();
    expect(screen.getByText(/正在取回引用明细/)).toBeInTheDocument();
  });

  it("命中且有明细：逐条显示 4.6.3 的 source 串、score 与正文", () => {
    render(
      <CitationCard
        retrieval={{
          ...BASE,
          hitCount: 1,
          chunks: [
            {
              chunkId: "c1",
              documentId: "d1",
              kbId: "kb_1",
              score: 0.83,
              source: "install.md#安装 page=2 score=0.83",
              content: "先安装依赖，再跑迁移。",
            },
          ],
        }}
      />,
    );

    expect(screen.getByText("install.md#安装 page=2 score=0.83")).toBeInTheDocument();
    expect(screen.getByText(/score 0.8300/)).toBeInTheDocument();
    expect(screen.getByText(/先安装依赖/)).toBeInTheDocument();
  });

  it("命中数大于 0 但明细拉取失败：降级提示，不影响回答", () => {
    render(<CitationCard retrieval={{ ...BASE, hitCount: 3, chunks: [] }} />);

    expect(screen.getByText(/引用明细获取失败/)).toBeInTheDocument();
  });
});
