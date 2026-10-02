import "antd/dist/reset.css";

import React from "react";
import ReactDOM from "react-dom/client";

import App from "@/App";

const container = document.getElementById("root");
if (!container) {
  throw new Error("Root container #root not found");
}

ReactDOM.createRoot(container).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
