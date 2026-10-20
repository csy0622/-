#!/usr/bin/env bash
# 投资助手 3.0 — 启动脚本
#
# 用法：
#   ./assistant/run.sh --print "IC 现在偏离基线多少？"      # 一次性提问
#   ./assistant/run.sh                                      # 进入交互模式
#
# 依赖：本目录已 npm install 过（pi 的依赖）；Python 侧需要能 import akshare。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

# 模型与凭据：models.json / auth.json 都放这里（不在 packages/ 里，跟 pi 上游隔离）
export PI_CODING_AGENT_DIR="$HERE/pi-agent"

# 扩展会用它来跑 assistant/py/bridge.py
export ASSISTANT_PYTHON="${ASSISTANT_PYTHON:-/Users/changsir/ic_backtest/.venv/bin/python}"

# 默认模型可用环境变量覆盖
MODEL="${ASSISTANT_MODEL:-deepseek/deepseek-flash}"

cd "$REPO"
exec ./pi-test.sh --model "$MODEL" "$@"
