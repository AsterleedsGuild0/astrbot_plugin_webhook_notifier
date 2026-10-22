/**
 * Webhook Notifier 的 OMP 客户端。
 * 基于既有本地 onebot.ts 建立维护基线，保留 session_stop V1 和环境变量兼容行为。
 * 此入口发送插件 Webhook，不直接调用 OneBot；后续开发以本文件为源。
 */
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent/extensibility/extensions";

interface RoundStart {
  prompt: string;
  imageCount: number;
  startedAt: number;
  entryCount: number;
  messageCount: number;
}

const WEBHOOK_URL_ENV_KEYS = [
  "OMP_SESSION_WEBHOOK_URL",
  "ONEBOT_WEBHOOK_URL",
] as const;
const WEBHOOK_TOKEN_ENV_KEYS = [
  "OMP_SESSION_WEBHOOK_TOKEN",
  "ONEBOT_WEBHOOK_TOKEN",
] as const;
const WEBHOOK_TIMEOUT_ENV_KEYS = [
  "OMP_SESSION_WEBHOOK_TIMEOUT_MS",
  "ONEBOT_WEBHOOK_TIMEOUT_MS",
] as const;

const DEFAULT_TIMEOUT_MS = 5_000;
const MAX_PROMPT_LENGTH = 2_000;

function configuredWebhookUrl(): string | undefined {
  for (const key of WEBHOOK_URL_ENV_KEYS) {
    const value = process.env[key]?.trim();
    if (value) {
      return value;
    }
  }
}

function configuredWebhookToken(): string | undefined {
  for (const key of WEBHOOK_TOKEN_ENV_KEYS) {
    const value = process.env[key]?.trim();
    if (value) {
      return `Bearer ${value}`;
    }
  }
}

function configuredTimeoutMs(): number {
  for (const key of WEBHOOK_TIMEOUT_ENV_KEYS) {
    const raw = process.env[key]?.trim();
    if (!raw) {
      continue;
    }
    const parsed = Number(raw);
    if (Number.isFinite(parsed) && parsed > 0) {
      return parsed;
    }
  }
  return DEFAULT_TIMEOUT_MS;
}

function truncate(value: string, maxLength: number): string {
  if (value.length <= maxLength) {
    return value;
  }
  return `${value.slice(0, maxLength)}…`;
}

function postWebhook(pi: ExtensionAPI, payload: unknown): void {
  const url = configuredWebhookUrl();
  if (!url) {
    return;
  }

  const timeoutMs = configuredTimeoutMs();
  const bearerToken = configuredWebhookToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "User-Agent": "omp-session-webhook",
    "X-OMP-Event": "session_stop",
  };
  if (bearerToken) {
    headers["Authorization"] = bearerToken;
  }

  void fetch(url, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(timeoutMs),
  }).catch((error) => {
    pi.logger.warn("Failed to send OMP session webhook", {
      error: error instanceof Error ? error.message : String(error),
      url,
    });
  });
}

export default (pi: ExtensionAPI) => {
  let currentRound: RoundStart | undefined;

  pi.on("before_agent_start", (event, ctx) => {
    currentRound = {
      prompt: event.prompt,
      imageCount: event.images?.length ?? 0,
      startedAt: Date.now(),
      entryCount: ctx.sessionManager.getEntries().length,
      messageCount: ctx.sessionManager
        .getBranch()
        .filter((entry) => entry.type === "message").length,
    };
  });

  pi.on("session_stop", (event, ctx) => {
    const endedAt = Date.now();
    const entriesAfter = ctx.sessionManager.getEntries();
    const messageCountAfter = event.messages.length;
    const lastAssistant =
      event.last_assistant_message?.role === "assistant"
        ? event.last_assistant_message
        : undefined;
    const model = ctx.model
      ? {
          provider: ctx.model.provider,
          id: ctx.model.id,
          name: ctx.model.name,
        }
      : undefined;

    const payload = {
      event: "omp.session_stop",
      version: 1,
      emittedAt: new Date(endedAt).toISOString(),
      session: {
        id: event.session_id || ctx.sessionManager.getSessionId(),
        file: event.session_file ?? ctx.sessionManager.getSessionFile(),
        cwd: ctx.sessionManager.getCwd(),
        name: ctx.sessionManager.getSessionName(),
        model,
      },
      round: {
        turnId: event.turn_id,
        startedAt: currentRound
          ? new Date(currentRound.startedAt).toISOString()
          : undefined,
        endedAt: new Date(endedAt).toISOString(),
        durationMs: currentRound ? endedAt - currentRound.startedAt : undefined,
        prompt: currentRound
          ? truncate(currentRound.prompt, MAX_PROMPT_LENGTH)
          : undefined,
        promptLength: currentRound?.prompt.length,
        imageCount: currentRound?.imageCount ?? 0,
        entryCountBefore: currentRound?.entryCount,
        entryCountAfter: entriesAfter.length,
        entryCountDelta: currentRound
          ? entriesAfter.length - currentRound.entryCount
          : undefined,
        messageCountBefore: currentRound?.messageCount,
        messageCountAfter,
        messageCountDelta: currentRound
          ? messageCountAfter - currentRound.messageCount
          : undefined,
        stopHookActive: event.stop_hook_active,
        lastAssistant: lastAssistant
          ? {
              provider: lastAssistant.provider,
              model: lastAssistant.model,
              stopReason: lastAssistant.stopReason,
              timestamp: new Date(lastAssistant.timestamp).toISOString(),
              durationMs: lastAssistant.duration,
            }
          : undefined,
      },
    };

    currentRound = undefined;
    setTimeout(() => postWebhook(pi, payload), 0);
  });
};
