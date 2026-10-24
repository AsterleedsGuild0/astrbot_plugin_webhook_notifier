import { afterEach, beforeEach, describe, expect, it, mock, spyOn } from "bun:test";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent/extensibility/extensions";
import register, { type Envelope } from "./index.ts";

type Event = Record<string, unknown>;
type Handler = (event: Event, context: ExtensionContext) => unknown;
const envKeys = ["OMP_SESSION_WEBHOOK_URL", "ONEBOT_WEBHOOK_URL", "OMP_SESSION_WEBHOOK_TOKEN", "ONEBOT_WEBHOOK_TOKEN", "OMP_SESSION_WEBHOOK_TIMEOUT_MS", "ONEBOT_WEBHOOK_TIMEOUT_MS", "OMP_SESSION_WEBHOOK_INSTANCE"];
const saved = new Map<string, string | undefined>();
const SENTINEL = "PRIVATE_SENTINEL";
let now: number;
let timerId: number;
let timers: Map<number, { at: number; fn: () => void }>;
let requests: { url: string; init: RequestInit; body: Envelope }[];
let clients: { emit: (name: string, event?: Event, context?: ExtensionContext) => unknown }[];
let sequence = 0;
const claims = new WeakMap<ExtensionContext, { id: string; agentId: string; ownerId: string }[]>();

function context(id = `session-${++sequence}`): ExtensionContext {
  const jobs: { id: string; agentId: string; ownerId: string }[] = [];
  // 仅模拟观察器使用的公开表面，其余 ExtensionContext 能力不执行。
  const ctx = {
    hasUI: true, mode: "tui", cwd: `/private/${SENTINEL}/project`,
    agent: { kind: "main", id: `raw-agent-${SENTINEL}`, name: "main", depth: 0 },
    model: { provider: "test", id: "model" },
    sessionManager: { getSessionId: () => id, getSessionName: () => "测试会话", getCwd: () => `/private/${SENTINEL}/project` },
    getAsyncJobSnapshot: () => ({ running: jobs, recent: [] }),
  } as unknown as ExtensionContext;
  claims.set(ctx, jobs);
  return ctx;
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
function child(parent: ExtensionContext, id: string, name = "显式任务名") {
  const ctx = context();
  ctx.hasUI = false;
  ctx.agent = { kind: "sub", id, name: "task", parentId: parent.agent.id, depth: parent.agent.depth + 1 };
  ctx.sessionManager.getSessionName = () => name;
  claims.get(parent)!.push({ id: `job-${id}`, agentId: id, ownerId: parent.agent.id });
  return client(ctx);
}
function rootPayload() {
  const body = requests.findLast(row => row.body.session.scope === "root" && row.body.event === "omp.run_finished")?.body;
  if (!body?.subagentTimeline || !body.userWaitTimeline) throw new Error("Missing root timeline");
  return { ...body, subagentTimeline: body.subagentTimeline, userWaitTimeline: body.userWaitTimeline };
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
  spyOn(performance, "now").mockImplementation(() => now);
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
    await advance(150);
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
    c.emit("agent_start"); c.emit("auto_retry_end", { success: false }); c.emit("agent_end", end("aborted")); await advance(150);
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
  it("会话名中的相对路径保留，绝对路径与 URL 替换为占位", async () => {
    const ctx = context(); ctx.sessionManager.getSessionName = () => `NODE: 测试 Phowx/script 的 /private/${SENTINEL}/x 与 https://e.invalid/${SENTINEL}`;
    const c = client(ctx); c.emit("agent_start"); c.emit("agent_end", end()); await advance();
    expect(requests[0].body.session.name).toBe("NODE: 测试 Phowx/script 的 … 与 …");
    expect(JSON.stringify(requests[0].body)).not.toContain(SENTINEL);
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
    const reminders = () => requests.filter(r => r.body.event === "omp.action_required");
    const e = { [idKey]: `raw-${SENTINEL}`, sessionId: c.ctx.sessionManager.getSessionId(), toolName: "ask", input: SENTINEL, planContent: SENTINEL };
    // 计划评审取消会结束本轮（见下方计划专项），此处用保持选择检验通用的重复与快速解决语义。
    c.emit(open, e); c.emit(close, { ...e, approved: false, choice: kind === "plan_review" ? "keep" : undefined }); c.emit(open, e); await advance(1000); expect(reminders()).toEqual([]);
    c.ctx.hasUI = false; c.emit(open, { ...e, [idKey]: "no-ui" }); await advance(1000); expect(reminders()).toEqual([]);
    c.ctx.hasUI = true; c.emit(open, { ...e, [idKey]: "visible" }); c.emit(open, { ...e, [idKey]: "visible" }); await advance(150);
    expect(reminders()[0].body.interaction.kind).toBe(kind); expect(reminders()).toHaveLength(1);
    expect(JSON.stringify(reminders()[0].body)).not.toContain(SENTINEL);
  });
  it("待处理提醒按 OpenCode 问题卡携带题干、标题、选项描述与推荐；审批带工具名、计划带标题，不带答案与正文", async () => {
    const c = client(); c.emit("agent_start");
    c.emit("tool_call", { toolName: "ask", toolCallId: "q", input: { questions: [
      { id: "a", header: "取得方式", question: "这次用哪种方式取得 Cookie？", recommended: 0,
        options: [{ label: "手动复制", description: `读取剪贴板，不读 /private/${SENTINEL}` }, { label: "临时 MITM 捕获" }] },
      { id: "b", question: "完成后清理？", options: [{ label: "是" }] },
    ] } });
    const sid = c.ctx.sessionManager.getSessionId();
    c.emit("tool_approval_requested", { sessionId: sid, toolCallId: "t", toolName: "bash", reason: SENTINEL });
    c.emit("plan_review_requested", { sessionId: sid, reviewId: "p", title: "迁移计划", planContent: SENTINEL, planFilePath: `/private/${SENTINEL}` });
    await advance(150);
    const byKind = Object.fromEntries(requests.map(row => [row.body.interaction.kind, row.body.interaction]));
    expect(byKind.ask).toMatchObject({ questionCount: 2, questions: [
      { text: "这次用哪种方式取得 Cookie？", header: "取得方式", options: [
        { label: "手动复制", description: "读取剪贴板，不读 …", recommended: true }, { label: "临时 MITM 捕获" }] },
      { text: "完成后清理？", options: [{ label: "是" }] },
    ] });
    expect(byKind.approval).toMatchObject({ title: "bash" });
    expect(byKind.plan_review).toMatchObject({ title: "迁移计划" });
    expect(JSON.stringify(requests.map(row => row.body))).not.toContain(SENTINEL);
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

describe("共享 run 的多代理观察时间线", () => {
  it("批量并发与嵌套按真实事件汇总；子代理无需 session_stop，也不逐项刷完成通知", async () => {
    const root = client(); root.emit("agent_start");
    await advance(100);
    const a = child(root.ctx, "A"); a.emit("agent_start");
    await advance(100);
    const b = child(root.ctx, "B"); b.emit("agent_start");
    await advance(100);
    const nested = child(a.ctx, "Nested"); nested.emit("agent_start");
    await advance(400); nested.emit("agent_end", end()); nested.emit("session_shutdown");
    await advance(100); a.emit("agent_end", end());
    await advance(100); b.emit("agent_end", end());
    await advance(100);
    expect(requests).toHaveLength(0);
    root.emit("agent_end", end()); await advance();
    const p = rootPayload(), items = p.subagentTimeline.items;
    expect(p.cycle.durationMs).toBe(1000);
    expect(items.map(i => [i.startOffsetMs, i.endOffsetMs, i.depth])).toEqual([[100, 800, 1], [200, 900, 1], [300, 700, 2]]);
    expect(items[2].parentRef).toBe(items[0].cycleRef);
    expect(items.every(i => i.status === "completed" && i.timingQuality === "observed")).toBe(true);
    expect(p.subagentTimeline.partial).toBe(false);
    expect(JSON.stringify(p)).not.toContain(SENTINEL);
  });
  it("workpool 同一 worker 多次运行保留不同 cycle，不把身份当执行记录", async () => {
    const root = client(); root.emit("agent_start");
    const worker = child(root.ctx, "ReusedWorker"); worker.emit("agent_start"); await advance(10); worker.emit("agent_end", end());
    worker.emit("agent_start"); await advance(20); worker.emit("agent_end", end());
    root.emit("agent_end", end()); await advance();
    const items = rootPayload().subagentTimeline.items;
    expect(items.map(i => i.durationMs)).toEqual([10, 20]);
    expect(new Set(items.map(i => i.cycleRef)).size).toBe(2);
    expect(items.map(i => i.attempt)).toEqual([1, 1]);
  });
  it("子代理成功 yield 后的 toolUse／aborted 结尾按完成汇总；yield 失败仍按原状态单独提醒", async () => {
    const root = client(); root.emit("agent_start");
    const evalChild = child(root.ctx, "E1", "E1"); evalChild.emit("agent_start");
    evalChild.emit("tool_result", { toolName: "yield", toolCallId: "y1", isError: false }); evalChild.emit("agent_end", end("toolUse"));
    const worker = child(root.ctx, "W1", "W1"); worker.emit("agent_start");
    worker.emit("tool_result", { toolName: "yield", toolCallId: "y2", isError: false }); worker.emit("agent_end", end("aborted"));
    const failed = child(root.ctx, "E2", "E2"); failed.emit("agent_start");
    failed.emit("tool_result", { toolName: "yield", toolCallId: "y3", isError: true }); failed.emit("agent_end", end("toolUse"));
    await advance();
    expect(requests.map(r => [r.body.session.name, r.body.status])).toEqual([["E2", "unknown"]]);
    root.emit("agent_end", end()); await advance();
    expect(rootPayload().subagentTimeline.items.map(i => [i.name, i.status])).toEqual([["E1", "completed"], ["W1", "completed"], ["E2", "unknown"]]);
  });
  it("后台等待与 agent 观察区间分离，续跑不扩大子代理覆盖", async () => {
    const root = client(); root.emit("agent_start");
    const sub = child(root.ctx, "Background"); sub.emit("agent_start");
    await advance(100); sub.emit("agent_end", end("stop", { willContinue: true, awaitingAsyncWork: true }));
    await advance(500); sub.emit("agent_start");
    await advance(100); sub.emit("agent_end", end());
    root.emit("agent_end", end()); await advance();
    const p = rootPayload(), items = p.subagentTimeline.items;
    expect(items.map(i => [i.startOffsetMs, i.endOffsetMs, i.status, i.attempt])).toEqual([[0, 100, "continued", 1], [600, 700, "completed", 2]]);
    expect(items[0].cycleRef).toBe(items[1].cycleRef);
    expect(items[0].ref).not.toBe(items[1].ref);
    expect(p.userWaitTimeline.intervals[0]).toMatchObject({ kind: "background_wait", intervalState: "complete", startOffsetMs: 100, endOffsetMs: 600, durationMs: 500 });
  });
  it("相同 Main 身份的两个根实例按快照证据隔离", async () => {
    const left = client(), right = client();
    left.ctx.sessionManager.getSessionName = () => "Left"; right.ctx.sessionManager.getSessionName = () => "Right";
    left.emit("agent_start"); right.emit("agent_start");
    const a = child(left.ctx, "LeftChild"), b = child(right.ctx, "RightChild");
    a.emit("agent_start"); b.emit("agent_start"); await advance(5);
    a.emit("agent_end", end()); b.emit("agent_end", end());
    left.emit("agent_end", end()); right.emit("agent_end", end()); await advance();
    const roots = requests.filter(r => r.body.session.scope === "root").map(r => r.body);
    expect(roots).toHaveLength(2);
    expect(roots.map(p => p.subagentTimeline?.items.length)).toEqual([1, 1]);
    expect(roots[0].subagentTimeline?.items[0].parentRef).toBe(roots[0].cycle.ref);
    expect(roots[1].subagentTimeline?.items[0].parentRef).toBe(roots[1].cycle.ref);
    expect(roots[0].session.ref).not.toBe(roots[1].session.ref);
  });
  it("后到的父方证据能关联已结束记录，不能从任务参数猜关联", async () => {
    const root = client(); root.emit("agent_start");
    const sub = child(root.ctx, "LateEvidence");
    const evidence = claims.get(root.ctx)!.pop()!;
    sub.emit("agent_start"); await advance(20); sub.emit("agent_end", end());
    claims.get(root.ctx)!.push(evidence);
    root.emit("agent_end", end()); await advance();
    expect(rootPayload().subagentTimeline.items[0].durationMs).toBe(20);
    expect(rootPayload().subagentTimeline.partialReasons).not.toContain("missing_parent");
  });
  it("缺开始、未结束和异常独立提醒保留不完整边界", async () => {
    const root = client(); root.emit("agent_start");
    const missing = child(root.ctx, "MissingStart"); missing.emit("agent_end", end("error"));
    const running = child(root.ctx, "StillRunning"); running.emit("agent_start");
    await advance(30); root.emit("agent_end", end()); await advance();
    const p = rootPayload();
    expect(p.subagentTimeline.partialReasons).toEqual(expect.arrayContaining(["missing_start", "missing_end"]));
    expect(p.subagentTimeline.items.every(i => i.durationMs === undefined)).toBe(true);
    expect(requests.some(r => r.body.session.scope === "subagent" && r.body.status === "failed")).toBe(true);
  });
  it("缺少已加载 child 只报告 loading_gap，不从 snapshot 制造生命周期", async () => {
    const root = client(); root.emit("agent_start");
    claims.get(root.ctx)!.push({ id: "unobserved", agentId: "unloaded", ownerId: root.ctx.agent.id });
    root.emit("agent_end", end()); await advance();
    expect(rootPayload().subagentTimeline.items).toEqual([]);
    expect(rootPayload().subagentTimeline.partialReasons).toContain("loading_gap");
  });
  it("深度超过八层标记省略，不伪装成浅层任务", async () => {
    const root = client(); root.emit("agent_start");
    let parent = root;
    const descendants = [];
    for (let i = 0; i < 10; i++) { parent = child(parent.ctx, `Deep-${i}`); parent.emit("agent_start"); descendants.push(parent); }
    await advance(10);
    for (const sub of descendants.toReversed()) sub.emit("agent_end", end());
    root.emit("agent_end", end()); await advance();
    const t = rootPayload().subagentTimeline;
    expect(t.observedItemCount).toBe(10);
    expect(t.displayedItemCount).toBe(8);
    expect(t.partialReasons).toContain("depth_limit");
    expect(t.items.every(i => i.depth <= 8)).toBe(true);
  });
  it("高扇出与 Unicode 长名受条数和字节双重上限约束", async () => {
    const root = client(); root.emit("agent_start");
    for (let i = 0; i < 80; i++) {
      const sub = child(root.ctx, `Fanout-${i}`, "长".repeat(96));
      sub.emit("agent_start"); await advance(1); sub.emit("agent_end", end()); sub.emit("session_shutdown");
    }
    root.emit("agent_end", end()); await advance();
    const t = rootPayload().subagentTimeline;
    expect(t.observedItemCount).toBe(80);
    expect(t.items.length).toBeLessThanOrEqual(64);
    expect(Buffer.byteLength(JSON.stringify(t))).toBeLessThanOrEqual(24 * 1024);
    expect(t.truncated).toBe(true);
    expect(t.partial).toBe(true);
  });
  it("父身份歧义不会因其中一个根先收束而被错误消解", async () => {
    const left = client(), right = client();
    left.emit("agent_start"); right.emit("agent_start");
    const sub = child(left.ctx, "Collision");
    claims.get(right.ctx)!.push({ id: "another-job", agentId: "Collision", ownerId: right.ctx.agent.id });
    sub.emit("agent_start"); await advance(5); sub.emit("agent_end", end());
    left.emit("agent_end", end()); right.emit("agent_end", end()); await advance();
    const roots = requests.filter(row => row.body.session.scope === "root");
    expect(roots.map(row => row.body.subagentTimeline?.items)).toEqual([[], []]);
    expect(roots.every(row => row.body.subagentTimeline?.partialReasons.includes("loading_gap"))).toBe(true);
  });
  it("过期只终结一次，迟到终结不复活旧 run，新开始得到新 cycle", async () => {
    const root = client(); root.emit("agent_start");
    await advance(86400001); root.emit("agent_end", end()); await advance();
    const old = rootPayload();
    expect(requests).toHaveLength(1);
    expect(old.status).toBe("unknown");
    expect(old.cycle.reasons).toContain("expired");
    root.emit("agent_start"); await advance(10); root.emit("agent_end", end()); await advance();
    expect(rootPayload().cycle.ref).not.toBe(old.cycle.ref);
    expect(rootPayload().cycle.durationMs).toBe(10);
  });
  it("墙钟跳变不改变 monotonic 运行时长", async () => {
    const root = client(); root.emit("agent_start");
    await advance(500);
    spyOn(Date, "now").mockImplementation(() => now + 3600000);
    root.emit("agent_end", end()); await advance();
    expect(rootPayload().cycle.durationMs).toBe(500);
    expect(rootPayload().cycle.reasons).toContain("clock_changed");
  });
});

describe("交互观察区间", () => {
  it("审批／计划／ask 区间保留独立语义，重复解决不扩大时长", async () => {
    const root = client(); root.emit("agent_start");
    const sessionId = root.ctx.sessionManager.getSessionId();
    await advance(10);
    root.emit("tool_approval_requested", { sessionId, toolCallId: "approval" });
    root.emit("plan_review_requested", { sessionId, reviewId: "plan" });
    root.emit("tool_call", { toolName: "ask", toolCallId: "ask", input: SENTINEL });
    await advance(20);
    root.emit("tool_approval_resolved", { sessionId, toolCallId: "approval", approved: false });
    root.emit("plan_review_resolved", { sessionId, reviewId: "plan", choice: "keep" });
    root.emit("tool_result", { toolName: "ask", toolCallId: "ask", content: SENTINEL });
    await advance(10);
    root.emit("tool_result", { toolName: "ask", toolCallId: "ask" });
    root.emit("agent_end", end()); await advance();
    const t = rootPayload().userWaitTimeline;
    expect(t.partial).toBe(false);
    expect(t.intervals.map(i => [i.kind, i.result, i.durationMs])).toEqual([["approval", "cancelled", 20], ["plan_review", "resolved", 20], ["ask", "resolved", 20]]);
    expect(requests).toHaveLength(1);
    expect(JSON.stringify(t)).not.toContain(SENTINEL);
  });
  it("计划提交先报 aborted 时不发中止：评审沿用原周期，批准后转新会话静默收束，取消则按中止报告", async () => {
    for (const choice of ["execute", undefined]) {
      requests = [];
      const root = client(); root.emit("agent_start");
      const sessionId = root.ctx.sessionManager.getSessionId();
      await advance(3000); root.emit("agent_end", end("aborted"));
      root.emit("plan_review_requested", { sessionId, reviewId: "plan", title: "计划" });
      await advance(150);
      expect(requests.map(r => [r.body.event, r.body.status])).toEqual([["omp.action_required", "action_required"]]);
      expect(requests[0].body.cycle.partial).toBe(false);
      await advance(5000); root.emit("plan_review_resolved", { sessionId, reviewId: "plan", choice });
      if (choice) {
        const next = context(); root.emit("session_switch", { reason: "new" }, next);
        root.emit("agent_start", {}, next); await advance(20000); root.emit("agent_end", end(), next); await advance();
        expect(requests.map(r => [r.body.event, r.body.status])).toEqual([["omp.action_required", "action_required"], ["omp.run_finished", "completed"]]);
        expect(requests[1].body.session.ref).not.toBe(requests[0].body.session.ref);
      } else {
        await advance();
        expect(requests.map(r => [r.body.event, r.body.status])).toEqual([["omp.action_required", "action_required"], ["omp.run_finished", "aborted"]]);
        expect(requests[1].body.userWaitTimeline.intervals.map((i: { kind: string; result: string }) => [i.kind, i.result])).toEqual([["plan_review", "cancelled"]]);
      }
      root.emit("session_shutdown"); await advance(40000);
    }
  });
  it("交互中止在暂缓窗口后照常报告；窗口内退出仍按中止，不改成未知", async () => {
    const a = client(); a.emit("agent_start"); a.emit("agent_end", end("aborted"));
    await advance(149); expect(requests).toEqual([]);
    await advance(1); expect(requests.map(r => r.body.status)).toEqual(["aborted"]);
    const b = client(); b.emit("agent_start"); b.emit("agent_end", end("aborted")); b.emit("session_shutdown"); await advance();
    expect(requests.map(r => r.body.status)).toEqual(["aborted", "aborted"]);
    expect(requests[1].body.cycle.reasons).not.toContain("shutdown");
  });
  it("孤立解决与收束时未解决只显示边界，不补造开始／结束", async () => {
    const root = client(); root.emit("agent_start");
    root.emit("tool_result", { toolName: "ask", toolCallId: "orphan" });
    root.emit("tool_call", { toolName: "ask", toolCallId: "open" });
    await advance(10); root.emit("agent_end", end()); await advance();
    const t = rootPayload().userWaitTimeline;
    expect(t.partialReasons).toEqual(["missing_start", "missing_end"]);
    expect(t.intervals.map(i => i.intervalState)).toEqual(["left_censored", "right_censored"]);
    expect(t.intervals.every(i => i.durationMs === undefined)).toBe(true);
  });
});

it("eval 子代理只按实例事件采集，不解析 eval 参数来伪造运行", async () => {
  const root = client(); root.emit("agent_start");
  root.emit("tool_call", { toolName: "eval", toolCallId: "eval", input: { code: SENTINEL } });
  const sub = child(root.ctx, "EvalChild");
  sub.emit("agent_start"); await advance(25); sub.emit("agent_end", end());
  root.emit("tool_result", { toolName: "eval", toolCallId: "eval", content: SENTINEL });
  root.emit("agent_end", end()); await advance();
  expect(rootPayload().subagentTimeline.items.map(item => item.durationMs)).toEqual([25]);
  expect(rootPayload().userWaitTimeline.intervals).toEqual([]);
  expect(JSON.stringify(rootPayload())).not.toContain(SENTINEL);
});

it("恢复时只有快照不补造历史，缺开始的根不能报告精确时长", async () => {
  const root = client();
  claims.get(root.ctx)!.push({ id: "old-job", agentId: "old-worker", ownerId: root.ctx.agent.id });
  root.emit("agent_end", end()); await advance();
  const payload = rootPayload();
  expect(payload.status).toBe("unknown");
  expect(payload.cycle.durationMs).toBeUndefined();
  expect(payload.subagentTimeline.partialReasons).toEqual(["missing_start", "loading_gap"]);
  expect(payload.subagentTimeline.items).toEqual([]);
});

it("模型／thinking 运行中切换后恢复原值也不能冒充全周期单一值", async () => {
  const root = client(); root.emit("agent_start");
  root.setThinking("low"); root.emit("turn_start");
  root.setThinking("high"); root.emit("before_provider_request", { payload: { prompt: SENTINEL } });
  root.emit("agent_end", end()); await advance();
  expect(rootPayload().cycle.metadataChanged).toBe(true);
  expect(rootPayload().cycle.model).toBeUndefined();
  expect(rootPayload().cycle.thinking).toBeUndefined();
  expect(JSON.stringify(rootPayload())).not.toContain(SENTINEL);
});

it.each([`/private/${SENTINEL}/model`, `C:\\private\\${SENTINEL}\\model`, `~/${SENTINEL}/model`, `\\server\\${SENTINEL}\\model`])("模型路径不能借 provider 前缀进入出站：%s", async (id) => {
  const root = client();
  const model = root.ctx.model;
  if (!model) throw new Error("Missing model fixture");
  model.id = id;
  root.emit("agent_start"); root.emit("agent_end", end()); await advance();
  expect(rootPayload().cycle.model).toBeUndefined();
  expect(rootPayload().cycle.reasons).toContain("incomplete_metadata");
  expect(JSON.stringify(requests.map(row => row.body)) + JSON.stringify(root.logs)).not.toContain(SENTINEL);
});

it("子代理会话没有标题时，用 OMP 任务名作为观察段名称", async () => {
  const root = client(); root.emit("agent_start");
  const worker = child(root.ctx, "ReleaseManagementSmoke");
  worker.ctx.sessionManager.getSessionName = () => undefined as unknown as string;
  worker.emit("agent_start"); worker.emit("agent_end", end());
  root.emit("agent_end", end()); await advance();
  expect(rootPayload().subagentTimeline.items[0].name).toBe("ReleaseManagementSmoke");
});
