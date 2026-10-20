# 投资助手 3.0（`assistant/`）

Pi（TypeScript）当主脑 + Python 当数据/回测工具。**`packages/` 是 pi 上游，一个字节不改**；自有东西全在这个目录下。

## 目录

| 路径 | 作用 |
| --- | --- |
| `run.sh` | **入口**。`./assistant/run.sh --print "问题"` 一次性提问；不带参数进交互模式 |
| `config/watchlist.yaml` | 标的清单 + 指数 + 基线 + 贝塔 + 板块口径 + 调度时点（**唯一事实来源**） |
| `config/preferences.yaml` | 投资偏好（目标、配置意向、数据缺口） |
| `py/bridge.py` | Python 适配层：吃 JSON 吐 JSON，供 agent 当工具调用。**不改 icbt 内核** |
| `pi-agent/models.json` | 模型注册（`deepseek-flash` 支持图片；密钥用 `$DEEPSEEK_API_KEY` 插值） |
| `pi-agent/auth.json` | 凭据存储（**明文，chmod 600，勿提交**） |
| `pi-agent/APPEND_SYSTEM.md` | 追加到系统提示词的「投资助手模式」硬规则 |
| `pi-agent/extensions/investment.ts` | Pi 扩展：注册投资工具（走官方扩展点，不碰 `packages/`） |
| `h5/index.html` | UI demo（静态假数据，三视图 + 指数基线） |
| `docs/` | PRD、技术方案（`.md` + 可粘贴飞书的 `.html`）、`md2doc.py` |
| `data/cache/` | 行情缓存（parquet，20 小时 TTL） |
| `data/` | 持仓、文章存档、日报、复盘素材（见技术方案 3.6） |

## 现在能跑什么

```bash
# 一次提问（会调用 baseline_deviation 工具取真实数据）
./assistant/run.sh --print "IC 和 IF 现在相对基线偏离多少？带上 R² 和数据截止日"

# 直接看适配层（不经模型）
/Users/changsir/ic_backtest/.venv/bin/python assistant/py/bridge.py deviate

# 只查部分标的
... assistant/py/bridge.py deviate --codes 512800,159857
```

已验证：Pi 从源码直跑 → DeepSeek(`deepseek-flash`) → 工具调用 → Python 取数 → 返回真实数字，这条链路是通的。

## 已知问题（按优先级）

1. **ETF 价格口径待定**：`fund_etf_hist_sina` 拿到的 ETF 序列的水平值可疑（如 512800 最新 0.85、515880 最新 0.59），需要与「单位净值 / 累计净值」对齐后确定用哪个口径做「当前 vs 基线」。在定下来之前，**ETF 的偏离数字不要当真**；指数与期指标的那部分（中证500 / 沪深300 / 8 个板块指数）是可用的。
2. **东财行情接口被网络层重置**（0.1s 断连，非限频）→ 已有多源回退；复权行情取不到，收益/贝塔走净值口径。
3. **基线参数尚未冻结**：除中证500（5660 / 73 / R²0.06）外，其余都是「临时拟合·待冻结」。拟合口径确认后要写回 `watchlist.yaml`。
4. **H5 还没接后端**：`h5/index.html` 目前是静态 demo，`/api/chat` 那层服务还没写。
5. 依赖 `ic_backtest/.venv` 里的 akshare（用 `ASSISTANT_PYTHON` 可覆盖）。后续应给助手自己的环境装依赖。
