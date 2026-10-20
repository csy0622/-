/**
 * 投资助手 3.0 — Pi 扩展
 *
 * 通过 Pi 官方扩展点做事，**不改 packages/ 里的 pi 源码**：
 *   1. 硬边界：模型不许写/改 assistant/ 之外的任何文件
 *   2. 注册投资工具：baseline_deviation、run_backtest
 *
 * 放在 <agent-dir>/extensions/ 下，pi 启动时自动加载。
 */

import { execFile, spawn } from "node:child_process";
import { homedir } from "node:os";
import { resolve as resolvePath } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const run = promisify(execFile);

/** Python 解释器：必须能 import akshare。可用环境变量覆盖。 */
const PYTHON = process.env.ASSISTANT_PYTHON || "/Users/changsir/ic_backtest/.venv/bin/python";

// 注意：必须用 fileURLToPath，不能用 URL.pathname —— 后者会把路径里的中文
// （投资助手3.0）percent-encode，导致 execFile 报 ENOENT。
const BRIDGE_DIR = fileURLToPath(new URL("../../py/", import.meta.url));
const BRIDGE = fileURLToPath(new URL("../../py/bridge.py", import.meta.url));
const STORE = fileURLToPath(new URL("../../py/store.py", import.meta.url));

/* ------------------------------------------------------------------ *
 * 硬边界：模型不许写/改 assistant/ 之外的任何文件
 * ------------------------------------------------------------------ *
 * 为什么必须有这一层：实测过——模型会自己写内联命令去跑 icbt，把产物写进了
 * ~/ic_backtest/data/processed/。只写在提示词里靠自觉不够，必须在 tool_call 拦住。
 * 拦截：write / edit 的目标路径，以及 bash 里“看起来在写文件”的命令。读取不限。
 */

/** 允许写入的根目录：只允许助手自己的目录 + 系统临时目录。 */
const ALLOWED_WRITE_ROOTS = [
	fileURLToPath(new URL("../../", import.meta.url)), // 即 assistant/
	"/tmp/",
	"/private/tmp/",
];

/** 出现这些“动词”就认为该 bash 命令意图写文件。 */
const WRITE_VERB =
	/(^|\s)(rm|mv|cp|mkdir|touch|tee|truncate|dd|chmod|chown|ln|install|rsync|git|npm|npx|pnpm|yarn|pip|pip3|brew|sed|perl|python|python3|node|bun|curl|wget)(\s|$)/;

function expandHome(p: string): string {
	if (p === "~") return homedir();
	if (p.startsWith("~/")) return resolvePath(homedir(), p.slice(2));
	return p;
}

/** 路径是否落在允许写入的范围内。相对路径放行（等价于仓内相对写入）。 */
function isAllowedWrite(rawPath: string): boolean {
	if (!rawPath) return true;
	const expanded = expandHome(rawPath);
	if (!expanded.startsWith("/")) return true; // 相对路径
	const abs = resolvePath(expanded);
	return ALLOWED_WRITE_ROOTS.some((root) => {
		const r = root.replace(/\/+$/, "");
		return abs === r || abs.startsWith(root);
	});
}

/** 从 bash 命令里挑出越界的绝对路径，返回第一个；没有则 null。 */
function bashOffendingPath(command: string): string | null {
	// 只要碰到 icbt 就拦（不单靠路径解析，避免拼接绕过）
	if (/ic_backtest/.test(command) && WRITE_VERB.test(command)) return "~/ic_backtest";
	const tokens = command.match(/(?:^|[\s"'=(])(~?\/[^\s"'`;|&()<>]+)/g) ?? [];
	for (const t of tokens) {
		const p = t.replace(/^[\s"'=(]+/, "");
		if (!isAllowedWrite(p)) return p;
	}
	return null;
}

/* ------------------------------------------------------------------ *
 * 工具：统一代理到 Python 适配层
 * ------------------------------------------------------------------ */

/** 统一调用 bridge.py，返回解析后的 JSON。 */
async function callBridge(args: string[]): Promise<{ ok: boolean; data?: any; error?: { message: string } }> {
	let stdout: string;
	try {
		const res = await run(PYTHON, [BRIDGE, ...args], { cwd: BRIDGE_DIR, maxBuffer: 32 * 1024 * 1024 });
		stdout = res.stdout;
	} catch (err) {
		// bridge.py 出错时以非 0 退出，但 stdout 里仍有结构化错误，优先用它
		const e = err as { stdout?: string; message?: string };
		if (e.stdout && e.stdout.trim().startsWith("{")) stdout = e.stdout;
		else return { ok: false, error: { message: e.message ?? String(err) } };
	}
	try {
		return JSON.parse(stdout);
	} catch {
		return { ok: false, error: { message: `适配层返回的不是 JSON：${stdout.slice(0, 300)}` } };
	}
}

const NEWS = fileURLToPath(new URL("../../py/news.py", import.meta.url));

/** 调 news.py（与 callBridge 同一套错误处理，只是换个脚本）。 */
async function callBridgeNews(args: string[]): Promise<{ ok: boolean; data?: any; error?: { message: string } }> {
	let stdout: string;
	try {
		const res = await run(PYTHON, [NEWS, ...args], { cwd: BRIDGE_DIR, maxBuffer: 32 * 1024 * 1024 });
		stdout = res.stdout;
	} catch (err) {
		const e = err as { stdout?: string; message?: string };
		if (e.stdout && e.stdout.trim().startsWith("{")) stdout = e.stdout;
		else return { ok: false, error: { message: e.message ?? String(err) } };
	}
	try {
		return JSON.parse(stdout);
	} catch {
		return { ok: false, error: { message: `news.py 返回的不是 JSON：${stdout.slice(0, 300)}` } };
	}
}

const DEVIATE_PARAMS = Type.Object({
	codes: Type.Optional(
		Type.String({
			description: "逗号分隔的标的代码，如 '000905,512800'。留空表示查询清单里的全部标的。",
		}),
	),
});

const BACKTEST_PARAMS = Type.Object({
	strategy: Type.Optional(
		Type.String({
			description:
				"configs/strategies/ 下的策略文件名，如 '策略1.yaml'（无脑季度滚动）。默认 策略1.yaml；不确定就先列策略。",
		}),
	),
	principal: Type.Optional(Type.Number({ description: "本金，默认 1000000" })),
	lots: Type.Optional(Type.Number({ description: "手数，默认 1" })),
	list_strategies: Type.Optional(Type.Boolean({ description: "只列出可用策略，不跑回测" })),
});

interface DeviateRow {
	code: string;
	label: string;
	as_of: string;
	latest: number;
	baseline: number;
	deviation_pct: number;
	slope_per_year: number;
	r2: number | null;
	start: string;
	points: number;
	source: string;
	price_basis: string;
	status: string;
}

function formatRows(rows: DeviateRow[], errors: { code: string; error: string }[], meta: string): string {
	const lines = [
		`口径：${meta}`,
		"",
		"标的 | 截至 | 最新 | 基线值 | 偏离 | 基线斜率/年 | R² | 价格口径 | 起 | 状态",
		"--- | --- | --- | --- | --- | --- | --- | --- | --- | ---",
	];
	for (const r of rows) {
		const dev = `${r.deviation_pct > 0 ? "+" : ""}${r.deviation_pct.toFixed(2)}%`;
		const r2 = r.r2 === null ? "—" : r.r2.toFixed(3);
		lines.push(
			`${r.label} [${r.code}] | ${r.as_of} | ${r.latest} | ${r.baseline} | ${dev} | ` +
				`${r.slope_per_year} | ${r2} | ${r.price_basis} | ${r.start} | ${r.status}`,
		);
	}
	if (errors.length) {
		lines.push("", "取数失败的标的：");
		for (const e of errors) lines.push(`- ${e.code}: ${e.error}`);
	}
	lines.push(
		"",
		"说明：偏离 = 最新 / 基线 − 1，正为高于基线。基线是**冻结**的线性线，不随新数据重拟合。",
		"R² 越小说明这条线越没有解释力，偏离要谨慎解读。不构成投资建议。",
	);
	return lines.join("\n");
}

const NEWS_PARAMS = Type.Object({
	kw: Type.Optional(Type.String({ description: "关键词，如 '光伏'、'银行'。留空=最新若干条。" })),
	limit: Type.Optional(Type.Number({ description: "返回条数，默认 20" })),
	days: Type.Optional(Type.Number({ description: "回看天数，默认 3（快讯）/ 90（研报）" })),
});

function fmtNews(items: any[]): string {
	return items
		.map(
			(x) =>
				`- [${x.pub_at}] ${x.title}${x.tags ? `　#${x.tags}` : ""}\n  ${(x.digest || "").slice(0, 160)}` +
				(x.url ? `\n  原文：${x.url}` : ""),
		)
		.join("\n");
}

function fmtResearch(items: any[]): string {
	return items
		.map((x) => `- [${(x.publish_at || "").slice(0, 10)}] ${x.title}\n  机构：${x.org || "—"}　行业：${x.industry || "—"}　评级：${x.rating || "—"}\n  ${x.url || ""}`)
		.join("\n");
}

export default function investmentExtension(pi: ExtensionAPI) {
	// ---- 边界拦截（先于一切工具）----
	pi.on("tool_call", async (event) => {
		const name = event.toolName;

		if (name === "write" || name === "edit") {
			const target = String((event.input as Record<string, unknown>).path ?? "");
			if (!isAllowedWrite(target)) {
				return {
					block: true,
					reason:
						`越界写入已被拦截：${target}\n` +
						`只允许写 assistant/ 与 /tmp 下的文件。不得修改 pi 上游（packages/）或 ~/ic_backtest。`,
				};
			}
			return undefined;
		}

		if (name === "bash") {
			const command = String((event.input as Record<string, unknown>).command ?? "");
			if (!WRITE_VERB.test(command)) return undefined; // 纯读命令放行
			const bad = bashOffendingPath(command);
			if (bad) {
				return {
					block: true,
					reason:
						`越界写入已被拦截：命令疑似会写 ${bad}\n` +
						`只允许写 assistant/ 与 /tmp。要取 icbt 的数据请走 baseline_deviation / run_backtest 工具——它们只读调用，产物落盘到助手目录。`,
				};
			}
		}

		return undefined;
	});

	// ---- 对话落库：每条消息写进 sqlite（失败也不影响对话）----
	pi.on("message_end", async (event) => {
		try {
			const msg = ((event as unknown as { message?: Record<string, unknown> }).message ?? {}) as Record<string, unknown>;
			const role = String(msg.role ?? "unknown");
			let content = "";
			if (typeof msg.content === "string") content = msg.content;
			else if (Array.isArray(msg.content)) {
				content = (msg.content as Record<string, unknown>[])
					.filter((b) => b && (b.type === "text" || typeof b.text === "string"))
					.map((b) => String(b.text ?? ""))
					.join("\n");
			}
			if (!content.trim()) return undefined;
			const sessionId = process.env.PI_SESSION_ID || "local";
			await new Promise<void>((res) => {
				const child = spawn(PYTHON, [STORE, "add-message"], { cwd: BRIDGE_DIR });
				child.stdin.end(JSON.stringify({ session_id: sessionId, role, content }));
				child.on("close", () => res());
				child.on("error", () => res());
			});
		} catch {
			/* 落库失败不应打断对话 */
		}
		return undefined;
	});

	// ---- 工具 1：基线与偏离 ----
	pi.registerTool({
		name: "baseline_deviation",
		label: "基线与偏离",
		description:
			"查询标的（股指期货标的指数、持仓 ETF、板块指数）当前相对其冻结基线的偏离幅度，含基线斜率与拟合优度 R²。数据在本地缓存，缺失时自动抓取。",
		promptSnippet: "查询标的相对基线的偏离、斜率与 R²（含 IC/IF 标的指数与清单内 ETF/指数）",
		promptGuidelines: [
			"当用户问『现在贵不贵 / 偏离基线多少 / 位置如何』时使用本工具，不要凭记忆回答。",
			"回答时必须带上 R²，并说明基线是冻结的线性线；R² 很低时不要下趋势结论。",
		],
		parameters: DEVIATE_PARAMS,
		async execute(_toolCallId, params) {
			const args = ["deviate"];
			if (params.codes?.trim()) args.push("--codes", params.codes.trim());
			const parsed = await callBridge(args);
			if (!parsed.ok || !parsed.data) {
				return {
					content: [{ type: "text" as const, text: `取数失败：${parsed.error?.message ?? "未知错误"}` }],
					isError: true,
				};
			}
			const d = parsed.data;
			const meta = `base_year=${d.base_year}，拟合输入=${d.fit_on}，生成于 ${d.as_of}`;
			return {
				content: [{ type: "text" as const, text: formatRows(d.rows, d.errors, meta) }],
				details: { rows: d.rows.length, errors: d.errors.length },
			};
		},
	});

	// ---- 工具 2：回测（强制落盘、可复核）----
	pi.registerTool({
		name: "run_backtest",
		label: "跑回测",
		description:
			"用 icbt 引擎跑一次滚动 IC 回测（策略取自 ~/ic_backtest/configs/strategies/）。结果落盘到助手自己的目录并返回 run_id，任何数字都可复核。",
		promptSnippet: "跑一次 IC 滚仓回测，拿到可复核的 run_id 与绩效指标",
		promptGuidelines: [
			"任何回测数字都必须来自本工具。不要自己写内联 python/命令行去跑回测，也不要估算——上一轮就是这么出错的：结果没落盘、其中一个数字还是手算的，无法复核。",
			"报告回测结果时，必须带上 run_id、区间、本金/手数/费用口径，并说明不是下单建议。",
		],
		parameters: BACKTEST_PARAMS,
		async execute(_toolCallId, params) {
			if (params.list_strategies) {
				const r = await callBridge(["backtest", "--list"]);
				if (!r.ok || !r.data) {
					return { content: [{ type: "text" as const, text: `列策略失败：${r.error?.message ?? "未知"}` }], isError: true };
				}
				return { content: [{ type: "text" as const, text: `可用策略：\n${(r.data.strategies as string[]).join("\n")}` }] };
			}
			const args = ["backtest", "--strategy", params.strategy?.trim() || "策略1.yaml"];
			if (params.principal) args.push("--principal", String(params.principal));
			if (params.lots) args.push("--lots", String(params.lots));
			const r = await callBridge(args);
			if (!r.ok || !r.data) {
				return { content: [{ type: "text" as const, text: `回测失败：${r.error?.message ?? "未知"}` }], isError: true };
			}
			const d = r.data;
			const m = d.metrics ?? {};
			const pct = (v: number) => `${(Number(v) * 100).toFixed(2)}%`;
			const lines = [
				`策略：${d.name}（${d.strategy_file}）`,
				`run_id：${d.run_id}`,
				`区间：${d.period?.[0]} → ${d.period?.[1]}（${d.trading_days} 个交易日）`,
				`交易笔数：${d.trade_count}`,
				"",
				`累计收益：${pct(m.total_return)}`,
				`年化(CAGR)：${pct(m.cagr)}`,
				`最大回撤：${pct(m.max_dd)}（${m.mdd_peak} → ${m.mdd_trough}）`,
				`夏普：${Number(m.sharpe).toFixed(2)}　Calmar：${Number(m.calmar).toFixed(2)}`,
				`期末权益：${Math.round(Number(m.final)).toLocaleString()}`,
				`持仓占比：${pct(d.extra?.held_ratio ?? 0)}　基差贡献：${Math.round(Number(d.extra?.basis_pnl ?? 0)).toLocaleString()}`,
				"",
				`结果已落盘：${d.artifact}`,
				"同一「配置+数据+代码」会得到同一 run_id，可复核。不构成下单建议。",
			];
			return { content: [{ type: "text" as const, text: lines.join("\n") }], details: { run_id: d.run_id } };
		},
	});

	// ---- 工具 3：最新快讯 ----
	pi.registerTool({
		name: "latest_news",
		label: "最新快讯",
		description:
			"查同花顺快讯（已入库，每日 0 点更新）。可按关键词过滤。回答「今天有什么消息 / 某板块有什么动静」时必须用它，不要凭记忆。",
		promptSnippet: "查同花顺快讯库（可按关键词过滤），用于回答「有什么消息/动静」",
		promptGuidelines: [
			"涉及市场消息、资讯、快讯的问题一律先查本工具，不要凭记忆回答。",
			"引用快讯时必须带上发布时间；库只保留近 90 天。",
		],
		parameters: NEWS_PARAMS,
		async execute(_toolCallId, params) {
			const args = ["latest-news", "--days", String(params.days ?? 3), "--limit", String(params.limit ?? 20)];
			if (params.kw?.trim()) args.push("--kw", params.kw.trim());
			const r = await callBridgeNews(args);
			if (!r.ok || !r.data) return { content: [{ type: "text" as const, text: `查快讯失败：${r.error?.message ?? "未知"}` }], isError: true };
			const items = r.data.items ?? [];
			if (!items.length) return { content: [{ type: "text" as const, text: `近 ${params.days ?? 3} 天内没有匹配「${params.kw ?? "全部"}」的快讯。` }] };
			return { content: [{ type: "text" as const, text: `共 ${r.data.count} 条（近 ${params.days ?? 3} 天）：\n\n${fmtNews(items)}\n\n来源：同花顺快讯。不构成投资建议。` }] };
		},
	});

	// ---- 工具 4：最新研报 ----
	pi.registerTool({
		name: "latest_research",
		label: "最新研报",
		description: "查东财研报库（已入库，每日 0 点更新）。可按行业/关键词过滤，返回机构、行业、评级。",
		promptSnippet: "查东财研报库（按行业/关键词），拿到机构观点与评级",
		promptGuidelines: ["用户问「机构怎么看 / 有什么研报」时用本工具；引用时带上发布日期与机构名。"],
		parameters: NEWS_PARAMS,
		async execute(_toolCallId, params) {
			const args = ["latest-research", "--days", String(params.days ?? 90), "--limit", String(params.limit ?? 10)];
			if (params.kw?.trim()) args.push("--kw", params.kw.trim());
			const r = await callBridgeNews(args);
			if (!r.ok || !r.data) return { content: [{ type: "text" as const, text: `查研报失败：${r.error?.message ?? "未知"}` }], isError: true };
			const items = r.data.items ?? [];
			if (!items.length) return { content: [{ type: "text" as const, text: `近 ${params.days ?? 90} 天内没有匹配的研报。` }] };
			return { content: [{ type: "text" as const, text: `共 ${r.data.count} 篇：\n\n${fmtResearch(items)}\n\n来源：东方财富研报库。不构成投资建议。` }] };
		},
	});
}
