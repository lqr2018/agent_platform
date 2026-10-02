import {
  ApiOutlined,
  DashboardOutlined,
  ExperimentOutlined,
  MessageOutlined,
  RobotOutlined,
  ShareAltOutlined,
} from "@ant-design/icons";
import { Layout, Menu, Typography } from "antd";
import { Link, Outlet, useLocation } from "react-router-dom";

import ReadinessBadge from "@/components/common/ReadinessBadge";

const { Header, Sider, Content } = Layout;

/**
 * 布局壳（详细设计 5.2 的菜单规则）。
 *
 * 只列**已实现**的页面：M1 = 总览 / Agent / 模型 / Chat / Trace；
 * Backlog 页面（记忆 / MCP / 评测台）不出现（SD-14②），是否显示入口由 `/api/v1/meta.features` 驱动。
 */
export default function AppLayout() {
  const location = useLocation();

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          background: "#fff",
          borderBottom: "1px solid #f0f0f0",
        }}
      >
        <Typography.Text strong style={{ fontSize: 16 }}>
          Agent Platform
        </Typography.Text>
        <ReadinessBadge />
      </Header>
      <Layout>
        <Sider width={208} theme="light">
          <Menu
            mode="inline"
            selectedKeys={[`/${location.pathname.split("/")[1] ?? ""}`]}
            style={{ borderInlineEnd: "none", paddingTop: 8 }}
            items={[
              { key: "/", icon: <DashboardOutlined />, label: <Link to="/">总览</Link> },
              { key: "/agents", icon: <RobotOutlined />, label: <Link to="/agents">Agent</Link> },
              { key: "/models", icon: <ApiOutlined />, label: <Link to="/models">模型</Link> },
              { key: "/chat", icon: <MessageOutlined />, label: <Link to="/chat">Chat</Link> },
              { key: "/traces", icon: <ShareAltOutlined />, label: <Link to="/traces">Trace</Link> },
              {
                key: "phase2",
                icon: <ExperimentOutlined />,
                label: "工具 / Workflow（Phase 2–3）",
                disabled: true,
              },
            ]}
          />
        </Sider>
        <Content style={{ padding: 20 }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
