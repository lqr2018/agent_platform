# Phase 3（Workflow）时序与链路

> 本文档按《详细设计》附录 F 规则 3，为 **Phase 3** 补的时序/流程图（Phase 1 见 `phase-1-agent-runtime.md`，
> Phase 2 见 `phase-2-tool-calling.md`）。图中的事件名、表名、错误码与《详细设计》3.2.7 / 2.9 / 4.5 一致。

## 1. 手动运行一次 Workflow（`POST /workflows/{id}/runs`）

```text
浏览器                services/workflow_service.py            runtime/workflow/*        db
  │ POST /workflows/{id}/runs  {"input": {...}}                │                       │
  ├────────────────────────────────────────────────────────────>│ ① 再校验 definition（引用可能已变）
  │                                            202 + workflow_runs(running)           │ ② 插 workflow_runs
  │                                            ③ 起后台任务 _execute（独立 session）   │ ③ 插 runs(kind=workflow)
  │                                            │                                       │     + traces
  │ 轮询 GET /workflow-runs/{id}（2s）          │ SimpleEngine.run(graph, state)        │
  │                                            │   ├─ workflow span │                  │
  │                                            │   ├─ sink.state_updated(current=node)  │ 4.5.4 断点：进节点前
  │                                            │   ├─ emit(workflow.node.started)（13） │
  │                                            │   ├─ node span（node:{id}）             │
  │                                            │   │   ├─ agent 节点 → AgentRuntime.run │ run→agent→llm/tool
  │                                            │   │   │      （共用同一 runs/trace）    │
  │                                            │   │   └─ tool 节点 → ToolExecutor        │ tool_invocations
  │                                            │   ├─ node_runs 行（开行 + 收尾各一次写） │
  │                                            │   ├─ emit(workflow.node.completed)（14）│
  │                                            │   └─ sink.state_updated(current=next)  │ 4.5.4 落点
  │ 轮询 GET /workflow-runs/{id}/node-runs（2s）│                                       │
  │    ← node_runs（按 seq）                    │ 结算：workflow_runs / runs / traces    │
```

要点：

- **不回 SSE**（3.4）：返回 202，前端按 2s 轮询运行详情与 `node-runs`（`WorkflowEditPage.tsx`）；
- 每个节点两次 `state_updated`：**进入前**（`current_node_id=本节点`，崩溃后从本节点续跑）与
  **结束后**（`current_node_id=下一个节点`，跑完 `end` 停在 `end`）；
- `node_runs.seq` 由落库 sink 用 `MAX(seq)` 续接 —— 同一 WorkflowRun 的多次尝试（含 `resume`）
  在页面上仍是稳定顺序；
- 一次执行 = 一条 `runs` 行 + 一棵 trace 树（4.8.1）：`run → workflow → node → agent → llm/tool`；
  `node_runs.agent_run_id` 指向这条 `runs.id`（附录 F v1.13 的口径说明）。

## 2. 节点执行与失败策略（4.5.2）

```text
render_inputs(node)                   → node_runs.input（模板静态检查已在保存时做过）
emit(workflow.node.started)
execute_node(node, inputs)
   ├─ start      → 原样（state 已含初始 input）            ├─ condition → branches[].when 首个为真 / default_next
   ├─ agent      → runner.run_agent_node（AgentRuntime）   └─ retriever → Phase 3 明确 NOT_IMPLEMENTED
   ├─ tool       → runner.run_tool_node（ToolExecutor 九步）
   └─ end        → 写 workflow_runs.output
成功 → node_runs(succeeded) + state 更新 + emit(completed)
失败 → 按 on_error：
        fail      → node_runs(failed) → Run 失败（error_code = 节点错误码）
        continue  → node_runs(failed) → 错误文本写入 output_key（tool / retriever 默认）
        retry(n)  → 重开一行 node_runs（attempt+1），最多 n+1 次
```

- 节点 span 的 `name` 固定为 `node:{node_id}`（DoD 4：分支行为在 Trace 中可见）；
- `max_steps`（总节点数）与 `recursion_limit`（同一节点进入次数）任一超出 → `WORKFLOW_MAX_STEPS_EXCEEDED`。

## 3. Checkpoint / Resume（4.5.4）

```text
checkpoint = { engine: "simple", engine_version: "1", state: {...}, current_node_id: "<下一个/失败节点>" }

resume 前置判定（其余一律 409 WORKFLOW_RUN_NOT_RESUMABLE）：
  succeeded / canceled / pending            → RUN_ALREADY_FINISHED
  failed + 错误码不在可重试集合              → ERROR_NOT_RETRYABLE
  running + 仍在本进程活跃表                 → RUN_STILL_ACTIVE
  running + 没有 current_node_id             → NO_CHECKPOINT
  engine / engine_version 不匹配             → 降级"从头重跑"（保留已保存 state，SD-10）

resume 执行：新 runs 行 + 新 trace → SimpleEngine.resume(checkpoint)
  └─ 从 checkpoint.current_node_id 起跑（**不回放**已完成的节点）
```

## 4. Chat 内联 Workflow（4.4.4 / 3.4 事件 13/14）

```text
POST /conversations/{id}/messages
  └─ chat_service.start_chat
       ├─ agent.workflow_id 为空 → AgentRuntime 直跑（Phase 1/2 行为不变）
       └─ agent.workflow_id 非空 → workflow_service.start_run(emit_queue=…)
            initial state = {"input": <用户消息>, "conversation_id": …, "agent_id": …}
SSE：run.started → workflow.node.started/completed（13/14）→ message.*（节点内的 agent）→ run.completed
      （同一条流；agent 节点共用会话，产出的 assistant 消息落 messages）
```

## 5. 前端落点（5.1 / 5.2）

```text
/workflows（pages/WorkflowsPage.tsx）   api/workflows.ts        app/api/v1/workflows.py
  列表 / 筛选 / 新建 / 发布 / 删除 ──────> listWorkflows / createWorkflow / publishWorkflow / deleteWorkflow
/workflows/:id（pages/WorkflowEditPage.tsx）
  JSON 编辑 + 保存（definition 变更回 draft）──> updateWorkflow      PATCH  /workflows/{id}
  校验（含未保存草稿 + 只读图投影）        ────> validateWorkflow    POST   /workflows/{id}/validate
  试跑（202）+ 2s 轮询节点状态            ────> startWorkflowRun    POST   /workflows/{id}/runs
                                               listWorkflowRuns   GET    /workflow-runs
                                               getWorkflowRun     GET    /workflow-runs/{id}
                                               listNodeRuns       GET    /workflow-runs/{id}/node-runs
  续跑 / 取消                             ────> resume/cancel       POST   /workflow-runs/{id}/{resume|cancel}
components/workflow/WorkflowGraphView.tsx ← 定义顺序 + 出边 + 运行中节点状态（只读，SD-6）
```
