<#
.SYNOPSIS
  一键起后端 + 前端（详细设计 7.1 DoD 第 1 条：一条命令起前后端，浏览器看到状态条）。

.DESCRIPTION
  - 先跑 `alembic upgrade head`（迁移未到 head 时后端会拒绝启动，见 6.3）；
  - 再各开一个 PowerShell 窗口跑 uvicorn（--reload）与 vite dev server；
  - 前端 5173 会把 /api、/healthz、/readyz 代理到后端（见 frontend/vite.config.ts）。

.EXAMPLE
  .\scripts\dev.ps1
  .\scripts\dev.ps1 -ApiPort 8001
#>
param(
  [int]$ApiPort = 8000,
  [switch]$SkipMigrate
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $root "backend"
$frontendDir = Join-Path $root "frontend"
$python = Join-Path $backendDir ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
  throw "未找到虚拟环境解释器：$python`n请先执行：python -m venv backend\.venv; backend\.venv\Scripts\python.exe -m pip install -e `"backend[dev]`""
}

if (-not (Test-Path (Join-Path $frontendDir "node_modules"))) {
  Write-Host "[dev] frontend/node_modules 不存在，正在执行 npm install ..." -ForegroundColor Yellow
  Push-Location $frontendDir
  try { & npm.cmd install --no-audit --no-fund } finally { Pop-Location }
}

if (-not $SkipMigrate) {
  Write-Host "[dev] alembic upgrade head ..." -ForegroundColor Cyan
  Push-Location $backendDir
  try { & $python -m alembic upgrade head } finally { Pop-Location }
}

Write-Host "[dev] 后端 -> http://localhost:$ApiPort   (docs: http://localhost:$ApiPort/docs)" -ForegroundColor Green
Start-Process powershell -ArgumentList @(
  "-NoExit", "-Command",
  "`$host.UI.RawUI.WindowTitle='agent-platform api'; cd '$backendDir'; & '$python' -m uvicorn app.main:app --reload --port $ApiPort"
)

Write-Host "[dev] 前端 -> http://localhost:5173" -ForegroundColor Green
Start-Process powershell -ArgumentList @(
  "-NoExit", "-Command",
  "`$host.UI.RawUI.WindowTitle='agent-platform web'; cd '$frontendDir'; & npm.cmd run dev"
)

Write-Host "[dev] 两个窗口已启动，关闭窗口即停止对应服务。" -ForegroundColor Cyan
