# Phase 2（Tool Calling）时序与链路

> 本文档按《详细设计》附录 F 规则 3，为 **Phase 2** 补的时序/流程图（Phase 1 见 `phase-1-agent-runtime.md`）。
> 图中的事件名、表名、错误码与《详细设计》3.4 / 2.5 / 4.2 / 4.4.3 一致。

## 1. 一轮"带工具"的对话端到端时序

```text
浏览器                runtime/agent/runtime.py             services/tool_service.py        tool 实现 + db
  │ POST /conversations/{id}/messages                      │                                │
  ├───────────────────────────────────────────────────────>│ build_toolkit(session, settings, tool_ids=…)
  │                                                        │  ├ ToolExecutor(registry, sink=DatabaseToolInvocationSink)
  │                                                        │  └ load_definitions()（每步重查 tools）
  │ event: agent.step.started(step=1)                      │                                │
  │ event: message.started → llm.chat(tools=visible_schemas)│                                │
  │ event: message.delta ×N / usage.updated                │                                │
  │ event: message.completed(finish_reason=tool_calls)      │                                │
  │                                       ├─ memory.complete(tool_calls=…) → messages.tool_calls
  │                                       └─ for call in tool_calls:            # 顺序执行（SD-2）
  │ event: tool.call.started ──────────────┤ ToolExecutor.execute(call, ctx=ToolRunContext)
  │                                        │   ① 查表 ② 合并权限 ③ 校验参数 ④ 权限判定 ⑤ 次数闸门
  │                                        │   ⑥ 执行（builtin / api + 限流 + 超时）⑦ 输出截断
  │                                        │   ⑧ tool_invocations 一行 + tool span（结束即写）
  │ event: tool.call.completed / tool.call.failed ─────────┤
  │                                       ├─ memory.append(role=tool, tool_call_id, name)
  │ event: agent.step.completed(has_tool_calls=true)       │
  │ event: agent.step.started(step=2)                      │
  │ event: message.* → llm.chat(tools=…)  # 带上 tool 结果  │
  │ event: agent.step.completed(has_tool_calls=false)      │
  │ event: run.completed(steps, tool_call_count)           │
```

要点：

- **每一步重新计算可见工具**（4.2.2）：`ToolKit.load_definitions()` 每步查一次 `tools`，
  再按 `agents.tool_ids` 顺序过滤 `status=enabled` —— 运行期禁用工具，下一步立即生效；
- `runtime/**` 不 import `db` / `services`（1.2）：落库全部经注入的 `ToolInvocationSink`；
- 工具失败**不中断 Run**（`RUN_CANCELED` 除外）：错误文本作为 `role=tool` 消息回填，
  模型可自行修正或换工具；同一工具**连续失败 3 次**才终止 Run（`TOOL_REPEATED_FAILURE`）。

## 2. 九步流水线的落点（4.2.3）

| 步骤 | 落点（`runtime/tools/executor.py`） | 失败时的 `error_code` |
|---|---|---|
| 1 查表 | `_require_definition` | `TOOL_NOT_FOUND` / `TOOL_DISABLED`（含 MCP → `TOOL_TYPE_NOT_SUPPORTED`） |
| 2 合并策略 | `_merge_permission`（`ToolPermissionConfig.merged_with`） | — |
| 3 参数校验 | `_validate_arguments` | `TOOL_INVALID_ARGUMENTS`（回填含 `schema` 摘要） |
| 4 权限判定 | `_decide` → `permissions.evaluate_policy` | `TOOL_PERMISSION_DENIED`（`permission_decision=deny`） |
| 5 次数闸门 | `_check_budget` | `TOOL_PERMISSION_DENIED`（`reason=MAX_CALLS_EXCEEDED`） |
| 6 执行 | `_invoke_catching` → `_dispatch`（builtin / api） | `TOOL_TIMEOUT` / `TOOL_EXECUTION_FAILED` / `TOOL_SANDBOX_VIOLATION` |
| 7 输出处理 | `_finalize`（按 `max_output_bytes` 截断） | —（`result_truncated=true`） |
| 8 落库 | `ToolInvocationSink.write_invocation` + tool span | 写失败只记日志，不影响主流程 |
| 9 返回 | `ToolInvocationResult` | 供 `AgentRuntime` 组装 `role=tool` 消息 |

`tool.call.started`（事件 6）**只在步骤 4/5 通过后**发出；被拒 / 参数错误的调用只有
`tool.call.failed`（事件 8），但**同样留下** `tool_invocations` 行与 tool span（不静默失败）。

## 3. 权限与开关（2.5 / SD-17）

```text
safe                     → allow（guarded 亦放行；路径 / host 的细节校验在 sandbox.py）
dangerous / require_approval=true
   ├─ 工具在 REQUIRED_SWITCHES 且开关关闭 → deny（reason=SWITCH_DISABLED）
   ├─ 其它（无开关可开）                → deny（reason=APPROVAL_CHANNEL_UNAVAILABLE / DANGEROUS_TOOL_DISABLED）
   └─ 开关开启（FILE_WRITE_ENABLED / PYTHON_EXECUTE_ENABLED）→ allow
```

- MVP **不挂起 Run、不建 `approvals` 行**（SD-17：审批属迭代 C），`tool_invocations.approval_id` 恒为 `NULL`；
- Agent 级与工具级 `permission_config` 取**更严格者**（4.2.3 步骤 2）：`level` 取高、
  `require_approval` 取或、`allow_network` 取与、白名单取交集、数值项取小。

## 4. 停止条件补充（4.4.3）

```text
... ──┬── 正常结束（finish_reason=stop 且无 tool_calls）────────────> succeeded
      ├── MODEL_MAX_STEPS_EXCEEDED / RUN_TIMEOUT / MODEL_* ────────> failed
      ├── 用户取消 ────────────────────────────────────────────────> canceled（RUN_CANCELED）
      └── 同一工具连续失败 3 次 ───────────────────────────────────> failed（TOOL_REPEATED_FAILURE）
```

`runtime/agent/stop.py::StopReason` 为此新增 `REPEATED_TOOL_FAILURE` 取值（错误映射集中在 `error_for`）。
