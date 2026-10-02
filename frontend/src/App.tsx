import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ConfigProvider } from "antd";
import { RouterProvider } from "react-router-dom";

import { router } from "@/router";
import { themeConfig } from "@/theme";

/**
 * 应用根组件：服务端状态（react-query）+ 主题 + 路由（详细设计 5.1）。
 *
 * react-query 天然适配"长任务轮询"约定（3.1）：探针与运行状态都靠 `refetchInterval` 轮询。
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
      staleTime: 5_000,
    },
  },
});

export default function App() {
  return (
    <ConfigProvider theme={themeConfig}>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ConfigProvider>
  );
}
