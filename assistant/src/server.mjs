/**
 * 投资助手 3.0 — 最小后端（零依赖 Node）
 *
 * 作用：把 Pi agent 暴露成 HTTP，供 h5/index.html 调用，这样能在浏览器里实测。
 *
 *   GET  /                  → 静态服务 h5/
 *   POST /api/chat          → {message} → {reply}（跑一次 pi --print）
 *   GET  /api/status        → 数据就绪情况（读 store.py stats）
 *   GET  /api/deviation     → 基线偏离表（读 bridge.py deviate，走缓存）
 *
 * 注意：本服务只读数据、只写 assistant/ 内；它不修改 pi 上游，也不碰 ~/ic_backtest。
 */
import { execFile, spawn } from "node:child_process";
import { createServer } from "node:http";
import { readFile, stat } from "node:fs/promises";
import { extname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const run = promisify(execFile);

const PORT = Number(process.env.ASSISTANT_PORT || 8790);
const ASSISTANT = fileURLToPath(new URL("../", import.meta.url)); // assistant/
const REPO = fileURLToPath(new URL("../../", import.meta.url));   // 投资助手3.0/
const H5 = join(ASSISTANT, "h5");
const PYTHON = process.env.ASSISTANT_PYTHON || "/Users/changsir/ic_backtest/.venv/bin/python";
const PI_SH = join(REPO, "pi-test.sh");

const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".json": "application/json; charset=utf-8" };

function pyEnv() {
	return { ...process.env, PI_CODING_AGENT_DIR: join(ASSISTANT, "pi-agent"), ASSISTANT_PYTHON: PYTHON, NO_PROXY: "csindex.com.cn,sina.com.cn,eastmoney.com" };
}

async function py(script, args) {
	const res = await run(PYTHON, [join(ASSISTANT, "py", script), ...args], { cwd: join(ASSISTANT, "py"), env: pyEnv(), maxBuffer: 32 * 1024 * 1024 });
	return JSON.parse(res.stdout);
}

/** 跑一次 pi --print 并拿回答案文本。 */
function askPi(message, sessionId) {
	return new Promise((resolve) => {
		const child = spawn("bash", [PI_SH, "--model", "deepseek/deepseek-flash", "--print", message], {
			cwd: REPO, env: { ...pyEnv(), PI_SESSION_ID: sessionId }, stdio: ["ignore", "pipe", "pipe"],
		});
		let out = "";
		let err = "";
		child.stdout.on("data", (d) => (out += d));
		child.stderr.on("data", (d) => (err += d));
		child.on("close", (code) => resolve({ ok: code === 0 && out.trim().length > 0, reply: out.trim(), stderr: err.slice(-800) }));
		child.on("error", (e) => resolve({ ok: false, reply: "", stderr: String(e) }));
	});
}

function send(res, code, body, type = "application/json; charset=utf-8") {
	res.writeHead(code, { "Content-Type": type, "Cache-Control": "no-store" });
	res.end(body);
}

createServer(async (req, res) => {
	const url = new URL(req.url, "http://localhost");
	try {
		if (req.method === "POST" && url.pathname === "/api/chat") {
			let body = "";
			for await (const chunk of req) body += chunk;
			const message = String(JSON.parse(body || "{}").message || "").trim();
			if (!message) return send(res, 400, JSON.stringify({ ok: false, error: "空消息" }));
			const sessionId = req.headers["x-session-id"] || "h5";
			console.log(`[chat] ${message.slice(0, 60)}`);
			const r = await askPi(message, sessionId);
			return send(res, r.ok ? 200 : 500, JSON.stringify(r));
		}
		if (url.pathname === "/api/status") {
			return send(res, 200, JSON.stringify(await py("store.py", ["stats"])));
		}
		if (url.pathname === "/api/sessions") {
			return send(res, 200, JSON.stringify(await py("store.py", ["sessions"])));
		}
		if (url.pathname === "/api/reports") {
			return send(res, 200, JSON.stringify(await py("store.py", ["reports"])));
		}
		// 晨报正文：/reports/<日期>.html（直接送文件，保留报告自己的样式与链接）
		if (url.pathname.startsWith("/reports/")) {
			const name = url.pathname.slice("/reports/".length).replace(/\.\./g, "");
			const f = join(ASSISTANT, "data", "reports", "daily", name);
			const st = await stat(f).catch(() => null);
			if (!st || !st.isFile()) return send(res, 404, "报告不存在", "text/plain; charset=utf-8");
			return send(res, 200, await readFile(f), "text/html; charset=utf-8");
		}
		if (url.pathname === "/api/messages") {
			const sid = url.searchParams.get("session") || "";
			if (!sid) return send(res, 400, JSON.stringify({ ok: false, error: "缺少 session 参数" }));
			return send(res, 200, JSON.stringify(await py("store.py", ["messages", "--session", sid])));
		}
		if (url.pathname === "/api/deviation") {
			return send(res, 200, JSON.stringify(await py("bridge.py", ["deviate"])));
		}

		// 静态文件
		const rel = url.pathname === "/" ? "/index.html" : url.pathname;
		const file = join(H5, rel.replace(/\.\./g, ""));
		const st = await stat(file).catch(() => null);
		if (!st || !st.isFile()) return send(res, 404, "not found", "text/plain");
		return send(res, 200, await readFile(file), TYPES[extname(file)] || "application/octet-stream");
	} catch (e) {
		return send(res, 500, JSON.stringify({ ok: false, error: String(e) }));
	}
}).listen(PORT, "127.0.0.1", () => {
	console.log(`投资助手 3.0 后端已启动 → http://127.0.0.1:${PORT}/`);
	console.log(`  H5  : http://127.0.0.1:${PORT}/`);
	console.log(`  会话: POST /api/chat   数据: GET /api/status   偏离: GET /api/deviation`);
});
