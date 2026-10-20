# 投资助手 3.0

跑在本地的个人投资研究助手：用 [Pi](https://pi.dev)（TypeScript Agent harness）当主脑，Python 负责取数与回测，每天自动生成一份投资晨报，也可以直接用自然语言向它提问。

## 能做什么

- **自然语言问数据** — 在终端里提问（如「IC 和 IF 现在相对基线偏离多少？」），Agent 自主调用工具取真实数据后作答，不是凭记忆编。
- **每日晨报** — 规则化生成（不调模型），每早 09:00 出一份单文件 HTML：
  - `a` 大 V 文章（猫笔刀）
  - `b` 要闻与异动（按关注板块筛选的快讯 + 当日涨跌幅）
  - `c` 研报与关注（东财研报按板块过滤）
  - `d` 基线与贝塔（指数/期指/ETF 相对拟合基线的偏离）
  - `e` 当前提示与风险（仓位、浮亏、异动、偏离基线，由数据规则算出）
- **资讯采集入库** — 同花顺快讯、东财研报、猫笔刀文章（RSS 主源 + 备份站交叉校验），落 SQLite 并保留 90 天。
- **行情与回测** — 标的行情入库、基线偏离计算、历史回测；ETF 用累计净值拟合，避免复权口径带来的偏差。
- **H5 前端** — 对话、每日晨报、持仓与回测三个视图；对话可切历史会话，晨报页直接嵌入最新一期并可下拉切换历史。

## 仓库结构

| 路径 | 说明 |
| --- | --- |
| `assistant/run.sh` | **入口**：`./assistant/run.sh --print "问题"` 一次性提问，不带参数进交互模式 |
| `assistant/pi-agent/` | Agent 配置：模型注册、系统提示词追加、投资工具扩展 |
| `assistant/py/` | Python 侧：适配层 `bridge.py`、资讯采集 `news.py`、晨报生成 `report.py`、数据入库 `store.py` |
| `assistant/config/` | 标的清单与投资偏好 |
| `assistant/src/server.mjs` | 后端服务：对话、状态、偏离、晨报等接口 |
| `assistant/h5/` | H5 前端页面 |
| `assistant/launchd/` | 定时任务：文章抓取、行情入库、晨报生成 |
| `packages/` | Pi 上游源码，未做改动 |

## 快速开始

```bash
# 一次性提问
./assistant/run.sh --print "现在各标的相对基线偏离多少？带上 R² 和数据截止日"

# 交互模式
./assistant/run.sh

# 不起模型，直接看数据层（需要能 import akshare 的 Python 环境）
python assistant/py/bridge.py deviate --codes 512800,159857
```

首次使用需要在 `assistant/pi-agent/auth.json` 里配置模型 API Key，并按 `assistant/config/watchlist.yaml` 调整自己关注的标的。

## 说明

- 本仓库不含密钥、持仓明细、数据库与历史报告，这些文件已在 `.gitignore` 中排除，只保留代码与文档。
- 所有输出仅做信息整理与提示，**不构成投资建议**。
