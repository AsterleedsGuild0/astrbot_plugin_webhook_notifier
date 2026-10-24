/** V2 线协议类型：出站字段的唯一定义处。 */
export type Status = "completed" | "failed" | "aborted" | "action_required" | "unknown";
export type Kind = "approval" | "plan_review" | "ask";
export type Reason = "missing_start" | "session_changed" | "shutdown" | "expired" | "capacity" | "unpaired_interaction" | "incomplete_metadata" | "clock_changed";
/** rootName：子代理所属主会话名称（仅能关联时）；createdAt：会话创建时间，恢复旧会话时保留原值。 */
export type Session = { ref: string; scope: "root" | "subagent" | "unknown"; name?: string; cwdName?: string; agent?: string; rootName?: string; createdAt?: string };
export type Cycle = { ref: string; partial: boolean; reasons: Reason[]; timeBasis: "receipt_monotonic"; startedAt?: string; endedAt?: string; durationMs?: number; model?: string; thinking?: string; metadataChanged?: boolean };
export type AskOption = { label: string; description?: string; recommended?: true };
export type AskQuestion = { text: string; header?: string; options?: AskOption[] };
export type InteractionDetail = { title?: string; questionCount?: number; questions?: AskQuestion[] };
/** 错误分类只来自结构化字段：最终 assistant 的 errorStatus／stopReason 与 auto_retry_end.success。 */
export type ErrorCategory = "auth" | "rate_limit" | "server" | "request" | "truncated" | "retry_exhausted";
export type RunError = { category: ErrorCategory; retried?: true };
export type GoalStatus = "active" | "paused" | "budget-limited" | "complete" | "dropped";
/** 运行健康与资源信号；全部可选，取不到时省略。 */
export type Signals = {
  /** 收束时当前上下文窗口的占用，不是累计用量。 */
  context?: { tokens: number; window: number };
  compaction?: { count: number; failed?: number };
  fallback?: { from: string; to: string; succeeded: boolean };
  /** 本次运行用量：agent_start 与收束时两次会话累计快照之差。 */
  usage?: { input: number; output: number; cacheRead: number; cacheWrite: number; total: number };
  goal?: { objective: string; status: GoalStatus; tokensUsed: number; tokenBudget?: number; timeUsedSeconds: number };
  /** 最后一次 todo 提醒：第几次／上限与当时未完成的条目。 */
  todo?: { reminders: number; maxReminders: number; pendingCount: number; pending: string[] };
};
export type Envelope = { event: "omp.run_finished" | "omp.action_required"; version: 2; id: string; emittedAt: string; session: Session; cycle: Cycle; status: Status; instance?: string; interaction?: { ref: string; kind: Kind } & InteractionDetail; subagentTimeline?: SubagentTimeline; userWaitTimeline?: WaitTimeline; error?: RunError; signals?: Signals };
/** 凭据被自动停用的独立告警：只含 provider，不含原因、账号或凭据身份。 */
export type CredentialAlert = { event: "omp.credential_disabled"; version: 2; id: string; emittedAt: string; provider: string; instance?: string };
export type Outbound = Envelope | CredentialAlert;
export type IntervalKind = Kind | "background_wait";
export type Resolution = "resolved" | "cancelled" | "unknown";
export const TIMELINE_REASONS = ["missing_parent", "ambiguous_parent", "missing_start", "missing_end", "loading_gap", "invalid_parent_graph", "depth_limit", "truncated", "clamped", "clock_changed"] as const;
export type TimelineReason = typeof TIMELINE_REASONS[number];
export type TimelineItem = { ref: string; cycleRef: string; parentRef: string; depth: number; attempt: number; name?: string; agent?: string; model?: string; thinking?: string; metadataChanged?: boolean; status: Status | "running" | "continued"; timingQuality: "observed" | "partial" | "unknown"; startOffsetMs?: number; endOffsetMs?: number; durationMs?: number };
export type WaitInterval = { ref: string; agentRef: string; kind: IntervalKind; result?: Resolution; intervalState: "complete" | "right_censored" | "left_censored" | "unknown"; startOffsetMs?: number; endOffsetMs?: number; durationMs?: number };
export type TimelineBase = { version: 1; partial: boolean; partialReasons: TimelineReason[]; truncated: boolean };
export type SubagentTimeline = TimelineBase & { timeBasis: "omp_agent_observation"; observedItemCount: number; displayedItemCount: number; items: TimelineItem[] };
export type WaitTimeline = TimelineBase & { timeBasis: "omp_interaction_observation"; observedIntervalCount: number; displayedIntervalCount: number; intervals: WaitInterval[] };
