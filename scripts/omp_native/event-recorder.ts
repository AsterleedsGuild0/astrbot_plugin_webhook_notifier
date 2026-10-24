/** 验收专用观察器：只记录公开事件的类型与结构字段，不记录正文、参数、输出；从不返回控制值。 */
import { appendFileSync } from "node:fs";

const out = process.env.OMP_ACCEPT_EVENT_LOG;

function stop(message: unknown): string | undefined {
  const value = message as { role?: string; stopReason?: string } | undefined;
  return value?.role === "assistant" ? value.stopReason : undefined;
}

export default (pi: any): void => {
  if (!out) return;
  const write = (type: string, ctx: any, fields: Record<string, unknown> = {}) => {
    try {
      const agent = ctx?.agent;
      appendFileSync(out, JSON.stringify({
        t: new Date().toISOString(), mono: Math.round(performance.now()), type,
        session: ctx?.sessionManager?.getSessionId?.(), sessionName: ctx?.sessionManager?.getSessionName?.(),
        agent: agent ? { kind: agent.kind, id: agent.id, parentId: agent.parentId, depth: agent.depth, name: agent.name } : undefined,
        hasUI: ctx?.hasUI, model: ctx?.model ? `${ctx.model.provider}/${ctx.model.id}` : undefined,
        thinking: pi.getThinkingLevel?.(),
        jobs: (() => { try { const s = ctx?.getAsyncJobSnapshot?.(); return s ? { running: s.running?.length, recent: s.recent?.length } : null; } catch { return "error"; } })(),
        ...fields,
      }) + "\n");
    } catch { /* 观察失败不影响 OMP。 */ }
  };
  const on = (name: string, pick: (event: any) => Record<string, unknown> = () => ({})) =>
    pi.on(name, (event: any, ctx: any) => { write(name, ctx, pick(event)); });

  on("agent_start");
  on("agent_end", e => ({
    willContinue: e.willContinue, awaitingAsyncWork: e.awaitingAsyncWork, messageCount: e.messages?.length,
    lastStop: [...(e.messages ?? [])].reverse().map(stop).find(Boolean),
  }));
  on("turn_start");
  on("session_stop", e => ({ lastStop: stop(e.last_assistant_message) }));
  on("auto_retry_start", e => ({ attempt: e.attempt, maxAttempts: e.maxAttempts, delayMs: e.delayMs }));
  on("auto_retry_end", e => ({ success: e.success, attempt: e.attempt }));
  on("tool_call", e => ({ toolName: e.toolName, toolCallId: e.toolCallId }));
  on("tool_result", e => ({ toolName: e.toolName, toolCallId: e.toolCallId, isError: e.isError }));
  on("tool_approval_requested", e => ({ toolName: e.toolName, toolCallId: e.toolCallId, eventSession: e.sessionId }));
  on("tool_approval_resolved", e => ({ toolName: e.toolName, toolCallId: e.toolCallId, approved: e.approved, eventSession: e.sessionId }));
  on("plan_review_requested", e => ({ reviewId: e.reviewId, choices: e.choices?.map((c: any) => c.id), eventSession: e.sessionId }));
  on("plan_review_resolved", e => ({ reviewId: e.reviewId, choice: e.choice, by: e.by, eventSession: e.sessionId }));
  on("before_subagent_spawn", e => ({ agentName: e.agent?.name ?? e.agent, invocationKind: e.invocationKind, spawnKey: e.spawnKey }));
  for (const name of ["session_start", "session_switch", "session_branch", "session_tree", "session_shutdown"]) on(name, e => ({ reason: e.reason }));
};
