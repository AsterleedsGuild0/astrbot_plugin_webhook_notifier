import { afterEach, beforeEach, describe, expect, it, mock, spyOn } from "bun:test";
import register from "./webhook-notifier.ts";

const envKeys = [
  "OMP_SESSION_WEBHOOK_URL", "ONEBOT_WEBHOOK_URL",
  "OMP_SESSION_WEBHOOK_TOKEN", "ONEBOT_WEBHOOK_TOKEN",
  "OMP_SESSION_WEBHOOK_TIMEOUT_MS", "ONEBOT_WEBHOOK_TIMEOUT_MS",
];
const savedEnv = new Map<string, string | undefined>();
let callbacks: (() => void)[];
type Model = { provider: string; id: string; name: string };
type Context = {
  model?: Model;
  sessionManager: {
    getEntries(): { type: string }[];
    getBranch(): { type: string }[];
    getSessionId(): string;
    getSessionFile(): string;
    getCwd(): string;
    getSessionName(): string;
  };
};
type Events = {
  before_agent_start: { prompt: string; images?: unknown[] };
  session_stop: {
    session_id?: string;
    session_file?: string;
    turn_id: string;
    messages: unknown[];
    stop_hook_active: boolean;
    last_assistant_message?: {
      role: string; provider?: string; model?: string; stopReason?: string;
      timestamp?: number; duration?: number; content?: unknown;
    };
  };
};
type Handler = (event: Events[keyof Events], context: Context) => void;
type Body = {
  event: string;
  version: number;
  emittedAt: string;
  session: Record<string, unknown>;
  round: Record<string, unknown>;
};
let requests: { url: string; init: RequestInit; body: Body }[];
let timeouts: number[];
let clock: number;

function client() {
  const handlers = new Map<string, Handler>();
  const warnings: unknown[][] = [];
  // OMP 边界替身只实现 Hook 消费的成员，不模拟完整 ExtensionAPI。
  const api = {
    on: (event: string, handler: Handler) => handlers.set(event, handler),
    logger: { warn: (...args: unknown[]) => warnings.push(args) },
  } as unknown as Parameters<typeof register>[0];
  register(api);
  return {
    emit<Name extends keyof Events>(name: Name, event: Events[Name], context = makeContext()) {
      const handler = handlers.get(name);
      if (!handler) throw new Error(`No handler for ${name}`);
      handler(event, context);
    },
    warnings,
  };
}

function makeContext(): Context {
  return {
    model: { provider: "test-provider", id: "test-model", name: "Test Model" },
    sessionManager: {
      getEntries: () => [{ type: "message" }, { type: "custom" }],
      getBranch: () => [{ type: "message" }, { type: "custom" }],
      getSessionId: () => "fixture-session",
      getSessionFile: () => "/fixture/session.jsonl",
      getCwd: () => "/fixture/project",
      getSessionName: () => "基准会话",
    },
  };
}

function stopEvent() {
  return { session_id: "event-session", session_file: "/fixture/event.jsonl", turn_id: "turn-1", messages: [{}, {}, {}], stop_hook_active: false };
}

async function flushSends() {
  for (const callback of callbacks.splice(0)) callback();
  await Promise.resolve();
  await Promise.resolve();
}

beforeEach(() => {
  for (const key of envKeys) {
    savedEnv.set(key, process.env[key]);
    delete process.env[key];
  }
  process.env.OMP_SESSION_WEBHOOK_URL = "https://hooks.example.invalid/webhook/test";
  process.env.OMP_SESSION_WEBHOOK_TOKEN = "fixture-token";
  callbacks = [];
  requests = [];
  timeouts = [];
  clock = Date.parse("2026-10-22T08:00:00Z");
  spyOn(Date, "now").mockImplementation(() => clock);
  spyOn(globalThis, "setTimeout").mockImplementation(((callback: () => void) => {
    callbacks.push(callback);
    return 1;
  }) as typeof setTimeout);
  spyOn(AbortSignal, "timeout").mockImplementation((ms) => {
    timeouts.push(ms);
    return new AbortController().signal;
  });
  spyOn(globalThis, "fetch").mockImplementation((async (url, init) => {
    // 数据来自当前 Hook 对受控事件的序列化；各场景断言实际出站字段。
    const body = JSON.parse(String(init!.body)) as Body;
    requests.push({ url: String(url), init: init!, body });
    return new Response('{"code":0,"message":"ok"}', { status: 200 });
  }) as typeof fetch);
});

afterEach(() => {
  mock.restore();
  for (const key of envKeys) {
    const value = savedEnv.get(key);
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  savedEnv.clear();
});

describe("OMP Hook V1 基线", () => {
  it("从完整回合生成可供服务端解析的统计、时间和模型字段", async () => {
    const hook = client();
    const context = makeContext();
    hook.emit("before_agent_start", { prompt: "执行验证", images: [{}, {}] }, context);
    context.sessionManager.getEntries = () => Array.from({ length: 5 }, () => ({ type: "message" }));
    clock += 20_000;
    hook.emit("session_stop", {
      ...stopEvent(),
      last_assistant_message: {
        role: "assistant", provider: "test-provider", model: "last-model",
        stopReason: "stop", timestamp: clock, duration: 1234,
        content: [{ type: "text", text: "不发送的回答正文" }],
      },
    }, context);
    expect(requests).toEqual([]);
    await flushSends();
    expect(requests).toHaveLength(1);
    const { init, body } = requests[0];
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("x-omp-event")).toBe("session_stop");
    expect(new Headers(init.headers).get("authorization")).toBe("Bearer fixture-token");
    expect(body.event).toBe("omp.session_stop");
    expect(body.version).toBe(1);
    expect(body.session).toEqual({ id: "event-session", file: "/fixture/event.jsonl", cwd: "/fixture/project", name: "基准会话", model: context.model });
    expect(body.round).toEqual({
      turnId: "turn-1", startedAt: "2026-10-22T08:00:00.000Z", endedAt: "2026-10-22T08:00:20.000Z",
      durationMs: 20000, prompt: "执行验证", promptLength: 4, imageCount: 2,
      entryCountBefore: 2, entryCountAfter: 5, entryCountDelta: 3,
      messageCountBefore: 1, messageCountAfter: 3, messageCountDelta: 2,
      stopHookActive: false,
      lastAssistant: { provider: "test-provider", model: "last-model", stopReason: "stop", timestamp: "2026-10-22T08:00:20.000Z", durationMs: 1234 },
    });
    expect(body.emittedAt).toBe(body.round.endedAt);
    expect(JSON.stringify(body)).not.toContain("不发送的回答正文");
  });

  it.each([1999, 2000, 2001])("限制 prompt 传输长度并保留原始长度：%i", async (length) => {
    const hook = client();
    hook.emit("before_agent_start", { prompt: "文".repeat(length) });
    hook.emit("session_stop", stopEvent());
    await flushSends();
    expect(requests[0].body.round.prompt).toBe("文".repeat(Math.min(length, 2000)) + (length > 2000 ? "…" : ""));
    expect(requests[0].body.round.promptLength).toBe(length);
  });

  it("后续无开始事件的结束通知不复用上一回合数据", async () => {
    const hook = client();
    hook.emit("before_agent_start", { prompt: "第一轮", images: [{}] });
    hook.emit("session_stop", stopEvent());
    hook.emit("session_stop", { ...stopEvent(), turn_id: "turn-2" });
    await flushSends();
    expect(requests[0].body.round.prompt).toBe("第一轮");
    const second = requests[1].body.round;
    expect(second.turnId).toBe("turn-2");
    for (const key of ["prompt", "promptLength", "startedAt", "durationMs", "entryCountDelta", "messageCountDelta"])
      expect(second).not.toHaveProperty(key);
    expect(second.imageCount).toBe(0);
  });

  it("Hook 实例之间不共享回合状态", async () => {
    const first = client();
    const second = client();
    first.emit("before_agent_start", { prompt: "甲" });
    second.emit("before_agent_start", { prompt: "乙" });
    second.emit("session_stop", stopEvent());
    first.emit("session_stop", stopEvent());
    await flushSends();
    expect(requests.map(r => r.body.round.prompt)).toEqual(["乙", "甲"]);
  });

  it("缺失会话标识时回退 sessionManager，非 assistant 不外发其内容", async () => {
    const hook = client();
    const context = { ...makeContext(), model: undefined };
    hook.emit("session_stop", { ...stopEvent(), session_id: "", session_file: undefined, last_assistant_message: { role: "user", content: "私密文本" } }, context);
    await flushSends();
    expect(requests[0].body.session.id).toBe("fixture-session");
    expect(requests[0].body.session.file).toBe("/fixture/session.jsonl");
    expect(requests[0].body.session).not.toHaveProperty("model");
    expect(requests[0].body.round).not.toHaveProperty("lastAssistant");
  });

  it("主配置优先于旧 ONEBOT 配置并去除首尾空白", async () => {
    process.env.OMP_SESSION_WEBHOOK_URL = "  https://hooks.example.invalid/primary  ";
    process.env.OMP_SESSION_WEBHOOK_TOKEN = "  primary-token  ";
    process.env.OMP_SESSION_WEBHOOK_TIMEOUT_MS = "15000";
    process.env.ONEBOT_WEBHOOK_URL = "https://hooks.example.invalid/legacy";
    process.env.ONEBOT_WEBHOOK_TOKEN = "legacy-token";
    process.env.ONEBOT_WEBHOOK_TIMEOUT_MS = "7000";
    client().emit("session_stop", stopEvent());
    await flushSends();
    expect(requests[0].url).toBe("https://hooks.example.invalid/primary");
    expect(new Headers(requests[0].init.headers).get("authorization")).toBe("Bearer primary-token");
    expect(timeouts).toEqual([15000]);
  });

  it("空主配置回退旧 ONEBOT 配置", async () => {
    process.env.OMP_SESSION_WEBHOOK_URL = " ";
    process.env.OMP_SESSION_WEBHOOK_TOKEN = " ";
    process.env.ONEBOT_WEBHOOK_URL = "https://hooks.example.invalid/legacy";
    process.env.ONEBOT_WEBHOOK_TOKEN = "legacy-token";
    client().emit("session_stop", stopEvent());
    await flushSends();
    expect(requests[0].url).toBe("https://hooks.example.invalid/legacy");
    expect(new Headers(requests[0].init.headers).get("authorization")).toBe("Bearer legacy-token");
  });

  it.each(["bad", "0", "-1", "Infinity"])("非法主 timeout 使用有效旧配置：%s", async (value) => {
    process.env.OMP_SESSION_WEBHOOK_TIMEOUT_MS = value;
    process.env.ONEBOT_WEBHOOK_TIMEOUT_MS = "7000";
    client().emit("session_stop", stopEvent());
    await flushSends();
    expect(timeouts).toEqual([7000]);
  });

  it("URL 未配置时不发送请求", async () => {
    delete process.env.OMP_SESSION_WEBHOOK_URL;
    const hook = client();
    hook.emit("session_stop", stopEvent());
    await flushSends();
    expect(requests).toEqual([]);
    expect(hook.warnings).toEqual([]);
  });
});
