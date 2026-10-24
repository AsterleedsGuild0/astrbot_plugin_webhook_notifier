/**
 * 验收专用本地代理，只监听 127.0.0.1：
 * - webhook：把 Hook 的请求原样转发到测试 Endpoint，记录请求体与响应（不记录 Token）。
 * - model：转发到本机 CPA，可经 /__fault 注入 HTTP 错误以触发 OMP 原生重试。
 */
import { appendFileSync } from "node:fs";

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
      const res = await fetch(webhookTarget, { method: "POST", headers, body, redirect: "manual" });
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

const model = Bun.serve({
  hostname: "127.0.0.1", port: Number(process.env.ACCEPT_MODEL_PORT ?? 0), idleTimeout: 255,
  async fetch(req) {
    const url = new URL(req.url);
    if (url.pathname === "/__fault") {
      const value = await req.json() as Partial<Fault> | null;
      fault = value && value.status ? { status: value.status, remaining: value.remaining ?? 1, body: value.body ?? "injected" } : undefined;
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
});

console.log(JSON.stringify({ webhook: webhook.port, model: model.port }));
