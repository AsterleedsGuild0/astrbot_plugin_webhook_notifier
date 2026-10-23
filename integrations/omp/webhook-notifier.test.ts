import { afterEach, beforeEach, describe, expect, it, mock, spyOn } from "bun:test";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent/extensibility/extensions";
import register from "./webhook-notifier.ts";

type Event = Record<string, unknown>;
type Handler = (event: Event, context: ExtensionContext) => unknown;
const envKeys = ["OMP_SESSION_WEBHOOK_URL", "ONEBOT_WEBHOOK_URL", "OMP_SESSION_WEBHOOK_TOKEN", "ONEBOT_WEBHOOK_TOKEN", "OMP_SESSION_WEBHOOK_TIMEOUT_MS", "ONEBOT_WEBHOOK_TIMEOUT_MS", "OMP_SESSION_WEBHOOK_INSTANCE"];
const saved = new Map<string, string | undefined>();
const SENTINEL = "PRIVATE_SENTINEL";
let now: number;
let timerId: number;
let timers: Map<number, { at: number; fn: () => void }>;
let requests: { url: string; init: RequestInit; body: Record<string, any> }[];
let clients: { emit: (name: string, event?: Event, context?: ExtensionContext) => unknown }[];
let sequence = 0;

function context(id = `session-${++sequence}`): ExtensionContext {
  return {
    hasUI: true, mode: "tui", cwd: `/private/${SENTINEL}/project`,
    agent: { kind: "main", id: `raw-agent-${SENTINEL}`, name: "main", depth: 0 },
    model: { provider: "test", id: "model" },
    sessionManager: { getSessionId: () => id, getSessionName: () => "测试会话", getCwd: () => `/private/${SENTINEL}/project` },
  } as unknown as ExtensionContext;
}
function client(ctx = context()) {
  const handlers = new Map<string, Handler>();
  const logs: unknown[][] = [];
  let thinking = "high";
  const api = { on: (name: string, handler: Handler) => handlers.set(name, handler), logger: { warn: (...values: unknown[]) => logs.push(values) }, getThinkingLevel: () => thinking } as unknown as ExtensionAPI;
  register(api);
  const value = {
    ctx, logs, setThinking: (level: string) => { thinking = level; },
    emit(name: string, event: Event = {}, target = ctx) {
      const handler = handlers.get(name);
      if (!handler) throw new Error(`Unobserved test event ${name}`);
      return handler({ type: name, ...event }, target);
    },
  };
  clients.push(value);
  return value;
}
function end(stopReason = "stop", extra: Event = {}) {
  return { messages: [{ role: "assistant", stopReason, content: SENTINEL, errorMessage: SENTINEL }], ...extra };
}
async function microtasks() { for (let i = 0; i < 30; i++) await Promise.resolve(); }
async function advance(ms = 0) {
  const until = now + ms;
  await microtasks();
  for (let guard = 0; guard < 10000; guard++) {
    const next = [...timers.entries()].filter(([, t]) => t.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
    if (!next) break;
    timers.delete(next[0]); now = next[1].at; next[1].fn(); await microtasks();
  }
  now = until;
  await microtasks();
}
function ok(message = "ok", data: Event = {}) {
  return Response.json({ code: 0, message, data: { delivered: true, retryable: false, ...data } });
}
function categories(logs: unknown[][]) {
  return logs.map(row => {
    const data = row[1];
    if (!data || typeof data !== "object" || !("category" in data)) throw new Error("Invalid diagnostic");
    return data.category;
  });
}

beforeEach(() => {
  for (const key of envKeys) { saved.set(key, process.env[key]); delete process.env[key]; }
  process.env.OMP_SESSION_WEBHOOK_URL = "https://fixture.invalid/webhook/local";
  process.env.OMP_SESSION_WEBHOOK_TOKEN = `token-${SENTINEL}`;
  now = Date.parse("2026-10-23T00:00:00Z"); timerId = 0; timers = new Map(); requests = []; clients = [];
  spyOn(Date, "now").mockImplementation(() => now);
  spyOn(globalThis, "setTimeout").mockImplementation(((fn: () => void, ms = 0) => { const id = ++timerId; timers.set(id, { at: now + Number(ms), fn }); return id; }) as typeof setTimeout);
  spyOn(globalThis, "clearTimeout").mockImplementation(((id: number) => { timers.delete(Number(id)); }) as typeof clearTimeout);
  spyOn(globalThis, "fetch").mockImplementation((async (url, init) => { requests.push({ url: String(url), init: init!, body: JSON.parse(String(init!.body)) }); return ok(); }) as typeof fetch);
});
afterEach(async () => {
  for (const c of clients) c.emit("session_shutdown");
  await advance(40000);
  mock.restore();
  for (const [key, value] of saved) { if (value === undefined) delete process.env[key]; else process.env[key] = value; }
  saved.clear();
});

describe("OMP V2 运行闭环", () => {
  it.each([["stop", "completed"], ["error", "failed"], ["aborted", "aborted"], ["length", "unknown"], ["toolUse", "unknown"]])("结构化最终结果 %s → %s", async (reason, status) => {
    const c = client();
    expect(c.emit("agent_start", { prompt: SENTINEL })).toBeUndefined();
    await advance(20000);
    c.emit("session_stop", { last_assistant_message: end(reason).messages[0], session_id: SENTINEL, turn_id: SENTINEL });
    await advance(); expect(requests).toEqual([]);
    expect(c.emit("agent_end", end(reason))).toBeUndefined();
    expect(requests).toEqual([]);
    await advance();
    expect(requests[0].body.status).toBe(status);
    expect(requests[0].body.cycle.durationMs).toBe(20000);
    expect(requests[0].body.event).toBe("omp.run_finished");
    expect(requests[0].body.version).toBe(2);
    expect(JSON.stringify(requests[0].body)).not.toContain(SENTINEL);
    expect(requests[0].body.session.cwdName).toBe("project");
    expect(requests[0].body.session.ref).toMatch(/^[a-f0-9]{32}$/);
    c.emit("agent_end", end(reason)); c.emit("session_stop", { last_assistant_message: end(reason).messages[0] });
    await advance(); expect(requests).toHaveLength(1);
  });
  it.each([false, true])("重试及后台续跑不提前结束：awaiting=%s", async (background) => {
    const c = client(); c.emit("agent_start"); await advance(1000);
    c.emit("agent_end", end("error", { willContinue: true, awaitingAsyncWork: background }));
    await advance(1000); expect(requests).toEqual([]);
    c.emit("agent_start"); await advance(1000); c.emit("agent_end", end()); await advance();
    expect(requests[0].body.status).toBe("completed"); expect(requests[0].body.cycle.durationMs).toBe(3000);
  });
  it.each([true, false])("重试结束与最终结束交错：恢复=%s", async (success) => {
    const c = client(); c.emit("agent_start"); c.emit("auto_retry_start", { errorMessage: SENTINEL });
    c.emit("agent_end", end()); await advance(); expect(requests).toEqual([]);
    c.emit("auto_retry_end", { success, finalError: SENTINEL }); await advance();
    expect(requests[0].body.status).toBe(success ? "completed" : "failed");
  });
  it("工具错误不升级为任务失败；中止优先于重试失败", async () => {
    const c = client(); c.emit("agent_start"); c.emit("tool_result", { toolName: "bash", isError: true, content: SENTINEL });
    c.emit("agent_end", end()); await advance(); expect(requests[0].body.status).toBe("completed");
    c.emit("agent_start"); c.emit("auto_retry_end", { success: false }); c.emit("agent_end", end("aborted")); await advance();
    expect(requests[1].body.status).toBe("aborted"); expect(requests[1].body.cycle.ref).not.toBe(requests[0].body.cycle.ref);
  });
  it("恢复没有开始记录时不拿旧消息证明成功或伪造耗时", async () => {
    const c = client(); c.emit("agent_end", end()); await advance();
    expect(requests[0].body.status).toBe("unknown"); expect(requests[0].body.cycle.reasons).toContain("missing_start");
    expect(requests[0].body.cycle).not.toHaveProperty("durationMs");
  });
  it("会话切换取消旧提醒并单独结束旧观察周期", async () => {
    const c = client(); c.emit("agent_start"); c.emit("tool_call", { toolName: "ask", toolCallId: "old" });
    const next = context(); c.emit("session_switch", {}, next); c.emit("agent_start", {}, next); c.emit("agent_end", end(), next); await advance(1000);
    expect(requests.map(r => r.body.status)).toEqual(["unknown", "completed"]);
    expect(requests[0].body.cycle.reasons).toContain("session_changed");
    expect(requests[0].body.session.ref).not.toBe(requests[1].body.session.ref);
  });
  it("模型或实际 thinking 变化不假装全程单一值", async () => {
    const c = client(); c.emit("agent_start"); c.setThinking("low"); c.emit("agent_end", end()); await advance();
    expect(requests[0].body.cycle.metadataChanged).toBe(true);
    expect(requests[0].body.cycle).not.toHaveProperty("model"); expect(requests[0].body.cycle).not.toHaveProperty("thinking");
  });
  it("清理控制字符、拒绝路径名称；不从 prompt 取名", async () => {
    const ctx = context(); ctx.sessionManager.getSessionName = () => `/private/${SENTINEL}`;
    const c = client(ctx); c.emit("agent_start", { prompt: SENTINEL }); c.emit("agent_end", end()); await advance();
    expect(requests[0].body.session).not.toHaveProperty("name"); expect(JSON.stringify(requests[0].body)).not.toContain(SENTINEL);
  });
});

describe("交互请求观察", () => {
  const cases = [
    ["tool_approval_requested", "tool_approval_resolved", "approval", "toolCallId"],
    ["plan_review_requested", "plan_review_resolved", "plan_review", "reviewId"],
    ["tool_call", "tool_result", "ask", "toolCallId"],
  ];
  it.each(cases)("%s 快速解决、取消、重复和无 UI 不过期提醒", async (open, close, kind, idKey) => {
    const c = client(); c.emit("agent_start");
    const e = { [idKey]: `raw-${SENTINEL}`, sessionId: c.ctx.sessionManager.getSessionId(), toolName: "ask", input: SENTINEL, planContent: SENTINEL };
    c.emit(open, e); c.emit(close, { ...e, approved: false, choice: undefined }); c.emit(open, e); await advance(1000); expect(requests).toEqual([]);
    c.ctx.hasUI = false; c.emit(open, { ...e, [idKey]: "no-ui" }); await advance(1000); expect(requests).toEqual([]);
    c.ctx.hasUI = true; c.emit(open, { ...e, [idKey]: "visible" }); c.emit(open, { ...e, [idKey]: "visible" }); await advance(150);
    expect(requests[0].body.interaction.kind).toBe(kind); expect(requests).toHaveLength(1);
    expect(JSON.stringify(requests[0].body)).not.toContain(SENTINEL);
  });
  it("eval 参数不是 ask 观察来源，只有实际间接工具事件才触发", async () => {
    const c = client(); c.emit("agent_start"); c.emit("tool_call", { toolName: "eval", toolCallId: "eval", input: { code: "tool.ask(PRIVATE_SENTINEL)" } }); await advance(1000); expect(requests).toEqual([]);
    c.emit("tool_call", { toolName: "ask", toolCallId: "eval:ask", input: SENTINEL }); await advance(150);
    expect(requests[0].body.interaction.kind).toBe("ask");
    c.emit("tool_result", { toolName: "ask", toolCallId: "eval:ask", content: SENTINEL });
  });
  it("解决先于 requested 时保留 tombstone，不补发提醒", async () => {
    const c = client(); c.emit("agent_start"); const e = { toolName: "ask", toolCallId: "late" };
    c.emit("tool_result", e); c.emit("tool_call", e); await advance(1000); expect(requests).toEqual([]);
    c.emit("agent_end", end()); await advance(); expect(requests[0].body.cycle.reasons).toContain("unpaired_interaction");
  });
  it("交互有界；超限不把原始 ID 记日志", async () => {
    const c = client(); c.emit("agent_start");
    for (let i = 0; i < 140; i++) { const e = { toolName: "ask", toolCallId: `${SENTINEL}-${i}` }; c.emit("tool_call", e); c.emit("tool_result", e); }
    c.emit("agent_end", end()); await advance(); expect(requests[0].body.cycle.reasons).toContain("capacity"); expect(JSON.stringify(c.logs)).not.toContain(SENTINEL);
  });
});

describe("安全投递", () => {
  it.each([401, 403, 429, 500, 503])("HTTP %i 分类与稳定重试 ID", async (status) => {
    const c = client(); spyOn(globalThis, "fetch").mockImplementation((async (url, init) => { requests.push({ url: String(url), init: init!, body: JSON.parse(String(init!.body)) }); return new Response(SENTINEL, { status }); }) as typeof fetch);
    c.emit("agent_start"); c.emit("agent_end", end()); await advance(1500);
    expect(requests.length).toBe(status >= 429 ? 3 : 1); expect(new Set(requests.map(r => r.body.id)).size).toBe(1);
    expect(categories(c.logs)).toContain(status >= 429 ? "transient_error" : "permanent_error"); expect(JSON.stringify(c.logs)).not.toContain(SENTINEL);
  });
  it.each([
    ["skipped", { delivered: false, skipped: true }, "policy_skipped"],
    ["skipped", { delivered: false, skip_reason: "idempotency_replay" }, "replayed"],
    ["partial_failure", { delivered: true, retryable: true }, "partial_failure"],
    ["partial_delivery", { delivered: true }, "partial_delivery"],
    ["ok", { delivered: false }, "unknown"],
  ])("业务 %s 不当作完整送达或绕过幂等", async (message, data, category) => {
    const c = client(); spyOn(globalThis, "fetch").mockResolvedValue(ok(message as string, data as Event));
    c.emit("agent_start"); c.emit("agent_end", end()); await advance(2000);
    expect(categories(c.logs)).toEqual([category]);
  });
  it("同步 fetch 异常不阻断观察；异步重试日志不含 URL 或异常原文", async () => {
    const c = client(); spyOn(globalThis, "fetch").mockImplementation(() => { throw new Error(SENTINEL); });
    expect(c.emit("agent_start")).toBeUndefined(); expect(c.emit("agent_end", end())).toBeUndefined(); await advance(1500);
    expect(categories(c.logs)).toEqual(["transport_unknown", "transport_unknown", "transport_unknown"]); expect(JSON.stringify(c.logs)).not.toContain(SENTINEL);
  });
  it.each(["url", "token"])("缺少 %s 停止发送并安全诊断", async (field) => {
    delete process.env[field === "url" ? "OMP_SESSION_WEBHOOK_URL" : "OMP_SESSION_WEBHOOK_TOKEN"];
    const c = client(); c.emit("agent_start"); c.emit("agent_end", end()); await advance(); expect(requests).toEqual([]); expect(categories(c.logs)).toContain("configuration_missing");
  });
  it.each(["https://user:secret@example.invalid", "file:///tmp/test", "https://example.invalid/?token=secret"])("拒绝不安全 URL %s", async (url) => {
    process.env.OMP_SESSION_WEBHOOK_URL = url; const c = client(); c.emit("agent_start"); c.emit("agent_end", end()); await advance();
    expect(requests).toEqual([]); expect(categories(c.logs)).toContain("configuration_invalid"); expect(JSON.stringify(c.logs)).not.toContain(url);
  });
  it("超限响应和非 JSON 不算送达", async () => {
    const c = client(); spyOn(globalThis, "fetch").mockResolvedValue(new Response(SENTINEL.repeat(1000)));
    c.emit("agent_start"); c.emit("agent_end", end()); await advance(); expect(categories(c.logs)).toEqual(["unknown"]);
  });
});

describe("发送资源与观察故障", () => {
  it("队列与并发有界，已入队的交互在发送前解决则撤销", async () => {
    const gate = Promise.withResolvers<void>();
    let inFlight = 0;
    let peak = 0;
    spyOn(globalThis, "fetch").mockImplementation((async (url, init) => {
      inFlight++; peak = Math.max(peak, inFlight);
      requests.push({ url: String(url), init: init!, body: JSON.parse(String(init!.body)) });
      await gate.promise; inFlight--; return ok();
    }) as typeof fetch);
    const c = client();
    for (let i = 0; i < 2; i++) { c.emit("agent_start"); c.emit("agent_end", end()); }
    await advance();
    c.emit("agent_start"); c.emit("tool_call", { toolName: "ask", toolCallId: "queued" }); await advance(150);
    c.emit("tool_result", { toolName: "ask", toolCallId: "queued" });
    gate.resolve(); await advance();
    expect(peak).toBe(2); expect(requests).toHaveLength(2);
    c.emit("agent_end", end()); await advance();
  });
  it("过载明确丢弃，不创建无界重试计时器", async () => {
    const gate = Promise.withResolvers<void>();
    spyOn(globalThis, "fetch").mockImplementation((async (url, init) => {
      requests.push({ url: String(url), init: init!, body: JSON.parse(String(init!.body)) });
      await gate.promise; return ok();
    }) as typeof fetch);
    const c = client();
    for (let i = 0; i < 80; i++) { c.emit("agent_start"); c.emit("agent_end", end()); }
    await advance();
    expect(requests).toHaveLength(2);
    expect(categories(c.logs).filter(v => v === "queue_capacity")).toHaveLength(16);
    gate.resolve(); await advance();
    expect(requests).toHaveLength(64);
  });
  it("公开 getter 抛错不阻断 OMP，也不输出异常正文", () => {
    const c = client();
    c.ctx.sessionManager.getSessionName = () => { throw new Error(SENTINEL); };
    expect(c.emit("agent_start")).toBeUndefined();
    expect(categories(c.logs)).toContain("observation_failed");
    expect(JSON.stringify(c.logs)).not.toContain(SENTINEL);
  });
  it("有限重试中的交互一旦解决不再发送", async () => {
    const c = client();
    spyOn(globalThis, "fetch").mockImplementation((async (url, init) => {
      requests.push({ url: String(url), init: init!, body: JSON.parse(String(init!.body)) });
      return new Response(null, { status: 503 });
    }) as typeof fetch);
    c.emit("agent_start"); c.emit("tool_call", { toolName: "ask", toolCallId: "cancel-retry" }); await advance(150);
    c.emit("tool_result", { toolName: "ask", toolCallId: "cancel-retry" }); await advance(2000);
    expect(requests).toHaveLength(1);
  });
});
