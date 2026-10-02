# Phase 1（最小 Agent Runtime）时序与链路

> 本文档按《详细设计》附录 F 规则 3，为 **Phase 1** 补的时序/流程图（Phase 0 见 `phase-0-bootstrap.md`）。
> 图中的事件名、表名、字段名与《详细设计》3.4 / 2.6 / 2.11 / 4.4 一致。

## 1. 一轮对话的端到端时序（Chat SSE）

```text
浏览器                  api/v1/chat.py           services/chat_service.py          runtime + db
  │                          │                            │                              │
  │ POST /api/v1/conversations/{id}/messages              │                              │
  │ Accept: text/event-stream│                            │                              │
  ├─────────────────────────>│ start_chat()               │                              │
  │                          ├───────────────────────────>│ ① 取会话 + Agent（禁用则 409）│
  │                          │                            │ ② registry.begin(run_id,     │
  │                          │                            │    conversation_id)          │
  │                          │                            │    └ 同会话已有活跃 Run → 409 │
  │                          │                            │ ③ messages 落 user 行（seq）  │
  │                          │                            │ ④ runs 落 running 行          │
  │                          │                            │    （input/trace_id 已写）    │
  │                          │                            ├── create_task(_execute) ────>│ 独立 session
  │<── 200 text/event-stream │                            │                              │
  │                          │                            │ ⑤ tracer.start_run(kind=chat)│
  │                          │                            │ ⑥ traces 落 running 行        │
  │ event: run.started       │<───────────── 队列 ────────┤ ⑦ emit(run.started)          │
  │ event: message.started   │<───────────── 队列 ────────┤ ⑧ memory.append(占位 assistant)
  │ event: message.delta ×N  │<───────────── 队列 ────────┤ ⑨ llm.chat(on_delta=…)       │
  │                          │                            │    └ spans 落 llm 行（结束即写）│
  │ event: usage.updated     │<───────────── 队列 ────────┤ ⑩ 累加 usage + cost          │
  │ event: message.completed │<───────────── 队列 ────────┤ ⑪ memory.complete(回填内容/用量)│
  │ event: run.completed     │<───────────── 队列 ────────┤ ⑫ runs 落终态 + traces 汇总   │
  │ event: heartbeat(15s 心跳)│<────────── 队列（无事件时）┤                              │
  │ event: done              │<───────────── 哨兵 ────────┤ finally: registry.finish()   │
```

要点：

- **Run 是独立任务**，不依赖请求生命周期：客户端断开默认**不取消**（3.4 的 `DETACH_CANCEL=false`），
  刷新页面仍能通过 `GET /conversations/{id}/messages` 看到完整历史；
- 队列只传 `(event, payload)`；SSE 帧的编码与心跳在 `api/v1/chat.py`（api 只做协议转换，1.2）；
- `runs` 行在 Run 开始时即插入（`status=running`），崩溃后能查到"孤儿 Run"（2.6）。

## 2. Trace 的固定层级与落库时机（4.8.1 / 2.11）

```text
seq=1  run      chat:{agent.name}          写入时机：Run 结束（traces 行在 Run 开始时先插入）
 └─ seq=2  agent   agent:{agent.name}      写入时机：agent span 退出（含聚合 usage / cost）
     └─ seq=3  llm    llm:{model_name}     写入时机：每次 LLM 调用结束（含 prompt/completion tokens）
```

- span 是"**结束时写库**"，所以物理写入顺序是 `llm → agent → run`；
  前端按 `seq` 排序即可还原 `run → agent → llm`（`spans(trace_id, seq)` 索引，2.13）；
- `traces.total_tokens / total_cost_usd` 只累加 **llm span**（agent/run span 上是聚合值，重复累加会翻倍）；
- `TRACE_STORE_IO=false` 时 `input` / `output` 存 `NULL`（4.8.2）。

## 3. Run 状态机在 Phase 1 的落点（2.6 / 4.4.3）

```text
pending ──start──> running ──┬── finish_reason=stop 且无 tool_calls ──> succeeded
                             ├── MODEL_* / RUN_TIMEOUT / INTERNAL_ERROR ─> failed
                             └── cancel（POST /runs/{id}/cancel）────────> canceled
```

| 停止条件 | 落点（代码） | `runs.error_code` |
|---|---|---|
| 正常结束 | `runtime/agent/runtime.py` 主循环 `return` | — |
| 步数上限 | `stop.evaluate` → `MODEL_MAX_STEPS_EXCEEDED` | `MODEL_MAX_STEPS_EXCEEDED` |
| 超时 | `asyncio.wait_for(agent.timeout_seconds)` → `RUN_TIMEOUT` | `RUN_TIMEOUT` |
| 用户取消 | `cancel` 事件 + `POST /runs/{id}/cancel` → `RUN_CANCELED` | `RUN_CANCELED` |
| 上游错误 | Provider 抛 `MODEL_*`（1.5.3 重试耗尽后） | `MODEL_TIMEOUT` 等 |
| 出现 tool_calls | Phase 1 显式拒绝（`NOT_IMPLEMENTED`，Phase 2 接管） | `NOT_IMPLEMENTED` |

## 4. Phase 1 的模块依赖（1.2 分层不可逆）

```text
api/v1/{model_providers,agents,conversations,chat,runs,traces}.py
   │  只做协议转换（含 SSE 帧编码）
   ▼
services/{model_provider_service,agent_service,conversation_service,run_service,trace_service,chat_service}.py
   │  事务边界 + ORM ↔ 快照装配 + 队列编排
   ▼
runtime/{llm,agent,memory,observability}   ← 不 import services / db（可脱离 DB 单测）
```

- `runtime` 只认快照（`ProviderConfig` / `AgentSpec`）与协议（`LLMProvider` / `ShortTermMemory` / `EventEmitter` / `SpanSink`）；
- **唯一适配点**是 `services/conversation_service.py::SqlShortTermMemory`（ORM ↔ runtime 的读写桥）与
  `services/trace_service.py::DatabaseSpanSink`（span 落库）。
