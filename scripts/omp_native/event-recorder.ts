/**
 * 验收专用观察器：只记录公开事件的类型与结构字段（状态码、枚举、计数、是否存在），
 * 不记录正文、参数、输出、错误原文或凭据身份；从不返回控制值。
 */
import { appendFileSync } from "node:fs";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent/extensibility/extensions";

type Bag = Record<string, unknown>;
const out = process.env.OMP_ACCEPT_EVENT_LOG;

/** 事件载荷按结构读取；非对象视为空。 */
function bag(value: unknown): Bag {
  return value && typeof value === "object" ? value as Bag : {};
}
function stop(message: unknown): unknown {
  return bag(message).role === "assistant" ? bag(message).stopReason : undefined;
}
function present(value: unknown): "present" | undefined {
  return value === undefined ? undefined : "present";
}
/** 只保留字段名与值类型；字符串值以长度代替。 */
function shape(value: unknown): unknown {
  if (typeof value === "string") return `string(${value.length})`;
  if (value === null || typeof value !== "object") return value;
  if (Array.isArray(value)) return { length: value.length, first: value.length ? shape(value[0]) : undefined };
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, shape(item)]));
}
function attempt<T>(query: () => T): T | "error" {
  try { return query(); } catch { return "error"; }
}

export default (pi: ExtensionAPI): void => {
  if (!out) return;
  const probes = (ctx: ExtensionContext) => ({
    contextUsage: attempt(() => ctx.getContextUsage()),
    usage: attempt(() => ctx.sessionManager.getUsageStatistics()),
    header: attempt(() => { const h = ctx.sessionManager.getHeader(); return h ? { timestamp: h.timestamp, parentSession: Boolean(h.parentSession), version: h.version } : null; }),
  });
  const write = (type: string, ctx: ExtensionContext, fields: Bag = {}) => {
    try {
      const agent = ctx.agent;
      appendFileSync(out, JSON.stringify({
        t: new Date().toISOString(), mono: Math.round(performance.now()), type,
        session: attempt(() => ctx.sessionManager.getSessionId()), sessionName: attempt(() => ctx.sessionManager.getSessionName()),
        agent: agent ? { kind: agent.kind, id: agent.id, parentId: agent.parentId, depth: agent.depth, name: agent.name } : undefined,
        hasUI: ctx.hasUI, model: ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : undefined,
        thinking: pi.getThinkingLevel?.(),
        jobs: attempt(() => { const s = ctx.getAsyncJobSnapshot?.(); return s ? { running: s.running.length, recent: s.recent.length } : null; }),
        ...fields,
      }) + "\n");
    } catch { /* 观察失败不影响 OMP。 */ }
  };
  const on = (name: string, pick: (event: Bag, ctx: ExtensionContext) => Bag = () => ({})) =>
    pi.on(name as never, ((event: unknown, ctx: ExtensionContext) => { write(name, ctx, pick(bag(event), ctx)); }) as never);

  on("agent_start", (_, ctx) => probes(ctx));
  on("agent_end", (e, ctx) => {
    const messages = Array.isArray(e.messages) ? e.messages : [];
    return { willContinue: e.willContinue, awaitingAsyncWork: e.awaitingAsyncWork, messageCount: messages.length, lastStop: [...messages].reverse().map(stop).find(Boolean), ...probes(ctx) };
  });
  on("turn_start");
  on("session_stop", e => ({ lastStop: stop(e.last_assistant_message), errorStatus: bag(e.last_assistant_message).errorStatus }));
  on("after_provider_response", e => ({ status: e.status, requestId: Boolean(e.requestId), headerCount: Object.keys(bag(e.headers)).length, metadata: shape(e.metadata) }));
  on("auto_retry_start", e => ({ attempt: e.attempt, maxAttempts: e.maxAttempts, delayMs: e.delayMs }));
  on("auto_retry_end", e => ({ success: e.success, attempt: e.attempt, finalError: present(e.finalError), retryErrors: shape(e.retryErrors) }));
  on("auto_compaction_start", e => ({ reason: e.reason, action: e.action }));
  on("auto_compaction_end", e => ({ action: e.action, aborted: e.aborted, willRetry: e.willRetry, skipped: e.skipped, result: present(e.result), errorMessage: present(e.errorMessage) }));
  on("retry_fallback_applied", e => ({ from: e.from, to: e.to, role: e.role, reason: present(e.reason) }));
  on("retry_fallback_succeeded", e => ({ model: e.model, role: e.role }));
  on("todo_reminder", e => ({ attempt: e.attempt, maxAttempts: e.maxAttempts, todos: Array.isArray(e.todos) ? e.todos.map(t => bag(t).status) : undefined, todoShape: Array.isArray(e.todos) ? shape(e.todos[0]) : undefined }));
  on("goal_updated", e => {
    const goal = e.goal ? bag(e.goal) : undefined, state = e.state ? bag(e.state) : undefined;
    return {
      goal: goal ? { status: goal.status, tokensUsed: goal.tokensUsed, tokenBudget: goal.tokenBudget, timeUsedSeconds: goal.timeUsedSeconds, objective: shape(goal.objective) } : null,
      state: state ? { enabled: state.enabled, mode: state.mode, reason: state.reason } : undefined,
    };
  });
  on("credential_disabled", e => ({ provider: e.provider, fields: Object.keys(e) }));
  on("tool_call", e => ({ toolName: e.toolName, toolCallId: e.toolCallId }));
  on("tool_result", e => ({ toolName: e.toolName, toolCallId: e.toolCallId, isError: e.isError }));
  on("tool_approval_requested", e => ({ toolName: e.toolName, toolCallId: e.toolCallId, eventSession: e.sessionId }));
  on("tool_approval_resolved", e => ({ toolName: e.toolName, toolCallId: e.toolCallId, approved: e.approved, eventSession: e.sessionId }));
  on("plan_review_requested", e => ({ reviewId: e.reviewId, eventSession: e.sessionId }));
  on("plan_review_resolved", e => ({ reviewId: e.reviewId, choice: e.choice, by: e.by, eventSession: e.sessionId }));
  on("before_subagent_spawn", e => ({ agentName: bag(e.agent).name ?? e.agent, invocationKind: e.invocationKind, spawnKey: e.spawnKey }));
  for (const name of ["session_start", "session_switch", "session_branch", "session_tree", "session_shutdown"]) on(name, (e, ctx) => ({ reason: e.reason, ...(name === "session_start" || name === "session_switch" ? probes(ctx) : {}) }));
};
