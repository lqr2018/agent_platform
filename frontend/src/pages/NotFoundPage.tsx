import { Button, Result } from "antd";
import { Link } from "react-router-dom";

export default function NotFoundPage() {
  return (
    <Result
      status="404"
      title="404"
      subTitle="页面不存在（Backlog 页面不建路由，见 SD-14②）"
      extra={
        <Link to="/">
          <Button type="primary">回到总览</Button>
        </Link>
      }
    />
  );
}
