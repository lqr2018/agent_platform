# Phase 7（评测脚本链路）时序与链路

> 本文档按《详细设计》附录 F 规则 3，为 **Phase 7 的评测脚本链路**补的时序/流程图（Phase 0–3 见
> `phase-0-bootstrap.md` / `phase-1-agent-runtime.md` / `phase-2-tool-calling.md` / `phase-3-workflow.md`，
> Phase 5 见 `phase-5-rag.md`）。图中的命令、表名、指标名与《详细设计》4.9 / 7.8 一致。
>
> **范围**：本图只画已落地的评测脚本链路。`GET /stats/overview`、`GET /traces/{trace_id}/tree` 与
> Evaluation 页面**尚未落地**（7.8 的 DoD 剩余项 + 迭代 D，见附录 F v1.19）。

## 1. 链路总览

```text
configs/eval/{contract-suite,demo-suite}.yaml
        │ YamlSuiteSource（4.9.1）：解析 + 结构校验 + 过滤 `enabled: false`（不进分母）
        ▼
python -m app.scripts.evaluate（`app/scripts/evaluate.py`）
        │ --suite / --agent / --cases / --concurrency / --judge / --output / --json / --timeout
        ├─ eval_service.build_setup
        │    ├─ resolve_agent(id 或 name) → Agent 快照（model_name / prompt_version / tool_ids / knowledge_base_ids）
        │    ├─ LLMProvider：按 Agent 的 model_provider 实例化（契约用例集 = FakeLLMProvider，无网络）
        │    ├─ judge：**仅当用例集含 `llm_judge`** 时构建（默认同 Agent 模型）
        │    └─ CaseExecutor = DatabaseCaseExecutor（真实运行时，不是评测专用执行器）
        ├─ EvalRunner.run
        │    ├─ 用例级并发 ≤ `EVAL_CONCURRENCY`（默认 2）
        │    ├─ 单 case 超时 = 用例 `timeout_seconds` 或 120s（+5s 余量）
        │    └─ 每条用例 → CaseObservation{answer, tool_calls, latency_ms, steps, total_tokens, cost_usd,
        │                trace_id, run_id, tool_spans_total/succeeded, workflow_runs/succeeded}
        │          → evaluate_assertions（**全部执行、不短路**）→ CaseResult{status, assertions[detail]}
        ├─ metrics.compute_metrics → EvalMetrics（4.9.2）
        │    task_success_rate / tool_success_rate / workflow_success_rate（无 workflow 用例 = null）
        │    avg 与 P95 延迟 / avg_tool_calls / avg_total_tokens / total_cost_usd / error_rate / judge 分布
        └─ report.write_report → data/reports/eval-<ts>.md + .json
             ├─ render_text（大纲式文本）/ render_markdown（4.9.2 模板）
             ├─ summary_to_dict：`--json` 的 stdout 与落盘 `.json` **同源**（同一次 summary）
             └─ prune_reports（REPORT_KEEP = 20）
        退出码 = min(99, failed + errors)；配置/环境问题（用例文件写错 / 库没迁移 / Agent 不存在）→ 2
```

## 2. 单条用例：执行 → 落库 → 断言

```text
EvalRunner                     eval_service.DatabaseCaseExecutor            运行时 / DB
   │ case, agent_id, timeout
   ├────────────────────────────>│
   │                             ├─ 新建 Conversation（title = `eval:<suite>:<case.id>`）
   │                             ├─ 新建 Run(kind=eval, status=running) ──────────> runs 一行（每条用例一条）
   │                             ├─ 绑了 KB？→ chat_service.retrieve_context（**首轮 LLM 之前**）
   │                             │     └─ span `retriever:{kb_ids}`：strategy=prefetch / hit_count / output.chunk_ids
   │                             ├─ AgentRuntime.run（工具循环；agent.workflow_id 非空则走 SimpleEngine）
   │                             │     └─ span `agent:*` → `llm:*` / `tool:{name}`（tool_invocations 同步落库）
   │                             ├─ 收尾：Run.status = succeeded（**断言失败与工具失败都不改 Run 状态**）
   │                             └─ _observe：从 runs / spans / tool_invocations 汇总 CaseObservation
   │<────────────────────────────┤  （answer、tool_calls 顺序、latency、tokens、cost、trace_id、run_id）
   ├─ evaluate_assertion(...)  ×  len(expected)   # 确定性断言走本地、`llm_judge` 走 judge 模型
   └─ CaseResult{status ∈ passed / failed / error, assertions[].detail}
```

要点：

- **`error` 与 `failed` 分开**：断言没过 = `failed`；执行抛异常（超时 / `AppError` / 未知异常）**或**断言本身求值不了 = `error`，
  两类都计入退出码，但报告里的定位信息不同（4.9.2）；
- **`trace_id` 是唯一的定位入口**：报告与 `--json` 里每条用例都带 `trace_id`（失败用例必带），
  `/traces/{trace_id}` 能看到 `agent → llm / tool / retriever` 全链路 —— 这与"跑一次评测"用的是同一条
  运行时链路，所以不存在"评测里过、线上不过"的口径分叉（4.9.1）；
- **契约用例集的证据在 DB 里，不在模型输出里**：`tool_sequence` 由 `tool_invocations` / tool span 判定，
  "引用被注入"由 `retriever` span 的 `output.chunk_ids` 判定（`tests/integration/test_eval_contract_suite.py`）。


## 3. 怎么跑（本机复现）

```powershell
cd backend
# 契约用例集（离线、FakeLLMProvider；CI 里跑的就是这一条）
.\.venv\Scripts\python.exe -m app.scripts.evaluate --suite contract-suite --agent <测试 Agent 的 id|name>
# 真实模型用例集（含 llm_judge；需先 seed 出 research-agent）
.\.venv\Scripts\python.exe -m app.scripts.evaluate --suite demo-suite --agent research-agent --json
```

`--json` 的 stdout 与 `data/reports/eval-<ts>.json` 是同一份数据（`summary_to_dict`），可直接进 CI 断言；
退出码即"没过的用例数"，所以 `... ; if ($LASTEXITCODE -ne 0) { throw }` 就能当门禁用（4.9.4）。

## 4. 尚未落地（7.8 剩余项 + 迭代 D，不假装完成）

| 项 | 归属 | 现状 |
|---|---|---|
| `GET /stats/overview`（成功率/时延/工具调用/token/错误率/成本） | 7.8 任务 5（MVP） | 未落地 |
| `GET /traces/{trace_id}/tree`（服务端建树 + 前端折叠） | 7.8 任务 6（MVP） | 未落地（Phase 1 已提供 `GET /traces/{id}` 的 span 列表） |
| `eval_*` 四张表 + `DbSuiteSource` + `eval_api.py` | 迭代 D | 未落地（MVP 的用例来源是 YAML） |
| Evaluation 页面（逐用例 `detail` + 历史对比） | 迭代 D | 未落地 |
