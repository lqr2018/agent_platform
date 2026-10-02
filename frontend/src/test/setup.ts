import "@testing-library/jest-dom/vitest";

import { afterEach, vi } from "vitest";

// 禁止测试打真实网络（后端 9.2 的同一原则）：未显式 stub fetch 时直接失败
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
