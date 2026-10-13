# Phase 8：三个 Demo 的链路与装载顺序（MVP 收口）

> 对齐《详细设计》7.9 / 8.1–8.5。本文画三件事：**三个 Demo 各自的运行链路**、**`seed_demo` 的装载顺序**、
> **8.5 的通用性验收怎么落地**。总架构见 `architecture.md`，各能力的细节见对应 Phase 图。

## 1. 三个 Demo 的类型差异只在 YAML

| Demo（8.2 / 8.3 / 8.4） | YAML | 绑定能力 | 主链路 |
|---|---|---|---|
| Research Agent（研究型） | `configs/agents/research-agent.yaml` | Workflow + Tool Calling + RAG | Chat 内联 Workflow（4.4.4） |
| Data Analysis Agent（数据/代码型） | `configs/agents/data-analysis-agent.yaml` | Tool Calling（危险工具默认关闭） | Chat 里的 Agent 工具循环 |
| Knowledge Assistant（知识库问答型） | `configs/agents/knowledge-assistant.yaml` | RAG（固定预检索） | Chat 里的预检索 + 引用 |

生产代码里**没有**任何 `if agent.name == ...` 分支：`tests/unit/test_demo_genericity.py` 用 AST 解析
`app/**/*.py`，把非 docstring 的字符串字面量拿去比对 Demo 侧标识符（Agent 名 / 配置名 / 示例 KB 名），
一旦出现就红；反向自检（扫描到的字面量 > 200 条）保证"没命中"不是因为扫描器坏了。

## 2. Demo 1：Chat 内联 Workflow（Research Agent）

```text
POST /conversations/{id}/messages   （agent.workflow_id = research-flow）
  │  chat_service 建 Run(kind=workflow)  → SimpleEngine 驱动整张图
  ├─① workflow.node.started   start
  ├─② workflow.node.started   planner(agent)  → 未绑 Workflow 的 Knowledge Assistant
  │     input_template: "只输出一条检索计划（一行，不要解释）：{{state.input}}"
  ├─③ workflow.node.started   search(tool: web_search, on_error=continue)
  ├─④ workflow.node.started   router(condition)  len(state.hits) > 0 ? reader : end
  ├─⑤ workflow.node.started   reader(retriever: kb_id=product-docs, on_error=continue)
  ├─⑥ workflow.node.started   writer(agent)  → "问题 / 线索 / 知识库" 三段拼进 input_template
  └─⑦ workflow.node.completed end → run.completed（kind=workflow、workflow_run_id 非空）
```

两个踩过的坑（都写进了 YAML 头注释与 8.2）：

- **图内 `agent` 节点不能引用绑了图的 Agent**：本图绑给 Research Agent 自己，Chat 内联时会触发
  `NESTED_WORKFLOW_NOT_SUPPORTED`（4.4.4）而无限展开；因此 `planner` / `writer` 指向同为 Demo、
  **未绑 Workflow** 的 Knowledge Assistant，节点角色由 `input_template` 表达。
- **入参键统一为 `input`**：Chat 内联注入 `{"input": 用户消息, "conversation_id", "agent_id"}`，
  手工 `POST /workflows/{id}/runs` 传 `{"input": {"input": "……"}}` —— 一份模板两种入口共用，
  所以 Demo 1 的链路测试直接走 Chat（也就是产品里"发一条消息就能编排"的体验）。

## 3. Demo 2：没有 Workflow 的 Agent 工具循环（Data Analysis Agent）

```text
POST /conversations/{id}/messages   （agent.workflow_id = null）
  ├─① run.started
  ├─② agent.step.started → llm（要工具）
  ├─③ tool.call.started   calculator   （权限判定 → 参数校验 → 沙箱 → 执行 → 落库）
  ├─④ tool.call.completed → 结果回填 role=tool → 下一轮 llm → message.delta…
  └─⑤ usage.updated → run.completed（tool_call_count=1）+ tool_invocations 行（allow / succeeded）

     python_execute（默认关闭）被调用时：
  ├─③' tool.call.failed   TOOL_PERMISSION_DENIED → 错误以 JSON 回填给模型
  └─④' 模型改用 file_read + calculator 给出简化结论（Run 不中断，4.2.3）
```

## 4. Demo 3：固定预检索 + 引用（Knowledge Assistant）

```text
POST /conversations/{id}/messages   （agent.knowledge_base_ids = [product-docs]）
  ├─① run.started
  ├─② retriever span（strategy=prefetch）→ retrieval.completed（kb_ids / query / hit_count）
  │     hit_count > 0 → system 注入 <chunk id source> 块
  │     hit_count = 0 → 只追加"未检索到相关内容"提示，**不打断对话**
  ├─③ llm（temperature=0.2）→ 回答带 [1] [2] → 前端引用来源卡片（消费事件 10）
  └─④ run.completed
```

注意：**预检索不是工具调用**，所以 4.9.3 的断言类型里没有"span 存在"这一项 ——
`configs/eval/demo-suite.yaml` 因此不再断言 `tool_sequence: [kb_search]`（`kb_search` 属迭代 E、MVP 不存在）：
task-2 改断言回答里的 `[n]` 引用编号，task-3 用 `tool_sequence: [calculator] + in_order`；
"检索真的发生了"由报告里的 `trace_id` 到 `/traces` 复核（`retriever` span 的 `output.chunk_ids`）。

## 5. 装载顺序（`python -m app.scripts.seed_demo`）

```text
① 内置工具对齐        tool_service.sync_builtin_tools()      代码是唯一事实来源，不动运营侧 status / 权限
② Provider 解析       --provider → YAML 的 model.provider     name → model_providers 行（找不到 → 退出码 2）
③ 知识库              按 name 建或复用（embedding provider）   Agent 与图里的 kb_id 都靠它的真实 id
④ Agent 第一遍        upsert_agent(workflow_id=None)          先把 name → id 定下来
⑤ Workflow 解析发布    占位名 → 真实 id（agent_id / kb_id）     未解析的 name → CONFIG_INVALID（不静默）
⑥ Agent 第二遍        回填 workflow_id                        YAML 里 workflow: null 的 Agent 跳过
⑦ 可选 --docs         上传 tests/fixtures/docs/*.md          等文档 ready（进程内 TaskRunner 收尾）
```

- **幂等**：Agent / KB / Workflow 都按 `name` upsert；`--reset` 先删同名（含运行记录）再从零装载。
- **没有 `0009_phase8_demo_agents` 迁移**：Demo 是内容不是 schema（7.9「数据模型变更：无」），
  把 Agent 名 / id 写进迁移会直接违反 8.5 的"配置即创建"。装载通道只有 `seed_demo`。
- 退出码：`0` = 全部就绪；`2` = 环境 / 配置问题（库未迁移、provider / tool 名字写错、embedding 不可用）。

## 6. 怎么跑

```powershell
# 本机（一键：迁移 → 就绪检查 → 装载 → 打印 0.5.5 的 8 步路径）
.\scripts\demo.ps1
.\scripts\demo.ps1 -Docs -Reset                 # 干净重放 + 灌演示语料

# 容器
docker compose -f docker/docker-compose.yml exec api python -m app.scripts.seed_demo --docs
```

> **Windows PowerShell 5.1 的编码坑（本轮踩到并修掉）**：`scripts/*.ps1` 必须存成**带 BOM 的 UTF-8** ——
> PS 5.1 对无 BOM 的脚本按 ANSI（中文机器上是 GBK）解码，中文乱码后会吞掉字符串的收尾引号，
> `dev.ps1` / `demo.ps1` 直接报 `Unexpected token 'demo]'` 这类语法错、脚本根本起不来。
> 三个脚本现在统一带 BOM（PS 7 / `pwsh` 默认按 UTF-8 读，不受影响，BOM 对它是合法的）。

三条链路的自动化验收：

```powershell
cd backend
.\.venv\Scripts\pytest.exe -q tests/e2e/test_demo_agents.py         # 三个 Demo 各一条链路
.\.venv\Scripts\pytest.exe -q tests/integration/test_seed_demo.py   # 装载与引用解析
.\.venv\Scripts\pytest.exe -q tests/unit/test_demo_genericity.py    # 零专属代码
```

## 7. 8.5 的验收表怎么落地

| 检查 | 落地物 |
|---|---|
| 无专属代码 | `tests/unit/test_demo_genericity.py`（AST 扫描 + 反向自检） |
| 配置即创建 | `tests/integration/test_seed_demo.py`：`planner` / `writer` 解析成 Knowledge Assistant 的 id，且 `!= research.id` |
| 能力可组合 | `configs/agents/*.yaml` 的 `workflow` 是可选字段（清空即退回纯 Agent 循环） |
| 工具可替换 | `tools` 列表即全部差异（`tests/unit/test_agent_configs.py` 钉住 YAML → `AgentCreate` 的映射） |
| 契约测试 | `tests/e2e/test_demo_agents.py`（三条链路，见 §2–§4） |
| 评测覆盖 | `configs/eval/contract-suite.yaml`（离线，成功率 100%）+ `demo-suite`（真实模型，需自备 Key） |

实测（本机 2026-10-13）：

| 命令 | 结果 |
|---|---|
| `pytest tests/e2e/test_demo_agents.py -q` | 3 passed（Workflow / Tool / RAG 各一条） |
| `pytest tests/integration/test_seed_demo.py -q` | passed（幂等装载 + `--reset` + `--docs` 摄取） |
| `pytest tests/unit/test_demo_genericity.py -q` | 2 passed（扫描 + 反向自检） |
| `pytest tests/integration/test_eval_contract_suite.py -q` | 4 passed（contract-suite 5/5、`task_success_rate=1.0`） |
| `python -m app.scripts.seed_demo --docs --reset`（临时库 + `kind=fake` Provider） | 退出码 0：KB `created:product-docs`、两张图 `created:kb-qa-flow, created:research-flow`、三个 Agent `created:*`、两篇文档 `ready:deployment.md, ready:platform-overview.md`；**再跑一次**：`workflows: unchanged:*`、`agents: updated:*`（不新增行、不重复摄取） |
| `.\scripts\demo.ps1`（本机库还没有 Provider） | 退出码 2 + 打印"先在 /models 建 Provider / 用 `-Provider` 指定"的指引 —— **不打印假成功**（7.9 DoD 第 4 条的容器走查仍未做，见 §8） |

> `FakeEmbedder` 的相似度分数与真实模型**不可比**：e2e 的 RAG 问句取短版"部署方式"（≈0.45）才能越过默认
> `score_threshold=0.3`，完整问句在替身下只有 ≈0.26 —— 那里证的是**链路**，不是分数。

## 8. 未做（不假装完成）

- `docker compose up --build` 的 10 分钟端到端走查：**本机没有 Docker**，CI 只做镜像构建
  （`.github/workflows/ci.yml` 的第 3 个 job），因此 7.9 DoD 第 4 条保持未勾。
- `demo-suite` 的 `llm_judge` 路径未对真实模型跑过（7.8 遗留，需自备 Provider Key）。
- Memory / MCP / 审批 / 评测平台页面属迭代 A–D，本阶段**不建空页面**（SD-14②）。
