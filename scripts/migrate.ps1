<#
.SYNOPSIS
  执行 Alembic 迁移（详细设计 1.1 / 2.14：迁移是 schema 变更的唯一通道）。

.DESCRIPTION
  默认执行 `upgrade head`；其余参数原样透传给 alembic。

.EXAMPLE
  .\scripts\migrate.ps1                      # alembic upgrade head
  .\scripts\migrate.ps1 downgrade base       # 回滚到空库
  .\scripts\migrate.ps1 revision --autogenerate -m "phase1 agent tables"
#>
param(
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$AlembicArgs = @("upgrade", "head")
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $root "backend"
$python = Join-Path $backendDir ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
  throw "未找到虚拟环境解释器：$python（先执行 python -m venv backend\.venv 并安装依赖）"
}

Push-Location $backendDir
try {
  Write-Host "[migrate] alembic $($AlembicArgs -join ' ')" -ForegroundColor Cyan
  & $python -m alembic @AlembicArgs
  exit $LASTEXITCODE
}
finally {
  Pop-Location
}
