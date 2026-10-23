/** OMP v18.12.6 公开事件观察器。只发 V2；不执行工具、不读取会话正文或凭据文件。 */
import { createHmac, randomBytes } from "node:crypto";
import { basename } from "node:path";
import type { ExtensionAPI, ExtensionContext, ExtensionEvent } from "@oh-my-pi/pi-coding-agent/extensibility/extensions";

type Status = "completed" | "failed" | "aborted" | "action_required" | "unknown";
type Kind = "approval" | "plan_review" | "ask";
type Reason = "missing_start" | "session_changed" | "shutdown" | "expired" | "capacity" | "unpaired_interaction" | "incomplete_metadata" | "clock_changed";
type Result = "delivered" | "policy_skipped" | "partial_delivery" | "partial_failure" | "replayed" | "permanent_error" | "transient_error" | "transport_unknown" | "unknown";
type Session = { ref: string; scope: "root" | "subagent" | "unknown"; name?: string; cwdName?: string; agent?: string };
type Cycle = { ref: string; partial: boolean; reasons: Reason[]; startedAt?: string; endedAt?: string; durationMs?: number; model?: string; thinking?: string; metadataChanged?: boolean };
type Envelope = { event: "omp.run_finished" | "omp.action_required"; version: 2; id: string; emittedAt: string; session: Session; cycle: Cycle; status: Status; instance?: string; interaction?: { ref: string; kind: Kind } };
type Interaction = { ref: string; kind: Kind; open: boolean; started: number; timer?: NodeJS.Timeout };
type Run = { ref: string; start?: number; created: number; session: Session; reasons: Set<Reason>; interactions: Map<string, Interaction>; retrying: boolean; retryFailed: boolean; pendingEnd?: Status; stop?: Status; model?: string; thinking?: string; metadataChanged: boolean; metadataMissing: boolean; closed: boolean };
type Binding = { owner: symbol; key: string; touched: number; run?: Run };
type Config = { url: string; token: string; timeout: number; instance?: string };
type Job = { payload: Envelope; config: Config; live: () => boolean; log: Log };
type Log = (category: string, id?: string, status?: number, attempt?: number) => void;

const TTL = 86_400_000;
const MAX_SESSIONS = 128;
const MAX_INTERACTIONS = 128;
const MAX_QUEUE = 64;
const MAX_RESPONSE_BYTES = 8192;
const salt = randomBytes(32);
const bindings = new Map<string, Binding>();
const queue: Job[] = [];
let sending = 0;
let pumpScheduled = false;

function ref(): string { return randomBytes(16).toString("hex"); }
function anonymous(value: string): string { return createHmac("sha256", salt).update(value).digest("hex").slice(0, 32); }
function text(value: unknown, limit = 96, model = false): string | undefined {
  if (typeof value !== "string") return;
  const clean = value.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "").replace(/[\p{Cc}\p{Cf}]/gu, "").trim();
  if (!clean || /(?:https?:\/\/|(?:^|\s)(?:[A-Za-z]:[\\/]|[~\\/]))/.test(clean) || (!model && /[\\/]/.test(clean))) return;
  return Array.from(clean).slice(0, limit).join("");
}
function configured(keys: string[]): string | undefined {
  for (const key of keys) { const value = process.env[key]?.trim(); if (value) return value; }
}
function configuration(log: Log): Config | undefined {
  const raw = configured(["OMP_SESSION_WEBHOOK_URL", "ONEBOT_WEBHOOK_URL"]);
  const token = configured(["OMP_SESSION_WEBHOOK_TOKEN", "ONEBOT_WEBHOOK_TOKEN"]);
  if (!raw || !token) { log("configuration_missing"); return; }
  try {
    const url = new URL(raw);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash || /[\r\n]/.test(token) || token.length > 4096) throw new Error();
    let timeout = 5000;
    for (const key of ["OMP_SESSION_WEBHOOK_TIMEOUT_MS", "ONEBOT_WEBHOOK_TIMEOUT_MS"]) {
      const value = Number(process.env[key]);
      if (Number.isInteger(value) && value >= 100 && value <= 30000) { timeout = value; break; }
    }
    return { url: url.href, token, timeout, instance: text(process.env.OMP_SESSION_WEBHOOK_INSTANCE) };
  } catch { log("configuration_invalid"); }
}

/** 不记录响应正文；流读取上限也覆盖没有 Content-Length 的响应。 */
async function responseResult(response: Response): Promise<Result> {
  if (response.status === 429 || response.status >= 500) { void response.body?.cancel().catch(() => {}); return "transient_error"; }
  if (!response.ok) { void response.body?.cancel().catch(() => {}); return "permanent_error"; }
  const reader = response.body?.getReader();
  if (!reader) return "unknown";
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      size += part.value.byteLength;
      if (size > MAX_RESPONSE_BYTES) { await reader.cancel(); return "unknown"; }
      chunks.push(part.value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    let value;
    try { value = JSON.parse(new TextDecoder().decode(bytes)); } catch { return "unknown"; }
    if (value?.code !== 0 || !value.data || typeof value.data.delivered !== "boolean" || typeof value.data.retryable !== "boolean") return "unknown";
    const { data, message } = value;
    if (message === "skipped" && data.delivered === false && data.retryable === false) {
      return data.skip_reason === "idempotency_replay" ? "replayed" : data.skipped === true ? "policy_skipped" : "unknown";
    }
    if (message === "partial_failure") return "partial_failure";
    if (message === "partial_delivery" && data.delivered === true) return "partial_delivery";
    if (message === "ok" && data.delivered === true && data.retryable === false) return "delivered";
    return "unknown";
  } finally { reader.releaseLock(); }
}

function schedulePump(): void {
  if (pumpScheduled) return;
  pumpScheduled = true;
  setTimeout(() => { pumpScheduled = false; pump(); }, 0);
}
function enqueue(job: Job): void {
  if (!job.live()) return;
  if (queue.length + sending >= MAX_QUEUE) { job.log("queue_capacity", job.payload.id); return; }
  queue.push(job);
  schedulePump();
}
function pump(): void {
  while (sending < 2 && queue.length) {
    const job = queue.shift()!;
    if (!job.live()) continue;
    sending++;
    void send(job).catch(() => job.log("transport_unknown", job.payload.id)).finally(() => { sending--; schedulePump(); });
  }
}
async function send(job: Job): Promise<void> {
  // 退避也占用并发名额：队列、重试与未完成响应均有界。
  for (let attempt = 1; attempt <= 3 && job.live(); attempt++) {
    let result: Result = "transport_unknown";
    let httpStatus: number | undefined;
    let retryAfter = 0;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), job.config.timeout);
    try {
      const response = await fetch(job.config.url, {
        method: "POST", redirect: "manual", signal: controller.signal,
        headers: { "Content-Type": "application/json", "Authorization": `Bearer ${job.config.token}`, "X-OMP-Event": job.payload.event, "User-Agent": "omp-webhook-notifier-v2" },
        body: JSON.stringify(job.payload),
      });
      httpStatus = response.status;
      if (httpStatus === 429) {
        const seconds = Number(response.headers.get("retry-after"));
        if (Number.isFinite(seconds)) retryAfter = Math.min(5000, Math.max(0, seconds * 1000));
      }
      result = await responseResult(response);
    } catch { result = "transport_unknown"; }
    finally { clearTimeout(timeout); }
    job.log(result, job.payload.id, httpStatus, attempt);
    if (!["transient_error", "transport_unknown"].includes(result) || attempt === 3 || !job.live()) return;
    const delay = Promise.withResolvers<void>();
    setTimeout(delay.resolve, Math.max(attempt === 1 ? 250 : 1000, retryAfter));
    await delay.promise;
  }
}

function assistantStatus(message: unknown): Status | undefined {
  if (!message || typeof message !== "object") return;
  const value = message as { role?: string; stopReason?: string };
  if (value.role !== "assistant") return;
  return value.stopReason === "stop" ? "completed" : value.stopReason === "error" ? "failed" : value.stopReason === "aborted" ? "aborted" : "unknown";
}
function lastStatus(messages: unknown[]): Status | undefined {
  for (let i = messages.length - 1; i >= 0; i--) { const status = assistantStatus(messages[i]); if (status) return status; }
}

export default (pi: ExtensionAPI): void => {
  const owner = Symbol();
  let active: Binding | undefined;
  const log: Log = (category, id, status, attempt) => {
    try { pi.logger.warn("OMP webhook", { category, ...(id ? { ref: id } : {}), ...(status === undefined ? {} : { status }), ...(attempt ? { attempt } : {}) }); } catch { /* 日志故障不能传入 OMP。 */ }
  };
  const config = configuration(log);
  function observe<K extends ExtensionEvent["type"]>(name: K, handler: (event: Extract<ExtensionEvent, { type: K }>, ctx: ExtensionContext) => void): void {
    // ExtensionAPI.on 是逐事件 overload；包装器不返回任何控制值。
    pi.on(name as never, ((event: Extract<ExtensionEvent, { type: K }>, ctx: ExtensionContext) => {
      try { handler(event, ctx); } catch { log("observation_failed"); }
    }) as never);
  }
  function metadata(run: Run, ctx: ExtensionContext): void {
    const model = ctx.model ? text(`${ctx.model.provider}/${ctx.model.id}`, 128, true) : undefined;
    const thinking = text(pi.getThinkingLevel?.(), 32);
    if (run.model !== model || run.thinking !== thinking) run.metadataChanged = true;
    if (!model || !thinking) run.metadataMissing = true;
    run.session.name = text(ctx.sessionManager.getSessionName());
  }
  function cycle(run: Run, now: number, terminal: boolean): Cycle {
    const reasons = new Set(run.reasons);
    if (run.metadataMissing) reasons.add("incomplete_metadata");
    const elapsed = run.start === undefined ? undefined : now - run.start;
    if (elapsed !== undefined && (elapsed < 0 || elapsed > TTL)) reasons.add("clock_changed");
    return {
      ref: run.ref, partial: reasons.size > 0, reasons: [...reasons],
      ...(run.start === undefined ? {} : { startedAt: new Date(run.start).toISOString() }),
      ...(terminal ? { endedAt: new Date(now).toISOString() } : {}),
      ...(elapsed === undefined || elapsed < 0 || elapsed > TTL ? {} : { durationMs: elapsed }),
      ...(!run.metadataChanged && !run.metadataMissing ? { model: run.model, thinking: run.thinking } : {}),
      ...(run.metadataChanged ? { metadataChanged: true } : {}),
    };
  }
  function notify(run: Run, status: Status, interaction?: Interaction): void {
    if (!config) return;
    const now = Date.now();
    const payload: Envelope = {
      event: interaction ? "omp.action_required" : "omp.run_finished", version: 2, id: ref(), emittedAt: new Date(now).toISOString(),
      session: { ...run.session }, cycle: cycle(run, now, !interaction), status,
      ...(config.instance ? { instance: config.instance } : {}),
      ...(interaction ? { interaction: { ref: interaction.ref, kind: interaction.kind } } : {}),
    };
    enqueue({ payload, config, live: interaction ? () => interaction.open && !run.closed : () => true, log });
  }
  function finish(binding: Binding, status: Status, reason?: Reason): void {
    const run = binding.run;
    if (!run || run.closed) return;
    if (reason) run.reasons.add(reason);
    for (const interaction of run.interactions.values()) {
      if (interaction.open) run.reasons.add("unpaired_interaction");
      interaction.open = false;
      clearTimeout(interaction.timer);
    }
    run.closed = true;
    notify(run, status);
  }
  function get(ctx: ExtensionContext): Binding | undefined {
    const now = Date.now();
    for (const [key, binding] of bindings) {
      if (now - binding.touched > TTL || (binding.run && now - binding.run.created > TTL)) {
        // 过期仅在本 owner 终结；其他 owner 的未发提醒由 closed 标志撤销。
        if (binding.owner === owner) finish(binding, "unknown", "expired");
        else if (binding.run) { binding.run.closed = true; for (const item of binding.run.interactions.values()) clearTimeout(item.timer); }
        bindings.delete(key);
      }
    }
    const id = ctx.sessionManager.getSessionId();
    if (!id) { log("session_unavailable"); return; }
    const key = anonymous(`${id}\0${ctx.agent?.id ?? ""}`);
    if (active && active.key !== key) finish(active, "unknown", "session_changed");
    let binding = bindings.get(key);
    if (binding && binding.owner !== owner) { log("duplicate_binding"); return; }
    if (!binding) {
      if (bindings.size >= MAX_SESSIONS) {
        for (const [oldKey, old] of bindings) {
          if (!old.run || old.run.closed) { bindings.delete(oldKey); break; }
        }
      }
      if (bindings.size >= MAX_SESSIONS) { log("state_capacity"); return; }
      binding = { key, owner, touched: now };
      bindings.set(key, binding);
    }
    binding.touched = now;
    active = binding;
    return binding;
  }
  function begin(binding: Binding, ctx: ExtensionContext, observed: boolean): Run {
    if (binding.run && !binding.run.closed) return binding.run;
    const model = ctx.model ? text(`${ctx.model.provider}/${ctx.model.id}`, 128, true) : undefined;
    const thinking = text(pi.getThinkingLevel?.(), 32);
    const run: Run = {
      ref: ref(), start: observed ? Date.now() : undefined, created: Date.now(), closed: false,
      session: { ref: binding.key, scope: ctx.agent?.kind === "main" ? "root" : ctx.agent?.kind === "sub" ? "subagent" : "unknown", name: text(ctx.sessionManager.getSessionName()), cwdName: text(basename(ctx.cwd || ctx.sessionManager.getCwd())), agent: text(ctx.agent?.name) },
      reasons: new Set(observed ? [] : ["missing_start"]), interactions: new Map(), retrying: false, retryFailed: false,
      model, thinking, metadataChanged: false, metadataMissing: !model || !thinking,
    };
    binding.run = run;
    return run;
  }
  function request(ctx: ExtensionContext, rawId: string, kind: Kind): void {
    const binding = get(ctx);
    if (!binding || !rawId) return;
    const run = begin(binding, ctx, false);
    const key = anonymous(`${kind}\0${rawId}`);
    if (run.interactions.has(key)) return;
    if (run.interactions.size >= MAX_INTERACTIONS) { run.reasons.add("capacity"); log("interaction_capacity"); return; }
    const interaction: Interaction = { ref: ref(), kind, open: true, started: Date.now() };
    run.interactions.set(key, interaction);
    // requested 不证明 UI 呈现。无 UI 仅配对记录，不发待处理提醒。
    if (ctx.hasUI) interaction.timer = setTimeout(() => {
      try { if (interaction.open && !run.closed) notify(run, "action_required", interaction); } catch { log("observation_failed"); }
    }, 150);
  }
  function resolve(ctx: ExtensionContext, rawId: string, kind: Kind): void {
    const binding = get(ctx);
    if (!binding || !rawId || binding.run?.closed) return;
    const run = begin(binding, ctx, false);
    const key = anonymous(`${kind}\0${rawId}`);
    let interaction = run.interactions.get(key);
    if (!interaction) {
      run.reasons.add("unpaired_interaction");
      if (run.interactions.size >= MAX_INTERACTIONS) { run.reasons.add("capacity"); return; }
      interaction = { ref: ref(), kind, open: false, started: Date.now() };
      run.interactions.set(key, interaction);
    }
    interaction.open = false;
    clearTimeout(interaction.timer);
  }
  observe("agent_start", (_, ctx) => {
    const binding = get(ctx);
    if (binding) {
      const run = begin(binding, ctx, true);
      run.stop = undefined;
      run.pendingEnd = undefined;
      metadata(run, ctx);
    }
  });
  observe("session_stop", (event, ctx) => {
    const binding = get(ctx);
    if (binding && !binding.run?.closed) begin(binding, ctx, false).stop = assistantStatus(event.last_assistant_message);
  });
  observe("auto_retry_start", (_, ctx) => {
    const binding = get(ctx);
    if (binding) { const run = begin(binding, ctx, false); run.retrying = true; run.retryFailed = false; }
  });
  observe("auto_retry_end", (event, ctx) => {
    const binding = get(ctx);
    if (!binding || binding.run?.closed) return;
    const run = begin(binding, ctx, false);
    run.retrying = false;
    run.retryFailed = event.success === false;
    if (run.pendingEnd) finish(binding, run.pendingEnd === "aborted" ? "aborted" : run.retryFailed ? "failed" : run.pendingEnd);
  });
  observe("agent_end", (event, ctx) => {
    const binding = get(ctx);
    if (!binding || binding.run?.closed) return;
    const run = begin(binding, ctx, false);
    metadata(run, ctx);
    if (event.willContinue === true || event.awaitingAsyncWork === true) return;
    const status = run.start === undefined ? "unknown" : run.stop ?? lastStatus(event.messages) ?? "unknown";
    if (run.retrying) { run.pendingEnd = status; return; }
    finish(binding, status === "aborted" ? "aborted" : run.retryFailed ? "failed" : status);
  });
  observe("tool_approval_requested", (event, ctx) => { if (event.sessionId === ctx.sessionManager.getSessionId()) request(ctx, event.toolCallId, "approval"); });
  observe("tool_approval_resolved", (event, ctx) => { if (event.sessionId === ctx.sessionManager.getSessionId()) resolve(ctx, event.toolCallId, "approval"); });
  observe("plan_review_requested", (event, ctx) => { if (event.sessionId === ctx.sessionManager.getSessionId()) request(ctx, event.reviewId, "plan_review"); });
  observe("plan_review_resolved", (event, ctx) => { if (event.sessionId === ctx.sessionManager.getSessionId()) resolve(ctx, event.reviewId, "plan_review"); });
  observe("tool_call", (event, ctx) => { if (event.toolName === "ask") request(ctx, event.toolCallId, "ask"); });
  observe("tool_result", (event, ctx) => { if (event.toolName === "ask") resolve(ctx, event.toolCallId, "ask"); });
  for (const name of ["session_switch", "session_branch", "session_tree"] as const) observe(name, (_, ctx) => {
    if (active) finish(active, "unknown", "session_changed");
    active = undefined;
    get(ctx);
  });
  observe("session_shutdown", () => {
    for (const [key, binding] of bindings) if (binding.owner === owner) { finish(binding, "unknown", "shutdown"); bindings.delete(key); }
    active = undefined;
    schedulePump();
  });
};
