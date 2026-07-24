/**
 * OpenCode V1 Webhook Notifier Plugin
 *
 * Single-file TypeScript Plugin for OpenCode Desktop/CLI (SDK 1.17.9).
 * Listens on `event` hook, filters to four MVP event types, constructs
 * a safe minimal envelope, and POSTs to a configurable webhook URL.
 *
 * Privacy constraints:
 *  - No raw session ID, cwd, prompt, message, tool, diff, token, headers,
 *    authorization, or unrelated metadata leaves this plugin. Question and
 *    permission content is opt-in and remains explicitly allowlisted/bounded.
 *  - session.ref is a non-reversible SHA-256 digest (first 32 hex chars).
 *  - session.name is sanitised (dangerous Unicode removed, control chars
 *    normalised, length capped at 200). HTML/MD escaping is the server's
 *    responsibility.
 *  - Normal diagnostic output contains only event type / attempt count /
 *    status category. Optional metadata diagnostics are bounded, sampled,
 *    and never include URL, token, body, raw session ID, or session name.
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
  actionContentMode?: string;
  metadataDiagnostics?: string;
  [key: string]: unknown;
}

type ActionContentMode = "strict" | "summary" | "full";
type MetadataDiagnostics = "off" | "once" | "sample";

/** Resolved, validated configuration (no {env} / {file} placeholders). */
interface ResolvedConfig {
  url: string;
  token: string;
  timeoutMs: number;
  enabled: boolean;
  events: Set<string>;
  projectDisplayName: string | undefined;
  actionContentMode: ActionContentMode;
  metadataDiagnostics: MetadataDiagnostics;
}

/** Logical event we send to the webhook server. */
interface Envelope {
  id: string;
  event:
    | "opencode.session_idle"
    | "opencode.session_error"
    | "opencode.permission_asked"
    | "opencode.question_asked";
  version: 1;
  emittedAt: string;
  session: {
    ref: string;
    name?: string;
    scope: SessionScope;
  };
  projectDisplayName?: string;
  agent?: string;
  model?: string;
  durationMs?: number;
  startedAt?: string;
  taskStartedAt?: string;
  endedAt?: string;
  counts?: {
    messages?: number;
    tools?: number;
    changes?: number;
  };
  permission?: PermissionEnvelope;
  question?: QuestionEnvelope;
  error?: { category: string; code?: string };
}

type SessionScope = "root" | "subagent" | "unknown";

interface QuestionEnvelope {
  count?: number;
  optionCount?: number;
  summary?: string;
  items?: QuestionItem[];
}

interface QuestionItem {
  text?: string;
  header?: string;
  recommended?: string | boolean | number;
  options?: QuestionOption[];
}

interface QuestionOption {
  label?: string;
  description?: string;
  recommended?: string | boolean | number;
}

interface PermissionEnvelope {
  category: string;
  title?: string;
  summary?: string;
  description?: string;
  action?: string;
  target?: string;
  patterns?: string[];
}

/** OpenCode V1 event (a subset of the full payload that we touch). */
interface OpenCodeEvent {
  type: string;
  sessionId?: string;
  status?: string;
  session?: {
    name?: string;
    title?: string;
    time?: { created?: unknown; updated?: unknown };
  };
  sessionScope?: SessionScope;
  agent?: string;
  model?: unknown;
  provider?: unknown;
  durationMs?: number;
  startedAt?: unknown;
  endedAt?: unknown;
  counts?: {
    messages?: unknown;
    tools?: unknown;
    changes?: unknown;
  };
  questions?: QuestionInput[];
  error?: {
    name?: string;
    message?: string;
    status?: number;
    [key: string]: unknown;
  };
  permission?: {
    type?: string;
    category?: unknown;
    title?: unknown;
    summary?: unknown;
    description?: unknown;
    action?: unknown;
    operation?: unknown;
    target?: unknown;
    path?: unknown;
    patterns?: unknown;
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

interface QuestionInput {
  text?: unknown;
  header?: unknown;
  recommended?: unknown;
  options?: QuestionOptionInput[];
}

interface QuestionOptionInput {
  label?: unknown;
  description?: unknown;
  recommended?: unknown;
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
  /** Last access time used for bounded cleanup. */
  lastAccessMs: number;
}

/** Safe Assistant-only metadata retained between OpenCode events. */
interface AssistantMetadata {
  agent?: string;
  providerID?: string;
  modelID?: string;
  created?: string;
  completed?: string;
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

interface MetadataDiagnosticContext {
  mode: MetadataDiagnostics;
  log: DiagnosticLog;
  sampleSession?: number;
}

// ─── Constants ──────────────────────────────────────────────

const MAX_NAME_LENGTH = 200;
const MAX_SESSION_REF_LENGTH = 128;
const MAX_AGENT_MODEL_LENGTH = 128;
const MAX_ACTION_TEXT_LENGTH = 512;
const MAX_ACTION_ITEMS = 8;
const MAX_ACTION_OPTIONS = 12;
const MAX_PERMISSION_PATTERNS = 16;
const MAX_ENVELOPE_BYTES = 64 * 1024;
const MAX_COUNT = 1_000_000;
const MAX_DURATION_MS = 604_800_000;
const MAX_RETRIES = 2; // 3 total attempts (1 initial + 2 retries)
const BASE_BACKOFF_MS = 400;
const MAX_BACKOFF_MS = 5000;
const REQUEST_TIMEOUT_MS = 10_000;
const MAX_CACHE_ENTRIES = 1000;
const CACHE_RETAIN_ENTRIES = 500;
const SESSION_GET_WARNING = "[webhook-notifier] session.get enrichment failed";
const SESSION_MESSAGES_WARNING = "[webhook-notifier] session.messages enrichment failed";
const METADATA_DIAGNOSTIC_PREFIX = "[webhook-notifier][metadata-diagnostic]";
const MAX_METADATA_DIAGNOSTIC_KEYS = 32;
const MAX_METADATA_DIAGNOSTIC_STRING_LENGTH = 128;
const MAX_METADATA_DIAGNOSTIC_LENGTH = 4096;
const MAX_METADATA_DIAGNOSTIC_ITEMS = 10;
const MAX_METADATA_DIAGNOSTIC_MODEL_KEYS = 24;
const MAX_METADATA_DIAGNOSTIC_SAMPLES_PER_PHASE = 8;
const MAX_METADATA_SAMPLE_SESSIONS = 1000;
const RETAIN_METADATA_SAMPLE_SESSIONS = 500;
const MAX_METADATA_DIAGNOSTIC_NUMBER = 1_000_000;

type MetadataDiagnosticPhase =
  | "message_updated"
  | "session_get"
  | "session_messages"
  | "outgoing_envelope";

/** Process-lifetime guard: each once-mode diagnostic phase is emitted once. */
const _metadataDiagnosticPhases = new Set<MetadataDiagnosticPhase>();

interface MetadataDiagnosticSampleState {
  count: number;
  payloads: Set<string>;
}

interface MetadataSampleSessionState {
  sampleSession: number;
}

const _metadataDiagnosticSamples = new Map<MetadataDiagnosticPhase, MetadataDiagnosticSampleState>();
const _metadataSampleSessions = new Map<string, MetadataSampleSessionState>();
let _nextMetadataSampleSession = 1;

/** Test-only reset; production code intentionally never resets this set. */
function _resetMetadataDiagnostics(): void {
  _metadataDiagnosticPhases.clear();
  _metadataDiagnosticSamples.clear();
  _metadataSampleSessions.clear();
  _nextMetadataSampleSession = 1;
}

const METADATA_DIAGNOSTIC_KEY_RE = /^[A-Za-z][A-Za-z0-9_$-]{0,63}$/;
const METADATA_DIAGNOSTIC_BLOCKED_KEYS = new Set([
  "token",
  "url",
  "headers",
  "raw",
  "rawsessionid",
  "sessionid",
  "sessionref",
  "messageid",
  "parentid",
  "title",
  "name",
  "question",
  "option",
  "options",
  "parts",
  "path",
  "cwd",
  "tool",
  "input",
  "output",
  "reasoning",
  "tokens",
  "cost",
  "response",
  "responsebody",
  "body",
  "message",
  "apikey",
  "secret",
  "password",
  "authorization",
  "credential",
  "credentials",
  "privatekey",
  "clientsecret",
  "provideroptions",
]);
const METADATA_DIAGNOSTIC_URL_RE = /^[A-Za-z][A-Za-z0-9+.-]*:\/\//;

const OUTPUT_EVENTS = new Set([
  "opencode.session_idle",
  "opencode.session_error",
  "opencode.permission_asked",
  "opencode.question_asked",
] as const);

type OutputEvent =
  | "opencode.session_idle"
  | "opencode.session_error"
  | "opencode.permission_asked"
  | "opencode.question_asked";

/**
 * Set of session refs with an idle notification currently in-flight.
 * Provides an atomic guard before the first await to prevent concurrent
 * idle events for the same session from constructing/sending more than
 * one envelope.
 */
const _idleProcessing = new Set<string>();

/** Reliable root/subagent classifications keyed by the existing anonymous ref. */
const _sessionScopes = new Map<string, SessionScope>();

/** Assistant metadata keyed only by the existing anonymous session ref. */
const _assistantMetadata = new Map<string, AssistantMetadata>();

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

/**
 * Clean action-required business text without treating it as trusted markup.
 * Full mode still has a bounded, single-segment representation.
 */
function _sanitiseActionText(raw: unknown, maxLength = MAX_ACTION_TEXT_LENGTH): string | undefined {
  if (typeof raw !== "string") return undefined;

  let s = raw.replace(DANGEROUS_UNICODE_RE, "");
  s = s.replace(CONTROL_RE, " ").replace(/[\r\n\t]+/g, " ");
  s = s.replace(/ {2,}/g, " ").trim();
  if (!s) return undefined;
  return s.length > maxLength ? `${s.slice(0, maxLength).trimEnd()}…` : s;
}

function _isRecord(raw: unknown): raw is Record<string, unknown> {
  return !!raw && typeof raw === "object" && !Array.isArray(raw);
}

function _safeMetadataDiagnosticKey(raw: string): boolean {
  if (!METADATA_DIAGNOSTIC_KEY_RE.test(raw)) return false;
  const normalised = raw.replace(/[^A-Za-z0-9]/g, "").toLowerCase();
  return !METADATA_DIAGNOSTIC_BLOCKED_KEYS.has(normalised);
}

/** Return bounded, sorted key names without inspecting any value. */
function _metadataDiagnosticKeys(raw: unknown): string[] {
  if (!_isRecord(raw)) return [];
  try {
    return Object.keys(raw)
      .filter(_safeMetadataDiagnosticKey)
      .sort()
      .slice(0, MAX_METADATA_DIAGNOSTIC_KEYS);
  } catch {
    return [];
  }
}

function _metadataDiagnosticModelKeys(raw: unknown): string[] {
  if (!_isRecord(raw)) return [];
  try {
    return Object.keys(raw)
      .filter(_safeMetadataDiagnosticKey)
      .sort()
      .slice(0, MAX_METADATA_DIAGNOSTIC_MODEL_KEYS);
  } catch {
    return [];
  }
}

/** Short metadata values may be logged only after cleaning and URL/path rejection. */
function _safeMetadataDiagnosticString(raw: unknown): string | undefined {
  const value = _sanitiseActionText(raw, MAX_METADATA_DIAGNOSTIC_STRING_LENGTH);
  if (!value) return undefined;
  if (METADATA_DIAGNOSTIC_URL_RE.test(value) || /^[\\/]/.test(value)) return undefined;
  return value;
}

function _metadataDiagnosticLength(raw: unknown): number {
  return typeof raw === "string" ? Math.min(raw.length, MAX_METADATA_DIAGNOSTIC_LENGTH) : 0;
}

function _metadataDiagnosticCandidate(raw: unknown): string | number | boolean | undefined {
  if (typeof raw === "string") return _safeMetadataDiagnosticString(raw);
  if (typeof raw === "boolean") return raw;
  if (typeof raw === "number") {
    return Number.isFinite(raw) && Math.abs(raw) <= MAX_METADATA_DIAGNOSTIC_NUMBER ? raw : undefined;
  }
  if (Array.isArray(raw)) return "array";
  if (_isRecord(raw)) return "object";
  return undefined;
}

function _metadataParentIDState(raw: unknown): "missing" | "null" | "empty" | "string" | "invalid" {
  if (!_isRecord(raw)) return "missing";
  if (!Object.prototype.hasOwnProperty.call(raw, "parentID")) return "missing";
  const parentID = raw.parentID;
  if (parentID === null) return "null";
  if (typeof parentID === "string") return parentID.trim() ? "string" : "empty";
  return "invalid";
}

function _metadataTimeKeys(raw: unknown): string[] {
  return _isRecord(raw) ? _metadataDiagnosticKeys(raw) : [];
}

function _metadataDiagnosticContextForSessionRef(
  context: MetadataDiagnosticContext | undefined,
  sessionRef: string | undefined,
): MetadataDiagnosticContext | undefined {
  if (!context || context.mode !== "sample" || !sessionRef) return context;
  const existing = _metadataSampleSessions.get(sessionRef);
  if (existing) {
    _metadataSampleSessions.delete(sessionRef);
    _metadataSampleSessions.set(sessionRef, existing);
    return { ...context, sampleSession: existing.sampleSession };
  }
  const created: MetadataSampleSessionState = { sampleSession: _nextMetadataSampleSession++ };
  _metadataSampleSessions.set(sessionRef, created);
  return { ...context, sampleSession: created.sampleSession };
}

function _cleanupMetadataSampleSessions(): void {
  if (_metadataSampleSessions.size <= MAX_METADATA_SAMPLE_SESSIONS) return;
  const entries = [..._metadataSampleSessions.keys()];
  for (let i = 0; i < entries.length - RETAIN_METADATA_SAMPLE_SESSIONS; i++) {
    _metadataSampleSessions.delete(entries[i]!);
  }
}

function _emitMetadataDiagnostic(
  context: MetadataDiagnosticContext | undefined,
  phase: MetadataDiagnosticPhase,
  fields: () => Record<string, unknown>,
): void {
  if (!context || context.mode === "off") return;
  try {
    const baseFields = fields();
    const payload = context.sampleSession === undefined
      ? { phase, ...baseFields }
      : { phase, sampleSession: context.sampleSession, ...baseFields };
    const payloadJSON = JSON.stringify(payload);

    if (context.mode === "once") {
      if (_metadataDiagnosticPhases.has(phase)) return;
      _metadataDiagnosticPhases.add(phase);
    } else {
      let sample = _metadataDiagnosticSamples.get(phase);
      if (!sample) {
        sample = { count: 0, payloads: new Set<string>() };
        _metadataDiagnosticSamples.set(phase, sample);
      }
      if (sample.count >= MAX_METADATA_DIAGNOSTIC_SAMPLES_PER_PHASE || sample.payloads.has(payloadJSON)) return;
      sample.payloads.add(payloadJSON);
      sample.count++;
    }

    context.log.warn(`${METADATA_DIAGNOSTIC_PREFIX} ${payloadJSON}`);
  } catch {
    // Diagnostics must never affect envelope construction or transport.
  }
}

function _diagnoseAssistantMessage(
  info: Record<string, unknown>,
  context: MetadataDiagnosticContext | undefined,
): void {
  if (info.role !== "assistant") return;
  _emitMetadataDiagnostic(context, "message_updated", () => {
    const fields: Record<string, unknown> = {
      infoKeys: _metadataDiagnosticKeys(info),
      role: "assistant",
      timeKeys: _metadataTimeKeys(info.time),
      parentIDState: _metadataParentIDState(info),
    };
    const mode = _safeMetadataDiagnosticString(info.mode);
    const providerID = _safeMetadataDiagnosticString(info.providerID);
    const modelID = _safeMetadataDiagnosticString(info.modelID);
    if (mode) fields.mode = mode;
    if (providerID) fields.providerID = providerID;
    if (modelID) fields.modelID = modelID;
    for (const key of ["variant", "reasoningEffort", "reasoning_effort"] as const) {
      const value = _metadataDiagnosticCandidate(info[key]);
      if (value !== undefined) fields[key] = value;
    }
    return fields;
  });
}

interface SessionDiagnosticResponse {
  responseShape: "data-wrapper" | "direct-object" | "invalid";
  data?: Record<string, unknown>;
}

function _inspectSessionResponse(response: unknown): SessionDiagnosticResponse {
  if (!_isRecord(response)) return { responseShape: "invalid" };
  if ("data" in response) {
    return _isRecord(response.data)
      ? { responseShape: "data-wrapper", data: response.data }
      : { responseShape: "invalid" };
  }
  return { responseShape: "direct-object", data: response };
}

interface SessionModelDiagnostic {
  modelShape: "missing" | "string" | "object" | "invalid";
  modelKeys: string[];
  modelProviderID?: string;
  modelID?: string;
  modelVariant?: string | number | boolean;
  modelReasoningEffort?: string | number | boolean;
  modelReasoning_effort?: string | number | boolean;
  topLevelVariant?: string | number | boolean;
  topLevelReasoningEffort?: string | number | boolean;
  topLevelReasoning_effort?: string | number | boolean;
}

function _diagnoseSessionModel(data: Record<string, unknown>): SessionModelDiagnostic {
  const rawModel = data.model;
  const modelShape: SessionModelDiagnostic["modelShape"] =
    rawModel === undefined
      ? "missing"
      : typeof rawModel === "string"
        ? "string"
        : _isRecord(rawModel)
          ? "object"
          : "invalid";
  const model = _isRecord(rawModel) ? rawModel : undefined;
  const modelVariant = _metadataDiagnosticCandidate(model?.variant);
  const modelReasoningEffort = _metadataDiagnosticCandidate(model?.reasoningEffort);
  const modelReasoning_effort = _metadataDiagnosticCandidate(model?.reasoning_effort);
  const topLevelVariant = _metadataDiagnosticCandidate(data.variant);
  const topLevelReasoningEffort = _metadataDiagnosticCandidate(data.reasoningEffort);
  const topLevelReasoning_effort = _metadataDiagnosticCandidate(data.reasoning_effort);
  const providerID = _safeMetadataDiagnosticString(
    model?.providerID ?? model?.providerId ?? model?.provider ??
      data.modelProviderID ?? data.providerID ?? data.providerId ?? data.provider,
  );
  const modelID = _safeMetadataDiagnosticString(
    model?.modelID ?? model?.modelId ?? model?.id ??
      data.modelID ?? data.modelId ?? (typeof rawModel === "string" ? rawModel : undefined),
  );
  return {
    modelShape,
    modelKeys: _metadataDiagnosticModelKeys(model),
    ...(providerID ? { modelProviderID: providerID } : {}),
    ...(modelID ? { modelID } : {}),
    ...(modelVariant !== undefined ? { modelVariant } : {}),
    ...(modelReasoningEffort !== undefined ? { modelReasoningEffort } : {}),
    ...(modelReasoning_effort !== undefined ? { modelReasoning_effort } : {}),
    ...(topLevelVariant !== undefined ? { topLevelVariant } : {}),
    ...(topLevelReasoningEffort !== undefined ? { topLevelReasoningEffort } : {}),
    ...(topLevelReasoning_effort !== undefined ? { topLevelReasoning_effort } : {}),
  };
}

function _diagnoseSessionGet(
  response: SessionDiagnosticResponse,
  context: MetadataDiagnosticContext | undefined,
): void {
  _emitMetadataDiagnostic(context, "session_get", () => {
    const data = response.data;
    const title = data?.title;
    const model = data
      ? _diagnoseSessionModel(data)
      : { modelShape: "missing" as const, modelKeys: [] };
    const agent = data
      ? _safeMetadataDiagnosticString(data.agent ?? data.mode)
      : undefined;
    const fields: Record<string, unknown> = {
      responseShape: response.responseShape,
      sessionKeys: _metadataDiagnosticKeys(data),
      titlePresent: typeof title === "string" && title.length > 0,
      titleLength: _metadataDiagnosticLength(title),
      parentIDState: _metadataParentIDState(data),
      timeKeys: _metadataTimeKeys(data?.time),
      modelShape: model.modelShape,
      modelKeys: model.modelKeys,
    };
    if (agent) fields.agent = agent;
    if (model.modelProviderID) fields.modelProviderID = model.modelProviderID;
    if (model.modelID) fields.modelID = model.modelID;
    for (const key of [
      "modelVariant",
      "modelReasoningEffort",
      "modelReasoning_effort",
      "topLevelVariant",
      "topLevelReasoningEffort",
      "topLevelReasoning_effort",
    ] as const) {
      const value = model[key];
      if (value !== undefined) fields[key] = value;
    }
    return fields;
  });
}

interface MessagesDiagnosticResponse {
  responseShape: "data-wrapper" | "direct-object" | "invalid";
  items?: unknown[];
}

function _inspectMessagesResponse(response: unknown): MessagesDiagnosticResponse {
  if (_isRecord(response) && "data" in response) {
    return Array.isArray(response.data)
      ? { responseShape: "data-wrapper", items: response.data }
      : { responseShape: "invalid" };
  }
  return Array.isArray(response)
    ? { responseShape: "direct-object", items: response }
    : { responseShape: "invalid" };
}

function _diagnoseSessionMessages(
  response: MessagesDiagnosticResponse,
  context: MetadataDiagnosticContext | undefined,
): void {
  _emitMetadataDiagnostic(context, "session_messages", () => {
    const items = response.items?.slice(0, MAX_METADATA_DIAGNOSTIC_ITEMS) ?? [];
    let assistantInfo: Record<string, unknown> | undefined;
    for (let i = items.length - 1; i >= 0; i--) {
      const item = items[i];
      if (!_isRecord(item) || !_isRecord(item.info) || item.info.role !== "assistant") continue;
      assistantInfo = item.info;
      break;
    }
    const fields: Record<string, unknown> = {
      responseShape: response.responseShape,
      itemCount: items.length,
      assistantFound: assistantInfo !== undefined,
      assistantInfoKeys: _metadataDiagnosticKeys(assistantInfo),
    };
    if (assistantInfo) {
      const mode = _safeMetadataDiagnosticString(assistantInfo.mode);
      const providerID = _safeMetadataDiagnosticString(assistantInfo.providerID);
      const modelID = _safeMetadataDiagnosticString(assistantInfo.modelID);
      if (mode) fields.mode = mode;
      if (providerID) fields.providerID = providerID;
      if (modelID) fields.modelID = modelID;
      fields.timeKeys = _metadataTimeKeys(assistantInfo.time);
      for (const key of ["variant", "reasoningEffort", "reasoning_effort"] as const) {
        const value = _metadataDiagnosticCandidate(assistantInfo[key]);
        if (value !== undefined) fields[key] = value;
      }
    }
    return fields;
  });
}

function _diagnoseOutgoingEnvelope(
  envelope: Envelope,
  context: MetadataDiagnosticContext | undefined,
): void {
  _emitMetadataDiagnostic(context, "outgoing_envelope", () => {
    const sessionName = envelope.session.name;
    const agent = _safeMetadataDiagnosticString(envelope.agent);
    const model = _safeMetadataDiagnosticString(envelope.model);
    const fields: Record<string, unknown> = {
      event: envelope.event,
      sessionNamePresent: typeof sessionName === "string" && sessionName.length > 0,
      sessionNameLength: _metadataDiagnosticLength(sessionName),
      sessionScope: envelope.session.scope,
      startedAtPresent: envelope.startedAt !== undefined,
      taskStartedAtPresent: envelope.taskStartedAt !== undefined,
      endedAtPresent: envelope.endedAt !== undefined,
      durationMsPresent: envelope.durationMs !== undefined,
      questionPresent: envelope.question !== undefined,
      permissionPresent: envelope.permission !== undefined,
      errorPresent: envelope.error !== undefined,
    };
    if (agent) fields.agent = agent;
    if (model) fields.model = model;
    return fields;
  });
}

function _safeActionScalar(raw: unknown): string | boolean | number | undefined {
  if (typeof raw === "boolean") return raw;
  if (typeof raw === "number" && Number.isFinite(raw)) return raw;
  return _sanitiseActionText(raw);
}

function _safeActionCount(raw: unknown): number | undefined {
  if (typeof raw !== "number" || !Number.isInteger(raw) || raw < 0 || raw > MAX_COUNT) {
    return undefined;
  }
  return raw;
}

function _safeTimestamp(raw: unknown): string | undefined {
  if (typeof raw === "number" && Number.isFinite(raw) && raw >= 0) {
    const date = new Date(raw);
    return Number.isFinite(date.getTime()) ? date.toISOString() : undefined;
  }
  if (typeof raw !== "string" || !raw.trim()) return undefined;
  const date = new Date(raw);
  return Number.isFinite(date.getTime()) ? date.toISOString() : undefined;
}

function _normaliseModel(raw: unknown): string | undefined {
  if (typeof raw === "string") return _sanitiseActionText(raw, MAX_AGENT_MODEL_LENGTH);
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return undefined;

  const value = raw as Record<string, unknown>;
  const nestedModel =
    value.model && typeof value.model === "object" && !Array.isArray(value.model)
      ? value.model as Record<string, unknown>
      : undefined;
  const explicitModelID =
    value.modelID ?? value.modelId ?? nestedModel?.modelID ?? nestedModel?.modelId ?? nestedModel?.id;
  const provider = _sanitiseActionText(
    value.provider ??
      value.providerID ??
      value.providerId ??
      nestedModel?.provider ??
      nestedModel?.providerID ??
      (explicitModelID !== undefined && typeof value.model === "string" ? value.model : undefined),
    MAX_AGENT_MODEL_LENGTH,
  );
  const model = _sanitiseActionText(
    explicitModelID ??
      (typeof value.model === "string" ? value.model : undefined) ??
      nestedModel?.model ??
      nestedModel?.name,
    MAX_AGENT_MODEL_LENGTH,
  );
  if (provider && model) return _sanitiseActionText(`${provider}/${model}`, MAX_AGENT_MODEL_LENGTH);
  return model ?? provider;
}

/**
 * Read only the safe assistant metadata fields from a message info object.
 * `parts` and all other message fields are deliberately never inspected.
 */
function _assistantMetadataFromInfo(raw: unknown): AssistantMetadata | undefined {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return undefined;
  const info = raw as Record<string, unknown>;
  if (info.role !== "assistant") return undefined;

  const metadata: AssistantMetadata = {};
  const agent = _sanitiseActionText(info.mode, MAX_AGENT_MODEL_LENGTH);
  const providerID = _sanitiseActionText(info.providerID, MAX_AGENT_MODEL_LENGTH);
  const modelID = _sanitiseActionText(info.modelID, MAX_AGENT_MODEL_LENGTH);
  if (agent) metadata.agent = agent;
  if (providerID) metadata.providerID = providerID;
  if (modelID) metadata.modelID = modelID;

  if (info.time && typeof info.time === "object" && !Array.isArray(info.time)) {
    const time = info.time as Record<string, unknown>;
    const created = _safeTimestamp(time.created);
    const completed = _safeTimestamp(time.completed);
    if (created) metadata.created = created;
    if (completed) metadata.completed = completed;
  }

  return Object.keys(metadata).length > 0 ? metadata : undefined;
}

/** Cache assistant metadata under the anonymous session ref only. */
function _cacheAssistantMetadata(sessionRef: string, metadata: AssistantMetadata): void {
  const safeMetadata: AssistantMetadata = {};
  const agent = _sanitiseActionText(metadata.agent, MAX_AGENT_MODEL_LENGTH);
  const providerID = _sanitiseActionText(metadata.providerID, MAX_AGENT_MODEL_LENGTH);
  const modelID = _sanitiseActionText(metadata.modelID, MAX_AGENT_MODEL_LENGTH);
  const created = _safeTimestamp(metadata.created);
  const completed = _safeTimestamp(metadata.completed);
  if (agent) safeMetadata.agent = agent;
  if (providerID) safeMetadata.providerID = providerID;
  if (modelID) safeMetadata.modelID = modelID;
  if (created) safeMetadata.created = created;
  if (completed) safeMetadata.completed = completed;
  if (Object.keys(safeMetadata).length === 0) return;

  const previous = _assistantMetadata.get(sessionRef);
  const startsNewAssistantMessage =
    created !== undefined && previous?.completed !== undefined && created !== previous.created;
  const merged: AssistantMetadata = {
    ...(previous ?? {}),
    ...safeMetadata,
  };
  if (startsNewAssistantMessage && safeMetadata.completed === undefined) {
    delete merged.completed;
  }
  _assistantMetadata.delete(sessionRef);
  _assistantMetadata.set(sessionRef, merged);
  _cleanupAssistantMetadata();
}

/** Read and refresh one assistant metadata entry in the bounded LRU. */
function _cachedAssistantMetadata(sessionRef: string): AssistantMetadata | undefined {
  const metadata = _assistantMetadata.get(sessionRef);
  if (!metadata) return undefined;
  _assistantMetadata.delete(sessionRef);
  _assistantMetadata.set(sessionRef, metadata);
  return metadata;
}

function _cleanupAssistantMetadata(): void {
  if (_assistantMetadata.size <= MAX_CACHE_ENTRIES) return;
  const entries = [..._assistantMetadata.keys()];
  for (let i = 0; i < entries.length - CACHE_RETAIN_ENTRIES; i++) {
    _assistantMetadata.delete(entries[i]!);
  }
}

function _modelFromAssistantMetadata(metadata: AssistantMetadata | undefined): string | undefined {
  if (!metadata) return undefined;
  return _normaliseModel({ providerID: metadata.providerID, modelID: metadata.modelID });
}

function _modelFromSessionData(sessionData: Record<string, unknown>): string | undefined {
  const rawModel = sessionData.model;
  const provider = sessionData.provider ?? sessionData.providerID ?? sessionData.providerId;
  const modelID = sessionData.modelID ?? sessionData.modelId;

  if (rawModel && typeof rawModel === "object" && !Array.isArray(rawModel)) {
    const model = { ...(rawModel as Record<string, unknown>) };
    if (model.provider === undefined && model.providerID === undefined && provider !== undefined) {
      model.provider = provider;
    }
    if (model.model === undefined && model.modelID === undefined && modelID !== undefined) {
      model.modelID = modelID;
    }
    return _normaliseModel(model);
  }

  if (modelID !== undefined) {
    return _normaliseModel({ provider: provider ?? (typeof rawModel === "string" ? rawModel : undefined), modelID });
  }
  if (typeof rawModel === "string" && provider !== undefined) {
    return _normaliseModel({ provider, model: rawModel });
  }
  if (rawModel !== undefined) return _normaliseModel(rawModel);
  return _normaliseModel({ provider });
}

function _applyAssistantMetadata(event: OpenCodeEvent, metadata: AssistantMetadata | undefined): void {
  if (!metadata) return;
  if (!event.agent && metadata.agent) event.agent = metadata.agent;
  if (!event.model) {
    const model = _modelFromAssistantMetadata(metadata);
    if (model) event.model = model;
  }
  if (event.taskStartedAt === undefined && metadata.created) {
    event.taskStartedAt = metadata.created;
  }
  if (event.endedAt === undefined && metadata.completed) {
    event.endedAt = metadata.completed;
  }
  const durationMs = _taskDurationMs(event.taskStartedAt, event.endedAt);
  if (durationMs !== undefined) event.durationMs = durationMs;
}

function _taskDurationMs(taskStartedAt: unknown, endedAt: unknown): number | undefined {
  const start = _safeTimestamp(taskStartedAt);
  const end = _safeTimestamp(endedAt);
  if (!start || !end) return undefined;
  const durationMs = Date.parse(end) - Date.parse(start);
  if (!Number.isInteger(durationMs) || durationMs < 0 || durationMs > MAX_DURATION_MS) {
    return undefined;
  }
  return durationMs;
}

function _normaliseCounts(raw: unknown): OpenCodeEvent["counts"] | undefined {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return undefined;
  const value = raw as Record<string, unknown>;
  const messages = _safeActionCount(value.messages ?? value.messageCount);
  const tools = _safeActionCount(value.tools ?? value.toolCount);
  const changes = _safeActionCount(value.changes ?? value.changeCount);
  if (messages === undefined && tools === undefined && changes === undefined) return undefined;
  return { messages, tools, changes };
}

// ─── Error / Permission Category Derivation ─────────────────

/**
 * Derive a safe error category and optional code from the raw
 * error object.  Never reads `error.message` or `error.responseBody`.
 */
function _deriveErrorCategory(err: NonNullable<OpenCodeEvent["error"]>): CategoryInfo {
  const raw = typeof err.name === "string" ? err.name : "unknown";
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
  const raw =
    typeof perm.type === "string"
      ? perm.type
      : typeof perm.category === "string"
        ? perm.category
        : "unknown";
  return raw.replace(/[^a-zA-Z0-9_-]/g, "_").toLowerCase().slice(0, 64) || "unknown";
}

function _normaliseQuestionItem(raw: unknown): QuestionItem | undefined {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return undefined;
  const item = raw as Record<string, unknown>;
  const result: QuestionItem = {};
  const text = _sanitiseActionText(item.question ?? item.text);
  const header = _sanitiseActionText(item.header ?? item.title);
  const recommended = _safeActionScalar(
    item.recommended ?? item.recommendation ?? item.recommendedOption,
  );
  if (text) result.text = text;
  if (header) result.header = header;
  if (recommended !== undefined) result.recommended = recommended;

  const rawOptions = Array.isArray(item.options) ? item.options : [];
  const options: QuestionOption[] = [];
  for (const rawOption of rawOptions.slice(0, MAX_ACTION_OPTIONS)) {
    if (typeof rawOption === "string") {
      const label = _sanitiseActionText(rawOption);
      if (label) options.push({ label });
      continue;
    }
    if (!rawOption || typeof rawOption !== "object" || Array.isArray(rawOption)) continue;
    const option = rawOption as Record<string, unknown>;
    const clean: QuestionOption = {};
    const label = _sanitiseActionText(option.label ?? option.name);
    const description = _sanitiseActionText(option.description);
    const optionRecommended = _safeActionScalar(
      option.recommended ?? option.recommendation ?? option.recommendedOption,
    );
    if (label) clean.label = label;
    if (description) clean.description = description;
    if (optionRecommended !== undefined) clean.recommended = optionRecommended;
    if (Object.keys(clean).length > 0) options.push(clean);
  }
  if (options.length > 0) result.options = options;

  return Object.keys(result).length > 0 ? result : undefined;
}

function _buildQuestionEnvelope(event: OpenCodeEvent, mode: ActionContentMode): QuestionEnvelope | undefined {
  const rawQuestions = Array.isArray(event.questions) ? event.questions : [];
  const items = rawQuestions
    .slice(0, MAX_ACTION_ITEMS)
    .map(_normaliseQuestionItem)
    .filter((item): item is QuestionItem => item !== undefined);

  const count = rawQuestions.length > 0 ? Math.min(rawQuestions.length, MAX_ACTION_ITEMS) : undefined;
  const optionCount = items.reduce((total, item) => total + (item.options?.length ?? 0), 0);
  if (count === undefined && items.length === 0) return undefined;

  const result: QuestionEnvelope = {
    count: count ?? items.length,
    optionCount,
  };

  const firstText = items.find((item) => item.text)?.text;
  if (mode !== "strict" && firstText) {
    result.summary = firstText;
  }
  if (mode === "full" && items.length > 0) {
    result.items = items;
  }
  return result;
}

function _buildPermissionEnvelope(
  event: OpenCodeEvent,
  mode: ActionContentMode,
): PermissionEnvelope | undefined {
  if (!event.permission) return undefined;
  const permission = event.permission;
  const result: PermissionEnvelope = { category: _derivePermissionCategory(permission) };
  if (mode === "strict") return result;

  const title = _sanitiseActionText(permission.title);
  const description = _sanitiseActionText(permission.description);
  const summary = _sanitiseActionText(permission.summary ?? title ?? description, 256);
  if (summary) result.summary = summary;
  if (mode !== "full") return result;

  if (title) result.title = title;
  if (description) result.description = description;
  const action = _sanitiseActionText(permission.action ?? permission.operation);
  const target = _sanitiseActionText(permission.target ?? permission.path);
  if (action) result.action = action;
  if (target) result.target = target;
  if (Array.isArray(permission.patterns)) {
    const patterns = permission.patterns
      .slice(0, MAX_PERMISSION_PATTERNS)
      .map((pattern) => _sanitiseActionText(pattern))
      .filter((pattern): pattern is string => pattern !== undefined);
    if (patterns.length > 0) result.patterns = patterns;
  }
  return result;
}

// ─── State Machine ──────────────────────────────────────────

const _sessions = new Map<string, SessionState>();

/** Get or initialise state for a session key (already the hashed ref). */
function _getState(sessionKey: string): SessionState {
  let st = _sessions.get(sessionKey);
  if (!st) {
    st = {
      hadBusy: false,
      sentIdle: false,
      hadErrorForCycle: false,
      cycle: 0,
      pendingEventId: undefined,
      lastAccessMs: Date.now(),
    };
    _sessions.set(sessionKey, st);
  } else {
    st.lastAccessMs = Date.now();
    // Refresh insertion order as a small LRU improvement for cleanup.
    _sessions.delete(sessionKey);
    _sessions.set(sessionKey, st);
  }
  return st;
}

function _cacheSessionScope(sessionRef: string, scope: SessionScope): void {
  if (scope !== "root" && scope !== "subagent") return;
  _sessionScopes.delete(sessionRef);
  _sessionScopes.set(sessionRef, scope);
}

function _cachedSessionScope(sessionRef: string): SessionScope | undefined {
  const scope = _sessionScopes.get(sessionRef);
  if (!scope) return undefined;
  _sessionScopes.delete(sessionRef);
  _sessionScopes.set(sessionRef, scope);
  return scope;
}

/** Bounded cleanup: remove state and scope entries that exceed their limits. */
function _cleanupSessions(): void {
  if (_sessions.size > 1000) {
    const entries = [..._sessions.entries()];
    // Keep the 500 most recently accessed (Map insertion order is refreshed above).
    for (let i = 0; i < entries.length - 500; i++) {
      _sessions.delete(entries[i]![0]);
    }
  }
  if (_sessionScopes.size > 1000) {
    const entries = [..._sessionScopes.keys()];
    for (let i = 0; i < entries.length - 500; i++) {
      _sessionScopes.delete(entries[i]!);
    }
  }
  _cleanupAssistantMetadata();
  _cleanupMetadataSampleSessions();
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

  // Events filter (default: all four)
  const eventFilter = new Set<string>();
  if (Array.isArray(raw.events) && raw.events.length > 0) {
    for (const e of raw.events) {
      if (typeof e === "string") eventFilter.add(e);
    }
  } else {
    eventFilter
      .add("session_idle")
      .add("session_error")
      .add("permission_asked")
      .add("question_asked");
  }

  const projectDisplayName =
    _sanitiseName(raw.projectDisplayName);

  const actionContentMode: ActionContentMode =
    raw.actionContentMode === "summary" || raw.actionContentMode === "full"
      ? raw.actionContentMode
      : "strict";

  const metadataDiagnostics: MetadataDiagnostics =
    raw.metadataDiagnostics === "once" || raw.metadataDiagnostics === "sample"
      ? raw.metadataDiagnostics
      : "off";

  return {
    url,
    token,
    timeoutMs,
    enabled: true,
    events: eventFilter,
    projectDisplayName,
    actionContentMode,
    metadataDiagnostics,
  };
}

// ─── Envelope Construction ──────────────────────────────────

const _SUPPORTED_INPUT_EVENTS = new Set([
  "session.status",
  "session.idle",
  "session.error",
  "permission.updated",
  "question.asked",
]);

async function _buildEnvelope(
  event: OpenCodeEvent,
  eventId: string,
  config?: Pick<ResolvedConfig, "projectDisplayName" | "actionContentMode">,
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
    session: { ref: sessionRef, scope: event.sessionScope ?? "unknown" },
  };

  const actionContentMode = config?.actionContentMode ?? "strict";

  if (config?.projectDisplayName) {
    envelope.projectDisplayName = config.projectDisplayName;
  }

  if (sessionName) {
    envelope.session.name = sessionName;
  }

  // Optional fields — only when reliably available and whitelisted
  if (typeof event.agent === "string" && event.agent.length > 0) {
    const agent = _sanitiseActionText(event.agent, MAX_AGENT_MODEL_LENGTH);
    if (agent) envelope.agent = agent;
  }
  const model = _normaliseModel(
    event.model !== undefined
      ? event.provider !== undefined && typeof event.model === "string"
        ? { provider: event.provider, model: event.model }
        : event.model
      : event.provider !== undefined
        ? { provider: event.provider }
        : undefined,
  );
  if (model) {
    envelope.model = model;
  }
  const startedAt = _safeTimestamp(event.startedAt);
  const taskStartedAt = _safeTimestamp(event.taskStartedAt);
  const endedAt = _safeTimestamp(event.endedAt);
  if (startedAt) envelope.startedAt = startedAt;
  if (taskStartedAt) envelope.taskStartedAt = taskStartedAt;
  if (endedAt) envelope.endedAt = endedAt;
  const durationMs = _taskDurationMs(taskStartedAt, endedAt);
  if (durationMs !== undefined) envelope.durationMs = durationMs;
  const counts = _normaliseCounts(event.counts);
  if (counts) {
    envelope.counts = {};
    if (counts.messages !== undefined) envelope.counts.messages = counts.messages;
    if (counts.tools !== undefined) envelope.counts.tools = counts.tools;
    if (counts.changes !== undefined) envelope.counts.changes = counts.changes;
  }

  // Event-specific fields
  if (outputEvent === "opencode.permission_asked" && event.permission) {
    const permission = _buildPermissionEnvelope(event, actionContentMode);
    if (permission) envelope.permission = permission;
  }
  if (outputEvent === "opencode.question_asked") {
    const question = _buildQuestionEnvelope(event, actionContentMode);
    if (question) envelope.question = question;
  }
  if (outputEvent === "opencode.session_error" && event.error) {
    envelope.error = _deriveErrorCategory(event.error);
  }

  // The individual action limits keep this deterministic; retain a final guard
  // so a future allowlisted field cannot accidentally create an oversized hook.
  if (_textEncoder.encode(JSON.stringify(envelope)).length > MAX_ENVELOPE_BYTES) return null;
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
    case "question.asked":
      return "opencode.question_asked";
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
  if (event.sessionScope === undefined) {
    event.sessionScope = _cachedSessionScope(sessionRef) ?? "unknown";
  }
  if (event.sessionScope === "root" || event.sessionScope === "subagent") {
    _cacheSessionScope(sessionRef, event.sessionScope);
  }

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

      const envelope = await _buildEnvelope(event, eventId, config);
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
    const envelope = await _buildEnvelope(event, eventId, config);
    if (!envelope) {
      // Rollback: no error notification was produced, so this cycle must remain
      // eligible for a later error retry or the final idle notification.
      st.hadErrorForCycle = false;
      st.sentIdle = false;
      st.pendingEventId = undefined;
      return null;
    }
    return envelope;
  }

  // --- permission.updated ---
  if (t === "permission.updated") {
    if (!config.events.has("permission_asked")) return null;
    // Permission events don't interact with the busy/idle/error state machine
    const eventId = _generateId();
    const envelope = await _buildEnvelope(event, eventId, config);
    return envelope;
  }

  // question.asked is the only question lifecycle event that requires action.
  // Its content is copied only according to the configured bounded mode.
  if (t === "question.asked") {
    if (!config.events.has("question_asked")) return null;
    const eventId = _generateId();
    const envelope = await _buildEnvelope(event, eventId, config);
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

    _diagnoseOutgoingEnvelope(
      envelope,
      _metadataDiagnosticContextForSessionRef(
        { mode: config.metadataDiagnostics, log },
        envelope.session.ref,
      ),
    );
    log.warn(`[webhook-notifier] sending ${envelope.event}`);
    await _sendWithRetry(envelope, config, log);
  } catch {
    // Last-resort safety net — should never fire
    log.error("[webhook-notifier] unexpected internal error");
  } finally {
    // Cleanup must also run for busy-only, ignored, filtered, and malformed
    // events, not only after a successful send.
    _cleanupSessions();
  }
}

// ─── Wrapper Event Normalization ─────────────────────────────

function _copyQuestionInputs(raw: unknown): QuestionInput[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const questions: QuestionInput[] = [];
  for (const rawQuestion of raw.slice(0, MAX_ACTION_ITEMS)) {
    if (!rawQuestion || typeof rawQuestion !== "object" || Array.isArray(rawQuestion)) continue;
    const value = rawQuestion as Record<string, unknown>;
    const question: QuestionInput = {
      text: value.question ?? value.text,
      header: value.header ?? value.title,
      recommended: value.recommended ?? value.recommendation ?? value.recommendedOption,
    };
    if (Array.isArray(value.options)) {
      question.options = value.options.slice(0, MAX_ACTION_OPTIONS).flatMap((rawOption) => {
        if (typeof rawOption === "string") return [{ label: rawOption }];
        if (!rawOption || typeof rawOption !== "object" || Array.isArray(rawOption)) return [];
        const option = rawOption as Record<string, unknown>;
        return [{
          label: option.label ?? option.name,
          description: option.description,
          recommended: option.recommended ?? option.recommendation ?? option.recommendedOption,
        }];
      });
    }
    questions.push(question);
  }
  return questions;
}

function _copyPermissionInput(props: Record<string, unknown>): OpenCodeEvent["permission"] {
  const permission: NonNullable<OpenCodeEvent["permission"]> = {};
  for (const key of ["type", "category", "title", "summary", "description", "action", "target", "patterns"] as const) {
    if (key in props) permission[key] = props[key];
  }
  if (permission.category === undefined && typeof props.permission === "string") {
    permission.category = props.permission;
  }
  if (permission.action === undefined && "operation" in props) {
    permission.action = props.operation;
  }
  if (permission.target === undefined && "path" in props) {
    permission.target = props.path;
  }
  return permission;
}

/**
 * Normalize an OpenCode runtime event (wrapped in { event: { id, type, properties } })
 * into our internal OpenCodeEvent shape.
 *
 * Maps the official Event properties to our flat event fields:
 * - session.status:  properties.sessionID, properties.status ({ type: "busy"|"idle"|"retry" })
 * - session.idle:    properties.sessionID
 * - session.error:   properties.sessionID?, properties.error?
 * - permission.updated / permission.asked: properties is the Permission
 * - question.asked:   properties.sessionID plus bounded allowlisted question data
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
      // properties IS the Permission object. Copy only the explicit action allowlist.
      return {
        type: "permission.updated",
        sessionId: typeof props.sessionID === "string" ? props.sessionID : undefined,
        permission: _copyPermissionInput(props),
      };
    }

    case "question.asked": {
      // Copy only bounded question text/options; cwd, token and all other
      // properties stay outside the internal event.
      return {
        type,
        sessionId: typeof props.sessionID === "string" ? props.sessionID : undefined,
        questions: _copyQuestionInputs(props.questions),
      };
    }

    default:
      return null;
  }
}

/**
 * Consume the v1.18.4 assistant message update before normalisation.  These
 * events are metadata-only: they never enter the state machine or transport.
 */
async function _consumeAssistantMetadata(
  wrapped: unknown,
  diagnostics?: MetadataDiagnosticContext,
): Promise<boolean> {
  const candidate = wrapped && typeof wrapped === "object"
    ? (wrapped as Record<string, unknown>).event
    : undefined;
  if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) return false;

  const rawEvent = candidate as Record<string, unknown>;
  if (rawEvent.type !== "message.updated") return false;

  try {
    const properties = rawEvent.properties;
    if (!properties || typeof properties !== "object" || Array.isArray(properties)) return true;
    const props = properties as Record<string, unknown>;
    const info = props.info;
    const sessionID = info && typeof info === "object" && !Array.isArray(info)
      ? (info as Record<string, unknown>).sessionID
      : undefined;
    let sessionRef: string | undefined;
    if (diagnostics?.mode === "sample" && typeof sessionID === "string" && sessionID.length > 0) {
      sessionRef = await _hashSessionRef(sessionID);
    }
    if (_isRecord(info)) {
      _diagnoseAssistantMessage(
        info,
        _metadataDiagnosticContextForSessionRef(diagnostics, sessionRef),
      );
    }
    const metadata = _assistantMetadataFromInfo(info);
    if (typeof sessionID === "string" && sessionID.length > 0 && metadata) {
      _cacheAssistantMetadata(sessionRef ?? await _hashSessionRef(sessionID), metadata);
    }
  } finally {
    // message.updated is intentionally not passed to _onEvent, but it still
    // participates in the same bounded in-memory cleanup policy.
    _cleanupSessions();
  }
  return true;
}

// ─── Session Enrichment ──────────────────────────────────────

function _deriveSessionScope(data: unknown): SessionScope {
  if (!data || typeof data !== "object" || Array.isArray(data)) return "unknown";
  const sessionData = data as Record<string, unknown>;
  if (!("parentID" in sessionData) || sessionData.parentID === undefined || sessionData.parentID === null) {
    return "root";
  }
  return typeof sessionData.parentID === "string" && sessionData.parentID.trim().length > 0
    ? "subagent"
    : "unknown";
}

/**
 * Attempt to enrich an event with safe metadata from the anonymous assistant
 * cache, session.get(), and finally the bounded session.messages fallback.
 * Every source is best-effort and never blocks notification delivery.
 */
async function _enrichEvent(
  event: OpenCodeEvent,
  input: PluginInput,
  diagnostics?: MetadataDiagnosticContext,
): Promise<void> {
  const rawSessionId = event.sessionId;
  if (!rawSessionId) return;

  const sessionRef = await _hashSessionRef(rawSessionId);
  const sessionDiagnostics = _metadataDiagnosticContextForSessionRef(diagnostics, sessionRef);
  if (event.sessionScope === undefined) {
    event.sessionScope = _cachedSessionScope(sessionRef) ?? "unknown";
  }
  const setScope = (scope: SessionScope): void => {
    event.sessionScope = scope;
    if (scope === "root" || scope === "subagent") {
      _cacheSessionScope(sessionRef, scope);
    }
  };

  // Existing event values win; cache is the first enrichment source.
  _applyAssistantMetadata(event, _cachedAssistantMetadata(sessionRef));

  let sessionData: Record<string, unknown> | undefined;
  try {
    const response = await input.client.session.get({ path: { id: rawSessionId } });
    const inspectedResponse = _inspectSessionResponse(response);
    _diagnoseSessionGet(inspectedResponse, sessionDiagnostics);
    const data = inspectedResponse.data;

    if (!data || typeof data !== "object" || Array.isArray(data)) {
      setScope("unknown");
      _log.warn(SESSION_GET_WARNING);
    } else {
      sessionData = data as Record<string, unknown>;
      setScope(_deriveSessionScope(sessionData));

      // Only fill fields not already present in the event or assistant cache.
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
      if (!event.agent) {
        const sessionAgent = _sanitiseActionText(sessionData.agent ?? sessionData.mode, MAX_AGENT_MODEL_LENGTH);
        if (sessionAgent) event.agent = sessionAgent;
      }
      if (!event.model) {
        const model = _modelFromSessionData(sessionData);
        if (model) event.model = model;
      }

      const sessionTime =
        sessionData.time && typeof sessionData.time === "object" && !Array.isArray(sessionData.time)
          ? sessionData.time as Record<string, unknown>
          : undefined;
      if (sessionTime) {
        if (event.startedAt === undefined) {
          const startedAt = _safeTimestamp(sessionTime.created);
          if (startedAt) event.startedAt = startedAt;
        }
      }

      if (!event.counts) {
        event.counts = _normaliseCounts(
          sessionData.counts ?? {
            messageCount: sessionData.messageCount,
            toolCount: sessionData.toolCount,
            changeCount: sessionData.changeCount,
          },
        );
      }
    }
  } catch {
    // Do not expose the exception, session ID, ref, title, or response body.
    _diagnoseSessionGet({ responseShape: "invalid" }, sessionDiagnostics);
    setScope("unknown");
    _log.warn(SESSION_GET_WARNING);
  }

  // Busy is a state transition only; defer the potentially heavier messages
  // fallback until an event that can actually produce a notification.
  if (event.type === "session.status" && event.status === "busy") return;

  // The SDK fallback is called at most once and only while assistant metadata
  // remains missing after event, cache, and session.get enrichment.
  if (event.agent && event.model && event.taskStartedAt && event.endedAt) return;
  const messages = input.client.session.messages;
  if (typeof messages !== "function") return;

  try {
    const response = await messages({ path: { id: rawSessionId }, query: { limit: 10 } });
    const inspectedResponse = _inspectMessagesResponse(response);
    _diagnoseSessionMessages(inspectedResponse, sessionDiagnostics);
    const items = inspectedResponse.items;
    if (!Array.isArray(items)) {
      _log.warn(SESSION_MESSAGES_WARNING);
      return;
    }

    // Only inspect info.role and the allowlisted assistant metadata fields.
    for (let i = items.length - 1; i >= 0; i--) {
      const item = items[i];
      if (!item || typeof item !== "object" || Array.isArray(item)) continue;
      const info = (item as Record<string, unknown>).info;
      if (!info || typeof info !== "object" || Array.isArray(info)) continue;
      if ((info as Record<string, unknown>).role !== "assistant") continue;

      const metadata = _assistantMetadataFromInfo(info);
      if (metadata) {
        _cacheAssistantMetadata(sessionRef, metadata);
        _applyAssistantMetadata(event, metadata);
      }
      break;
    }
  } catch {
    // Do not expose the exception, session ID, ref, title, or response body.
    _diagnoseSessionMessages({ responseShape: "invalid" }, sessionDiagnostics);
    _log.warn(SESSION_MESSAGES_WARNING);
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
      messages?(params: { path: { id: string }; query: { limit: number } }): Promise<unknown>;
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
  actionContentMode?: string;
  metadataDiagnostics?: string;
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

  const diagnostics: MetadataDiagnosticContext = {
    mode: config.metadataDiagnostics,
    log: _log,
  };

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
        // v1.18.4 assistant updates are metadata-only and must be consumed
        // before normalisation so they never enter the state machine/HTTP path.
        if (await _consumeAssistantMetadata(wrapped, diagnostics)) return;

        const normalized = _normalizeWrappedEvent(wrapped);
        if (!normalized) return;

        // Attempt session metadata enrichment from runtime
        if (normalized.sessionId) {
          await _enrichEvent(normalized, input, diagnostics);
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
  ActionContentMode,
  MetadataDiagnostics,
  SessionScope,
  ResolvedConfig,
  Envelope,
  OpenCodeEvent,
  SessionState,
  AssistantMetadata,
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
  _sessionScopes,
  _sessions,
  _cleanupSessions,
  _idleProcessing,
  _normalizeWrappedEvent,
  _consumeAssistantMetadata,
  _resetMetadataDiagnostics,
  _metadataDiagnosticSamples,
  _metadataSampleSessions,
  _assistantMetadata,
  _cacheAssistantMetadata,
  _cachedAssistantMetadata,
  _cleanupAssistantMetadata,
  _enrichEvent,
};
