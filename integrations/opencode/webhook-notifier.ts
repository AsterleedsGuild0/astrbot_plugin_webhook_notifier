/**
 * OpenCode V1 Webhook Notifier Plugin
 *
 * Single-file TypeScript Plugin for OpenCode Desktop/CLI (SDK 1.17.9).
 * Listens on `event` hook, filters to three MVP event types, constructs
 * a safe minimal envelope, and POSTs to a configurable webhook URL.
 *
 * Privacy constraints:
 *  - No raw session ID, cwd, prompt, message, tool, diff, token, or
 *    full directory path leaves this plugin.
 *  - session.ref is a non-reversible SHA-256 digest (first 32 hex chars).
 *  - session.name is sanitised (dangerous Unicode removed, control chars
 *    normalised, length capped at 200). HTML/MD escaping is the server's
 *    responsibility.
 *  - Diagnostic output contains only event type / attempt count /
 *    status category — never URL, token, body, raw session ID, or
 *    session name (before or after cleaning).
 *
 * @packageDocumentation
 */

// ─── Types ──────────────────────────────────────────────────

/** User-facing plugin options from opencode.jsonc. */
interface RawPluginOptions {
  url?: string;
  token?: string;
  timeoutMs?: number;
  enabled?: boolean;
  events?: string[];
  projectDisplayName?: string;
  [key: string]: unknown;
}

/** Resolved, validated configuration (no {env} / {file} placeholders). */
interface ResolvedConfig {
  url: string;
  token: string;
  timeoutMs: number;
  enabled: boolean;
  events: Set<string>;
  projectDisplayName: string | undefined;
}

/** Logical event we send to the webhook server. */
interface Envelope {
  id: string;
  event: "opencode.session_idle" | "opencode.session_error" | "opencode.permission_asked";
  version: 1;
  emittedAt: string;
  session: {
    ref: string;
    name?: string;
  };
  agent?: string;
  model?: string;
  durationMs?: number;
  permission?: { category: string };
  error?: { category: string; code?: string };
}

/** OpenCode V1 event (a subset of the full payload that we touch). */
interface OpenCodeEvent {
  type: string;
  sessionId?: string;
  status?: string;
  session?: { name?: string; title?: string };
  agent?: string;
  model?: string;
  durationMs?: number;
  error?: {
    name?: string;
    message?: string;
    status?: number;
    [key: string]: unknown;
  };
  permission?: {
    type?: string;
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

/** Per-session state machine record. */
interface SessionState {
  /** Whether we have seen a "busy" for the current cycle. */
  hadBusy: boolean;
  /** Whether we have already sent an idle notification for this cycle. */
  sentIdle: boolean;
  /** Whether an error occurred in the current cycle (suppresses idle). */
  hadErrorForCycle: boolean;
  /** Opaque cycle key — incremented on each new busy. */
  cycle: number;
  /** Event ID for a pending retry (stable across retries). */
  pendingEventId: string | undefined;
}

/** Category-name pair for error/permission events. */
interface CategoryInfo {
  category: string;
  code?: string;
}

/** Minimal diagnostic logger — no dependency on OpenCode's logger API. */
interface DiagnosticLog {
  warn: (...args: unknown[]) => void;
  error: (...args: unknown[]) => void;
}

// ─── Constants ──────────────────────────────────────────────

const MAX_NAME_LENGTH = 200;
const MAX_SESSION_REF_LENGTH = 128;
const MAX_RETRIES = 2; // 3 total attempts (1 initial + 2 retries)
const BASE_BACKOFF_MS = 400;
const MAX_BACKOFF_MS = 5000;
const REQUEST_TIMEOUT_MS = 10_000;

const OUTPUT_EVENTS = new Set([
  "opencode.session_idle",
  "opencode.session_error",
  "opencode.permission_asked",
] as const);

type OutputEvent = "opencode.session_idle" | "opencode.session_error" | "opencode.permission_asked";

/**
 * Set of session refs with an idle notification currently in-flight.
 * Provides an atomic guard before the first await to prevent concurrent
 * idle events for the same session from constructing/sending more than
 * one envelope.
 */
const _idleProcessing = new Set<string>();

// ─── Session Ref Hashing ────────────────────────────────────

const _textEncoder = new TextEncoder();

/**
 * Compute session.ref = SHA-256("opencode:" + rawSessionID).
 * Returns first 32 lowercase hex characters.
 *
 * The prefix binds the hash to this specific plugin context, preventing
 * trivial rainbow-table matching of short session IDs.
 */
async function _hashSessionRef(rawSessionId: string): Promise<string> {
  const data = _textEncoder.encode("opencode:" + rawSessionId);
  const hashBuffer = await crypto.subtle.digest("SHA-256", data);
  const hashArray = new Uint8Array(hashBuffer);
  let hex = "";
  for (let i = 0; i < 16; i++) {
    // Only first 16 bytes → 32 hex chars
    hex += hashArray[i]!.toString(16).padStart(2, "0");
  }
  return hex;
}

// ─── ID Generation ──────────────────────────────────────────

/** Generate a random UUID v4 as the logical event ID. */
function _generateId(): string {
  return crypto.randomUUID();
}

// ─── Timestamp ──────────────────────────────────────────────

/** ISO-8601 with timezone (trailing Z). */
function _nowISO(): string {
  return new Date().toISOString();
}

// ─── Name Sanitisation ──────────────────────────────────────

// Unicode bidi / zero-width / format / separator control chars
const DANGEROUS_UNICODE_RE = /[\u200b-\u200f\u202a-\u202e\u2028\u2029\u2066-\u2069\ufeff]/g;
// Control characters (excluding TAB/CR/LF which are handled separately)
const CONTROL_RE = /[\x00-\x08\x0b\x0c\x0e-\x1f]/g;
// Whitespace normalisation
const MULTI_SPACE_RE = / {2,}/g;

/**
 * Sanitise a session name for the envelope.
 *
 * - Removes Unicode bidi control chars, zero-width chars, format
 *   chars, and line/paragraph separators.
 * - Replaces other control characters with a space.
 * - Normalises consecutive spaces to a single space.
 * - Truncates to MAX_NAME_LENGTH.
 *
 * NOTE: HTML/MD character escaping is the server renderer's
 * responsibility (see Server `_clean_session_name`).  We do NOT
 * double-escape here.
 */
function _sanitiseName(raw: string | null | undefined): string | undefined {
  if (!raw) return undefined;

  let s = String(raw);
  s = s.replace(DANGEROUS_UNICODE_RE, "");
  s = s.replace(CONTROL_RE, " ");
  s = s.replace(/\r\n/g, " ");
  s = s.replace(/[\r\n\t]/g, " ");
  s = s.replace(MULTI_SPACE_RE, " ").trim();

  if (!s) return undefined;
  if (s.length > MAX_NAME_LENGTH) {
    s = s.slice(0, MAX_NAME_LENGTH).trimEnd();
  }
  return s || undefined;
}

// ─── Error / Permission Category Derivation ─────────────────

/**
 * Derive a safe error category and optional code from the raw
 * error object.  Never reads `error.message` or `error.responseBody`.
 */
function _deriveErrorCategory(err: NonNullable<OpenCodeEvent["error"]>): CategoryInfo {
  const raw = err.name ?? "unknown";
  const category = raw.replace(/[^a-zA-Z0-9_-]/g, "_").toLowerCase().slice(0, 64) || "unknown";
  let code: string | undefined;
  if (typeof err.status === "number" && Number.isFinite(err.status)) {
    code = String(err.status).slice(0, 64);
  }
  return { category, code };
}

/**
 * Derive a safe permission category from the permission object.
 * Never reads permission title, description, or target path.
 */
function _derivePermissionCategory(perm: NonNullable<OpenCodeEvent["permission"]>): string {
  const raw = perm.type ?? "unknown";
  return raw.replace(/[^a-zA-Z0-9_-]/g, "_").toLowerCase().slice(0, 64) || "unknown";
}

// ─── State Machine ──────────────────────────────────────────

const _sessions = new Map<string, SessionState>();

/** Get or initialise state for a session key (already the hashed ref). */
function _getState(sessionKey: string): SessionState {
  let st = _sessions.get(sessionKey);
  if (!st) {
    st = { hadBusy: false, sentIdle: false, hadErrorForCycle: false, cycle: 0, pendingEventId: undefined };
    _sessions.set(sessionKey, st);
  }
  return st;
}

/** Bounded cleanup: remove sessions that exceed the limit. */
function _cleanupSessions(): void {
  if (_sessions.size > 1000) {
    const entries = [..._sessions.entries()];
    // Keep the 500 most recently accessed (stay within Map insertion order heuristic)
    for (let i = 0; i < entries.length - 500; i++) {
      _sessions.delete(entries[i]![0]);
    }
  }
}

// ─── Config Resolution ──────────────────────────────────────

/**
 * Try to resolve an {env:...} or {file:...} interpolation.
 * Returns the resolved value, or null if the pattern is unrecognised
 * or resolution fails.
 */
function _resolveInterpolation(value: string): string | null {
  const envMatch = value.match(/^\{env:(.+)\}$/);
  if (envMatch) {
    return process.env[envMatch[1]!] ?? null;
  }
  const fileMatch = value.match(/^\{file:(.+)\}$/);
  if (fileMatch) {
    try {
      // Bun / Node.js compatible sync read
      const fs = require("fs") as typeof import("fs");
      return fs.readFileSync(fileMatch[1]!, "utf-8").trim();
    } catch {
      return null;
    }
  }
  // Already resolved — return as-is
  return value;
}

/** Resolve and validate plugin options.  Returns null when plugin should be disabled. */
function _resolveConfig(raw: RawPluginOptions | undefined, log: DiagnosticLog): ResolvedConfig | null {
  if (!raw) {
    log.warn("[webhook-notifier] no config provided; plugin disabled");
    return null;
  }

  if (raw.enabled === false) {
    return null;
  }

  // Resolve URL
  const rawUrl = typeof raw.url === "string" ? raw.url : "";
  if (!rawUrl) {
    log.warn("[webhook-notifier] missing url; plugin disabled");
    return null;
  }
  const url = _resolveInterpolation(rawUrl);
  if (!url) {
    log.warn("[webhook-notifier] url resolution failed; plugin disabled");
    return null;
  }

  // Resolve token
  const rawToken = typeof raw.token === "string" ? raw.token : "";
  if (!rawToken) {
    log.warn("[webhook-notifier] missing token; plugin disabled");
    return null;
  }
  const token = _resolveInterpolation(rawToken);
  if (!token) {
    log.warn("[webhook-notifier] token resolution failed; plugin disabled");
    return null;
  }

  // timeoutMs
  const timeoutMs =
    typeof raw.timeoutMs === "number" && raw.timeoutMs > 0 && Number.isFinite(raw.timeoutMs)
      ? raw.timeoutMs
      : REQUEST_TIMEOUT_MS;

  // Events filter (default: all three)
  const eventFilter = new Set<string>();
  if (Array.isArray(raw.events) && raw.events.length > 0) {
    for (const e of raw.events) {
      if (typeof e === "string") eventFilter.add(e);
    }
  } else {
    eventFilter.add("session_idle").add("session_error").add("permission_asked");
  }

  const projectDisplayName =
    typeof raw.projectDisplayName === "string" && raw.projectDisplayName.length > 0
      ? raw.projectDisplayName
      : undefined;

  return { url, token, timeoutMs, enabled: true, events: eventFilter, projectDisplayName };
}

// ─── Envelope Construction ──────────────────────────────────

const _SUPPORTED_INPUT_EVENTS = new Set([
  "session.status",
  "session.idle",
  "session.error",
  "permission.updated",
]);

async function _buildEnvelope(
  event: OpenCodeEvent,
  eventId: string,
): Promise<Envelope | null> {
  // Derive output event type
  const outputEvent = _mapEventType(event);
  if (!outputEvent) return null;

  // Session ref (hashed)
  const rawSessionId = event.sessionId;
  if (!rawSessionId) return null;
  const sessionRef = await _hashSessionRef(rawSessionId);

  // Session name from event
  const eventName = event.session?.name ?? event.session?.title ?? undefined;
  const sessionName = _sanitiseName(eventName);

  const envelope: Envelope = {
    id: eventId,
    event: outputEvent,
    version: 1 as const,
    emittedAt: _nowISO(),
    session: { ref: sessionRef },
  };

  if (sessionName) {
    envelope.session.name = sessionName;
  }

  // Optional fields — only when reliably available and whitelisted
  if (typeof event.agent === "string" && event.agent.length > 0) {
    envelope.agent = event.agent;
  }
  if (typeof event.model === "string" && event.model.length > 0) {
    envelope.model = event.model;
  }
  if (typeof event.durationMs === "number" && Number.isFinite(event.durationMs) && event.durationMs >= 0) {
    envelope.durationMs = event.durationMs;
  }

  // Event-specific fields
  if (outputEvent === "opencode.permission_asked" && event.permission) {
    envelope.permission = { category: _derivePermissionCategory(event.permission) };
  }
  if (outputEvent === "opencode.session_error" && event.error) {
    envelope.error = _deriveErrorCategory(event.error);
  }

  return envelope;
}

/**
 * Map input event type to output event type.
 * Returns null for events we should not send (e.g., command, tool, message).
 */
function _mapEventType(event: OpenCodeEvent): OutputEvent | null {
  const t = event.type;
  if (!t) return null;

  switch (t) {
    case "session.status": {
      if (event.status === "idle") return "opencode.session_idle";
      return null; // busy is just a state transition, no webhook
    }
    case "session.idle":
      return "opencode.session_idle";
    case "session.error":
      return "opencode.session_error";
    case "permission.updated":
      return "opencode.permission_asked";
    default:
      return null; // command, tool, todo, diff, message etc
  }
}

// ─── State Machine Logic ────────────────────────────────────

/**
 * Process an event through the session state machine.
 * Returns an envelope to send, or null if the event should be suppressed.
 */
async function _processEvent(
  event: OpenCodeEvent,
  config: ResolvedConfig,
): Promise<Envelope | null> {
  const rawSessionId = event.sessionId;
  if (!rawSessionId) return null;

  const sessionRef = await _hashSessionRef(rawSessionId);
  const st = _getState(sessionRef);

  const t = event.type;

  // --- session.status = busy ---
  if (t === "session.status" && event.status === "busy") {
    st.hadBusy = true;
    st.sentIdle = false;
    st.hadErrorForCycle = false;
    st.cycle++;
    st.pendingEventId = undefined;
    return null; // no webhook for busy
  }

  // --- session.idle (deprecated) or session.status = idle ---
  if (t === "session.idle" || (t === "session.status" && event.status === "idle")) {
    if (!config.events.has("session_idle")) return null;

    // Atomic claim: prevent concurrent idle processing for the same session.
    // This guard runs before any await in this branch, ensuring that
    // concurrent session.status=idle + legacy session.idle for the same
    // session only construct/send one envelope.
    if (_idleProcessing.has(sessionRef)) return null;
    _idleProcessing.add(sessionRef);

    try {
      // State machine rules:
      // 1. No busy observed → ignore (initial idle)
      // 2. Error in cycle → suppress (error already notified)
      // 3. Already sent → ignore (dedup)
      if (!st.hadBusy || st.hadErrorForCycle || st.sentIdle) {
        return null;
      }

      const eventId = _generateId();
      st.sentIdle = true;
      st.pendingEventId = eventId;

      const envelope = await _buildEnvelope(event, eventId);
      if (!envelope) {
        // Rollback: envelope construction failed, allow retry on next idle
        st.sentIdle = false;
        st.pendingEventId = undefined;
        return null;
      }
      return envelope;
    } finally {
      _idleProcessing.delete(sessionRef);
    }
  }

  // --- session.error ---
  if (t === "session.error") {
    if (!config.events.has("session_error")) return null;
    const eventId = _generateId();
    st.hadErrorForCycle = true;
    st.sentIdle = true; // suppress any subsequent idle
    st.pendingEventId = eventId;
    const envelope = await _buildEnvelope(event, eventId);
    return envelope;
  }

  // --- permission.updated ---
  if (t === "permission.updated") {
    if (!config.events.has("permission_asked")) return null;
    // Permission events don't interact with the busy/idle/error state machine
    const eventId = _generateId();
    const envelope = await _buildEnvelope(event, eventId);
    return envelope;
  }

  return null;
}

// ─── Transport ──────────────────────────────────────────────

/** Response classification for retry decisions. */
interface SendResult {
  ok: boolean;
  status?: number;
  /** Raw Retry-After header value (seconds or HTTP-date), for backoff. */
  retryAfter?: string | null;
}

/**
 * Single HTTP POST with timeout.
 * Uses AbortController for timeout.
 */
async function _sendSingle(
  url: string,
  token: string,
  envelope: Envelope,
  timeoutMs: number,
  attempt: number,
  log: DiagnosticLog,
): Promise<SendResult> {
  const body = JSON.stringify(envelope);
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-OpenCode-Event": envelope.event,
    Authorization: `Bearer ${token}`,
  };

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers,
      body,
      signal: controller.signal,
    });
  } catch (err: unknown) {
    clearTimeout(timeoutId);
    const category = err instanceof DOMException && err.name === "AbortError"
      ? "timeout"
      : "network";
    log.warn(`[webhook-notifier] send attempt ${attempt} failed: ${category}`);
    return { ok: false };
  } finally {
    clearTimeout(timeoutId);
  }

  const status = response.status;
  log.warn(`[webhook-notifier] send attempt ${attempt} status: ${status}`);

  const result: SendResult = { ok: false, status };

  if (status >= 200 && status < 300) {
    result.ok = true;
    return result;
  }

  // Extract Retry-After header for retryable responses
  const retryAfter = response.headers.get("Retry-After");
  if (retryAfter !== null) {
    result.retryAfter = retryAfter;
  }

  return result;
}

/** Determine whether a failed send should be retried. */
function _shouldRetry(result: SendResult): boolean {
  if (result.ok) return false;
  const s = result.status;
  if (s === undefined) return true; // network / timeout
  if (s === 429) return true; // rate-limited
  if (s >= 500) return true; // server error
  return false; // 4xx (including 401, 403, 413) → no retry
}

/**
 * Compute backoff delay for a given attempt (0-based).
 * Exponential backoff + jitter, capped at MAX_BACKOFF_MS.
 * Respects Retry-After header if present, bounded to MAX_BACKOFF_MS.
 * Supports both seconds-integer and HTTP-date formats.
 * Never throws on unparseable header; falls through to exponential backoff.
 */
function _backoffDelay(attempt: number, retryAfter?: string | null): number {
  if (retryAfter) {
    const trimmed = retryAfter.trim();
    let seconds: number | undefined;

    // Try integer seconds (most common)
    if (/^\d+$/.test(trimmed)) {
      seconds = parseInt(trimmed, 10);
    } else {
      // Try HTTP-date format, e.g. "Wed, 21 Oct 2015 07:28:00 GMT"
      const date = new Date(trimmed);
      if (Number.isFinite(date.getTime())) {
        seconds = Math.max(0, (date.getTime() - Date.now()) / 1000);
      }
    }

    if (seconds !== undefined && seconds >= 0) {
      return Math.min(seconds * 1000, MAX_BACKOFF_MS);
    }
    // Unparseable → fall through to exponential backoff
  }

  const delay = Math.min(BASE_BACKOFF_MS * Math.pow(2, attempt), MAX_BACKOFF_MS);
  // Add up to 20 % jitter
  return delay + Math.random() * delay * 0.2;
}

/** Async sleep helper. */
const _sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Send the envelope with retry.
 *
 * - 3 total attempts (1 initial + 2 retries).
 * - Retries on: network error, timeout, 429, 5xx.
 * - No retry on: 401, 403, 413, other 4xx.
 * - All failures are caught and logged (no throw to caller).
 */
async function _sendWithRetry(
  envelope: Envelope,
  config: ResolvedConfig,
  log: DiagnosticLog,
): Promise<void> {
  for (let attempt = 1; attempt <= MAX_RETRIES + 1; attempt++) {
    const result = await _sendSingle(
      config.url,
      config.token,
      envelope,
      config.timeoutMs,
      attempt,
      log,
    );

    if (result.ok) return;

    if (attempt <= MAX_RETRIES && _shouldRetry(result)) {
      const delay = _backoffDelay(attempt - 1, result.retryAfter);
      await _sleep(delay);
    } else {
      log.warn(
        `[webhook-notifier] permanently failed after ${attempt} attempt(s)${result.status !== undefined ? ` (status ${result.status})` : ""}`,
      );
      return;
    }
  }
}

// ─── Event Handler ──────────────────────────────────────────

/**
 * Main event handler — fires-and-forgets.
 * Never throws to the OpenCode runtime.
 */
async function _onEvent(
  rawEvent: OpenCodeEvent,
  config: ResolvedConfig,
  log: DiagnosticLog,
): Promise<void> {
  try {
    const envelope = await _processEvent(rawEvent, config);
    if (!envelope) return;

    log.warn(`[webhook-notifier] sending ${envelope.event}`);
    await _sendWithRetry(envelope, config, log);

    // Periodically clean up old sessions
    _cleanupSessions();
  } catch {
    // Last-resort safety net — should never fire
    log.error("[webhook-notifier] unexpected internal error");
  }
}

// ─── Wrapper Event Normalization ─────────────────────────────

/**
 * Normalize an OpenCode runtime event (wrapped in { event: { id, type, properties } })
 * into our internal OpenCodeEvent shape.
 *
 * Maps the official Event properties to our flat event fields:
 * - session.status:  properties.sessionID, properties.status ({ type: "busy"|"idle"|"retry" })
 * - session.idle:    properties.sessionID
 * - session.error:   properties.sessionID?, properties.error?
 * - permission.updated / permission.asked: properties is the Permission
 * - All other events → null (ignored)
 *
 * Original sessionID from properties is mapped to sessionId for internal use
 * but NEVER appears in logs or webhook payload.
 */
function _normalizeWrappedEvent(wrapped: { event: Event }): OpenCodeEvent | null {
  const { type, properties } = wrapped.event;
  if (!type) return null;

  const props = properties ?? {};

  switch (type) {
    case "session.status": {
      const rawStatus = props.status;
      let statusStr: string | undefined;

      if (rawStatus && typeof rawStatus === "object") {
        // Official: properties.status is { type: "busy"|"idle"|"retry" }
        statusStr = (rawStatus as Record<string, unknown>).type as string;
      } else if (typeof rawStatus === "string") {
        // Defensive fallback: plain string status
        statusStr = rawStatus;
      }

      if (!statusStr) return null;

      return {
        type,
        sessionId: typeof props.sessionID === "string" ? props.sessionID : undefined,
        status: statusStr,
      };
    }

    case "session.idle": {
      return {
        type,
        sessionId: typeof props.sessionID === "string" ? props.sessionID : undefined,
      };
    }

    case "session.error": {
      return {
        type,
        sessionId: typeof props.sessionID === "string" ? props.sessionID : undefined,
        error: props.error ? { ...(props.error as Record<string, unknown>) } as OpenCodeEvent["error"] : undefined,
      };
    }

    case "permission.updated":
    case "permission.asked": {
      // properties IS the Permission object: { sessionID, type (permission type), ... }
      return {
        type: "permission.updated",
        sessionId: typeof props.sessionID === "string" ? props.sessionID : undefined,
        permission: {
          type: typeof props.type === "string" ? props.type : undefined,
        },
      };
    }

    default:
      return null;
  }
}

// ─── Session Enrichment ──────────────────────────────────────

/**
 * Attempt to enrich an event with session metadata (title, agent, model)
 * from the OpenCode runtime via input.client.session.get().
 *
 * Non-fatal: all errors are silently swallowed. The event is processed
 * normally even if enrichment fails.
 */
async function _enrichEvent(event: OpenCodeEvent, input: PluginInput): Promise<void> {
  const rawSessionId = event.sessionId;
  if (!rawSessionId) return;

  try {
    const response = await input.client.session.get({ path: { id: rawSessionId } });
    // Response could be { data: { title?, name?, agent?, model? } } or direct object
    const data =
      response && typeof response === "object" && "data" in (response as Record<string, unknown>)
        ? (response as Record<string, unknown>).data
        : response;

    if (!data || typeof data !== "object") return;

    const sessionData = data as Record<string, unknown>;

    // Only fill fields not already present in the event
    if (!event.session) event.session = {};
    if (!event.session.name) {
      const name =
        typeof sessionData.title === "string"
          ? sessionData.title
          : typeof sessionData.name === "string"
            ? sessionData.name
            : undefined;
      if (name) event.session.name = name;
    }
    if (!event.agent && typeof sessionData.agent === "string") {
      event.agent = sessionData.agent;
    }
    if (!event.model && typeof sessionData.model === "string") {
      event.model = sessionData.model;
    }
  } catch {
    // Enrichment failure is non-fatal
  }
}

// ─── Logger ──────────────────────────────────────────────────

const _log: DiagnosticLog = {
  warn: (...args: unknown[]) => {
    console.warn(...args);
  },
  error: (...args: unknown[]) => {
    console.error(...args);
  },
};

// ─── Plugin API Types (self-declared, matching OpenCode v1.17.9) ──
// No runtime dependency on @opencode-ai/plugin.

/** Plugin input context from the OpenCode runtime. */
interface PluginInput {
  client: {
    session: {
      get(params: { path: { id: string } }): Promise<unknown>;
    };
  };
}

/** Plugin configuration options from opencode.jsonc plugins array. */
interface PluginOptions {
  url?: string;
  token?: string;
  timeoutMs?: number;
  enabled?: boolean;
  events?: string[];
  projectDisplayName?: string;
  [key: string]: unknown;
}

/** Hooks returned by a V1 Plugin server function. */
interface Hooks {
  event?: (input: { event: Event }) => Promise<void>;
}

/** Event dispatched by the OpenCode runtime to plugins. */
interface Event {
  id: string;
  type: string;
  properties: Record<string, unknown>;
}

/** V1 Plugin signature: (input, options?) → Hooks. */
type Plugin = (input: PluginInput, options?: PluginOptions) => Promise<Hooks>;

/** V1 Plugin module shape for file-based default exports. */
interface PluginModule {
  id?: string;
  server: Plugin;
  tui?: never;
}

// ─── Plugin Definition ──────────────────────────────────────

/**
 * V1 file server plugin.
 *
 * OpenCode loader calls: readV1Plugin(mod).default.server(input, options)
 * Config arrives as the second parameter (from [path, options] tuple).
 * Invalid configuration returns empty Hooks — plugin silently disabled.
 */
const server: Plugin = async (input, options) => {
  const config = _resolveConfig(options as RawPluginOptions | undefined, _log);
  if (!config) {
    // Plugin disabled — register no hooks
    return {};
  }

  return {
    /**
     * V1 Plugin event hook.
     * Receives OpenCode runtime events in the official wrapper:
     *   { event: { id, type, properties } }
     * Runtime calls void hook.event(...) — fire-and-forget.
     * All errors caught and logged; no unhandled rejection.
     */
    async event(wrapped: { event: Event }): Promise<void> {
      try {
        const normalized = _normalizeWrappedEvent(wrapped);
        if (!normalized) return;

        // Attempt session metadata enrichment from runtime
        if (normalized.sessionId) {
          await _enrichEvent(normalized, input);
        }

        await _onEvent(normalized, config, _log);
      } catch {
        _log.error("[webhook-notifier] unexpected internal error");
      }
    },
  };
};

/**
 * Default export conforming to PluginModule.
 *
 * The V1 loader reads: readV1Plugin(mod).default.server(input, options)
 * Named testing exports below are safe — loader only accesses default.server.
 */
export default { id: "webhook-notifier", server } satisfies PluginModule;

// ─── Exported for testing only ──────────────────────────────
// These named exports are safe for test imports but will NOT cause
// duplicate plugin loading because OpenCode only loads the default export.
//
export type {
  RawPluginOptions,
  ResolvedConfig,
  Envelope,
  OpenCodeEvent,
  SessionState,
  CategoryInfo,
  DiagnosticLog,
  Hooks,
  PluginModule,
  PluginInput,
  PluginOptions,
  Plugin,
  Event,
};

export {
  _hashSessionRef,
  _generateId,
  _sanitiseName,
  _deriveErrorCategory,
  _derivePermissionCategory,
  _resolveConfig,
  _resolveInterpolation,
  _buildEnvelope,
  _processEvent,
  _sendSingle,
  _sendWithRetry,
  _shouldRetry,
  _backoffDelay,
  _mapEventType,
  _getState,
  _idleProcessing,
  _normalizeWrappedEvent,
  _enrichEvent,
};
