import { createBrowserRouter } from "react-router-dom";

import AppLayout from "@/components/layout/AppLayout";
import AgentEditPage from "@/pages/AgentEditPage";
import AgentsPage from "@/pages/AgentsPage";
import ChatPage from "@/pages/ChatPage";
import DashboardPage from "@/pages/DashboardPage";
import ModelsPage from "@/pages/ModelsPage";
import NotFoundPage from "@/pages/NotFoundPage";
import TraceDetailPage from "@/pages/TraceDetailPage";
import TracesPage from "@/pages/TracesPage";

/**
 * 路由表（详细设计 5.2）。
 *
 * M1（Phase 0–1）：`/`（总览）、`/agents[/:id]`、`/models`、`/chat[/:id]`、`/traces[/:traceId]`。
 * **Backlog 页面（`/memory`、`/mcp`、`/evaluation`）不建空路由**（SD-14②）：
 * 是否显示入口由 `/api/v1/meta` 的 `features` 驱动。
 */
export const router = createBrowserRouter([
  {
    path: "/",
    element: <AppLayout />,
    children: [
      { index: true, element: <DashboardPage /> },
      { path: "agents", element: <AgentsPage /> },
      { path: "agents/:id", element: <AgentEditPage /> },
      { path: "models", element: <ModelsPage /> },
      { path: "chat", element: <ChatPage /> },
      { path: "chat/:id", element: <ChatPage /> },
      { path: "traces", element: <TracesPage /> },
      { path: "traces/:traceId", element: <TraceDetailPage /> },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
]);
