/**
 * antd 主题（详细设计 5.1：只取组件库，不追求视觉打磨）。
 */

import type { ThemeConfig } from "antd";

export const themeConfig: ThemeConfig = {
  token: {
    colorPrimary: "#2f54eb",
    borderRadius: 6,
    fontSize: 14,
  },
  components: {
    Layout: {
      headerHeight: 56,
      headerPadding: "0 20px",
    },
  },
};
