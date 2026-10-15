<#
.SYNOPSIS
  一键装载 Demo 数据并打印 0.5.5 的体验路径（详细设计 7.9 交付物 / 8.1）。

.DESCRIPTION
  步骤固定，任一步失败即退出（不打印"演示可用"的假话）：

  1) `alembic upgrade head` —— 迁移没到 head 时后端会拒绝启动（6.3）；
  2) `python -m app.scripts.check_readyz` —— 连库 + 可写 + 迁移在 head；
  3) `python -m app.scripts.seed_demo` —— 把 `configs/agents/*.yaml` 与 `configs/workflows/*.yaml`
     幂等装载成知识库 / Workflow / Agent；`-Docs` 再灌一遍演示语料；`-FakeEmbedding`（= 脚本的
     `--fake-embedding`）让知识库改用确定性 embedding —— Provider 只有 Chat 模型时（如 DeepSeek）也能建档；
  4) 探测后端 `/healthz`；可达则提示先 `.\scripts\dev.ps1`（或容器方式），最后打印 8 步体验路径。

  Demo 数据**只有这一条装载通道**：没有 `0009_phase8_demo_agents` 迁移 —— Demo 是内容不是 schema
  （7.9「数据模型变更：无」），而迁移里硬编码 Agent 名/id 会直接违反 8.5 的"配置即创建"。

.EXAMPLE
  .\scripts\demo.ps1                 # 装载三个 Demo + 打印体验路径
  .\scripts\demo.ps1 -Docs -Reset     # 从干净状态重放，并把演示语料灌进知识库
  .\scripts\demo.ps1 -Provider my-provider -Model qwen-plus
  .\scripts\demo.ps1 -Provider deepseek -Model deepseek-flash -FakeEmbedding
#>
param(
  [string]$Provider,
  [string]$Model,
  [string]$EmbeddingProvider,
  [switch]$FakeEmbedding,
  [switch]$Docs,
  [switch]$Reset,
  [string]$BaseUrl = "http://localhost:8000",
  [switch]$SkipMigrate
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $root "backend"
$python = Join-Path $backendDir ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
  throw "未找到虚拟环境解释器：$python`n请先执行：python -m venv backend\.venv; backend\.venv\Scripts\python.exe -m pip install -e `"backend[dev]`""
}

if (-not $SkipMigrate) {
  Write-Host "[demo] alembic upgrade head ..." -ForegroundColor Cyan
  Push-Location $backendDir
  try { & $python -m alembic upgrade head } finally { Pop-Location }
  if ($LASTEXITCODE -ne 0) { throw "[demo] 迁移失败（见上面输出）" }
}

Write-Host "[demo] 就绪探针（连库 + 可写 + 迁移在 head）..." -ForegroundColor Cyan
Push-Location $backendDir
try { & $python -m app.scripts.check_readyz } finally { Pop-Location }
if ($LASTEXITCODE -ne 0) { throw "[demo] 就绪检查未通过：先解决上面的问题再装载（6.3）" }

$seedArgs = @("-m", "app.scripts.seed_demo")
if ($Provider) { $seedArgs += @("--provider", $Provider) }
if ($Model) { $seedArgs += @("--model", $Model) }
if ($EmbeddingProvider) { $seedArgs += @("--embedding-provider", $EmbeddingProvider) }
if ($FakeEmbedding) { $seedArgs += "--fake-embedding" }
if ($Reset) { $seedArgs += "--reset" }
if ($Docs) { $seedArgs += "--docs" }

Write-Host "[demo] 装载 Demo 数据：python $($seedArgs -join ' ')" -ForegroundColor Cyan
Push-Location $backendDir
try { & $python @seedArgs } finally { Pop-Location }
$seedExit = $LASTEXITCODE

if ($seedExit -eq 2) {
  Write-Host "[demo] 装载中止（退出码 2 = 环境/配置问题）。常见原因：" -ForegroundColor Yellow
  Write-Host "  - 库里还没有 `model_providers`：先在 /models 建一个 Provider，或用 -Provider 指定已存在的名字；"
  Write-Host "  - 工具名写错：内置工具由代码同步，先 `python -m app.scripts.seed_demo` 跑一遍看报错里的工具名；"
  Write-Host "  - embedding 不可用：知识库需要 provider 支持 embedding（4.6.1）；" -ForegroundColor Yellow
  Write-Host "    只有 Chat 模型的 Provider（如 DeepSeek 无 /embeddings）请加 -FakeEmbedding（需 EMBED_ALLOW_FAKE=true）。"
}
if ($seedExit -ne 0) { exit $seedExit }

# 后端是否已经在跑（装载是直接写库，不依赖 HTTP；但下一步体验需要它）
$apiUp = $false
try {
  $health = Invoke-RestMethod -Uri "$BaseUrl/healthz" -TimeoutSec 3
  $apiUp = $health.status -eq "ok"
} catch {
  $apiUp = $false
}

if (-not $apiUp) {
  Write-Host "[demo] 后端未在 $BaseUrl 响应 —— 先起服务再按下面的路径体验：" -ForegroundColor Yellow
  Write-Host "  .\scripts\dev.ps1                      # 本机：api(8000) + web(5173)"
  Write-Host "  cd docker; docker compose up --build    # 容器：http://localhost:8080" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "[demo] 0.5.5 的 8 步体验路径（能一次跑通 = MVP 达标）：" -ForegroundColor Green
Write-Host "  1. 打开 $BaseUrl/docs 或前端首页，确认 /readyz 为 ok"
Write-Host "  2. /agents   ：打开 Research Agent，看 Prompt + 工具 + 知识库 + workflow + max_steps"
Write-Host "  3. /chat     ：选 Data Analysis Agent，问「读取 sandbox/notes.md 的第一段，并算 12 万的 15% 是多少」"
Write-Host "                 → 观察流式增量、工具调用卡片、最终答案"
Write-Host "  4. /traces   ：展开该 Run 的 span 树（run → agent → llm → tool → llm）"
Write-Host "  5. /workflows：打开 research-flow 看只读图 → 运行 → 节点状态逐个变绿（node_runs）"
Write-Host "  6. /knowledge：确认 product-docs 的文档为 ready → 在 /chat 选 Knowledge Assistant 提问"
Write-Host "                  「我们产品的部署方式有哪些？」→ 答案带 [1] 引用与引用来源卡片"
Write-Host "  7. 终端      ：cd backend; .\.venv\Scripts\python.exe -m app.scripts.evaluate --suite demo-suite --agent research-agent"
Write-Host "  8. README    ：架构图 + 评测结果 + 已知限制与再评估触发条件（10.3）"
Write-Host ""
Write-Host "[demo] 装载幂等：重复执行不会产生重复 Agent；-Reset 会先删同名 Agent / Workflow 再重建。" -ForegroundColor Cyan
