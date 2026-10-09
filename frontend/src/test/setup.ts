import "@testing-library/jest-dom/vitest";

import { afterEach, vi } from "vitest";

/**
 * 补上 jsdom 缺的浏览器 API（antd 依赖它们，缺了会在渲染期抛错）：
 *
 * - `matchMedia`：`Table` / `Grid` 的 responsive observer（`useBreakpoint`）；
 * - `ResizeObserver`：`Typography.Paragraph` 的 `ellipsis` 测量。
 */
if (typeof window.matchMedia !== "function") {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

if (!("ResizeObserver" in globalThis)) {
  class NoopResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = NoopResizeObserver;
}

// 禁止测试打真实网络（后端 9.2 的同一原则）：未显式 stub fetch 时直接失败
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
