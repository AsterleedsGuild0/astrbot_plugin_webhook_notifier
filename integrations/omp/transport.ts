/** 配置读取与发送：环境变量配置、有界队列、并发、重试和 HTTP／业务结果分类。 */
import type { Envelope } from "./wire.ts";
import { text } from "./sanitize.ts";

export type Config = { url: string; token: string; timeout: number; instance?: string };
export type Job = { payload: Envelope; config: Config; live: () => boolean; log: Log };
export type Log = (category: string, id?: string, status?: number, attempt?: number) => void;
export type Result = "delivered" | "policy_skipped" | "partial_delivery" | "partial_failure" | "replayed" | "permanent_error" | "transient_error" | "transport_unknown" | "unknown";

const MAX_QUEUE = 64;
const MAX_RESPONSE_BYTES = 8192;
const queue: Job[] = [];
let sending = 0;
let pumpScheduled = false;

export function configured(keys: string[]): string | undefined {
  for (const key of keys) { const value = process.env[key]?.trim(); if (value) return value; }
}
export function configuration(log: Log): Config | undefined {
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
export async function responseResult(response: Response): Promise<Result> {
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

export function schedulePump(): void {
  if (pumpScheduled) return;
  pumpScheduled = true;
  setTimeout(() => { pumpScheduled = false; pump(); }, 0);
}
export function enqueue(job: Job): void {
  if (!job.live()) return;
  if (queue.length + sending >= MAX_QUEUE) { job.log("queue_capacity", job.payload.id); return; }
  queue.push(job);
  schedulePump();
}
export function pump(): void {
  while (sending < 2 && queue.length) {
    const job = queue.shift()!;
    if (!job.live()) continue;
    sending++;
    void send(job).catch(() => job.log("transport_unknown", job.payload.id)).finally(() => { sending--; schedulePump(); });
  }
}
export async function send(job: Job): Promise<void> {
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
