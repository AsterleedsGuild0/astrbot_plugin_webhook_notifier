/**
 * OMP v18.12.6 公开事件观察器的 extension 入口。只发 V2；不执行工具、不读取会话正文或凭据文件。
 * 本文件负责事件订阅与运行收束判定；配置／传输、状态、时间线和清洗分别在同目录模块中。
 */
import { basename } from "node:path";
import type { ExtensionAPI, ExtensionContext, ExtensionEvent } from "@oh-my-pi/pi-coding-agent/extensibility/extensions";
import { anonymous, askDetail, ref, text } from "./sanitize.ts";
import { MAX_INTERACTIONS, MAX_SESSIONS, TTL, assistantStatus, bindings, lastStatus, linkRuns, snapshotJobs, type Binding, type Interaction, type Run, type Span } from "./state.ts";
import { timelines } from "./timeline.ts";
import { configuration, enqueue, schedulePump, type Log } from "./transport.ts";
import type { Cycle, Envelope, InteractionDetail, Kind, Reason, Resolution, Status } from "./wire.ts";

export type { Envelope } from "./wire.ts";

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
