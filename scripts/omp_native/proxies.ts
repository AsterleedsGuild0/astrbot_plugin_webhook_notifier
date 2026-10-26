/**
 * 验收专用本地代理，只监听 127.0.0.1：
 * - webhook：把 Hook 的请求原样转发到测试 Endpoint，记录请求体与响应（不记录 Token）。
 * - model：转发到本机 CPA（HTTP 与 WebSocket），可经 /__fault 注入 HTTP 错误以触发 OMP 原生重试。
 */
import { appendFileSync } from "node:fs";
import type { ServerWebSocket } from "bun";

const webhookTarget = process.env.ACCEPT_WEBHOOK_TARGET!;
const webhookLog = process.env.ACCEPT_WEBHOOK_LOG!;
const modelTarget = process.env.ACCEPT_MODEL_TARGET ?? "http://localhost:8317";
const modelLog = process.env.ACCEPT_MODEL_LOG!;
type Fault = { status: number; remaining: number; body: string };
let fault: Fault | undefined;

const webhook = Bun.serve({
  hostname: "127.0.0.1", port: Number(process.env.ACCEPT_WEBHOOK_PORT ?? 0),
  async fetch(req) {
    const body = await req.text();
    const started = Date.now();
    let status = 0, text = "", error: string | undefined;
    try {
      const headers = new Headers(req.headers);
      headers.delete("host");
      headers.delete("content-length");
      // 有界等待：上游卡住时也要落一条记录，而不是无声挂起。
      const res = await fetch(webhookTarget, { method: "POST", headers, body, redirect: "manual", signal: AbortSignal.timeout(300_000) });
      status = res.status; text = await res.text();
    } catch (e) { error = String(e); }
    let request: unknown = body, response: unknown = text;
    try { request = JSON.parse(body); } catch {}
    try { response = JSON.parse(text); } catch {}
    appendFileSync(webhookLog, JSON.stringify({ t: new Date(started).toISOString(), ms: Date.now() - started, event: req.headers.get("x-omp-event"), userAgent: req.headers.get("user-agent"), request, status, response, error }) + "\n");
    if (error) return new Response("upstream error", { status: 502 });
    return new Response(text, { status, headers: { "Content-Type": "application/json" } });
  },
});

type Tunnel = { path: string; headers: Record<string, string>; upstream?: WebSocket; pending: (string | Buffer)[] };
const tunnels = new Set<ServerWebSocket<Tunnel>>();

const model = Bun.serve<Tunnel>({
  hostname: "127.0.0.1", port: Number(process.env.ACCEPT_MODEL_PORT ?? 0), idleTimeout: 255,
  async fetch(req, server) {
    const url = new URL(req.url);
    // OMP 的 Responses 传输优先走 WebSocket；原样转发升级请求，避免回退 HTTP 前的连接超时混入 TTFT。
    // 注入故障期间拒绝升级并关闭已有隧道（含 OMP 启动时的预热连接），让 OMP 回退到可注入错误的 HTTP 请求。
    if (req.headers.get("upgrade")?.toLowerCase() === "websocket") {
      if (fault && fault.remaining !== 0) return new Response("websocket disabled while fault injected", { status: 426 });
      const headers: Record<string, string> = {};
      req.headers.forEach((value, key) => { if (!/^(host|upgrade|connection|sec-websocket-(key|version|extensions|accept))$/i.test(key)) headers[key] = value; });
      appendFileSync(modelLog, JSON.stringify({ t: new Date().toISOString(), method: "WS", path: url.pathname }) + "\n");
      if (server.upgrade(req, { data: { path: url.pathname + url.search, headers, pending: [] } })) return undefined;
      return new Response("upgrade failed", { status: 400 });
    }
    if (url.pathname === "/__fault") {
      const value = await req.json() as Partial<Fault> | null;
      fault = value && value.status ? { status: value.status, remaining: value.remaining ?? 1, body: value.body ?? "injected" } : undefined;
      if (fault) for (const ws of tunnels) ws.close(1011, "fault injected");
      return Response.json({ fault: fault ?? null });
    }
    const entry = { t: new Date().toISOString(), method: req.method, path: url.pathname, injected: undefined as number | undefined };
    if (fault && fault.remaining !== 0 && req.method === "POST") {
      entry.injected = fault.status;
      if (fault.remaining > 0) fault.remaining--;
      appendFileSync(modelLog, JSON.stringify(entry) + "\n");
      return new Response(JSON.stringify({ error: { message: fault.body, type: "server_error" } }), { status: fault.status, headers: { "Content-Type": "application/json" } });
    }
    appendFileSync(modelLog, JSON.stringify(entry) + "\n");
    const headers = new Headers(req.headers);
    headers.delete("host");
    return fetch(modelTarget + url.pathname + url.search, { method: req.method, headers, body: req.body, redirect: "manual" });
  },
  websocket: {
    idleTimeout: 255,
    open(ws) {
      tunnels.add(ws);
      const upstream = new WebSocket(modelTarget.replace(/^http/, "ws") + ws.data.path, { headers: ws.data.headers });
      upstream.binaryType = "nodebuffer" as BinaryType;
      ws.data.upstream = upstream;
      upstream.onopen = () => { for (const item of ws.data.pending.splice(0)) upstream.send(item); };
      upstream.onmessage = event => ws.send(event.data as string | Buffer);
      upstream.onclose = event => ws.close(event.code === 1005 ? 1000 : event.code, event.reason);
      upstream.onerror = () => ws.close(1011, "upstream error");
    },
    message(ws, message) {
      const upstream = ws.data.upstream;
      if (upstream?.readyState === WebSocket.OPEN) upstream.send(message);
      else ws.data.pending.push(message);
    },
    close(ws) { tunnels.delete(ws); ws.data.upstream?.close(); },
  },
});

console.log(JSON.stringify({ webhook: webhook.port, model: model.port }));
