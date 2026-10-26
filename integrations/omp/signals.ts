/**
 * 运行健康、资源信号与错误分类的读取与清洗。只读公开事件与只读查询，不返回控制值；
 * 不读取错误原文（errorMessage、finalError、reason、disabledCause），取不到时返回 undefined。
 */
import { text } from "./sanitize.ts";
import type { ErrorCategory, GoalStatus, RunError, Signals, Status } from "./wire.ts";

export type Usage = NonNullable<Signals["usage"]>;
export type Goal = NonNullable<Signals["goal"]> & { updatedAt: number };
export type Todo = NonNullable<Signals["todo"]>;
export type Assistant = { status: Status; errorStatus?: number; stopReason?: string };

const GOAL_STATUSES: readonly GoalStatus[] = ["active", "paused", "budget-limited", "complete", "dropped"];
const MAX_COUNT = 1e12;
const MAX_TODOS = 8;

function count(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= MAX_COUNT ? Math.floor(value) : undefined;
}
function field(value: unknown, key: string): unknown {
  return value && typeof value === "object" ? (value as Record<string, unknown>)[key] : undefined;
}

/** 最终 assistant 的结构化结束信息；errorStatus 是 provider 填写的 HTTP 状态码。 */
export function assistant(message: unknown): Assistant | undefined {
  if (field(message, "role") !== "assistant") return;
  const stopReason = field(message, "stopReason");
  const status: Status = stopReason === "stop" ? "completed" : stopReason === "error" ? "failed" : stopReason === "aborted" ? "aborted" : "unknown";
  const errorStatus = field(message, "errorStatus");
  return {
    status,
    ...(typeof stopReason === "string" ? { stopReason } : {}),
    ...(Number.isInteger(errorStatus) && (errorStatus as number) >= 400 && (errorStatus as number) <= 599 ? { errorStatus: errorStatus as number } : {}),
  };
}
export function lastAssistant(messages: unknown): Assistant | undefined {
  if (!Array.isArray(messages)) return;
  for (let i = messages.length - 1; i >= 0; i--) { const found = assistant(messages[i]); if (found) return found; }
}

/** 只按状态码分类；缺少结构化依据时返回 undefined（卡片不显示错误类型）。 */
export function errorCategory(final: Status, last: Assistant | undefined, retryFailed: boolean): RunError | undefined {
  if (final !== "failed" && final !== "unknown") return;
  const code = last?.errorStatus;
  let category: ErrorCategory | undefined =
    code === 401 || code === 403 ? "auth" : code === 429 ? "rate_limit" : code !== undefined && code >= 500 ? "server" : code !== undefined ? "request" : undefined;
  if (!category && last?.stopReason === "length") category = "truncated";
  if (!category && retryFailed) category = "retry_exhausted";
  if (!category) return;
  return { category, ...(retryFailed && category !== "retry_exhausted" ? { retried: true as const } : {}) };
}

export function usage(value: unknown): Usage | undefined {
  const input = count(field(value, "input")), output = count(field(value, "output"));
  const cacheRead = count(field(value, "cacheRead")), cacheWrite = count(field(value, "cacheWrite")), total = count(field(value, "totalTokens"));
  if (input === undefined || output === undefined || cacheRead === undefined || cacheWrite === undefined || total === undefined) return;
  return { input, output, cacheRead, cacheWrite, total };
}
/** 会话累计值之差；任一项回退（例如统计口径变化）时整体省略，没有请求时也省略。 */
export function usageDelta(start: Usage | undefined, end: Usage | undefined): Usage | undefined {
  if (!start || !end) return;
  const delta = { input: end.input - start.input, output: end.output - start.output, cacheRead: end.cacheRead - start.cacheRead, cacheWrite: end.cacheWrite - start.cacheWrite, total: end.total - start.total };
  if (Object.values(delta).some(item => item < 0) || delta.total === 0) return;
  return delta;
}

/** 会话自身的估算费用（美元）：累计 cost 减去随同步 task 结果计入的 subagentCost，避免与子代理会话重复计算。 */
export function directCost(value: unknown): number | undefined {
  const cost = field(value, "cost"), sub = field(value, "subagentCost");
  if (typeof cost !== "number" || typeof sub !== "number" || !Number.isFinite(cost) || !Number.isFinite(sub) || sub < 0 || cost < sub) return;
  return cost - sub;
}
/** 两次直接费用快照之差；缺失或回退时返回 undefined。 */
export function costDelta(start: number | undefined, end: number | undefined): number | undefined {
  if (start === undefined || end === undefined || end < start) return;
  return end - start;
}

/** 本会话本次运行各助手消息的计时：OMP 在消息上记录 ttft 与 duration（毫秒，含首字前等待）。 */
export type Speed = { ttfts: number[]; output: number; generationMs: number };
const MAX_SPEED_SAMPLES = 1024;
const MIN_GENERATION_MS = 1000;
/** 与服务端校验上限一致：超出即省略，避免整条通知被拒收。 */
const MAX_TTFT_MS = 3_600_000, MAX_TPS = 1_000_000;
export const MAX_COST_USD = 100_000;
export function recordSpeed(speed: Speed, message: unknown): void {
  if (field(message, "role") !== "assistant") return;
  const ttft = field(message, "ttft"), duration = field(message, "duration"), output = count(field(field(message, "usage"), "output"));
  if (typeof ttft !== "number" || !Number.isFinite(ttft) || ttft <= 0) return;
  if (speed.ttfts.length < MAX_SPEED_SAMPLES) speed.ttfts.push(ttft);
  if (typeof duration === "number" && Number.isFinite(duration) && duration > ttft && output) {
    speed.output += output; speed.generationMs += duration - ttft;
  }
}
/** 首字延迟取中位数；输出速度为首字后生成速度，生成时长合计不足 1 秒时省略（短回复不足以估算）。 */
export function speedFields(speed: Speed): { ttftMs?: number; outputTps?: number } {
  const sorted = [...speed.ttfts].sort((a, b) => a - b), mid = sorted.length >> 1;
  const median = sorted.length ? (sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2) : undefined;
  const ttftMs = median === undefined || median > MAX_TTFT_MS ? undefined : Math.max(1, Math.round(median));
  const tps = speed.generationMs >= MIN_GENERATION_MS && speed.output > 0 ? Math.round(speed.output / speed.generationMs * 10000) / 10 : undefined;
  return { ...(ttftMs !== undefined ? { ttftMs } : {}), ...(tps && tps <= MAX_TPS ? { outputTps: tps } : {}) };
}

export function context(value: unknown): Signals["context"] {
  const tokens = count(field(value, "tokens")), window = count(field(value, "contextWindow"));
  if (tokens === undefined || !window) return;
  return { tokens: Math.min(tokens, window), window };
}

/** Goal 目标是用户为自己的任务编写的内容：清洗控制字符，URL 与绝对／用户目录路径替换为 …，截断 200 字。 */
export function goal(value: unknown): Goal | undefined {
  const status = field(value, "status");
  const objective = text(field(value, "objective"), 200);
  const tokensUsed = count(field(value, "tokensUsed")), timeUsedSeconds = count(field(value, "timeUsedSeconds"));
  const tokenBudget = count(field(value, "tokenBudget"));
  // updatedAt 是毫秒时间戳，超出计数上限，单独校验。
  const updated = field(value, "updatedAt");
  const updatedAt = typeof updated === "number" && Number.isFinite(updated) && updated > 0 ? updated : undefined;
  if (!objective || !GOAL_STATUSES.includes(status as GoalStatus) || tokensUsed === undefined || timeUsedSeconds === undefined) return;
  return { objective, status: status as GoalStatus, tokensUsed, timeUsedSeconds, ...(tokenBudget ? { tokenBudget } : {}), updatedAt: updatedAt ?? 0 };
}

/** todo 提醒：只取未完成条目的文字（清洗、每条 120 字、至多 8 条），其余字段（详情、备注、阻塞原因）不读取。 */
export function todo(event: unknown): Todo | undefined {
  const reminders = count(field(event, "attempt")), maxReminders = count(field(event, "maxAttempts"));
  const todos = field(event, "todos");
  if (!reminders || !maxReminders || !Array.isArray(todos)) return;
  const unfinished = todos.filter(item => !["completed", "abandoned"].includes(String(field(item, "status"))));
  const pending = unfinished.slice(0, MAX_TODOS).map(item => text(field(item, "content"), 120)).filter((item): item is string => Boolean(item));
  return { reminders: Math.min(reminders, 99), maxReminders: Math.min(maxReminders, 99), pendingCount: Math.min(unfinished.length, 999), pending };
}
