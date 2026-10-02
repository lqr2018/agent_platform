#!/usr/bin/env bash
# CI 失败自注解（详细设计 6.4）。
#
# 动机：GitHub 的 Actions **完整日志需要登录**才能查看（即使是公开仓库，job 页只显示
# "Sign in for the full log view"），而**注解（Annotation）**在 `/commit/<sha>/checks`
# 页面是公开可读的。把命令输出同时落盘，失败时把尾部写进注解与 job summary，
# 这样"为什么红"不再依赖某个人登录后截图。
#
# 用法：bash .github/scripts/tee_annotate.sh <标签> <命令> [参数...]
#   标签同时用作日志文件名（落在 $RUNNER_TEMP/<标签>.log，不进仓库工作区）。
set -o pipefail

label="$1"
shift
log_file="${RUNNER_TEMP:-/tmp}/${label}.log"

echo "+ $*" | tee "$log_file"
"$@" 2>&1 | tee -a "$log_file"
status=${PIPESTATUS[0]}

if [ "$status" -ne 0 ]; then
  # 注解消息里换行必须写成 %0A、% 必须写成 %25，否则 GitHub 会截断在第一个换行处。
  tail_text="$(tail -n 40 "$log_file" | sed -e 's/%/%25/g' -e 's/\r//g' | tr '\n' '\r' | sed -e 's/\r/%0A/g' | head -c 3500)"
  echo "::error title=${label} 失败（exit ${status}）::${tail_text}"
  {
    echo "### ${label} 失败（exit ${status}）"
    echo '```'
    tail -n 150 "$log_file"
    echo '```'
  } >>"${GITHUB_STEP_SUMMARY:-/dev/null}"
fi

exit "$status"
