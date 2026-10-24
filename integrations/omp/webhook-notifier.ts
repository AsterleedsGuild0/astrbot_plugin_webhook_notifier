/** OMP v18.12.6 公开事件观察器。只发 V2；不执行工具、不读取会话正文或凭据文件。 */
import { createHmac, randomBytes } from "node:crypto";
import { basename } from "node:path";
import type { ExtensionAPI, ExtensionContext, ExtensionEvent } from "@oh-my-pi/pi-coding-agent/extensibility/extensions";

type Status = "completed" | "failed" | "aborted" | "action_required" | "unknown";
type Kind = "approval" | "plan_review" | "ask";
type Reason = "missing_start" | "session_changed" | "shutdown" | "expired" | "capacity" | "unpaired_interaction" | "incomplete_metadata" | "clock_changed";
type Result = "delivered" | "policy_skipped" | "partial_delivery" | "partial_failure" | "replayed" | "permanent_error" | "transient_error" | "transport_unknown" | "unknown";
type Session = { ref: string; scope: "root" | "subagent" | "unknown"; name?: string; cwdName?: string; agent?: string };
type Cycle = { ref: string; partial: boolean; reasons: Reason[]; timeBasis: "receipt_monotonic"; startedAt?: string; endedAt?: string; durationMs?: number; model?: string; thinking?: string; metadataChanged?: boolean };
type AskOption = { label: string; description?: string; recommended?: true };
type AskQuestion = { text: string; header?: string; options?: AskOption[] };
type InteractionDetail = { title?: string; questionCount?: number; questions?: AskQuestion[] };
export type Envelope = { event: "omp.run_finished" | "omp.action_required"; version: 2; id: string; emittedAt: string; session: Session; cycle: Cycle; status: Status; instance?: string; interaction?: { ref: string; kind: Kind } & InteractionDetail; subagentTimeline?: SubagentTimeline; userWaitTimeline?: WaitTimeline };
type IntervalKind = Kind | "background_wait";
type Resolution = "resolved" | "cancelled" | "unknown";
type Interaction = { ref: string; kind: IntervalKind; open: boolean; started?: number; ended?: number; result?: Resolution; timer?: NodeJS.Timeout; detail?: InteractionDetail };
type Span = { ref: string; started?: number; ended?: number; status: Status | "running" | "continued" };
const TIMELINE_REASONS = ["missing_parent", "ambiguous_parent", "missing_start", "missing_end", "loading_gap", "invalid_parent_graph", "depth_limit", "truncated", "clamped", "clock_changed"] as const;
type TimelineReason = typeof TIMELINE_REASONS[number];
type TimelineItem = { ref: string; cycleRef: string; parentRef: string; depth: number; attempt: number; name?: string; agent?: string; model?: string; thinking?: string; metadataChanged?: boolean; status: Status | "running" | "continued"; timingQuality: "observed" | "partial" | "unknown"; startOffsetMs?: number; endOffsetMs?: number; durationMs?: number };
type WaitInterval = { ref: string; agentRef: string; kind: IntervalKind; result?: Resolution; intervalState: "complete" | "right_censored" | "left_censored" | "unknown"; startOffsetMs?: number; endOffsetMs?: number; durationMs?: number };
type TimelineBase = { version: 1; partial: boolean; partialReasons: TimelineReason[]; truncated: boolean };
type SubagentTimeline = TimelineBase & { timeBasis: "omp_agent_observation"; observedItemCount: number; displayedItemCount: number; items: TimelineItem[] };
type WaitTimeline = TimelineBase & { timeBasis: "omp_interaction_observation"; observedIntervalCount: number; displayedIntervalCount: number; intervals: WaitInterval[] };
type SnapshotJob = { id: string; agentId?: string; ownerId?: string };
type Snapshot = { running: SnapshotJob[]; recent: SnapshotJob[] } | null;
type Run = {
  ref: string; start?: number; monoStart?: number; created: number; session: Session; reasons: Set<Reason>;
  interactions: Map<string, Interaction>; background?: Interaction; spans: Span[]; span?: Span; spanCount: number;
  agentKey?: string; parentKey?: string; depth: number; parent?: Run; root?: Run; children?: Run[];
  initialJobs: Set<string>; observedSegments: number; timelineReasons: Set<TimelineReason>;
  retrying: boolean; retryFailed: boolean; pendingEnd?: Status; stop?: Status; model?: string; thinking?: string;
  metadataChanged: boolean; metadataMissing: boolean; closed: boolean; yielded?: boolean;
  /** 交互式根运行以 aborted 收束时暂缓发送：OMP 计划模式提交计划时先 agent_end(aborted) 再打开评审。 */
  abortTimer?: NodeJS.Timeout; abortAt?: { now: number; mono: number }; planHandoff?: boolean;
};
type Binding = { owner: symbol; key: string; touched: number; run?: Run; snapshot?: () => Snapshot };
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
const ABSOLUTE_OR_URL = /(?:^|\s|\/)(?:[A-Za-z]:[\\/]|[~\\/])|https?:\/\//;
/**
 * 名称与交互内容的公共清洗：去控制字符、压缩空白；URL 和绝对／用户目录路径整段替换为 …，
 * 相对路径（如 `core/omp.py`）保留。模型名不做替换，含路径即舍弃。
 */
function text(value: unknown, limit = 96, model = false): string | undefined {
  if (typeof value !== "string") return;
  let clean = value.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "").replace(/\s+/gu, " ").replace(/[\p{Cc}\p{Cf}]/gu, "").trim();
  if (!model) clean = clean.replace(/https?:\/\/\S+/g, "…").replace(/(^|\s)(?:[A-Za-z]:[\\/]|~[\\/]|[\\/])\S*/g, "$1…").trim();
  if (!clean || /^[…\s]+$/.test(clean) || ABSOLUTE_OR_URL.test(clean)) return;
  return Array.from(clean).slice(0, limit).join("").trim() || undefined;
}
/** ask 与 OpenCode 问题卡一致：发送题干、标题、选项标签、描述和推荐项；最多 4 题、每题 8 个选项。不读取答案。 */
function askDetail(input: unknown): InteractionDetail | undefined {
  const raw = (input as { questions?: unknown } | undefined)?.questions;
  if (!Array.isArray(raw) || !raw.length) return;
  const questions: AskQuestion[] = [];
  for (const item of raw.slice(0, 4) as { question?: unknown; header?: unknown; options?: unknown; recommended?: unknown }[]) {
    const questionText = text(item?.question, 300);
    if (!questionText) continue;
    const header = text(item.header, 40);
    const options: AskOption[] = [];
    if (Array.isArray(item.options)) item.options.slice(0, 8).forEach((option, index) => {
      const label = text((option as { label?: unknown } | undefined)?.label, 80);
      if (!label || options.some(existing => existing.label === label)) return;
      const description = text((option as { description?: unknown }).description, 200);
      options.push({ label, ...(description ? { description } : {}), ...(item.recommended === index ? { recommended: true as const } : {}) });
    });
    questions.push({ text: questionText, ...(header ? { header } : {}), ...(options.length ? { options } : {}) });
  }
  return questions.length ? { questionCount: Math.min(raw.length, 99), questions } : undefined;
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
    // 服务端同步渲染并投递：单图约 10 秒，主卡加时间线附图实测约 20 秒；超时过短会在已送达后重试，只得到幂等回放。
    let timeout = 30000;
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

function snapshotJobs(binding: Binding, initial = false): SnapshotJob[] {
  try {
    const snapshot = binding.snapshot?.();
    if (!snapshot) { binding.run?.timelineReasons.add("loading_gap"); return []; }
    if (initial && snapshot.running.length) binding.run?.timelineReasons.add("loading_gap");
    if (snapshot.running.length + snapshot.recent.length > 128) binding.run?.timelineReasons.add("truncated");
    return [...snapshot.running.slice(0, 128), ...snapshot.recent.slice(0, 128)].slice(0, 128);
  } catch { binding.run?.timelineReasons.add("loading_gap"); return []; }
}

/** Factory 共享模块，只关联公开身份与父方快照均能证明的边；从不读取 task 参数或私有 bus。 */
function linkRuns(): void {
  for (const binding of bindings.values()) {
    const run = binding.run;
    if (!run || run.root || !run.parentKey || run.timelineReasons.has("ambiguous_parent")) continue;
    const parents: Run[] = [];
    for (const candidate of bindings.values()) {
      const parent = candidate.run;
      if (!parent || parent.closed || !parent.root || parent.root.closed || parent.agentKey !== run.parentKey) continue;
      if (snapshotJobs(candidate).some(job => job.agentId && job.ownerId && anonymous(job.agentId) === run.agentKey && anonymous(job.ownerId) === parent.agentKey)) parents.push(parent);
    }
    run.timelineReasons.delete("missing_parent"); run.timelineReasons.delete("ambiguous_parent");
    if (parents.length !== 1) { run.timelineReasons.add(parents.length ? "ambiguous_parent" : "missing_parent"); continue; }
    const parent = parents[0];
    if (run.depth !== parent.depth + 1) { parent.root!.timelineReasons.add("invalid_parent_graph"); continue; }
    run.parent = parent; run.root = parent.root;
    const root = run.root!;
    root.observedSegments = Math.min(4096, root.observedSegments + run.spanCount);
    if (root.children!.length < 64) root.children!.push(run);
    else root.timelineReasons.add("truncated");
    if (run.depth > 8) root.timelineReasons.add("depth_limit");
  }
}

function timelines(root: Run, monoNow: number): { subagentTimeline: SubagentTimeline; userWaitTimeline: WaitTimeline } {
  const reasons = new Set(root.timelineReasons);
  const waitReasons = new Set(root.timelineReasons);
  if (root.monoStart === undefined) { reasons.add("missing_start"); waitReasons.add("missing_start"); }
  const duration = root.monoStart === undefined ? undefined : Math.min(TTL, Math.max(0, monoNow - root.monoStart));
  const items: TimelineItem[] = [];
  const intervals: WaitInterval[] = [];
  let observedIntervals = 0;
  for (const run of [root, ...(root.children ?? [])]) {
    for (const reason of run.timelineReasons) { reasons.add(reason); waitReasons.add(reason); }
    if (run.reasons.has("capacity")) waitReasons.add("truncated");
    if (run !== root) {
      if (run.depth > 8) reasons.add("depth_limit");
      else for (let i = 0; i < run.spans.length; i++) {
        if (items.length >= 64) { reasons.add("truncated"); break; }
        const span = run.spans[i];
        const start = root.monoStart === undefined || span.started === undefined ? undefined : span.started - root.monoStart;
        const end = root.monoStart === undefined || span.ended === undefined ? undefined : span.ended - root.monoStart;
        const clamped = (start !== undefined && start < 0) || (end !== undefined && (end < 0 || end > duration! || (start !== undefined && end < start)));
        if (start === undefined) reasons.add("missing_start");
        if (end === undefined) reasons.add("missing_end");
        if (clamped) reasons.add("clamped");
        const reliable = start !== undefined && end !== undefined && !clamped;
        items.push({
          ref: span.ref, cycleRef: run.ref, parentRef: run.parent?.ref ?? root.ref, depth: run.depth, attempt: i + 1,
          name: run.session.name, agent: run.session.agent, status: span.status,
          ...(!run.metadataChanged ? { model: run.model, thinking: run.thinking } : { metadataChanged: true }),
          timingQuality: reliable ? "observed" : start === undefined && end === undefined ? "unknown" : "partial",
          ...(start === undefined ? {} : { startOffsetMs: Math.max(0, Math.min(start, duration!)) }),
          ...(end === undefined ? {} : { endOffsetMs: Math.max(0, Math.min(end, duration!)) }),
          ...(reliable ? { durationMs: end - start } : {}),
        });
      }
    }
    observedIntervals = Math.min(4096, observedIntervals + run.interactions.size);
    for (const interaction of run.interactions.values()) {
      if (intervals.length >= 64) { waitReasons.add("truncated"); break; }
      const start = root.monoStart === undefined || interaction.started === undefined ? undefined : interaction.started - root.monoStart;
      const end = root.monoStart === undefined || interaction.ended === undefined ? undefined : interaction.ended - root.monoStart;
      if (start === undefined) waitReasons.add("missing_start");
      if (end === undefined) waitReasons.add("missing_end");
      if ((start !== undefined && start < 0) || (end !== undefined && (end < 0 || end > duration! || (start !== undefined && end < start)))) {
        waitReasons.add("clamped");
        intervals.push({ ref: interaction.ref, agentRef: run.ref, kind: interaction.kind, intervalState: "unknown" });
        continue;
      }
      intervals.push({
        ref: interaction.ref, agentRef: run.ref, kind: interaction.kind,
        intervalState: start !== undefined && end !== undefined ? "complete" : start !== undefined ? "right_censored" : end !== undefined ? "left_censored" : "unknown",
        ...(start === undefined ? {} : { startOffsetMs: start }),
        ...(end === undefined ? {} : { endOffsetMs: end, result: interaction.result ?? "unknown" }),
        ...(start === undefined || end === undefined ? {} : { durationMs: end - start }),
      });
    }
  }
  const subagentTimeline: SubagentTimeline = { version: 1, timeBasis: "omp_agent_observation", partial: false, partialReasons: [], truncated: false, observedItemCount: root.observedSegments, displayedItemCount: 0, items };
  while (items.length > 64 || Buffer.byteLength(JSON.stringify(subagentTimeline)) > 23 * 1024) { items.pop(); reasons.add("truncated"); }
  if (root.observedSegments > items.length && !reasons.has("depth_limit")) reasons.add("truncated");
  const visibleParents = new Set([root.ref, ...items.map(item => item.cycleRef)]);
  if (items.some(item => !visibleParents.has(item.parentRef))) reasons.add("missing_parent");
  subagentTimeline.partialReasons = TIMELINE_REASONS.filter(reason => reasons.has(reason));
  subagentTimeline.partial = reasons.size > 0; subagentTimeline.truncated = reasons.has("truncated"); subagentTimeline.displayedItemCount = items.length;
  const userWaitTimeline: WaitTimeline = { version: 1, timeBasis: "omp_interaction_observation", partial: false, partialReasons: [], truncated: false, observedIntervalCount: observedIntervals, displayedIntervalCount: 0, intervals };
  while (intervals.length > 64 || Buffer.byteLength(JSON.stringify(userWaitTimeline)) > 11 * 1024) { intervals.pop(); waitReasons.add("truncated"); }
  userWaitTimeline.partialReasons = TIMELINE_REASONS.filter(reason => waitReasons.has(reason));
  userWaitTimeline.partial = waitReasons.size > 0; userWaitTimeline.truncated = waitReasons.has("truncated"); userWaitTimeline.displayedIntervalCount = intervals.length;
  return { subagentTimeline, userWaitTimeline };
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
    run.session.name = sessionName(ctx);
  }
  /** 子代理会话通常没有标题；用 OMP 展示的任务名（agent.id，如 ReleaseManagementSmoke）兜底。 */
  function sessionName(ctx: ExtensionContext): string | undefined {
    return text(ctx.sessionManager.getSessionName()) ?? (ctx.agent?.kind === "sub" ? text(ctx.agent.id) : undefined);
  }
  function cycle(run: Run, now: number, monoNow: number, terminal: boolean): Cycle {
    const reasons = new Set(run.reasons);
    if (run.metadataMissing) reasons.add("incomplete_metadata");
    const elapsed = run.monoStart === undefined ? undefined : monoNow - run.monoStart;
    if (elapsed !== undefined && (elapsed < 0 || elapsed > TTL || Math.abs(now - run.start! - elapsed) > 10)) reasons.add("clock_changed");
    return {
      ref: run.ref, timeBasis: "receipt_monotonic", partial: reasons.size > 0, reasons: [...reasons],
      ...(run.start === undefined ? {} : { startedAt: new Date(run.start).toISOString() }),
      ...(terminal ? { endedAt: new Date(now).toISOString() } : {}),
      ...(elapsed === undefined || elapsed < 0 || elapsed > TTL ? {} : { durationMs: elapsed }),
      ...(!run.metadataChanged ? { model: run.model, thinking: run.thinking } : {}),
      ...(run.metadataChanged ? { metadataChanged: true } : {}),
    };
  }
  function notify(run: Run, status: Status, interaction?: Interaction): void {
    if (!config || interaction?.kind === "background_wait") return;
    // 暂缓发送的中止按 agent_end 时刻计时，不把等待窗口算进耗时。
    const at = interaction ? undefined : run.abortAt;
    const now = at?.now ?? Date.now();
    const monoNow = at?.mono ?? Math.floor(performance.now());
    const payload: Envelope = {
      event: interaction ? "omp.action_required" : "omp.run_finished", version: 2, id: ref(), emittedAt: new Date(now).toISOString(),
      session: { ...run.session }, cycle: cycle(run, now, monoNow, !interaction), status,
      ...(!interaction && run.session.scope === "root" ? timelines(run, monoNow) : {}),
      ...(config.instance ? { instance: config.instance } : {}),
      ...(interaction ? { interaction: { ref: interaction.ref, kind: interaction.kind as Kind, ...interaction.detail } } : {}),
    };
    enqueue({ payload, config, live: interaction ? () => interaction.open && !run.closed : () => true, log });
  }
  function finish(binding: Binding, status: Status, reason?: Reason, silent = false): void {
    const run = binding.run;
    if (!run || run.closed) return;
    if (run.abortTimer) {
      // 暂缓中的中止被其他收束抢先：仍按观察到的 aborted 报告，不追加切换／退出原因。
      clearTimeout(run.abortTimer); run.abortTimer = undefined; status = "aborted"; reason = undefined;
    }
    if (reason) run.reasons.add(reason);
    linkRuns();
    if (run.root === run) {
      const agents = new Set(run.children?.map(child => child.agentKey));
      if (snapshotJobs(binding).some(job => !run.initialJobs.has(anonymous(job.id)) && job.agentId && !agents.has(anonymous(job.agentId)))) run.timelineReasons.add("loading_gap");
    }
    if (run.span) run.span.status = status;
    for (const interaction of run.interactions.values()) {
      if (interaction.open) run.reasons.add("unpaired_interaction");
      interaction.open = false;
      clearTimeout(interaction.timer);
    }
    run.closed = true;
    // 常规子代理收束由主通知汇总；异常仍独立提醒，便于在 root 缺失时发现问题。
    if (!silent && (run.session.scope !== "subagent" || status !== "completed")) notify(run, status);
  }
  function get(ctx: ExtensionContext): Binding | undefined {
    const now = Math.floor(performance.now());
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
      binding = { key, owner, touched: now, run: active?.key === key && active.run?.closed ? active.run : undefined };
      bindings.set(key, binding);
    }
    binding.touched = now;
    binding.snapshot = ctx.getAsyncJobSnapshot?.bind(ctx);
    active = binding;
    return binding;
  }
  function begin(binding: Binding, ctx: ExtensionContext, observed: boolean): Run {
    if (binding.run && !binding.run.closed) return binding.run;
    const model = ctx.model ? text(`${ctx.model.provider}/${ctx.model.id}`, 128, true) : undefined;
    const thinking = text(pi.getThinkingLevel?.(), 32);
    const monoNow = Math.floor(performance.now());
    const runRef = ref();
    const span: Span = { ref: runRef, started: observed ? monoNow : undefined, status: "running" };
    const run: Run = {
      ref: runRef, start: observed ? Date.now() : undefined, monoStart: observed ? monoNow : undefined, created: monoNow, closed: false,
      session: { ref: binding.key, scope: ctx.agent?.kind === "main" ? "root" : ctx.agent?.kind === "sub" ? "subagent" : "unknown", name: sessionName(ctx), cwdName: text(basename(ctx.cwd || ctx.sessionManager.getCwd())), agent: text(ctx.agent?.name) },
      reasons: new Set(observed ? [] : ["missing_start"]), interactions: new Map(), retrying: false, retryFailed: false,
      model, thinking, metadataChanged: false, metadataMissing: !model || !thinking,
      agentKey: ctx.agent?.id ? anonymous(ctx.agent.id) : undefined, parentKey: ctx.agent?.parentId ? anonymous(ctx.agent.parentId) : undefined,
      depth: ctx.agent?.depth ?? 0, spans: [span], span, spanCount: 1, initialJobs: new Set(), observedSegments: 0, timelineReasons: new Set(),
    };
    binding.run = run;
    if (run.session.scope === "root") { run.root = run; run.children = []; }
    run.initialJobs = new Set(snapshotJobs(binding, true).map(job => anonymous(job.id)));
    linkRuns();
    return run;
  }
  function request(ctx: ExtensionContext, rawId: string, kind: Kind, detail?: InteractionDetail): void {
    const binding = get(ctx);
    if (!binding || !rawId) return;
    const run = begin(binding, ctx, false);
    const key = anonymous(`${kind}\0${rawId}`);
    if (run.interactions.has(key)) return;
    if (run.interactions.size >= MAX_INTERACTIONS) { run.reasons.add("capacity"); log("interaction_capacity"); return; }
    const interaction: Interaction = { ref: ref(), kind, open: true, started: Math.floor(performance.now()), ...(detail ? { detail } : {}) };
    run.interactions.set(key, interaction);
    // requested 不证明 UI 呈现。无 UI 仅配对记录，不发待处理提醒。
    if (ctx.hasUI) interaction.timer = setTimeout(() => {
      try { if (interaction.open && !run.closed) notify(run, "action_required", interaction); } catch { log("observation_failed"); }
    }, 150);
  }
  function resolve(ctx: ExtensionContext, rawId: string, kind: Kind, result: Resolution): void {
    const binding = get(ctx);
    if (!binding || !rawId || binding.run?.closed) return;
    const run = begin(binding, ctx, false);
    const key = anonymous(`${kind}\0${rawId}`);
    let interaction = run.interactions.get(key);
    if (!interaction) {
      run.reasons.add("unpaired_interaction");
      if (run.interactions.size >= MAX_INTERACTIONS) { run.reasons.add("capacity"); return; }
      interaction = { ref: ref(), kind, open: false };
      run.interactions.set(key, interaction);
    }
    if (interaction.ended !== undefined) return;
    interaction.ended = Math.floor(performance.now());
    interaction.result = result;
    interaction.open = false;
    clearTimeout(interaction.timer);
  }
  observe("agent_start", (_, ctx) => {
    const binding = get(ctx);
    if (binding) {
      // 暂缓窗口内开始新一轮（没有计划评审）：先按中止收束上一轮。
      if (binding.run?.abortTimer) finish(binding, "aborted");
      const run = begin(binding, ctx, true);
      run.planHandoff = undefined;
      const now = Math.floor(performance.now());
      if (run.background?.open) { run.background.open = false; run.background.ended = now; run.background.result = "resolved"; run.background = undefined; }
      if (run.span?.ended !== undefined) {
        run.span = { ref: ref(), started: now, status: "running" };
        run.spanCount = Math.min(4096, run.spanCount + 1);
        if (run.spans.length < 64) run.spans.push(run.span); else run.timelineReasons.add("truncated");
        if (run.root && run.root !== run) run.root.observedSegments = Math.min(4096, run.root.observedSegments + 1);
      }
      run.stop = undefined;
      run.yielded = undefined;
      run.pendingEnd = undefined;
      metadata(run, ctx);
    }
  });
  for (const name of ["turn_start", "before_provider_request"] as const) observe(name, (_, ctx) => {
    const binding = get(ctx);
    if (binding?.run && !binding.run.closed) metadata(binding.run, ctx);
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
    if (run.span && run.span.ended === undefined) run.span.ended = Math.floor(performance.now());
    if (event.awaitingAsyncWork === true && !run.background) {
      if (run.interactions.size < MAX_INTERACTIONS) {
        run.background = { ref: ref(), kind: "background_wait", open: true, started: Math.floor(performance.now()) };
        run.interactions.set(run.background.ref, run.background);
      } else run.reasons.add("capacity");
    }
    if (event.willContinue === true || event.awaitingAsyncWork === true) {
      if (run.span) run.span.status = "continued";
      return;
    }
    // eval agent() 子代理以 yield 提交后结束，最终 stopReason 为 toolUse；workpool worker 提交全部条目后 OMP 中止其后续回合（aborted）。
    // 两者在成功 yield 之后都属于正常完成；yield 失败或未 yield 时保持原映射。
    const last = lastStatus(event.messages);
    const observedStatus = run.stop ?? (run.yielded && (last === "unknown" || last === "aborted") ? "completed" : last) ?? "unknown";
    const status = run.start === undefined && observedStatus === "completed" ? "unknown" : observedStatus;
    if (run.retrying) { run.pendingEnd = status; return; }
    const final = status === "aborted" ? "aborted" : run.retryFailed ? "failed" : status;
    if (final === "aborted" && run.session.scope === "root" && ctx.hasUI) {
      // OMP 计划模式提交计划时先 agent_end(aborted)，随后同步打开评审；暂缓同一提醒窗口，评审打开则不发“已中止”。
      run.abortAt = { now: Date.now(), mono: Math.floor(performance.now()) };
      run.abortTimer = setTimeout(() => {
        try { if (run.abortTimer) finish(binding, "aborted"); } catch { log("observation_failed"); }
      }, 150);
      return;
    }
    finish(binding, final);
  });
  observe("tool_approval_requested", (event, ctx) => {
    if (event.sessionId !== ctx.sessionManager.getSessionId()) return;
    const tool = text(event.toolName, 64);
    request(ctx, event.toolCallId, "approval", tool ? { title: tool } : undefined);
  });
  observe("tool_approval_resolved", (event, ctx) => { if (event.sessionId === ctx.sessionManager.getSessionId()) resolve(ctx, event.toolCallId, "approval", event.approved ? "resolved" : "cancelled"); });
  observe("plan_review_requested", (event, ctx) => {
    if (event.sessionId !== ctx.sessionManager.getSessionId()) return;
    const run = get(ctx)?.run;
    if (run?.abortTimer && !run.closed) { clearTimeout(run.abortTimer); run.abortTimer = undefined; run.abortAt = undefined; }
    const title = text(event.title, 180);
    request(ctx, event.reviewId, "plan_review", title ? { title } : undefined);
  });
  observe("plan_review_resolved", (event, ctx) => {
    if (event.sessionId !== ctx.sessionManager.getSessionId()) return;
    resolve(ctx, event.reviewId, "plan_review", event.choice === undefined ? "cancelled" : "resolved");
    const binding = get(ctx);
    if (!binding?.run || binding.run.closed) return;
    // 取消评审即本轮计划结束，按中止报告；选择后若随即切到新会话执行，交由新会话通知，旧周期静默收束。
    if (event.choice === undefined) finish(binding, "aborted");
    else binding.run.planHandoff = true;
  });
  observe("tool_call", (event, ctx) => { if (event.toolName === "ask") request(ctx, event.toolCallId, "ask", askDetail(event.input)); });
  observe("tool_result", (event, ctx) => {
    if (event.toolName === "ask") resolve(ctx, event.toolCallId, "ask", event.isError ? "unknown" : "resolved");
    else if (event.toolName === "yield" && !event.isError && ctx.agent?.kind === "sub") {
      const binding = get(ctx);
      if (binding?.run && !binding.run.closed) binding.run.yielded = true;
    }
  });
  for (const name of ["session_switch", "session_branch", "session_tree"] as const) observe(name, (_, ctx) => {
    if (active) finish(active, "unknown", "session_changed", active.run?.planHandoff === true);
    active = undefined;
    get(ctx);
  });
  observe("session_shutdown", () => {
    for (const [key, binding] of bindings) if (binding.owner === owner) { finish(binding, "unknown", "shutdown"); bindings.delete(key); }
    active = undefined;
    schedulePump();
  });
};
