/**
 * OpenCode V1 Webhook Notifier Plugin
 *
 * Single-file TypeScript Plugin for OpenCode Desktop/CLI (SDK 1.18.x).
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
  instanceDisplayName?: string;
  auxiliarySessionNames?: string[];
  actionContentMode?: string;
  metadataDiagnostics?: string;
  [key: string]: unknown;
}

type ActionContentMode = "strict" | "summary" | "full";
type MetadataDiagnostics = "off" | "once" | "sample" | "anomaly";

/** Resolved, validated configuration (no {env} / {file} placeholders). */
interface ResolvedConfig {
  url: string;
  token: string;
  timeoutMs: number;
  enabled: boolean;
  events: Set<string>;
  instanceDisplayName: string | undefined;
  auxiliarySessionNames: Set<string>;
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
  instanceDisplayName?: string;
  projectName?: string;
  agent?: string;
  model?: string;
  modelVariant?: string;
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
  subagentTimeline?: SubagentTimelineEnvelope;
  userWaitTimeline?: UserWaitTimelineEnvelope;
}

type TimelinePartialReason =
  | "missing_parent"
  | "missing_start"
  | "missing_end"
  | "invalid_parent_graph"
  | "truncated"
  | "clamped";

type TimelineItemStatus = "running" | "completed" | "failed" | "cancelled" | "unknown";
type TimelineTimingQuality = "observed" | "fallback" | "partial" | "unknown";

interface SubagentTimelineItem {
  ref: string;
  parentRef: string;
  name?: string;
  agent?: string;
  model?: string;
  modelVariant?: string;
  status: TimelineItemStatus;
  startOffsetMs?: number;
  endOffsetMs?: number;
  durationMs?: number;
  timingQuality: TimelineTimingQuality;
  depth: number;
  attempt: number;
}

interface SubagentTimelineEnvelope {
  version: 1;
  partial: boolean;
  partialReasons: TimelinePartialReason[];
  timeBasis: "root_cycle";
  observedItemCount: number;
  displayedItemCount: number;
  truncated: boolean;
  items: SubagentTimelineItem[];
}

type UserWaitKind = "question" | "permission";
type UserWaitResult = "replied" | "rejected";
type UserWaitIntervalState = "complete" | "right_censored" | "left_censored";
type UserWaitPartialReason =
  | "open_at_cycle_end"
  | "orphan_resolution"
  | "missing_request_id"
  | "evicted"
  | "truncated"
  | "clock_invalid";

interface UserWaitInterval {
  kind: UserWaitKind;
  result?: UserWaitResult;
  intervalState: UserWaitIntervalState;
  startOffsetMs?: number;
  endOffsetMs?: number;
  durationMs?: number;
}

interface UserWaitTimelineEnvelope {
  version: 1;
  partial: boolean;
  partialReasons: UserWaitPartialReason[];
  timeBasis: "root_cycle_receipt_monotonic";
  observedIntervalCount: number;
  displayedIntervalCount: number;
  truncated: boolean;
  intervals: UserWaitInterval[];
}

/**
 * Anonymous, process-local wait record. Raw session/request ids never enter
 * this map: the key is a hashed session ref + kind + hashed request key.
 */
interface UserWaitRecord {
  key: string;
  sessionRef: string;
  kind: UserWaitKind;
  requestKeyHash: string;
  askedMonoMs?: number;
  resolvedMonoMs?: number;
  result?: UserWaitResult;
  state: "pending" | "resolved";
  /** Terminal observed without a matching asked in this process lifetime. */
  orphan?: boolean;
  /** True once a successfully committed root idle reported this record. */
  reported?: boolean;
  lastAccessMonoMs: number;
}

type SessionScope = "root" | "subagent" | "auxiliary" | "unknown";

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

interface PermissionItem {
  category: string;
  title?: string;
  summary?: string;
  description?: string;
  action?: string;
  target?: string;
  patterns?: string[];
}

interface PermissionEnvelope {
  count: number;
  items: PermissionItem[];
}

/** OpenCode V1 event (a subset of the full payload that we touch). */
interface OpenCodeEvent {
  type: string;
  sessionId?: string;
  /** Official request id; only retained in local aggregation state. */
  requestId?: string;
  /** Wrapper event id used as a local fallback when request id is absent. */
  eventId?: string;
  /** Plugin receipt time; never copied into the outgoing envelope. */
  receivedAtMs?: number;
  /** Monotonic receipt time captured adjacently to receivedAtMs. */
  receivedMonoMs?: number;
  /** Raw permission reply marker (only normalised, never serialised). */
  reply?: string;
  /** Internal marker for a timing snapshot claimed by an idle cycle. */
  cycleTimingCaptured?: boolean;
  /** Internal marker that the claimed cycle timing passed validation. */
  cycleTimingReliable?: boolean;
  /** Internal anonymous parent reference populated by session enrichment. */
  timelineParentRef?: string;
  status?: string;
  session?: {
    name?: string;
    title?: string;
    time?: { created?: unknown; updated?: unknown };
  };
  sessionScope?: SessionScope;
  projectName?: string;
  agent?: string;
  model?: unknown;
  modelVariant?: unknown;
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
  questionCount?: number;
  questionOptionCount?: number;
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
  /** Epoch milliseconds when the current busy cycle began. */
  cycleStartedAtMs?: number;
  /** Monotonic milliseconds when the current busy cycle began. */
  cycleStartedMonoMs?: number;
  /**
   * True only when the busy receipt carried an explicit, valid monotonic
   * capture (hook path).  Direct/legacy callers without receivedMonoMs keep
   * wall-clock duration fallback so API/Assistant ISO timestamps are not
   * misrepresented as mono-observed.
   */
  cycleStartedMonoReliable?: boolean;
  /** Epoch milliseconds when the current cycle's idle event was received. */
  cycleEndedAtMs?: number;
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
  modelVariant?: string;
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
  /** Anonymous session ref for anomaly-mode dedup; never written to diagnostic output. */
  sessionRef?: string;
}

// ─── Constants ──────────────────────────────────────────────

const MAX_NAME_LENGTH = 200;
const MAX_AUXILIARY_SESSION_NAMES = 16;
const DEFAULT_AUXILIARY_SESSION_NAMES = new Set(["smartfetch-secondary"]);
const MAX_SESSION_REF_LENGTH = 128;
const MAX_AGENT_MODEL_LENGTH = 128;
const MAX_ACTION_TEXT_LENGTH = 512;
const MAX_ACTION_SUMMARY = 256;
const MAX_ACTION_ITEMS = 8;
const MAX_ACTION_OPTIONS = 12;
const MAX_PERMISSION_ITEMS = 16;
const MAX_PERMISSION_PATTERNS = 16;
const MAX_ENVELOPE_BYTES = 64 * 1024;
const MAX_TIMELINE_BYTES = 24 * 1024;
const MAX_TIMELINE_ITEMS = 64;
const MAX_TIMELINE_DEPTH = 8;
const TIMELINE_OVERLAP_TOLERANCE_MS = 100;
const MAX_TIMELINE_RUNS = 4096;
const RETAIN_TIMELINE_RUNS = 2048;
const MAX_TIMELINE_PARENTS = 4096;
const RETAIN_TIMELINE_PARENTS = 2048;
const MAX_TIMELINE_CAPACITY_DROPS = 256;
const RETAIN_TIMELINE_CAPACITY_DROPS = 128;
const TIMELINE_RUNNING_TTL_MS = 15 * 60 * 1000;
const TIMELINE_ENDED_TTL_MS = 60 * 60 * 1000;
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
const MAX_METADATA_DIAGNOSTIC_ANOMALIES_PER_PHASE = 32;
const MAX_METADATA_SAMPLE_SESSIONS = 1000;
const RETAIN_METADATA_SAMPLE_SESSIONS = 500;
const FALLBACK_SESSION_NAME_RE = /^OpenCode Session [0-9a-f]{12}$/;
const MAX_METADATA_DIAGNOSTIC_NUMBER = 1_000_000;
const ACTION_DEBOUNCE_MS = 150;
const MAX_ACTION_BUCKETS = 1000;
const MAX_ACTION_BUCKET_REQUESTS = 1_000_000;
const MAX_WAIT_RECORDS = 2048;
const RETAIN_WAIT_RECORDS = 1024;
const WAIT_RESOLVED_TTL_MS = 60 * 60 * 1000;
const WAIT_PENDING_TTL_MS = 24 * 60 * 60 * 1000;
const MAX_WAIT_INTERVALS = 64;
const MAX_USER_WAIT_TIMELINE_BYTES = 12 * 1024;
const MAX_WAIT_EVIDENCE_SESSIONS = 1000;
const RETAIN_WAIT_EVIDENCE_SESSIONS = 500;
const MAX_HOOK_QUEUE_DEPTH = 1024;

/**
 * Per-session FIFO for the hook's state phase (wait collector + busy/idle
 * transition + claim freeze).  Keyed by the raw session id only as a
 * transient, in-memory scheduling key (identical to `_actionBuckets`); it
 * never enters the wait map, envelope, logs, or any serialised state.
 * Enrichment and network sends run after the queue so they never block state
 * ordering, and an exception in one task can never stall later tasks.
 */
const _hookQueueTails = new Map<string, Promise<void>>();
const _hookQueueDepth = new Map<string, number>();

/** Test-only delay hook fired at the start of the wait collectors. */
let _waitCollectorTestDelay: (() => Promise<void>) | undefined;

function _setWaitCollectorTestDelay(delay?: () => Promise<void>): void {
  _waitCollectorTestDelay = delay;
}

/** One-shot test delay consumed by the first collector call only. */
async function _consumeWaitCollectorTestDelay(): Promise<void> {
  if (!_waitCollectorTestDelay) return;
  const delay = _waitCollectorTestDelay;
  _waitCollectorTestDelay = undefined;
  await delay();
}

function _enqueueSessionState(sessionId: string, task: () => Promise<void>): Promise<void> {
  const depth = _hookQueueDepth.get(sessionId) ?? 0;
  if (depth >= MAX_HOOK_QUEUE_DEPTH) {
    // Fail-closed saturation: never bypass the queue out of order (that would
    // let a later event's collector/claim jump ahead of an earlier one).  The
    // state phase is dropped, and because we cannot safely derive the
    // anonymous session ref here, we set the process-lifetime fail-closed
    // overflow flag so no future timeline can ever claim a "reliable zero".
    _waitEvidenceOverflow = true;
    _log.warn("[webhook-notifier] hook state queue saturated; dropped state phase (fail-closed)");
    return Promise.resolve();
  }
  _hookQueueDepth.set(sessionId, depth + 1);
  const previous = _hookQueueTails.get(sessionId) ?? Promise.resolve();
  const next = previous.then(task, task);
  _hookQueueTails.set(sessionId, next);
  const settle = (): void => {
    const remaining = (_hookQueueDepth.get(sessionId) ?? 1) - 1;
    if (remaining <= 0) {
      _hookQueueDepth.delete(sessionId);
      _hookQueueTails.delete(sessionId);
    } else {
      _hookQueueDepth.set(sessionId, remaining);
    }
  };
  next.then(settle, settle);
  return next;
}

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
const _metadataDiagnosticAnomalyCounts = new Map<MetadataDiagnosticPhase, number>();
const _metadataDiagnosticAnomalySeen = new Set<string>();
const _metadataSampleSessions = new Map<string, MetadataSampleSessionState>();
let _nextMetadataSampleSession = 1;

/** Test-only reset; production code intentionally never resets this set. */
function _resetMetadataDiagnostics(): void {
  _metadataDiagnosticPhases.clear();
  _metadataDiagnosticSamples.clear();
  _metadataDiagnosticAnomalyCounts.clear();
  _metadataDiagnosticAnomalySeen.clear();
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

/**
 * Safe Session metadata retained between events.  Populated from official
 * `session.created` / `session.updated` events (whose `properties.info` is a
 * full Session) and refreshed on successful `session.get`.  Keyed only by the
 * anonymous session ref so no raw session id, directory, projectID, or raw
 * parentID is ever retained.  Serves as a stable fallback when a later
 * `session.get` transiently fails or returns an SDK error result.
 */
interface SessionMetadataCacheEntry {
  /** Sanitised session name/title. */
  name?: string;
  /** Derived scope; only reliable scopes (root/subagent/auxiliary) are stored. */
  scope?: SessionScope;
  /** Available session created time as a safe ISO timestamp. */
  startedAt?: string;
}

/** Bounded LRU of safe Session metadata keyed by the anonymous session ref. */
const _sessionMetadata = new Map<string, SessionMetadataCacheEntry>();

interface TimelineRun {
  key: string;
  ref: string;
  parentRef?: string;
  /** Internal anonymous ownership anchor; never serialized in an envelope. */
  rootOwnership?: TimelineRootOwnership;
  /** Whether ownership is confirmed by a compatible parent chain or provisional. */
  rootOwnershipQuality?: TimelineOwnershipQuality;
  scope: SessionScope;
  name?: string;
  agent?: string;
  model?: string;
  modelVariant?: string;
  status: TimelineItemStatus;
  startMs?: number;
  endMs?: number;
  startQuality?: "observed" | "fallback";
  endQuality?: "observed" | "fallback";
  cycle: number;
  attempt: number;
  lastAccessMs: number;
  consumed?: boolean;
}

interface TimelineRootOwnership {
  rootRef: string;
  rootCycle: number;
  rootRunKey: string;
}

type TimelineOwnershipQuality = "confirmed" | "provisional";

interface TimelineCapacityDrop {
  key: string;
  ref: string;
  cycle: number;
  parentRef?: string;
  /** Internal anonymous ownership anchor; never serialized in an envelope. */
  rootOwnership?: TimelineRootOwnership;
  rootOwnershipQuality?: TimelineOwnershipQuality;
  scope: SessionScope;
  startMs?: number;
  lastAccessMs: number;
}

/** Timeline state is anonymous and process-local; no raw session IDs are kept. */
const _timelineRuns = new Map<string, TimelineRun>();
/** Parent hints are keyed by the exact run key, never by mutable session ref. */
const _timelineParents = new Map<string, string>();
/** Bounded markers for runs rejected only because the collector was at capacity. */
const _timelineCapacityDrops = new Map<string, TimelineCapacityDrop>();

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

/**
 * Production timing source. Tests may replace it temporarily without
 * changing the production default of Date.now().
 */
let _clock: () => number = () => Date.now();

function _nowMs(): number {
  const now = _clock();
  return Number.isFinite(now) && now >= 0 ? now : Date.now();
}

function _setClockForTests(clock?: () => number): void {
  _clock = clock ?? (() => Date.now());
  // Legacy wall-only test helpers expect both axes to move together when a
  // value is supplied; distinct mono control remains available through
  // _setMonoClockForTests / _setClocksForTests.
  _monoClock = clock ?? (() => performance.now());
}

/**
 * Monotonic timing source for user-wait durations/offsets, the root-cycle
 * axis of userWaitTimeline, and the wait-map TTL/LRU.  It is deliberately
 * separate from the wall clock: wall-clock jumps (NTP, manual changes) must
 * never corrupt wait intervals.  In production it defaults to
 * performance.now() which is monotonic within a process.
 */
let _monoClock: () => number = () => performance.now();

function _nowMonoMs(): number {
  const now = _monoClock();
  if (!Number.isFinite(now) || now < 0 || now > Number.MAX_SAFE_INTEGER) return performance.now();
  return now;
}

function _setMonoClockForTests(clock?: () => number): void {
  _monoClock = clock ?? (() => performance.now());
}

/** Convenience for tests that need to control both clocks together. */
function _setClocksForTests(wall?: () => number, mono?: () => number): void {
  _setClockForTests(wall);
  _setMonoClockForTests(mono);
}

function _safeEpochMs(raw: unknown): number | undefined {
  if (typeof raw !== "number" || !Number.isFinite(raw) || raw < 0) return undefined;
  return raw;
}

/**
 * Validate a mono value for wire use: finite, non-negative, and bounded to a
 * safe integer.  Raw fractional values are kept for comparisons/intersection;
 * only the final wire offsets/durations are integerised with Math.round.
 */
function _safeMonoValue(raw: unknown): number | undefined {
  if (typeof raw !== "number" || !Number.isFinite(raw) || raw < 0) return undefined;
  if (raw > Number.MAX_SAFE_INTEGER) return undefined;
  return raw;
}

/** Integerise a bounded, non-negative mono delta for the wire contract. */
function _intMonoMs(value: number): number {
  const rounded = Math.round(value);
  return Number.isSafeInteger(rounded) && rounded >= 0 ? rounded : 0;
}

function _timelineRunKey(sessionRef: string, cycle: number): string {
  return `${sessionRef}:${cycle}`;
}

function _timelineRootOwnership(rootRef: string, rootCycle: number): TimelineRootOwnership {
  return {
    rootRef,
    rootCycle,
    rootRunKey: _timelineRunKey(rootRef, rootCycle),
  };
}

function _timelineOwnershipForRun(run: TimelineRun): TimelineRootOwnership | undefined {
  if (run.scope === "root") return _timelineRootOwnership(run.ref, run.cycle);
  return run.rootOwnership;
}

function _timelineOwnershipQualityForRun(run: TimelineRun): TimelineOwnershipQuality {
  if (run.scope === "root") return "confirmed";
  return run.rootOwnershipQuality ?? "provisional";
}

function _timelineOwnershipMatchesTarget(
  ownership: TimelineRootOwnership | undefined,
  rootRunKey: string,
): "target" | "unrelated" | "unknown" {
  if (!ownership) return "unknown";
  return ownership.rootRunKey === rootRunKey ? "target" : "unrelated";
}

function _timelinePartialIntervalsOverlap(
  aStartMs: number | undefined,
  aEndMs: number | undefined,
  bStartMs: number | undefined,
  bEndMs: number | undefined,
): boolean {
  if (aStartMs === undefined && aEndMs === undefined) return false;
  if (bStartMs === undefined && bEndMs === undefined) return false;
  if (aEndMs !== undefined && bStartMs !== undefined && aEndMs <= bStartMs) return false;
  if (bEndMs !== undefined && aStartMs !== undefined && bEndMs <= aStartMs) return false;
  return true;
}

interface TimelineParentOwnershipResolution {
  ownership: TimelineRootOwnership;
  quality: TimelineOwnershipQuality;
}

/**
 * A parent run is compatible only when its own interval can belong to the
 * child's cycle.  Once a parent has an ownership anchor, the anchored root
 * interval is an additional cycle guard: a stale running/unenriched parent
 * from an older root cycle must not remain a candidate merely because its
 * end time is missing.
 */
function _timelineParentCandidateQuality(
  run: TimelineRun,
  ownership: TimelineRootOwnership,
  childStartMs: number | undefined,
  childEndMs: number | undefined,
): TimelineOwnershipQuality | undefined {
  const hasParentTiming = run.startMs !== undefined || run.endMs !== undefined;
  const hasChildTiming = childStartMs !== undefined || childEndMs !== undefined;

  if (hasParentTiming || hasChildTiming) {
    if (!_timelinePartialIntervalsOverlap(run.startMs, run.endMs, childStartMs, childEndMs)) {
      return undefined;
    }

    const rootRun = _timelineRuns.get(ownership.rootRunKey);
    if (rootRun && hasChildTiming
      && !_timelinePartialIntervalsOverlap(rootRun.startMs, rootRun.endMs, childStartMs, childEndMs)) {
      return undefined;
    }
  }

  const candidateQuality = _timelineOwnershipQualityForRun(run);
  if (run.scope === "root" || candidateQuality === "confirmed") return "confirmed";
  return "provisional";
}

function _timelineParentOwnership(
  parentRef: string,
  childStartMs: number | undefined,
  childEndMs: number | undefined,
): TimelineParentOwnershipResolution | undefined {
  const candidates = _timelineRunsForRef(parentRef)
    .map((run) => ({ run, ownership: _timelineOwnershipForRun(run) }))
    .filter((candidate): candidate is { run: TimelineRun; ownership: TimelineRootOwnership } => candidate.ownership !== undefined)
    .flatMap((candidate) => {
      const quality = _timelineParentCandidateQuality(
        candidate.run,
        candidate.ownership,
        childStartMs,
        childEndMs,
      );
      return quality ? [{ ...candidate, quality }] : [];
    });
  if (candidates.length === 0) return undefined;

  candidates.sort((a, b) => {
    const aQuality = a.quality === "confirmed" ? 1 : 0;
    const bQuality = b.quality === "confirmed" ? 1 : 0;
    return bQuality - aQuality
      || (b.run.startMs ?? Number.NEGATIVE_INFINITY) - (a.run.startMs ?? Number.NEGATIVE_INFINITY)
      || b.run.cycle - a.run.cycle;
  });
  const selected = candidates[0]!;
  return { ownership: selected.ownership, quality: selected.quality };
}

function _timelineOwnershipCompatibleWithRun(
  ownership: TimelineRootOwnership,
  run: TimelineRun,
): boolean | undefined {
  const rootRun = _timelineRuns.get(ownership.rootRunKey);
  if (!rootRun) return undefined;
  if (run.startMs === undefined && run.endMs === undefined) return undefined;
  return _timelinePartialIntervalsOverlap(rootRun.startMs, rootRun.endMs, run.startMs, run.endMs);
}

function _adoptTimelineOwnership(
  run: TimelineRun,
  ownership: TimelineRootOwnership,
  quality: TimelineOwnershipQuality,
): boolean {
  const existing = run.rootOwnership;
  if (!existing) {
    run.rootOwnership = ownership;
    run.rootOwnershipQuality = quality;
    return true;
  }

  if (existing.rootRunKey === ownership.rootRunKey) {
    if (quality === "confirmed" || run.rootOwnershipQuality === undefined) {
      run.rootOwnershipQuality = quality;
    }
    return true;
  }

  const existingQuality = run.rootOwnershipQuality ?? "provisional";
  if (existingQuality === "confirmed") return false;
  if (quality === "confirmed") {
    run.rootOwnership = ownership;
    run.rootOwnershipQuality = quality;
    return true;
  }

  // Two provisional hints are not ordered by arrival alone.  A stale hint
  // whose anchored root interval is already disjoint may be replaced by the
  // newer compatible hint; otherwise remain unresolved rather than guessing.
  if (_timelineOwnershipCompatibleWithRun(existing, run) === false) {
    run.rootOwnership = ownership;
    run.rootOwnershipQuality = quality;
    return true;
  }
  return false;
}

function _propagateTimelineOwnership(
  parentRef: string,
  ownership: TimelineRootOwnership,
  quality: TimelineOwnershipQuality,
  parentStartMs?: number,
  parentEndMs?: number,
): void {
  const visited = new Set<string>();
  const visit = (
    ancestorRef: string,
    ancestorStartMs: number | undefined,
    ancestorEndMs: number | undefined,
  ): void => {
    if (visited.has(ancestorRef)) return;
    visited.add(ancestorRef);
    for (const run of [..._timelineRuns.values()]) {
      if (run.parentRef !== ancestorRef || run.scope === "root" || run.scope === "auxiliary") continue;
      if (!_timelinePartialIntervalsOverlap(ancestorStartMs, ancestorEndMs, run.startMs, run.endMs)) continue;
      const adopted = _adoptTimelineOwnership(run, ownership, quality);
      if (!adopted && run.rootOwnership?.rootRunKey !== ownership.rootRunKey) continue;
      _timelineRefreshRun(run);
      visit(run.ref, run.startMs, run.endMs);
    }
    for (const drop of [..._timelineCapacityDrops.values()]) {
      if (drop.parentRef !== ancestorRef || drop.scope === "root" || drop.scope === "auxiliary") continue;
      if (!_timelinePartialIntervalsOverlap(ancestorStartMs, ancestorEndMs, drop.startMs, undefined)) continue;
      if (!drop.rootOwnership
        || drop.rootOwnership.rootRunKey === ownership.rootRunKey
        || (drop.rootOwnershipQuality !== "confirmed" && quality === "confirmed")) {
        drop.rootOwnership = ownership;
        drop.rootOwnershipQuality = quality;
      } else if (drop.rootOwnership.rootRunKey !== ownership.rootRunKey) {
        continue;
      }
    }
  };
  visit(parentRef, parentStartMs, parentEndMs);
}

function _timelineRefreshRun(run: TimelineRun): void {
  run.lastAccessMs = _nowMs();
  _timelineRuns.delete(run.key);
  _timelineRuns.set(run.key, run);
}

function _timelineCapacityAvailable(): boolean {
  if (_timelineRuns.size < MAX_TIMELINE_RUNS) return true;
  _cleanupTimelineRuns(true);
  return _timelineRuns.size < MAX_TIMELINE_RUNS;
}

function _recordTimelineCapacityDrop(sessionRef: string, cycle: number, startMs?: number): void {
  const key = _timelineRunKey(sessionRef, cycle);
  const existing = _timelineCapacityDrops.get(key);
  const marker: TimelineCapacityDrop = existing ?? {
    key,
    ref: sessionRef,
    cycle,
    scope: _cachedSessionScope(sessionRef) ?? "unknown",
    lastAccessMs: _nowMs(),
  };
  if (marker.scope === "root") {
    marker.rootOwnership = _timelineRootOwnership(sessionRef, cycle);
    marker.rootOwnershipQuality = "confirmed";
  }
  if (startMs !== undefined) marker.startMs = startMs;
  marker.lastAccessMs = _nowMs();
  _timelineCapacityDrops.delete(key);
  _timelineCapacityDrops.set(key, marker);
  if (_timelineCapacityDrops.size > MAX_TIMELINE_CAPACITY_DROPS) {
    const entries = [..._timelineCapacityDrops.keys()];
    for (const oldKey of entries.slice(0, Math.max(0, entries.length - RETAIN_TIMELINE_CAPACITY_DROPS))) {
      _timelineCapacityDrops.delete(oldKey);
    }
  }
}

function _timelineRunFor(sessionRef: string, cycle: number): TimelineRun | undefined {
  const run = _timelineRuns.get(_timelineRunKey(sessionRef, cycle));
  if (run) _timelineRefreshRun(run);
  return run;
}

function _timelineRunForCurrentCycle(sessionRef: string, cycle?: number): TimelineRun | undefined {
  const resolvedCycle = cycle ?? _sessions.get(sessionRef)?.cycle;
  return resolvedCycle && resolvedCycle > 0 ? _timelineRunFor(sessionRef, resolvedCycle) : undefined;
}

function _startTimelineRun(sessionRef: string, cycle: number, startMs: number): void {
  const key = _timelineRunKey(sessionRef, cycle);
  if (_timelineRuns.has(key)) return;
  if (!_timelineCapacityAvailable()) {
    _recordTimelineCapacityDrop(sessionRef, cycle, startMs);
    return;
  }
  const scope = _cachedSessionScope(sessionRef) ?? "unknown";
  _timelineRuns.set(key, {
    key,
    ref: sessionRef,
    scope,
    rootOwnership: scope === "root" ? _timelineRootOwnership(sessionRef, cycle) : undefined,
    rootOwnershipQuality: scope === "root" ? "confirmed" : undefined,
    status: "running",
    startMs,
    startQuality: "observed",
    cycle,
    attempt: cycle,
    lastAccessMs: _nowMs(),
  });
}

function _updateTimelineIdentity(
  sessionRef: string,
  event: OpenCodeEvent,
  parentKnown: boolean,
  parentRef?: string,
  cycle?: number,
): void {
  const resolvedCycle = cycle ?? _sessions.get(sessionRef)?.cycle;
  const run = resolvedCycle && resolvedCycle > 0
    ? _timelineRunFor(sessionRef, resolvedCycle)
    : undefined;
  const dropped = resolvedCycle && resolvedCycle > 0
    ? _timelineCapacityDrops.get(_timelineRunKey(sessionRef, resolvedCycle))
    : undefined;
  if (!run && !dropped) return;

  if (run && event.sessionScope && event.sessionScope !== "unknown") {
    run.scope = event.sessionScope;
  }
  const name = _sanitiseName(event.session?.name ?? event.session?.title);
  if (run && name) run.name = name;
  const agent = _sanitiseActionText(event.agent, MAX_AGENT_MODEL_LENGTH);
  if (run && agent) run.agent = agent;
  const model = _normaliseModel(
    event.model !== undefined
      ? event.provider !== undefined && typeof event.model === "string"
        ? { provider: event.provider, model: event.model }
        : event.model
      : event.provider !== undefined
        ? { provider: event.provider }
        : undefined,
  );
  if (run && model) run.model = model;
  const modelVariant = _sanitiseActionText(event.modelVariant, MAX_AGENT_MODEL_LENGTH);
  if (run && modelVariant) run.modelVariant = modelVariant;

  if (dropped && event.sessionScope && event.sessionScope !== "unknown") {
    dropped.scope = event.sessionScope;
  }

  if (parentKnown) {
    if (parentRef) {
      if (run) run.parentRef = parentRef;
      if (dropped) dropped.parentRef = parentRef;
      if (run) {
        _timelineParents.delete(run.key);
        _timelineParents.set(run.key, parentRef);
      }
    } else {
      if (run) {
        delete run.parentRef;
        _timelineParents.delete(run.key);
      }
    }
  } else if (run && !run.parentRef) {
    const cachedParentRef = _timelineParents.get(run.key);
    if (cachedParentRef) run.parentRef = cachedParentRef;
  }

  const currentScope = run?.scope ?? dropped?.scope;
  if (currentScope === "root") {
    const ownership = _timelineRootOwnership(sessionRef, resolvedCycle!);
    if (run) {
      run.rootOwnership = ownership;
      run.rootOwnershipQuality = "confirmed";
    }
    if (dropped) {
      dropped.rootOwnership = ownership;
      dropped.rootOwnershipQuality = "confirmed";
    }
    _propagateTimelineOwnership(
      sessionRef,
      ownership,
      "confirmed",
      run?.startMs ?? dropped?.startMs,
      run?.endMs,
    );
  } else if (currentScope === "subagent" && parentRef) {
    const childStartMs = run?.startMs ?? dropped?.startMs;
    const childEndMs = run?.endMs;
    const resolution = _timelineParentOwnership(parentRef, childStartMs, childEndMs);
    if (resolution) {
      const runAdopted = run
        ? _adoptTimelineOwnership(run, resolution.ownership, resolution.quality)
        : true;
      const dropExisting = dropped?.rootOwnership;
      const dropAdopted = !dropped || !dropExisting
        || dropExisting.rootRunKey === resolution.ownership.rootRunKey
        || (dropped.rootOwnershipQuality !== "confirmed" && resolution.quality === "confirmed");
      if (dropped && dropAdopted) {
        dropped.rootOwnership = resolution.ownership;
        dropped.rootOwnershipQuality = resolution.quality;
      }
      if (runAdopted && dropAdopted) {
        _propagateTimelineOwnership(
          sessionRef,
          resolution.ownership,
          resolution.quality,
          run?.startMs ?? dropped?.startMs,
          run?.endMs,
        );
      }
    }
  } else if (parentKnown && currentScope !== "subagent") {
    if (run) {
      delete run.rootOwnership;
      delete run.rootOwnershipQuality;
    }
    if (dropped) {
      delete dropped.rootOwnership;
      delete dropped.rootOwnershipQuality;
    }
  }
  if (run) _timelineRefreshRun(run);
  if (dropped) {
    dropped.lastAccessMs = _nowMs();
    _timelineCapacityDrops.delete(dropped.key);
    _timelineCapacityDrops.set(dropped.key, dropped);
  }
}

function _updateTimelineTimingFromEvent(sessionRef: string, event: OpenCodeEvent, cycle?: number): void {
  const run = _timelineRunForCurrentCycle(sessionRef, cycle);
  if (!run) return;

  const allowFallback = event.cycleTimingReliable !== true;
  const startMs = allowFallback
    ? _safeEpochMs(event.taskStartedAt !== undefined ? Date.parse(String(event.taskStartedAt)) : undefined)
    : undefined;
  const endMs = allowFallback
    ? _safeEpochMs(event.endedAt !== undefined ? Date.parse(String(event.endedAt)) : undefined)
    : undefined;
  if (run.startMs === undefined && startMs !== undefined) {
    run.startMs = startMs;
    run.startQuality = "fallback";
  }
  if (run.endMs === undefined && endMs !== undefined) {
    run.endMs = endMs;
    run.endQuality = "fallback";
  }
  _timelineRefreshRun(run);
}

function _finishTimelineRun(
  sessionRef: string,
  cycle: number,
  status: TimelineItemStatus,
  endMs: number | undefined,
): void {
  const run = _timelineRunFor(sessionRef, cycle);
  if (!run) return;
  run.status = status;
  if (endMs !== undefined) {
    run.endMs = endMs;
    run.endQuality = "observed";
  }
  _timelineRefreshRun(run);
}

function _rollbackTimelineEnd(sessionRef: string, cycle: number, endMs: number): void {
  const run = _timelineRunFor(sessionRef, cycle);
  if (!run || run.endQuality !== "observed" || run.endMs !== endMs) return;
  run.endMs = undefined;
  run.endQuality = undefined;
  run.status = "running";
  _timelineRefreshRun(run);
}

function _rollbackTimelineFailure(sessionRef: string, cycle: number, endMs: number): void {
  const run = _timelineRunFor(sessionRef, cycle);
  if (!run || run.endQuality !== "observed" || run.endMs !== endMs) return;
  run.endMs = undefined;
  run.endQuality = undefined;
  run.status = "running";
  _timelineRefreshRun(run);
}

function _timelineIntervalsOverlap(
  startMs: number | undefined,
  endMs: number | undefined,
  rootStartMs: number,
  rootEndMs: number,
): boolean {
  if (startMs === undefined && endMs === undefined) return false;
  if (startMs !== undefined && endMs !== undefined) {
    // Complete intervals use true half-open overlap.  Tolerance is reserved
    // for events missing one endpoint and must never turn zero overlap into
    // a fabricated slice of the root cycle.
    return startMs < rootEndMs && rootStartMs < endMs;
  }
  if (startMs !== undefined) return startMs < rootEndMs + TIMELINE_OVERLAP_TOLERANCE_MS;
  return rootStartMs < (endMs as number) + TIMELINE_OVERLAP_TOLERANCE_MS;
}

function _timelineRunsForRef(ref: string): TimelineRun[] {
  return [..._timelineRuns.values()]
    .filter((run) => run.ref === ref)
    .sort((a, b) => {
      const aStart = a.startMs ?? Number.NEGATIVE_INFINITY;
      const bStart = b.startMs ?? Number.NEGATIVE_INFINITY;
      return bStart - aStart || b.cycle - a.cycle;
    });
}

interface TimelineParentResolution {
  relation: "belongs_to_target" | "unrelated_root" | "broken";
  ownership: "target" | "unrelated" | "unknown";
  depth?: number;
  reason?: "missing_parent" | "invalid_parent_graph";
}

function _resolveTimelineParentChain(
  run: TimelineRun,
  rootRef: string,
  rootCycle: number,
  rootRunKey: string,
  rootStartMs: number,
  rootEndMs: number,
): TimelineParentResolution {
  const resolve = (current: TimelineRun, distance: number, visited: Set<string>): TimelineParentResolution => {
    if (current.key === rootRunKey || (current.ref === rootRef && current.cycle === rootCycle)) {
      return { relation: "belongs_to_target", ownership: "target", depth: distance };
    }
    if (current.scope === "root") return { relation: "unrelated_root", ownership: "unrelated" };

    const currentOwnership = _timelineOwnershipMatchesTarget(current.rootOwnership, rootRunKey);
    if (currentOwnership === "unrelated") {
      return { relation: "unrelated_root", ownership: "unrelated" };
    }
    if (distance >= MAX_TIMELINE_DEPTH) {
      return {
        relation: "broken",
        ownership: currentOwnership,
        reason: currentOwnership === "target" ? "invalid_parent_graph" : undefined,
      };
    }
    if (visited.has(current.key)) {
      return {
        relation: "broken",
        ownership: currentOwnership,
        reason: currentOwnership === "target" ? "invalid_parent_graph" : undefined,
      };
    }
    visited.add(current.key);

    const parentRef = current.parentRef;
    if (!parentRef) {
      return {
        relation: "broken",
        ownership: currentOwnership,
        reason: currentOwnership === "target" ? "missing_parent" : undefined,
      };
    }

    // A direct parentRef is an explicit session anchor.  The frozen root
    // interval is only used to choose the target cycle, never to infer an
    // owner for an otherwise unknown parent chain.
    if (parentRef === rootRef && _timelineIntervalsOverlap(
      current.startMs,
      current.endMs,
      rootStartMs,
      rootEndMs,
    )) {
      return { relation: "belongs_to_target", ownership: "target", depth: distance + 1 };
    }
    if (parentRef === rootRef && currentOwnership === "target") {
      return { relation: "belongs_to_target", ownership: "target", depth: distance + 1 };
    }
    if (parentRef !== rootRef && _cachedSessionScope(parentRef) === "root") {
      return { relation: "unrelated_root", ownership: "unrelated" };
    }

    const allParents = _timelineRunsForRef(parentRef);
    const parents = allParents.filter((candidate) => {
      const ownership = _timelineOwnershipMatchesTarget(_timelineOwnershipForRun(candidate), rootRunKey);
      return ownership !== "unknown" || _timelineIntervalsOverlap(
        candidate.startMs,
        candidate.endMs,
        rootStartMs,
        rootEndMs,
      );
    });
    if (parents.length === 0) {
      const knownOwnerships = allParents.map((candidate) => _timelineOwnershipMatchesTarget(
        _timelineOwnershipForRun(candidate),
        rootRunKey,
      ));
      if (knownOwnerships.includes("target")) {
        return {
          relation: "broken",
          ownership: "target",
          reason: "missing_parent",
        };
      }
      if (knownOwnerships.includes("unrelated") || allParents.some((candidate) => candidate.scope === "root")) {
        return { relation: "unrelated_root", ownership: "unrelated" };
      }
      return {
        relation: "broken",
        ownership: currentOwnership,
        reason: currentOwnership === "target" ? "missing_parent" : undefined,
      };
    }

    let sawTargetBroken: "missing_parent" | "invalid_parent_graph" | undefined;
    let sawUnrelated = false;
    for (const parent of parents) {
      const result = resolve(parent, distance + 1, new Set(visited));
      if (result.relation === "belongs_to_target") return result;
      if (result.ownership === "target" && result.reason) sawTargetBroken = result.reason;
      if (result.relation === "unrelated_root") sawUnrelated = true;
    }
    if (sawTargetBroken) return { relation: "broken", ownership: "target", reason: sawTargetBroken };
    if (sawUnrelated) return { relation: "unrelated_root", ownership: "unrelated" };
    return { relation: "broken", ownership: currentOwnership };
  };

  return resolve(run, 0, new Set<string>());
}

function _timelineQuality(
  run: TimelineRun,
  startOffsetMs: number | undefined,
  endOffsetMs: number | undefined,
  clamped: boolean,
): TimelineTimingQuality {
  if (clamped) return "partial";
  if (startOffsetMs === undefined && endOffsetMs === undefined) return "unknown";
  if (startOffsetMs === undefined || endOffsetMs === undefined) return "partial";
  if (run.startQuality === "fallback" || run.endQuality === "fallback") return "fallback";
  return "observed";
}

function _timelineItem(
  run: TimelineRun,
  depth: number,
  rootStartMs: number,
  rootDurationMs: number,
  reasons: Set<TimelinePartialReason>,
): SubagentTimelineItem {
  const rawStartOffset = run.startMs === undefined ? undefined : run.startMs - rootStartMs;
  const rawEndOffset = run.endMs === undefined ? undefined : run.endMs - rootStartMs;
  const clamp = (value: number): number => Math.min(rootDurationMs, Math.max(0, value));
  const startOffsetMs = rawStartOffset === undefined ? undefined : clamp(rawStartOffset);
  const endOffsetMs = rawEndOffset === undefined ? undefined : clamp(rawEndOffset);
  const clamped = (rawStartOffset !== undefined && startOffsetMs !== rawStartOffset)
    || (rawEndOffset !== undefined && endOffsetMs !== rawEndOffset);
  if (run.startMs === undefined) reasons.add("missing_start");
  if (run.endMs === undefined) reasons.add("missing_end");
  if (clamped) reasons.add("clamped");

  const item: SubagentTimelineItem = {
    ref: run.ref,
    parentRef: run.parentRef ?? "",
    status: run.status,
    timingQuality: _timelineQuality(run, startOffsetMs, endOffsetMs, clamped),
    depth,
    attempt: run.attempt,
  };
  if (run.name) item.name = run.name;
  if (run.agent) item.agent = run.agent;
  if (run.model) item.model = run.model;
  if (run.modelVariant) item.modelVariant = run.modelVariant;
  if (startOffsetMs !== undefined) item.startOffsetMs = startOffsetMs;
  if (endOffsetMs !== undefined) item.endOffsetMs = endOffsetMs;
  if (!clamped && startOffsetMs !== undefined && endOffsetMs !== undefined) {
    item.durationMs = Math.max(0, endOffsetMs - startOffsetMs);
  }
  return item;
}

function _timelineReasonList(reasons: Set<TimelinePartialReason>): TimelinePartialReason[] {
  const order: TimelinePartialReason[] = [
    "missing_parent",
    "missing_start",
    "missing_end",
    "invalid_parent_graph",
    "truncated",
    "clamped",
  ];
  return order.filter((reason) => reasons.has(reason));
}

interface TimelineBuildResult {
  timeline: SubagentTimelineEnvelope;
  runKeys: string[];
  capacityDropKeys: string[];
}

function _timelineCapacityDropOwnership(
  drop: TimelineCapacityDrop,
  rootRef: string,
  rootCycle: number,
  rootRunKey: string,
  rootStartMs: number,
  rootEndMs: number,
): "target" | "unrelated" | "unknown" {
  if (drop.key === rootRunKey || (drop.ref === rootRef && drop.cycle === rootCycle)) return "target";
  const anchoredOwnership = _timelineOwnershipMatchesTarget(drop.rootOwnership, rootRunKey);
  if (anchoredOwnership !== "unknown") return anchoredOwnership;
  if (drop.scope === "root" || drop.scope === "auxiliary") return "unrelated";
  if (!drop.parentRef) return "unknown";
  const synthetic: TimelineRun = {
    key: drop.key,
    ref: drop.ref,
    parentRef: drop.parentRef,
    scope: drop.scope,
    status: "unknown",
    startMs: drop.startMs,
    cycle: drop.cycle,
    attempt: drop.cycle,
    lastAccessMs: drop.lastAccessMs,
  };
  const resolution = _resolveTimelineParentChain(
    synthetic,
    rootRef,
    rootCycle,
    rootRunKey,
    rootStartMs,
    rootEndMs,
  );
  return resolution.ownership;
}

function _collectSubagentTimeline(
  rootRef: string,
  rootCycle: number,
  rootStartMs: number,
  rootEndMs: number,
  rootRunKey = _timelineRunKey(rootRef, rootCycle),
): TimelineBuildResult {
  const rootDurationMs = Math.max(0, rootEndMs - rootStartMs);
  const reasons = new Set<TimelinePartialReason>();
  const candidates: Array<{ run: TimelineRun; depth: number }> = [];
  const runKeys: string[] = [];
  const capacityDropKeys: string[] = [];
  let observedItemCount = 0;

  for (const run of _timelineRuns.values()) {
    if (run.key === rootRunKey || run.ref === rootRef || run.scope === "root" || run.scope === "auxiliary") continue;
    const hasAnyTiming = run.startMs !== undefined || run.endMs !== undefined;
    if (hasAnyTiming && !_timelineIntervalsOverlap(run.startMs, run.endMs, rootStartMs, rootEndMs)) {
      continue;
    }
    const resolution = _resolveTimelineParentChain(
      run,
      rootRef,
      rootCycle,
      rootRunKey,
      rootStartMs,
      rootEndMs,
    );
    if (resolution.relation === "unrelated_root") continue;
    if (resolution.relation === "broken") {
      if (resolution.ownership === "target" && resolution.reason) reasons.add(resolution.reason);
      continue;
    }
    if (resolution.reason) {
      reasons.add(resolution.reason);
      continue;
    }
    candidates.push({ run, depth: resolution.depth! });
    runKeys.push(run.key);
    observedItemCount++;
  }

  let capacityDropCount = 0;
  for (const drop of _timelineCapacityDrops.values()) {
    const ownership = _timelineCapacityDropOwnership(
      drop,
      rootRef,
      rootCycle,
      rootRunKey,
      rootStartMs,
      rootEndMs,
    );
    if (ownership === "target") {
      capacityDropKeys.push(drop.key);
      const isRootMarker = drop.key === rootRunKey
        || (drop.ref === rootRef && drop.cycle === rootCycle)
        || drop.scope === "root"
        || drop.scope === "auxiliary";
      if (!isRootMarker && drop.scope === "subagent") {
        capacityDropCount++;
      }
    }
  }
  if (capacityDropCount > 0) {
    observedItemCount = Math.min(MAX_TIMELINE_RUNS, observedItemCount + capacityDropCount);
    reasons.add("truncated");
  }

  candidates.sort((a, b) => {
    const clampOffset = (value: number): number => Math.min(rootDurationMs, Math.max(0, value));
    const aStart = a.run.startMs === undefined
      ? Number.POSITIVE_INFINITY
      : clampOffset(a.run.startMs - rootStartMs);
    const bStart = b.run.startMs === undefined
      ? Number.POSITIVE_INFINITY
      : clampOffset(b.run.startMs - rootStartMs);
    if (aStart !== bStart) return aStart - bStart;
    const aEnd = a.run.endMs === undefined
      ? Number.POSITIVE_INFINITY
      : clampOffset(a.run.endMs - rootStartMs);
    const bEnd = b.run.endMs === undefined
      ? Number.POSITIVE_INFINITY
      : clampOffset(b.run.endMs - rootStartMs);
    if (aEnd !== bEnd) return aEnd - bEnd;
    return a.run.ref.localeCompare(b.run.ref);
  });

  const items = candidates
    .slice(0, MAX_TIMELINE_ITEMS)
    .map(({ run, depth }) => _timelineItem(run, depth, rootStartMs, rootDurationMs, reasons));
  let truncated = candidates.length > MAX_TIMELINE_ITEMS || capacityDropCount > 0;
  if (truncated) reasons.add("truncated");

  const makeTimeline = (): SubagentTimelineEnvelope => ({
    version: 1,
    partial: reasons.size > 0,
    partialReasons: _timelineReasonList(reasons),
    timeBasis: "root_cycle",
    observedItemCount,
    displayedItemCount: items.length,
    truncated,
    items: [...items],
  });

  let timeline = makeTimeline();
  while (_textEncoder.encode(JSON.stringify(timeline)).length > MAX_TIMELINE_BYTES && items.length > 0) {
    items.pop();
    truncated = true;
    reasons.add("truncated");
    timeline = makeTimeline();
  }
  if (_textEncoder.encode(JSON.stringify(timeline)).length > MAX_TIMELINE_BYTES) {
    items.length = 0;
    truncated = true;
    reasons.add("truncated");
    timeline = makeTimeline();
  }
  return { timeline, runKeys, capacityDropKeys };
}

function _buildSubagentTimeline(
  rootRef: string,
  rootCycle: number,
  rootStartMs: number,
  rootEndMs: number,
  rootRunKey = _timelineRunKey(rootRef, rootCycle),
): SubagentTimelineEnvelope {
  const result = _collectSubagentTimeline(rootRef, rootCycle, rootStartMs, rootEndMs, rootRunKey);
  for (const key of result.runKeys) {
    const run = _timelineRuns.get(key);
    if (!run || run.status === "running") continue;
    run.consumed = true;
    _timelineRuns.delete(key);
    _timelineParents.delete(key);
  }
  for (const key of result.capacityDropKeys) {
    _timelineCapacityDrops.delete(key);
  }
  return result.timeline;
}

function _cleanupTimelineRuns(force = false): void {
  const now = _nowMs();
  for (const [key, drop] of _timelineCapacityDrops) {
    if (now - drop.lastAccessMs >= TIMELINE_RUNNING_TTL_MS) _timelineCapacityDrops.delete(key);
  }

  const removable = [..._timelineRuns.values()]
    .filter((run) => {
      if (run.consumed && run.status !== "running") return true;
      if (run.status === "running") return now - run.lastAccessMs >= TIMELINE_RUNNING_TTL_MS;
      return now - run.lastAccessMs >= TIMELINE_ENDED_TTL_MS;
    })
    .sort((a, b) => {
      const priority = (run: TimelineRun): number => {
        if (run.consumed && run.status !== "running") return 0;
        if (run.status !== "running") return 1;
        return 2;
      };
      return priority(a) - priority(b) || a.lastAccessMs - b.lastAccessMs;
    });
  const target = Math.min(RETAIN_TIMELINE_RUNS, MAX_TIMELINE_RUNS);
  for (const run of removable) {
    if (!force && run.status !== "running" && !run.consumed && now - run.lastAccessMs < TIMELINE_ENDED_TTL_MS) continue;
    if (_timelineRuns.size <= target && !run.consumed && !(!force && run.status === "running")) break;
    _timelineRuns.delete(run.key);
    _timelineParents.delete(run.key);
  }

  if (_timelineParents.size > MAX_TIMELINE_PARENTS) {
    const entries = [..._timelineParents.keys()];
    for (let i = 0; i < entries.length - RETAIN_TIMELINE_PARENTS; i++) {
      _timelineParents.delete(entries[i]!);
    }
  }
}

/**
 * Drop every timeline/state entry anchored to one anonymous session ref.
 * Used by `session.deleted` so a deleted session's runs, capacity drops, and
 * parent hints never linger; only the anonymous ref is ever inspected.
 */
function _dropSessionTimeline(sessionRef: string): void {
  for (const [key, run] of _timelineRuns) {
    if (run.ref === sessionRef) {
      _timelineRuns.delete(key);
      _timelineParents.delete(key);
    }
  }
  for (const [key, drop] of _timelineCapacityDrops) {
    if (drop.ref === sessionRef) {
      _timelineCapacityDrops.delete(key);
      _timelineParents.delete(key);
    }
  }
}

// ─── User Wait Collection ──────────────────────────────────

/**
 * Anonymous wait records keyed by `sessionRef:kind:requestKeyHash`.
 * Raw session/request ids exist only in the event and the current call
 * stack; they never enter this map, the envelope, or logs.
 */
const _userWaits = new Map<string, UserWaitRecord>();
/** One-shot partial evidence: events with no usable request id, per sessionRef. */
const _waitMissingRequestIds = new Map<string, number>();
/** One-shot partial evidence: active (pending) records evicted by capacity, per sessionRef. */
const _waitEvicted = new Map<string, number>();
/**
 * Fail-closed flag: once evidence maps overflow their bounded capacity, we
 * can no longer trust a "reliable zero" for any session, so every future
 * timeline reports an `evicted` partial reason.  Never auto-resets.
 */
let _waitEvidenceOverflow = false;

function _waitRequestKey(event: OpenCodeEvent): string | undefined {
  // Only the official request id may associate an asked with its terminal.
  // Wrapper event ids are never used: without an official request id the
  // record is untrackable and must surface as missing_request_id rather than
  // fabricate right+left censored intervals from distinct event ids.
  const requestId = typeof event.requestId === "string" && event.requestId.length > 0
    ? event.requestId
    : undefined;
  return requestId ?? undefined;
}

async function _hashWaitRequestKey(raw: string): Promise<string> {
  const data = _textEncoder.encode("opencode:waitreq:" + raw);
  const hashBuffer = await crypto.subtle.digest("SHA-256", data);
  const hashArray = new Uint8Array(hashBuffer);
  let hex = "";
  for (let i = 0; i < 16; i++) {
    hex += hashArray[i]!.toString(16).padStart(2, "0");
  }
  return hex;
}

function _waitKey(sessionRef: string, kind: UserWaitKind, requestKeyHash: string): string {
  return `${sessionRef}:${kind}:${requestKeyHash}`;
}

function _recordWaitMissingRequestId(sessionRef: string): void {
  _waitMissingRequestIds.set(sessionRef, (_waitMissingRequestIds.get(sessionRef) ?? 0) + 1);
  _cleanupWaitEvidence();
}

function _recordWaitEvicted(sessionRef: string): void {
  _waitEvicted.set(sessionRef, (_waitEvicted.get(sessionRef) ?? 0) + 1);
  _cleanupWaitEvidence();
}

function _cleanupWaitEvidence(): void {
  let dropped = false;
  for (const map of [_waitMissingRequestIds, _waitEvicted]) {
    if (map.size > MAX_WAIT_EVIDENCE_SESSIONS) {
      const entries = [...map.keys()];
      for (let i = 0; i < entries.length - RETAIN_WAIT_EVIDENCE_SESSIONS; i++) {
        map.delete(entries[i]!);
        dropped = true;
      }
    }
  }
  // Fail-closed: any evidence overflow means we can no longer certify a
  // reliable zero for any session, so keep a process-lifetime evicted flag.
  if (dropped) _waitEvidenceOverflow = true;
}

/** Test-only reset for the fail-closed overflow flag. */
function _setWaitEvidenceOverflowForTests(value: boolean): void {
  _waitEvidenceOverflow = value;
}

/**
 * Record a question/permission asked. Runs before any action-notification
 * filter so a disabled question/permission notification never produces a
 * false zero wait statistic. Duplicate asks keep the earliest start.
 */
async function _recordUserWaitAsked(event: OpenCodeEvent, kind: UserWaitKind): Promise<void> {
  await _consumeWaitCollectorTestDelay();
  if (!event.sessionId) return;
  const sessionRef = await _hashSessionRef(event.sessionId);
  const rawRequestKey = _waitRequestKey(event);
  if (!rawRequestKey) {
    _recordWaitMissingRequestId(sessionRef);
    return;
  }
  const requestKeyHash = await _hashWaitRequestKey(rawRequestKey);
  const key = _waitKey(sessionRef, kind, requestKeyHash);
  const monoMs = _safeMonoValue(event.receivedMonoMs) ?? _nowMonoMs();
  const existing = _userWaits.get(key);
  if (existing) {
    if (existing.orphan && existing.askedMonoMs === undefined
      && existing.resolvedMonoMs !== undefined && monoMs <= existing.resolvedMonoMs) {
      // A late asked that precedes its already-seen terminal in receipt order:
      // merge the orphan into a complete resolved record instead of merely
      // refreshing recency or fabricating a second interval.
      existing.orphan = false;
      existing.askedMonoMs = monoMs;
      existing.lastAccessMonoMs = monoMs;
      _userWaits.delete(key);
      _userWaits.set(key, existing);
    } else {
      // Earliest asked wins; refresh recency only.
      existing.lastAccessMonoMs = monoMs;
      _userWaits.delete(key);
      _userWaits.set(key, existing);
    }
  } else {
    _userWaits.set(key, {
      key,
      sessionRef,
      kind,
      requestKeyHash,
      askedMonoMs: monoMs,
      state: "pending",
      lastAccessMonoMs: monoMs,
    });
  }
  _cleanupUserWaits();
}

/**
 * Record a question/permission terminal. First terminal wins; later
 * terminals only refresh recency. A terminal without a matching asked is
 * kept as an orphan (left-censored in the enclosing root cycle).
 */
async function _recordUserWaitTerminal(
  event: OpenCodeEvent,
  kind: UserWaitKind,
  result: UserWaitResult,
): Promise<void> {
  await _consumeWaitCollectorTestDelay();
  if (!event.sessionId) return;
  const sessionRef = await _hashSessionRef(event.sessionId);
  const rawRequestKey = _waitRequestKey(event);
  if (!rawRequestKey) {
    _recordWaitMissingRequestId(sessionRef);
    return;
  }
  const requestKeyHash = await _hashWaitRequestKey(rawRequestKey);
  const key = _waitKey(sessionRef, kind, requestKeyHash);
  const monoMs = _safeMonoValue(event.receivedMonoMs) ?? _nowMonoMs();
  const existing = _userWaits.get(key);
  if (existing) {
    if (existing.state === "pending") {
      existing.state = "resolved";
      existing.resolvedMonoMs = monoMs;
      existing.result = result;
    }
    // First terminal wins; later terminals only refresh recency.
    existing.lastAccessMonoMs = monoMs;
    _userWaits.delete(key);
    _userWaits.set(key, existing);
  } else {
    _userWaits.set(key, {
      key,
      sessionRef,
      kind,
      requestKeyHash,
      resolvedMonoMs: monoMs,
      result,
      state: "resolved",
      orphan: true,
      lastAccessMonoMs: monoMs,
    });
  }
  _cleanupUserWaits();
}

/**
 * Bounded cleanup. Order: expired resolved, then oldest resolved, then
 * oldest pending. Deleting any record that has not yet been reported by a
 * successfully committed root idle keeps anonymous `evicted` partial
 * evidence for the owning session, so cleanup can never silently turn into a
 * "reliable zero".  A resolved record must not be silently TTL-dropped
 * before its owning cycle has claimed/committed it.
 */
function _cleanupUserWaits(force = false): void {
  const now = _nowMonoMs();
  const remove = (rec: UserWaitRecord): void => {
    _userWaits.delete(rec.key);
    // Unreported deletions always surface as evicted evidence.  Reported
    // records were already counted by a committed root idle, so a silent
    // TTL drop cannot turn them into a false zero.
    if (rec.reported !== true) _recordWaitEvicted(rec.sessionRef);
  };

  // 1. Expired resolved records first.
  for (const [key, rec] of _userWaits) {
    if (rec.state === "resolved" && now - rec.lastAccessMonoMs >= WAIT_RESOLVED_TTL_MS) {
      remove(rec);
    }
  }
  // 2. Capacity: oldest resolved first, then oldest pending.
  if (_userWaits.size > MAX_WAIT_RECORDS) {
    const removable = [..._userWaits.values()].sort((a, b) => {
      const aPriority = a.state === "resolved" ? 0 : 1;
      const bPriority = b.state === "resolved" ? 0 : 1;
      return aPriority - bPriority || a.lastAccessMonoMs - b.lastAccessMonoMs;
    });
    for (const rec of removable) {
      if (_userWaits.size <= RETAIN_WAIT_RECORDS) break;
      remove(rec);
    }
  }
  // 3. Pending TTL.
  for (const [key, rec] of _userWaits) {
    if (rec.state === "pending" && now - rec.lastAccessMonoMs >= WAIT_PENDING_TTL_MS) {
      remove(rec);
    }
  }
  _cleanupWaitEvidence();
}

function _userWaitReasonList(reasons: Set<UserWaitPartialReason>): UserWaitPartialReason[] {
  const order: UserWaitPartialReason[] = [
    "open_at_cycle_end",
    "orphan_resolution",
    "missing_request_id",
    "evicted",
    "truncated",
    "clock_invalid",
  ];
  return order.filter((reason) => reasons.has(reason));
}

/**
 * Compute one interval by intersecting a wait record with a frozen root
 * cycle (monotonic axis). Returns undefined when the record does not
 * intersect this cycle and must be re-evaluated by a later cycle.
 */
function _userWaitIntervalForCycle(
  rec: UserWaitRecord,
  rootStartMonoMs: number,
  rootEndMonoMs: number,
  rootDurationMs: number,
  reasons: Set<UserWaitPartialReason>,
): UserWaitInterval | undefined {
  // Raw (possibly fractional) mono values are used for comparison/intersection;
  // only the emitted offsets/duration are integerised for the wire.
  const clamp = (value: number): number => {
    const bounded = Math.min(rootDurationMs, Math.max(0, value));
    return _intMonoMs(bounded);
  };

  // Orphan terminal: no asked observed → left-censored within this cycle.
  if (rec.orphan && rec.askedMonoMs === undefined) {
    const resolved = rec.resolvedMonoMs;
    if (resolved === undefined) return undefined;
    if (resolved < rootStartMonoMs || resolved > rootEndMonoMs) return undefined;
    reasons.add("orphan_resolution");
    return {
      kind: rec.kind,
      result: rec.result ?? "replied",
      intervalState: "left_censored",
      endOffsetMs: clamp(resolved - rootStartMonoMs),
    };
  }

  const asked = rec.askedMonoMs;
  if (asked === undefined) return undefined;
  const resolved = rec.resolvedMonoMs;

  // Asked after cycle end → evaluated in a later cycle.
  if (asked > rootEndMonoMs) return undefined;
  // Fully resolved before cycle start → already reported by an earlier cycle.
  if (resolved !== undefined && resolved < rootStartMonoMs) return undefined;

  if (resolved === undefined || resolved > rootEndMonoMs) {
    // Still open at cycle end → right-censored.
    reasons.add("open_at_cycle_end");
    return {
      kind: rec.kind,
      intervalState: "right_censored",
      startOffsetMs: clamp(asked - rootStartMonoMs),
    };
  }

  // Resolved within the cycle → complete intersection.
  if (resolved < asked) {
    // Monotonic clock anomaly; never fabricate a negative duration.
    reasons.add("clock_invalid");
    return undefined;
  }
  const startOffsetMs = clamp(asked - rootStartMonoMs);
  const endOffsetMs = clamp(resolved - rootStartMonoMs);
  const durationMs = endOffsetMs - startOffsetMs;
  if (durationMs < 0) {
    reasons.add("clock_invalid");
    return undefined;
  }
  return {
    kind: rec.kind,
    result: rec.result ?? "replied",
    intervalState: "complete",
    startOffsetMs,
    endOffsetMs,
    durationMs,
  };
}

interface UserWaitFreezeResult {
  timeline: UserWaitTimelineEnvelope;
  /** Anonymous keys of records that produced an interval in this snapshot. */
  includedKeys: string[];
  /** Evidence counts observed at freeze time (committed only on success). */
  frozenMissingCount: number;
  frozenEvictedCount: number;
}

/**
 * Freeze the wait snapshot for one root idle claim and build the
 * userWaitTimeline envelope.  Called before enrichment so replies that
 * arrive during enrichment can never enter the frozen envelope.  MVP only
 * counts the same root session's own waits; child-session waits are never
 * attributed to the root by time overlap alone.
 *
 * Evidence (missing_request_id / evicted) is read but NOT consumed here:
 * the caller keeps the frozen counts and subtracts them only on a successful
 * commit; a rolled-back claim leaves them intact.  This prevents a failed
 * build/send from silently restoring a "reliable zero" for evidence that was
 * observed.  The fail-closed overflow flag is process-lifetime and always
 * forces `evicted` once evidence capacity was exceeded.
 */
function _freezeUserWaitSnapshot(
  sessionRef: string,
  rootStartMonoMs: number | undefined,
  rootEndMonoMs: number | undefined,
): UserWaitFreezeResult {
  const reasons = new Set<UserWaitPartialReason>();
  const intervals: UserWaitInterval[] = [];
  const includedKeys: string[] = [];

  // Frozen (not consumed) evidence counts; the claim commit will subtract.
  const frozenMissingCount = _waitMissingRequestIds.get(sessionRef) ?? 0;
  const frozenEvictedCount = _waitEvicted.get(sessionRef) ?? 0;
  if (frozenMissingCount > 0) reasons.add("missing_request_id");
  if (frozenEvictedCount > 0 || _waitEvidenceOverflow) reasons.add("evicted");

  // Raw (possibly fractional) mono root cycle; integerise only the wire
  // duration.  Comparisons/intersections below use the raw values.  A delta
  // outside [0, MAX_DURATION_MS] is clock_invalid: we never fabricate
  // intervals or durations on an unusable axis.
  const start = _safeMonoValue(rootStartMonoMs);
  const end = _safeMonoValue(rootEndMonoMs);
  let rootDurationMs: number | undefined;
  if (start !== undefined && end !== undefined) {
    const duration = end - start;
    if (duration >= 0 && duration <= MAX_DURATION_MS) {
      rootDurationMs = _intMonoMs(duration);
    }
  }
  if (rootDurationMs === undefined) {
    reasons.add("clock_invalid");
  }

  let observedIntervalCount = 0;
  if (rootDurationMs !== undefined && start !== undefined && end !== undefined) {
    for (const rec of _userWaits.values()) {
      if (rec.sessionRef !== sessionRef) continue;
      const interval = _userWaitIntervalForCycle(rec, start, end, rootDurationMs, reasons);
      if (!interval) continue;
      observedIntervalCount++;
      intervals.push(interval);
      includedKeys.push(rec.key);
    }
  }

  // Stable ordering: complete/right-censored by start, left-censored by end.
  intervals.sort((a, b) => {
    const aPos = a.startOffsetMs ?? a.endOffsetMs ?? 0;
    const bPos = b.startOffsetMs ?? b.endOffsetMs ?? 0;
    return aPos - bPos || a.kind.localeCompare(b.kind) || a.intervalState.localeCompare(b.intervalState);
  });

  if (intervals.length > MAX_WAIT_INTERVALS) reasons.add("truncated");
  const displayed = intervals.slice(0, MAX_WAIT_INTERVALS);

  const makeTimeline = (): UserWaitTimelineEnvelope => ({
    version: 1,
    partial: reasons.size > 0,
    partialReasons: _userWaitReasonList(reasons),
    timeBasis: "root_cycle_receipt_monotonic",
    observedIntervalCount,
    displayedIntervalCount: displayed.length,
    truncated: displayed.length < observedIntervalCount,
    intervals: [...displayed],
  });

  let timeline = makeTimeline();
  while (
    _textEncoder.encode(JSON.stringify(timeline)).length > MAX_USER_WAIT_TIMELINE_BYTES
    && displayed.length > 0
  ) {
    displayed.pop();
    reasons.add("truncated");
    timeline = makeTimeline();
  }
  if (_textEncoder.encode(JSON.stringify(timeline)).length > MAX_USER_WAIT_TIMELINE_BYTES) {
    displayed.length = 0;
    reasons.add("truncated");
    timeline = makeTimeline();
  }
  return { timeline, includedKeys, frozenMissingCount, frozenEvictedCount };
}

/** Test/standalone wrapper: freeze and return only the envelope. */
function _freezeUserWaitTimeline(
  sessionRef: string,
  rootStartMonoMs: number | undefined,
  rootEndMonoMs: number | undefined,
): UserWaitTimelineEnvelope {
  return _freezeUserWaitSnapshot(sessionRef, rootStartMonoMs, rootEndMonoMs).timeline;
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
 * Keep only a safe final path component for the project display field.
 * Paths are never copied into the envelope; only this bounded basename may
 * leave the plugin.
 */
function _projectNameFromPath(raw: unknown): string | undefined {
  if (typeof raw !== "string") return undefined;
  const value = raw.trim();
  if (!value || value === "." || value === "..") return undefined;
  if (/^(?:[\\/]+|[A-Za-z]:[\\/]*)$/.test(value)) return undefined;

  const withoutTrailingSeparators = value.replace(/[\\/]+$/, "");
  if (!withoutTrailingSeparators || withoutTrailingSeparators === "." || withoutTrailingSeparators === "..") {
    return undefined;
  }
  const basename = withoutTrailingSeparators.split(/[\\/]/).pop();
  if (!basename || basename === "." || basename === ".." || /^[A-Za-z]:$/.test(basename)) {
    return undefined;
  }
  return _sanitiseName(basename);
}

function _projectNameFromInput(input: PluginInput): string | undefined {
  const projectWorktree = input.project && typeof input.project === "object"
    ? input.project.worktree
    : undefined;
  for (const candidate of [input.worktree, projectWorktree, input.directory]) {
    const projectName = _projectNameFromPath(candidate);
    if (projectName) return projectName;
  }
  return undefined;
}

function _normaliseAuxiliarySessionNames(raw: unknown): Set<string> {
  const names = new Set(DEFAULT_AUXILIARY_SESSION_NAMES);
  if (!Array.isArray(raw)) return names;
  for (const value of raw.slice(0, MAX_AUXILIARY_SESSION_NAMES)) {
    if (names.size >= MAX_AUXILIARY_SESSION_NAMES) break;
    if (typeof value !== "string") continue;
    const name = _sanitiseName(value);
    if (name) names.add(name);
  }
  return names;
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
  if (!context || (context.mode !== "sample" && context.mode !== "anomaly") || !sessionRef) return context;
  const existing = _metadataSampleSessions.get(sessionRef);
  if (existing) {
    _metadataSampleSessions.delete(sessionRef);
    _metadataSampleSessions.set(sessionRef, existing);
    return { ...context, sampleSession: existing.sampleSession, sessionRef };
  }
  const created: MetadataSampleSessionState = { sampleSession: _nextMetadataSampleSession++ };
  _metadataSampleSessions.set(sessionRef, created);
  return { ...context, sampleSession: created.sampleSession, sessionRef };
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
    } else if (context.mode === "anomaly") {
      const sessionRef = context.sessionRef ?? "";
      const dedupKey = `${phase}:${sessionRef}:${payloadJSON}`;
      if (_metadataDiagnosticAnomalySeen.has(dedupKey)) return;
      const currentCount = _metadataDiagnosticAnomalyCounts.get(phase) ?? 0;
      if (currentCount >= MAX_METADATA_DIAGNOSTIC_ANOMALIES_PER_PHASE) return;
      _metadataDiagnosticAnomalyCounts.set(phase, currentCount + 1);
      _metadataDiagnosticAnomalySeen.add(dedupKey);
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
  if (context?.mode === "anomaly") return; // anomaly does not log ordinary message_updated
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
  responseShape: "data-wrapper" | "direct-object" | "error-result" | "invalid";
  data?: Record<string, unknown>;
}

function _inspectSessionResponse(response: unknown): SessionDiagnosticResponse {
  if (!_isRecord(response)) return { responseShape: "invalid" };
  if ("data" in response) {
    return _isRecord(response.data)
      ? { responseShape: "data-wrapper", data: response.data }
      : { responseShape: "invalid" };
  }
  // The v1 SDK returns { data, request, response } on success and
  // { error, request, response } on failure.  An error result is NOT a
  // Session: never treat it as a direct object (which would otherwise be
  // mis-derived as a root session and poison the scope cache).
  if ("error" in response) {
    return { responseShape: "error-result" };
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
  if (context?.mode === "anomaly") {
    // Anomaly only records: response invalid/error-result, parentIDState
    // empty/invalid, or parentIDState missing/null with no title (possible
    // legitimate root fallback).  An SDK error result carries no Session data,
    // so it is recorded by shape only — never its error/request payload.
    const data = response.data;
    const title = data?.title;
    const titlePresent = typeof title === "string" && title.length > 0;
    const pidState = _metadataParentIDState(data);
    const isCandidate = response.responseShape === "invalid"
      || response.responseShape === "error-result"
      || pidState === "empty" || pidState === "invalid"
      || ((pidState === "missing" || pidState === "null") && !titlePresent);
    if (!isCandidate) return;
  }
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
  if (context?.mode === "anomaly") return; // anomaly does not log session_messages
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
  if (context?.mode === "anomaly") {
    // Anomaly only records: sessionScope unknown, or root with fallback name.
    const scope = envelope.session.scope;
    const name = envelope.session.name;
    const isFallbackName = typeof name === "string" && FALLBACK_SESSION_NAME_RE.test(name);
    const isCandidate = scope === "unknown" || (scope === "root" && isFallbackName);
    if (!isCandidate) return;
  }
  _emitMetadataDiagnostic(context, "outgoing_envelope", () => {
    const sessionName = envelope.session.name;
    const agent = _safeMetadataDiagnosticString(envelope.agent);
    const model = _safeMetadataDiagnosticString(envelope.model);
    const isFallbackName = typeof sessionName === "string" && FALLBACK_SESSION_NAME_RE.test(sessionName);
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
    if (isFallbackName) fields.sessionNameFallback = true;
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
  const modelVariant = _sanitiseActionText(info.variant, MAX_AGENT_MODEL_LENGTH);
  if (agent) metadata.agent = agent;
  if (providerID) metadata.providerID = providerID;
  if (modelID) metadata.modelID = modelID;
  if (modelVariant) metadata.modelVariant = modelVariant;

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
  const modelVariant = _sanitiseActionText(metadata.modelVariant, MAX_AGENT_MODEL_LENGTH);
  const created = _safeTimestamp(metadata.created);
  const completed = _safeTimestamp(metadata.completed);
  if (agent) safeMetadata.agent = agent;
  if (providerID) safeMetadata.providerID = providerID;
  if (modelID) safeMetadata.modelID = modelID;
  if (modelVariant) safeMetadata.modelVariant = modelVariant;
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
  if (startsNewAssistantMessage && safeMetadata.modelVariant === undefined) {
    delete merged.modelVariant;
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

/** Read only the OpenCode session.model.variant fallback. */
function _modelVariantFromSessionData(sessionData: Record<string, unknown>): string | undefined {
  const rawModel = sessionData.model;
  if (!rawModel || typeof rawModel !== "object" || Array.isArray(rawModel)) return undefined;
  return _sanitiseActionText(
    (rawModel as Record<string, unknown>).variant,
    MAX_AGENT_MODEL_LENGTH,
  );
}

function _applyAssistantMetadata(event: OpenCodeEvent, metadata: AssistantMetadata | undefined): void {
  if (!metadata) return;
  if (!event.agent && metadata.agent) event.agent = metadata.agent;
  if (!event.model) {
    const model = _modelFromAssistantMetadata(metadata);
    if (model) event.model = model;
  }
  if (!event.modelVariant && metadata.modelVariant) {
    event.modelVariant = metadata.modelVariant;
  }
  // A reliable claimed idle cycle owns its timing snapshot. Assistant
  // timestamps are only a fallback when busy-cycle timing is not reliable.
  if (event.cycleTimingReliable !== true) {
    if (event.taskStartedAt === undefined && metadata.created) {
      event.taskStartedAt = metadata.created;
    }
    if (event.endedAt === undefined && metadata.completed) {
      event.endedAt = metadata.completed;
    }
    const durationMs = _taskDurationMs(event.taskStartedAt, event.endedAt);
    if (durationMs !== undefined) event.durationMs = durationMs;
  }
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

  const count = event.questionCount !== undefined
    ? Math.min(event.questionCount, MAX_COUNT)
    : rawQuestions.length > 0
      ? Math.min(rawQuestions.length, MAX_COUNT)
      : undefined;
  const optionCount = event.questionOptionCount !== undefined
    ? Math.min(event.questionOptionCount, MAX_COUNT)
    : items.reduce((total, item) => total + (item.options?.length ?? 0), 0);
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
): PermissionItem | undefined {
  if (!event.permission) return undefined;
  const permission = event.permission;
  const result: PermissionItem = { category: _derivePermissionCategory(permission) };
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

type ActionKind = "permission" | "question";

interface ActionBucketMember {
  /** Official request id, outer event id fallback, or opaque local key. */
  requestId: string;
  permission?: PermissionItem;
  question?: QuestionEnvelope;
}

interface ActionEnvelopeOverride {
  kind: ActionKind;
  permission?: PermissionEnvelope;
  question?: QuestionEnvelope;
}

interface ActionEventSnapshot extends OpenCodeEvent {
  sessionId: string;
  type: "permission.updated" | "question.asked";
}

interface ActionBucket {
  sessionId: string;
  kind: ActionKind;
  eventId: string;
  createdAtMs: number;
  timer: ReturnType<typeof setTimeout>;
  baseEvent: ActionEventSnapshot;
  members: Map<string, ActionBucketMember>;
  config: ResolvedConfig;
  input: PluginInput;
  diagnostics: MetadataDiagnosticContext;
}

// ─── State Machine ──────────────────────────────────────────

const _sessions = new Map<string, SessionState>();

/** Action buckets are keyed by raw session ID only inside this process. */
const _actionBuckets = new Map<string, Map<ActionKind, ActionBucket>>();

function _actionBucketFor(sessionId: string, kind: ActionKind): ActionBucket | undefined {
  return _actionBuckets.get(sessionId)?.get(kind);
}

function _unrefTimer(timer: ReturnType<typeof setTimeout>): void {
  const candidate = timer as unknown as { unref?: () => void };
  candidate.unref?.();
}

function _deleteActionBucket(bucket: ActionBucket): void {
  const byKind = _actionBuckets.get(bucket.sessionId);
  if (!byKind || byKind.get(bucket.kind) !== bucket) return;
  clearTimeout(bucket.timer);
  byKind.delete(bucket.kind);
  if (byKind.size === 0) _actionBuckets.delete(bucket.sessionId);
}

function _takeActionBucket(sessionId: string, kind: ActionKind): ActionBucket | undefined {
  const bucket = _actionBucketFor(sessionId, kind);
  if (!bucket) return undefined;
  _deleteActionBucket(bucket);
  return bucket;
}

function _actionBucketCount(): number {
  let count = 0;
  for (const byKind of _actionBuckets.values()) count += byKind.size;
  return count;
}

function _cleanupActionBuckets(): void {
  if (_actionBucketCount() <= MAX_ACTION_BUCKETS) return;
  const buckets: ActionBucket[] = [];
  for (const byKind of _actionBuckets.values()) {
    for (const bucket of byKind.values()) buckets.push(bucket);
  }
  buckets.sort((a, b) => a.createdAtMs - b.createdAtMs);
  for (const bucket of buckets.slice(0, Math.max(0, buckets.length - MAX_ACTION_BUCKETS))) {
    _takeActionBucket(bucket.sessionId, bucket.kind);
    void _flushActionBucket(bucket);
  }
}

/** Test-only reset; production has no shutdown hook assumption. */
function _resetActionBuckets(): void {
  for (const byKind of _actionBuckets.values()) {
    for (const bucket of byKind.values()) clearTimeout(bucket.timer);
  }
  _actionBuckets.clear();
}

interface IdleClaim {
  sessionRef: string;
  cycle: number;
  eventId: string;
  endedAtMs: number;
  /** Frozen root terminal claim; never re-read from _sessions after enrichment. */
  rootRef: string;
  rootCycle: number;
  rootRunKey: string;
  rootStartMs?: number;
  rootEndMs?: number;
  /** Frozen monotonic root-cycle axis for userWaitTimeline. */
  rootStartMonoMs?: number;
  rootEndMonoMs?: number;
  /** Integerised monotonic root-cycle duration (wire value for durationMs). */
  rootDurationMonoMs?: number;
  /**
   * True only when both busy and idle receipts carried explicit monotonic
   * captures (production hook path).  When false, wall-derived duration is
   * kept so legacy/direct callers never get mono-mislabeled values.
   */
  monoReliable?: boolean;
  /** Frozen user-wait timeline; built before enrichment, never amended later. */
  userWaitTimeline?: UserWaitTimelineEnvelope;
  /** Anonymous keys of records included in the frozen timeline (commit marks reported). */
  includedWaitKeys: string[];
  /** Evidence counts frozen at claim time; committed only on success. */
  frozenMissingCount: number;
  frozenEvictedCount: number;
  /** Internal: committed exactly once, then report/evidence are final. */
  committed?: boolean;
}

interface ErrorClaim {
  sessionRef: string;
  cycle: number;
  eventId: string;
  endedAtMs: number;
}

interface EventStateContext {
  sessionRef: string;
  state: SessionState;
}

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
      lastAccessMs: _nowMs(),
    };
    _sessions.set(sessionKey, st);
  } else {
    st.lastAccessMs = _nowMs();
    // Refresh insertion order as a small LRU improvement for cleanup.
    _sessions.delete(sessionKey);
    _sessions.set(sessionKey, st);
  }
  return st;
}

/** Clear only Assistant timestamps when a genuinely new busy cycle starts. */
function _clearAssistantTiming(sessionRef: string): void {
  const metadata = _assistantMetadata.get(sessionRef);
  if (!metadata) return;
  const retained: AssistantMetadata = { ...metadata };
  delete retained.created;
  delete retained.completed;
  if (Object.keys(retained).length === 0) {
    _assistantMetadata.delete(sessionRef);
    return;
  }
  _assistantMetadata.delete(sessionRef);
  _assistantMetadata.set(sessionRef, retained);
}

function _startBusyCycle(
  state: SessionState,
  sessionRef: string,
  receivedAtMs: number,
  receivedMonoMs?: number,
): void {
  state.hadBusy = true;
  state.sentIdle = false;
  state.hadErrorForCycle = false;
  state.cycle++;
  state.cycleStartedAtMs = receivedAtMs;
  const monoMs = _safeMonoValue(receivedMonoMs);
  state.cycleStartedMonoMs = monoMs ?? _nowMonoMs();
  state.cycleStartedMonoReliable = monoMs !== undefined;
  state.cycleEndedAtMs = undefined;
  state.pendingEventId = undefined;
  _clearAssistantTiming(sessionRef);
  _startTimelineRun(sessionRef, state.cycle, receivedAtMs);
}

async function _eventStateContext(event: OpenCodeEvent): Promise<EventStateContext | null> {
  if (!event.sessionId) return null;
  const sessionRef = await _hashSessionRef(event.sessionId);
  const state = _getState(sessionRef);
  if (event.sessionScope === undefined) {
    event.sessionScope = _cachedSessionScope(sessionRef) ?? "unknown";
  }
  if (event.sessionScope === "root" || event.sessionScope === "subagent" || event.sessionScope === "auxiliary") {
    _cacheSessionScope(sessionRef, event.sessionScope);
  }
  return { sessionRef, state };
}

function _applyCycleTimingSnapshot(
  event: OpenCodeEvent,
  state: SessionState,
  endedAtMs: number,
): void {
  event.cycleTimingCaptured = true;
  const startedAtMs = _safeEpochMs(state.cycleStartedAtMs);
  const durationMs = startedAtMs === undefined
    ? undefined
    : endedAtMs - startedAtMs;
  const validDuration = durationMs !== undefined
    && Number.isInteger(durationMs)
    && durationMs >= 0
    && durationMs <= MAX_DURATION_MS;
  event.cycleTimingReliable = validDuration;
  if (!validDuration) return;

  event.taskStartedAt = new Date(startedAtMs).toISOString();
  event.endedAt = new Date(endedAtMs).toISOString();
  event.durationMs = durationMs;
}

async function _claimIdleEvent(
  event: OpenCodeEvent,
  config: ResolvedConfig,
): Promise<IdleClaim | null> {
  if (!config.events.has("session_idle")) return null;
  const context = await _eventStateContext(event);
  if (!context) return null;
  const { sessionRef, state } = context;

  // This guard is held across enrichment and transport preparation so the
  // legacy and status idle events share one frozen claim.
  if (_idleProcessing.has(sessionRef)) return null;
  _idleProcessing.add(sessionRef);

  if (!state.hadBusy || state.hadErrorForCycle || state.sentIdle) {
    _idleProcessing.delete(sessionRef);
    return null;
  }

  let claim: IdleClaim | undefined;
  try {
    const eventId = _generateId();
    const endedAtMs = _safeEpochMs(event.receivedAtMs) ?? _nowMs();
    const rootCycle = state.cycle;
    const rootStartMs = _safeEpochMs(state.cycleStartedAtMs);
    const rootStartMonoMs = _safeMonoValue(state.cycleStartedMonoMs);
    const endedMonoMs = _safeMonoValue(event.receivedMonoMs);
    const monoReliable = state.cycleStartedMonoReliable === true && endedMonoMs !== undefined;
    const freeze = _freezeUserWaitSnapshot(
      sessionRef,
      monoReliable ? rootStartMonoMs : undefined,
      monoReliable ? endedMonoMs : undefined,
    );
    let rootDurationMonoMs: number | undefined;
    if (monoReliable && rootStartMonoMs !== undefined && endedMonoMs !== undefined) {
      const duration = endedMonoMs - rootStartMonoMs;
      // Aligned with the Python _MAX_DURATION_MS (7 days): only a valid,
      // in-range delta may drive the top-level duration.  An invalid/out-of-
      // range mono delta must NOT fall back to wall — durationMs is omitted.
      if (duration >= 0 && duration <= MAX_DURATION_MS) {
        rootDurationMonoMs = _intMonoMs(duration);
      }
    }
    claim = {
      sessionRef,
      cycle: rootCycle,
      eventId,
      endedAtMs,
      rootRef: sessionRef,
      rootCycle,
      rootRunKey: _timelineRunKey(sessionRef, rootCycle),
      rootStartMs,
      rootEndMs: endedAtMs,
      rootStartMonoMs: monoReliable ? rootStartMonoMs : undefined,
      rootEndMonoMs: monoReliable ? endedMonoMs : undefined,
      rootDurationMonoMs,
      monoReliable,
      userWaitTimeline: freeze.timeline,
      includedWaitKeys: freeze.includedKeys,
      frozenMissingCount: freeze.frozenMissingCount,
      frozenEvictedCount: freeze.frozenEvictedCount,
    };
    state.sentIdle = true;
    state.pendingEventId = eventId;
    state.cycleEndedAtMs = endedAtMs;
    _applyCycleTimingSnapshot(event, state, endedAtMs);
    _finishTimelineRun(sessionRef, state.cycle, "completed", endedAtMs);
    return claim;
  } catch (error) {
    // Roll back the claim itself, while the cycle/event guard prevents an old
    // idle failure from mutating a newer busy cycle.
    if (claim) {
      _rollbackIdleClaim(claim);
    } else {
      _idleProcessing.delete(sessionRef);
    }
    throw error;
  }
}

async function _claimErrorEvent(
  event: OpenCodeEvent,
  config: ResolvedConfig,
): Promise<ErrorClaim | null> {
  if (!config.events.has("session_error")) return null;
  const context = await _eventStateContext(event);
  if (!context) return null;
  const { sessionRef, state } = context;
  const eventId = _generateId();
  const endedAtMs = _safeEpochMs(event.receivedAtMs) ?? _nowMs();
  const claim: ErrorClaim = {
    sessionRef,
    cycle: state.cycle,
    eventId,
    endedAtMs,
  };
  state.hadErrorForCycle = true;
  state.sentIdle = true;
  state.pendingEventId = eventId;
  _finishTimelineRun(sessionRef, claim.cycle, "failed", endedAtMs);
  return claim;
}

function _rollbackErrorClaim(claim: ErrorClaim): void {
  const state = _sessions.get(claim.sessionRef);
  if (!state || state.cycle !== claim.cycle || state.pendingEventId !== claim.eventId) return;
  state.hadErrorForCycle = false;
  state.sentIdle = false;
  state.pendingEventId = undefined;
  _rollbackTimelineFailure(claim.sessionRef, claim.cycle, claim.endedAtMs);
}

/**
 * Commit a successfully built root idle claim: permanently subtract the
 * evidence counts frozen at claim time, and mark the included records as
 * reported so later cleanup TTL-drops of them stay silent.  Idempotent.
 */
function _commitIdleClaim(claim: IdleClaim): void {
  if (claim.committed) return;
  claim.committed = true;

  if (claim.frozenMissingCount > 0) {
    const current = _waitMissingRequestIds.get(claim.sessionRef) ?? 0;
    const remaining = current - claim.frozenMissingCount;
    if (remaining <= 0) _waitMissingRequestIds.delete(claim.sessionRef);
    else _waitMissingRequestIds.set(claim.sessionRef, remaining);
  }
  if (claim.frozenEvictedCount > 0) {
    const current = _waitEvicted.get(claim.sessionRef) ?? 0;
    const remaining = current - claim.frozenEvictedCount;
    if (remaining <= 0) _waitEvicted.delete(claim.sessionRef);
    else _waitEvicted.set(claim.sessionRef, remaining);
  }
  for (const key of claim.includedWaitKeys) {
    const rec = _userWaits.get(key);
    if (rec) {
      rec.reported = true;
      _userWaits.delete(key);
      _userWaits.set(key, rec);
    }
  }
}

function _rollbackIdleClaim(claim: IdleClaim): void {
  const state = _sessions.get(claim.sessionRef);
  if (state && state.cycle === claim.cycle && state.pendingEventId === claim.eventId) {
    state.sentIdle = false;
    state.pendingEventId = undefined;
    _rollbackTimelineEnd(claim.sessionRef, claim.cycle, claim.endedAtMs);
  }
  _idleProcessing.delete(claim.sessionRef);
}

function _cacheSessionScope(sessionRef: string, scope: SessionScope): void {
  if (scope !== "root" && scope !== "subagent" && scope !== "auxiliary") return;
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

/**
 * Cache safe Session metadata under the anonymous session ref only.  Only
 * sanitised name/title, a reliable derived scope, and a valid created time
 * are retained; raw ids, directory, projectID, and parentID never enter.
 */
function _cacheSessionMetadata(sessionRef: string, entry: SessionMetadataCacheEntry): void {
  const safe: SessionMetadataCacheEntry = {};
  const name = _sanitiseName(entry.name);
  const startedAt = _safeTimestamp(entry.startedAt);
  if (name) safe.name = name;
  if (entry.scope === "root" || entry.scope === "subagent" || entry.scope === "auxiliary") {
    safe.scope = entry.scope;
  }
  if (startedAt) safe.startedAt = startedAt;
  if (Object.keys(safe).length === 0) return;

  const previous = _sessionMetadata.get(sessionRef);
  const merged: SessionMetadataCacheEntry = { ...(previous ?? {}), ...safe };
  _sessionMetadata.delete(sessionRef);
  _sessionMetadata.set(sessionRef, merged);
  _cleanupSessionMetadata();
}

/** Read and refresh one safe Session metadata entry in the bounded LRU. */
function _cachedSessionMetadata(sessionRef: string): SessionMetadataCacheEntry | undefined {
  const entry = _sessionMetadata.get(sessionRef);
  if (!entry) return undefined;
  _sessionMetadata.delete(sessionRef);
  _sessionMetadata.set(sessionRef, entry);
  return entry;
}

/** Remove one Session's safe metadata entry (session.deleted). */
function _dropSessionMetadata(sessionRef: string): void {
  _sessionMetadata.delete(sessionRef);
}

function _cleanupSessionMetadata(): void {
  if (_sessionMetadata.size <= MAX_CACHE_ENTRIES) return;
  const entries = [..._sessionMetadata.keys()];
  for (let i = 0; i < entries.length - CACHE_RETAIN_ENTRIES; i++) {
    _sessionMetadata.delete(entries[i]!);
  }
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
  _cleanupSessionMetadata();
  _cleanupAssistantMetadata();
  _cleanupMetadataSampleSessions();
  _cleanupTimelineRuns();
  _cleanupUserWaits();
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

  const instanceDisplayName = _sanitiseName(raw.instanceDisplayName);

  const actionContentMode: ActionContentMode =
    raw.actionContentMode === "summary" || raw.actionContentMode === "full"
      ? raw.actionContentMode
      : "strict";

  const metadataDiagnostics: MetadataDiagnostics =
    raw.metadataDiagnostics === "once" || raw.metadataDiagnostics === "sample" || raw.metadataDiagnostics === "anomaly"
      ? raw.metadataDiagnostics
      : "off";

  return {
    url,
    token,
    timeoutMs,
    enabled: true,
    events: eventFilter,
    instanceDisplayName,
    auxiliarySessionNames: _normaliseAuxiliarySessionNames(raw.auxiliarySessionNames),
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
  "permission.asked",
  "permission.replied",
  "question.asked",
  "question.replied",
  "question.rejected",
]);

async function _buildEnvelope(
  event: OpenCodeEvent,
  eventId: string,
  config?: Pick<ResolvedConfig, "actionContentMode">
    & Partial<Pick<ResolvedConfig, "instanceDisplayName">>
    & {
      actionOverride?: ActionEnvelopeOverride;
      allowOversizedAction?: boolean;
      idleClaim?: IdleClaim;
      errorClaim?: ErrorClaim;
    },
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

  const instanceDisplayName = config?.instanceDisplayName;
  if (instanceDisplayName) {
    envelope.instanceDisplayName = instanceDisplayName;
  }
  const projectName = _projectNameFromPath(event.projectName);
  if (projectName) {
    envelope.projectName = projectName;
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
  const modelVariant = _sanitiseActionText(event.modelVariant, MAX_AGENT_MODEL_LENGTH);
  if (modelVariant) {
    envelope.modelVariant = modelVariant;
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
  if (outputEvent === "opencode.permission_asked") {
    const permission = config?.actionOverride?.kind === "permission"
      ? config.actionOverride.permission
      : event.permission
        ? { count: 1, items: [_buildPermissionEnvelope(event, actionContentMode)].filter((item): item is PermissionItem => item !== undefined) }
        : undefined;
    if (permission) envelope.permission = permission;
  }
  if (outputEvent === "opencode.question_asked") {
    const question = config?.actionOverride?.kind === "question"
      ? config.actionOverride.question
      : _buildQuestionEnvelope(event, actionContentMode);
    if (question) envelope.question = question;
  }
  if (outputEvent === "opencode.session_error" && event.error) {
    envelope.error = _deriveErrorCategory(event.error);
  }

  // Timeline is deliberately attached only to a completed root cycle.  The
  // terminal claim was frozen before any async enrichment; never consult the
  // mutable current session state here.
  if (outputEvent === "opencode.session_idle" && event.sessionScope === "root") {
    const rootClaim = config?.idleClaim;
    if (rootClaim?.rootStartMs !== undefined && rootClaim.rootEndMs !== undefined) {
      envelope.subagentTimeline = _buildSubagentTimeline(
        rootClaim.rootRef,
        rootClaim.rootCycle,
        rootClaim.rootStartMs,
        rootClaim.rootEndMs,
        rootClaim.rootRunKey,
      );
    }
    // The user-wait timeline is always attached to a complete root
    // completion, even when zero waits were observed (empty intervals +
    // partial=false = reliable zero).  It was frozen at claim time; replies
    // arriving during enrichment cannot enter this envelope.
    if (rootClaim?.userWaitTimeline) {
      envelope.userWaitTimeline = rootClaim.userWaitTimeline;
    }
    // Root duration must be the frozen monotonic value: wall-clock jumps
    // must never distort the reported duration.  The wall clock only forms
    // the taskStartedAt/endedAt ISO labels above; the mono duration is
    // integerised and bounded at claim time.
    if (rootClaim?.monoReliable === true) {
      // Reliable mono capture present: only a valid in-range delta may set
      // durationMs.  An invalid/out-of-range mono delta (end<start or >
      // MAX_DURATION_MS) explicitly omits the top-level duration — it must
      // never fall back to the wall-derived value.
      if (rootClaim.rootDurationMonoMs !== undefined) {
        envelope.durationMs = rootClaim.rootDurationMonoMs;
      } else {
        delete envelope.durationMs;
      }
    }
    // Without reliable mono capture (legacy/direct callers) the wall-derived
    // duration computed above is intentionally left intact.
  }

  // The individual action limits keep this deterministic; retain a final guard
  // so a future allowlisted field cannot accidentally create an oversized hook.
  if (!config?.allowOversizedAction && _serializedEnvelopeBytes(envelope) > MAX_ENVELOPE_BYTES) {
    if (envelope.subagentTimeline) {
      // Timeline is optional; never allow it to turn an otherwise valid root
      // completion into a dropped envelope.
      delete envelope.subagentTimeline;
    }
  }
  if (!config?.allowOversizedAction && _serializedEnvelopeBytes(envelope) > MAX_ENVELOPE_BYTES) {
    if (envelope.userWaitTimeline) {
      // Same optional-treatment for the user-wait timeline.
      delete envelope.userWaitTimeline;
    }
  }
  if (!config?.allowOversizedAction && _serializedEnvelopeBytes(envelope) > MAX_ENVELOPE_BYTES) {
    return null;
  }
  return envelope;
}

function _actionRequestId(event: OpenCodeEvent, kind: ActionKind): string {
  const requestId = typeof event.requestId === "string" && event.requestId.length > 0
    ? event.requestId
    : typeof event.eventId === "string" && event.eventId.length > 0
      ? event.eventId
      : undefined;
  if (requestId) return requestId;

  // This opaque key is local-only. Without either official request id or the
  // wrapper event id, a later reply/rejection cannot precisely withdraw it.
  _log.warn(`[webhook-notifier] ${kind} action member has no reliable request id; precise withdrawal is unavailable`);
  return _generateId();
}

function _snapshotActionEvent(event: OpenCodeEvent): ActionEventSnapshot | null {
  if (!event.sessionId) return null;
  const snapshot: ActionEventSnapshot = {
    type: event.type === "question.asked" ? "question.asked" : "permission.updated",
    sessionId: event.sessionId,
    session: {
      name: _sanitiseName(event.session?.name ?? event.session?.title),
    },
    sessionScope: event.sessionScope,
    projectName: _projectNameFromPath(event.projectName),
    agent: _sanitiseActionText(event.agent, MAX_AGENT_MODEL_LENGTH),
    model: _normaliseModel(
      event.model !== undefined
        ? event.provider !== undefined && typeof event.model === "string"
          ? { provider: event.provider, model: event.model }
          : event.model
        : event.provider !== undefined
          ? { provider: event.provider }
          : undefined,
    ),
    modelVariant: _sanitiseActionText(event.modelVariant, MAX_AGENT_MODEL_LENGTH),
    startedAt: _safeTimestamp(event.startedAt),
    taskStartedAt: _safeTimestamp(event.taskStartedAt),
    endedAt: _safeTimestamp(event.endedAt),
    counts: _normaliseCounts(event.counts),
  };
  return snapshot;
}

function _buildActionMember(
  event: OpenCodeEvent,
  kind: ActionKind,
  mode: ActionContentMode,
): ActionBucketMember | undefined {
  const requestId = _actionRequestId(event, kind);
  if (kind === "permission") {
    const permission = _buildPermissionEnvelope(event, mode);
    return permission ? { requestId, permission } : undefined;
  }
  const question = _buildQuestionEnvelope(event, mode) ?? { count: 0, optionCount: 0 };
  return { requestId, question };
}

function _mergePermissionBucket(bucket: ActionBucket): PermissionEnvelope {
  const items: PermissionItem[] = [];
  for (const member of bucket.members.values()) {
    if (member.permission && items.length < MAX_PERMISSION_ITEMS) items.push(member.permission);
  }
  return { count: bucket.members.size, items };
}

function _mergeQuestionBucket(bucket: ActionBucket): QuestionEnvelope {
  let count = 0;
  let optionCount = 0;
  const summaries: string[] = [];
  const items: QuestionItem[] = [];
  for (const member of bucket.members.values()) {
    const question = member.question;
    if (!question) continue;
    count = Math.min(MAX_COUNT, count + (question.count ?? 0));
    optionCount = Math.min(MAX_COUNT, optionCount + (question.optionCount ?? 0));
    if (question.summary) summaries.push(question.summary);
    if (question.items) {
      for (const item of question.items) {
        if (items.length >= MAX_ACTION_ITEMS) break;
        items.push(item);
      }
    }
  }
  const result: QuestionEnvelope = { count, optionCount };
  if (summaries.length > 0) {
    result.summary = _sanitiseActionText(summaries.join("；"), MAX_ACTION_SUMMARY);
  }
  if (items.length > 0) result.items = items;
  return result;
}

function _actionOverride(bucket: ActionBucket): ActionEnvelopeOverride {
  return bucket.kind === "permission"
    ? { kind: "permission", permission: _mergePermissionBucket(bucket) }
    : { kind: "question", question: _mergeQuestionBucket(bucket) };
}

function _serializedEnvelopeBytes(envelope: Envelope): number {
  return _textEncoder.encode(JSON.stringify(envelope)).length;
}

/** Remove action正文 from an oversized aggregate without changing its ID/metadata. */
function _degradeActionEnvelope(envelope: Envelope, kind: ActionKind): Envelope {
  const degraded: Envelope = { ...envelope };
  if (kind === "permission" && envelope.permission) {
    degraded.permission = {
      count: envelope.permission.count,
      items: envelope.permission.items.map((item) => ({ category: item.category })),
    };
  }
  if (kind === "question" && envelope.question) {
    degraded.question = {
      count: envelope.question.count,
      optionCount: envelope.question.optionCount,
    };
  }
  return degraded;
}

function _prepareActionEnvelopeForSend(
  envelope: Envelope,
  kind: ActionKind,
  log: DiagnosticLog,
): Envelope | null {
  if (_serializedEnvelopeBytes(envelope) <= MAX_ENVELOPE_BYTES) return envelope;

  const degraded = _degradeActionEnvelope(envelope, kind);
  if (_serializedEnvelopeBytes(degraded) <= MAX_ENVELOPE_BYTES) {
    log.warn("[webhook-notifier] action aggregate exceeded 64KiB; sent count/category fallback");
    return degraded;
  }

  log.warn("[webhook-notifier] action aggregate exceeded 64KiB after fallback; skipped");
  return null;
}

async function _flushActionBucket(bucket: ActionBucket): Promise<void> {
  try {
    await _enrichEvent(
      bucket.baseEvent,
      bucket.input,
      bucket.diagnostics,
      bucket.config.auxiliarySessionNames,
    );
    const envelope = await _buildEnvelope(
      bucket.baseEvent,
      bucket.eventId,
      { ...bucket.config, actionOverride: _actionOverride(bucket), allowOversizedAction: true },
    );
    if (!envelope) return;
    const sendEnvelope = _prepareActionEnvelopeForSend(envelope, bucket.kind, _log);
    if (!sendEnvelope) return;
    _diagnoseOutgoingEnvelope(
      sendEnvelope,
      _metadataDiagnosticContextForSessionRef(
        { mode: bucket.config.metadataDiagnostics, log: _log },
        sendEnvelope.session.ref,
      ),
    );
    _log.warn(`[webhook-notifier] sending ${sendEnvelope.event}`);
    await _sendWithRetry(sendEnvelope, bucket.config, _log);
  } catch {
    _log.error("[webhook-notifier] unexpected internal error");
  }
}

async function _queueActionEvent(
  event: OpenCodeEvent,
  input: PluginInput,
  config: ResolvedConfig,
  diagnostics: MetadataDiagnosticContext,
  kind: ActionKind,
): Promise<void> {
  if (!event.sessionId) return;
  const snapshot = _snapshotActionEvent(event);
  const member = _buildActionMember(event, kind, config.actionContentMode);
  if (!snapshot || !member) return;

  let byKind = _actionBuckets.get(event.sessionId);
  if (!byKind) {
    byKind = new Map<ActionKind, ActionBucket>();
    _actionBuckets.set(event.sessionId, byKind);
  }
  let bucket = byKind.get(kind);
  if (!bucket) {
    const eventId = _generateId();
    bucket = {
      sessionId: event.sessionId,
      kind,
      eventId,
      createdAtMs: _nowMs(),
      timer: setTimeout(() => {
        const pending = _takeActionBucket(event.sessionId!, kind);
        if (pending) void _flushActionBucket(pending);
      }, ACTION_DEBOUNCE_MS),
      baseEvent: snapshot,
      members: new Map<string, ActionBucketMember>(),
      config,
      input,
      diagnostics,
    };
    _unrefTimer(bucket.timer);
    byKind.set(kind, bucket);
  }

  if (bucket.members.has(member.requestId)) return;
  if (bucket.members.size >= MAX_ACTION_BUCKET_REQUESTS) return;
  bucket.members.set(member.requestId, member);
  _cleanupActionBuckets();
}

function _removeActionRequest(event: OpenCodeEvent, kind: ActionKind): void {
  if (!event.sessionId || !event.requestId) return;
  const bucket = _actionBucketFor(event.sessionId, kind);
  if (!bucket) return;
  bucket.members.delete(event.requestId);
  if (bucket.members.size === 0) _deleteActionBucket(bucket);
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
    case "permission.asked":
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
  options?: { idleClaim?: IdleClaim; errorClaim?: ErrorClaim; statePrepared?: boolean },
): Promise<Envelope | null> {
  const rawSessionId = event.sessionId;
  if (!rawSessionId) return null;
  const t = event.type;

  // Replies only mutate the local pre-flush bucket. They never enter the
  // busy/idle state machine and never produce a webhook.  The wait collector
  // runs before the early return so disabled notifications never produce a
  // false zero wait statistic.
  if (t === "permission.replied") {
    await _recordUserWaitTerminal(
      event,
      "permission",
      event.reply === "reject" ? "rejected" : "replied",
    );
    _removeActionRequest(event, "permission");
    return null;
  }
  if (t === "question.replied" || t === "question.rejected") {
    await _recordUserWaitTerminal(event, "question", t === "question.rejected" ? "rejected" : "replied");
    _removeActionRequest(event, "question");
    return null;
  }

  // Direct callers retain the historical single-event processing behavior.
  // The V1 hook uses _queueActionEvent instead, so real notifications debounce.
  if (t === "permission.updated" || t === "permission.asked") {
    // Wait collection must precede the events filter below.
    await _recordUserWaitAsked(event, "permission");
    // Production V1 events must use _queueActionEvent so same-session action
    // requests get the fixed aggregation window. Keep this direct path only
    // for existing unit helpers and explicit internal callers.
    if (!config.events.has("permission_asked")) return null;
    return _buildEnvelope(event, _generateId(), config);
  }
  if (t === "question.asked") {
    // Wait collection must precede the events filter below.
    await _recordUserWaitAsked(event, "question");
    // Do not route the production hook through this immediate helper; it is a
    // compatibility path for direct tests/internal single-event processing.
    if (!config.events.has("question_asked")) return null;
    return _buildEnvelope(event, _generateId(), config);
  }

  const context = await _eventStateContext(event);
  if (!context) return null;
  const { sessionRef, state: st } = context;

  // --- session.status = busy ---
  if (t === "session.status" && event.status === "busy") {
    if (!options?.statePrepared && (!st.hadBusy || st.sentIdle)) {
      _startBusyCycle(
        st,
        sessionRef,
        _safeEpochMs(event.receivedAtMs) ?? _nowMs(),
        event.receivedMonoMs,
      );
    }
    return null; // no webhook for busy
  }

  // --- session.idle (deprecated) or session.status = idle ---
  if (t === "session.idle" || (t === "session.status" && event.status === "idle")) {
    const claim = options?.idleClaim ?? await _claimIdleEvent(event, config);
    if (!claim) return null;
    try {
      const envelope = await _buildEnvelope(event, claim.eventId, { ...config, idleClaim: claim });
      if (!envelope) {
        // Roll back only the claimed cycle. A newer busy cycle may already
        // have replaced this state while enrichment was in flight.  Evidence
        // stays frozen (not consumed), so a retry can re-report it.
        _rollbackIdleClaim(claim);
        return null;
      }
      // Commit the wait snapshot only after the envelope is successfully
      // built (the same boundary the existing claim uses: a build failure
      // rolls back, a successful build marks the cycle reported even if the
      // later transport fails and never resends).  This permanently consumes
      // the frozen evidence and marks included records as reported.
      _commitIdleClaim(claim);
      return envelope;
    } catch (error) {
      // _onEvent is a last-resort safety net, but rollback must happen here,
      // adjacent to the claim, when envelope construction rejects.
      _rollbackIdleClaim(claim);
      throw error;
    } finally {
      _idleProcessing.delete(sessionRef);
    }
  }

  // --- session.error ---
  if (t === "session.error") {
    const claim = options?.errorClaim ?? await _claimErrorEvent(event, config);
    if (!claim) return null;
    try {
      const envelope = await _buildEnvelope(event, claim.eventId, { ...config, errorClaim: claim });
      if (!envelope) {
        // Rollback: no error notification was produced, so this cycle must remain
        // eligible for a later error retry or the final idle notification.
        _rollbackErrorClaim(claim);
        return null;
      }
      return envelope;
    } catch (error) {
      _rollbackErrorClaim(claim);
      throw error;
    }
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
  // Check the exact serialized UTF-8 body before the first fetch. This also
  // protects callers that bypass action-bucket preparation.
  if (_serializedEnvelopeBytes(envelope) > MAX_ENVELOPE_BYTES) {
    log.warn("[webhook-notifier] envelope exceeded 64KiB; skipped before send");
    return;
  }
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
  options?: { idleClaim?: IdleClaim; errorClaim?: ErrorClaim; statePrepared?: boolean },
): Promise<void> {
  try {
    const envelope = await _processEvent(rawEvent, config, options);
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
    if (options?.idleClaim) _rollbackIdleClaim(options.idleClaim);
    if (options?.errorClaim) _rollbackErrorClaim(options.errorClaim);
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

function _questionOptionCount(raw: unknown): number {
  if (!Array.isArray(raw)) return 0;
  let count = 0;
  for (const rawQuestion of raw) {
    if (!rawQuestion || typeof rawQuestion !== "object" || Array.isArray(rawQuestion)) continue;
    const options = (rawQuestion as Record<string, unknown>).options;
    if (Array.isArray(options)) count = Math.min(MAX_COUNT, count + Math.min(options.length, MAX_ACTION_OPTIONS));
  }
  return count;
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
        eventId: wrapped.event.id,
        sessionId: typeof props.sessionID === "string" ? props.sessionID : undefined,
      };
    }

    case "session.error": {
      return {
        type,
        eventId: wrapped.event.id,
        sessionId: typeof props.sessionID === "string" ? props.sessionID : undefined,
        error: props.error ? { ...(props.error as Record<string, unknown>) } as OpenCodeEvent["error"] : undefined,
      };
    }

    case "permission.updated":
    case "permission.asked": {
      // properties IS the Permission object. Copy only the explicit action allowlist.
      return {
        type: "permission.updated",
        eventId: wrapped.event.id,
        requestId: typeof props.id === "string" ? props.id : undefined,
        sessionId: typeof props.sessionID === "string" ? props.sessionID : undefined,
        permission: _copyPermissionInput(props),
      };
    }

    case "permission.replied": {
      return {
        type,
        eventId: wrapped.event.id,
        requestId: typeof props.requestID === "string" ? props.requestID : undefined,
        sessionId: typeof props.sessionID === "string" ? props.sessionID : undefined,
        reply: typeof props.reply === "string" ? props.reply : undefined,
      };
    }

    case "question.asked": {
      // Copy only bounded question text/options; cwd, token and all other
      // properties stay outside the internal event.
      return {
        type,
        eventId: wrapped.event.id,
        requestId: typeof props.id === "string" ? props.id : undefined,
        sessionId: typeof props.sessionID === "string" ? props.sessionID : undefined,
        questions: _copyQuestionInputs(props.questions),
        questionCount: Array.isArray(props.questions) ? Math.min(props.questions.length, MAX_COUNT) : undefined,
        questionOptionCount: _questionOptionCount(props.questions),
      };
    }

    case "question.replied":
    case "question.rejected": {
      return {
        type,
        eventId: wrapped.event.id,
        requestId: typeof props.requestID === "string" ? props.requestID : undefined,
        sessionId: typeof props.sessionID === "string" ? props.sessionID : undefined,
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

/**
 * Consume official session lifecycle events before normalisation.  These are
 * metadata-only: `session.created` / `session.updated` populate the safe
 * Session metadata cache (never send a webhook), and `session.deleted`
 * removes the corresponding anonymous cache/scope/assistant/timeline entries.
 * Returns true when the event was consumed and must not reach the state
 * machine or transport.
 */
async function _consumeSessionMetadata(
  wrapped: unknown,
  auxiliarySessionNames: ReadonlySet<string> = DEFAULT_AUXILIARY_SESSION_NAMES,
): Promise<boolean> {
  const candidate = wrapped && typeof wrapped === "object"
    ? (wrapped as Record<string, unknown>).event
    : undefined;
  if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) return false;

  const rawEvent = candidate as Record<string, unknown>;
  const type = rawEvent.type;
  if (type !== "session.created" && type !== "session.updated" && type !== "session.deleted") {
    return false;
  }

  try {
    const properties = rawEvent.properties;
    if (!properties || typeof properties !== "object" || Array.isArray(properties)) return true;
    const props = properties as Record<string, unknown>;
    const info = _isRecord(props.info) ? props.info : undefined;

    const rawSessionId =
      typeof props.sessionID === "string" && props.sessionID.length > 0
        ? props.sessionID
        : typeof info?.id === "string" && info.id.length > 0
          ? info.id
          : undefined;
    if (!rawSessionId) return true;

    const sessionRef = await _hashSessionRef(rawSessionId);

    if (type === "session.deleted") {
      _dropSessionMetadata(sessionRef);
      _sessionScopes.delete(sessionRef);
      _assistantMetadata.delete(sessionRef);
      _sessions.delete(sessionRef);
      _dropSessionTimeline(sessionRef);
      return true;
    }

    // created/updated: properties.info is the full Session
    // ({ id, title, parentID, time }).  Only sanitised name, derived scope,
    // and created time are cached; raw ids/directory/projectID/parentID never.
    if (info) {
      const title = typeof info.title === "string" ? info.title : undefined;
      const name = _sanitiseName(
        title ?? (typeof info.name === "string" ? info.name : undefined),
      );
      const scope = _deriveSessionScope(info, auxiliarySessionNames);
      if (scope === "root" || scope === "subagent" || scope === "auxiliary") {
        _cacheSessionScope(sessionRef, scope);
      }
      const time = _isRecord(info.time) ? info.time : undefined;
      const startedAt = time
        ? _safeTimestamp((time as Record<string, unknown>).created)
        : undefined;
      _cacheSessionMetadata(sessionRef, { name, scope, startedAt });
    }
    return true;
  } catch {
    // Never expose raw ids or titles from a lifecycle event parse failure.
    return true;
  } finally {
    _cleanupSessions();
  }
}

// ─── Session Enrichment ──────────────────────────────────────

function _deriveSessionScope(
  data: unknown,
  auxiliarySessionNames: ReadonlySet<string> = DEFAULT_AUXILIARY_SESSION_NAMES,
): SessionScope {
  if (!data || typeof data !== "object" || Array.isArray(data)) return "unknown";
  const sessionData = data as Record<string, unknown>;
  const sessionName = _sanitiseName(
    typeof sessionData.title === "string"
      ? sessionData.title
      : typeof sessionData.name === "string"
        ? sessionData.name
        : undefined,
  );
  // Explicit auxiliary identities win independently of parentID.  Some
  // smartfetch helper sessions carry a parent for orchestration, but must
  // never become timeline subagents or focused notifications.
  if (sessionName && auxiliarySessionNames.has(sessionName)) return "auxiliary";

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
  auxiliarySessionNames: ReadonlySet<string> = DEFAULT_AUXILIARY_SESSION_NAMES,
  timelineCycle?: number,
): Promise<void> {
  if (!event.projectName) {
    event.projectName = _projectNameFromInput(input);
  }
  const rawSessionId = event.sessionId;
  if (!rawSessionId) return;

  const sessionRef = await _hashSessionRef(rawSessionId);
  const sessionDiagnostics = _metadataDiagnosticContextForSessionRef(diagnostics, sessionRef);

  // Apply cached safe Session metadata (from session.created/updated events or
  // a prior successful session.get) BEFORE any live enrichment.  Existing
  // event values always win; the cache is the first fallback source.
  const cachedMetadata = _cachedSessionMetadata(sessionRef);
  if (cachedMetadata?.name && !event.session?.name && !event.session?.title) {
    if (!event.session) event.session = {};
    event.session.name = cachedMetadata.name;
  }
  if (cachedMetadata?.startedAt && event.startedAt === undefined) {
    event.startedAt = cachedMetadata.startedAt;
  }
  if (event.sessionScope === undefined) {
    event.sessionScope = cachedMetadata?.scope ?? _cachedSessionScope(sessionRef) ?? "unknown";
  }
  const setScope = (scope: SessionScope): void => {
    event.sessionScope = scope;
    if (scope === "root" || scope === "subagent" || scope === "auxiliary") {
      _cacheSessionScope(sessionRef, scope);
    }
  };

  // Existing event values win; cache is the first enrichment source.
  _applyAssistantMetadata(event, _cachedAssistantMetadata(sessionRef));

  let sessionData: Record<string, unknown> | undefined;
  let timelineParentKnown = false;
  let timelineParentRef: string | undefined;
  try {
    const response = await input.client.session.get({ path: { id: rawSessionId } });
    const inspectedResponse = _inspectSessionResponse(response);
    _diagnoseSessionGet(inspectedResponse, sessionDiagnostics);
    const data = inspectedResponse.data;

    if (!data || typeof data !== "object" || Array.isArray(data)) {
      // session.get failed, returned an SDK error result
      // ({ error, request, response }), or produced no usable Session data.
      // Never treat the error shape as a Session, and never force the scope
      // back to root/unknown when a reliable cached scope was already applied.
      if (event.sessionScope === undefined || event.sessionScope === "unknown") {
        setScope("unknown");
      }
      _log.warn(SESSION_GET_WARNING);
    } else {
      sessionData = data as Record<string, unknown>;
      const derivedScope = _deriveSessionScope(sessionData, auxiliarySessionNames);
      setScope(derivedScope);

      // Hash the parent in this call stack and retain only the anonymous ref.
      // The raw parentID is never copied to the event, state, logs, or envelope.
      timelineParentKnown = true;
      const rawParentID = sessionData.parentID;
      if (typeof rawParentID === "string" && rawParentID.trim().length > 0) {
        timelineParentRef = await _hashSessionRef(rawParentID.trim());
        event.timelineParentRef = timelineParentRef;
      }

      // Only fill fields not already present in the event or assistant cache.
      if (!event.session) event.session = {};
      const sessionName =
        typeof sessionData.title === "string"
          ? sessionData.title
          : typeof sessionData.name === "string"
            ? sessionData.name
            : undefined;
      if (!event.session.name && sessionName) {
        event.session.name = sessionName;
      }
      if (!event.agent) {
        const sessionAgent = _sanitiseActionText(sessionData.agent ?? sessionData.mode, MAX_AGENT_MODEL_LENGTH);
        if (sessionAgent) event.agent = sessionAgent;
      }
      if (!event.model) {
        const model = _modelFromSessionData(sessionData);
        if (model) event.model = model;
      }
      if (!event.modelVariant) {
        const modelVariant = _modelVariantFromSessionData(sessionData);
        if (modelVariant) event.modelVariant = modelVariant;
      }

      const sessionTime =
        sessionData.time && typeof sessionData.time === "object" && !Array.isArray(sessionData.time)
          ? sessionData.time as Record<string, unknown>
          : undefined;
      const sessionStartedAt = _safeTimestamp(sessionTime?.created);
      if (sessionStartedAt && event.startedAt === undefined) {
        event.startedAt = sessionStartedAt;
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

      // session.get succeeded: refresh the safe metadata cache so a later
      // transient session.get failure still has a reliable fallback.
      _cacheSessionMetadata(sessionRef, {
        name: sessionName,
        scope: derivedScope,
        startedAt: sessionStartedAt,
      });
    }
  } catch {
    // Do not expose the exception, session ID, ref, title, or response body.
    _diagnoseSessionGet({ responseShape: "invalid" }, sessionDiagnostics);
    // Failure must not wipe out a reliable cached scope applied above.
    if (event.sessionScope === undefined || event.sessionScope === "unknown") {
      setScope("unknown");
    }
    _log.warn(SESSION_GET_WARNING);
  }

  const updateTimeline = (): void => {
    _updateTimelineIdentity(sessionRef, event, timelineParentKnown, timelineParentRef, timelineCycle);
    _updateTimelineTimingFromEvent(sessionRef, event, timelineCycle);
  };
  updateTimeline();

  // Busy is a state transition only; defer the potentially heavier messages
  // fallback until an event that can actually produce a notification.
  if (event.type === "session.status" && event.status === "busy") return;

  // The SDK fallback is called at most once and only while assistant metadata
  // remains missing after event, cache, and session.get enrichment.
  // Cycle timing is intentionally not treated as Assistant metadata here:
  // retain the existing messages fallback for modelVariant/agent/model
  // enrichment even when busy→idle already supplied complete timing.
  const hasCompleteAssistantTiming = event.cycleTimingReliable !== true
    && event.taskStartedAt
    && event.endedAt;
  if (event.agent && event.model && hasCompleteAssistantTiming) return;
  const sessionClient = input.client.session;
  if (typeof sessionClient.messages !== "function") return;

  try {
    // Keep the generated SDK method bound to its Session client.  Detaching
    // `messages` loses `this._client` and makes every fallback call fail.
    const response = await sessionClient.messages({ path: { id: rawSessionId }, query: { limit: 10 } });
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
        if (metadata.modelVariant) event.modelVariant = metadata.modelVariant;
        _applyAssistantMetadata(event, metadata);
      }
      break;
    }
    updateTimeline();
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

// ─── Plugin API Types (self-declared, matching OpenCode v1.18.x) ──
// No runtime dependency on @opencode-ai/plugin.

/** Plugin input context from the OpenCode runtime. */
interface PluginInput {
  client: {
    session: {
      get(params: { path: { id: string } }): Promise<unknown>;
      messages?(params: { path: { id: string }; query: { limit: number } }): Promise<unknown>;
    };
  };
  /** OpenCode project context; only worktree is used for the safe basename. */
  project?: { worktree?: unknown } | null;
  directory?: unknown;
  worktree?: unknown;
}

/** Plugin configuration options from opencode.jsonc plugins array. */
interface PluginOptions {
  url?: string;
  token?: string;
  timeoutMs?: number;
  enabled?: boolean;
  events?: string[];
  instanceDisplayName?: string;
  auxiliarySessionNames?: string[];
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
      // Capture wall and monotonic clocks adjacently at hook entry.  The
      // wall clock feeds existing ISO/timeline fields; the monotonic clock
      // feeds user-wait durations/offsets and is immune to wall-clock jumps.
      const receivedAtMs = _nowMs();
      const receivedMonoMs = _nowMonoMs();
      let idleClaim: IdleClaim | null = null;
      let errorClaim: ErrorClaim | null = null;
      let normalized: OpenCodeEvent | null = null;
      let isBusy = false;
      let handled = false;
      try {
        // v1.18.4 assistant updates are metadata-only and must be consumed
        // before normalisation so they never enter the state machine/HTTP path.
        if (await _consumeAssistantMetadata(wrapped, diagnostics)) return;
        if (await _consumeSessionMetadata(wrapped, config.auxiliarySessionNames)) return;

        const rawSessionId = typeof wrapped.event?.properties?.sessionID === "string"
          ? wrapped.event.properties.sessionID
          : undefined;
        if (!rawSessionId) return;

        // Per-session serial state phase: wait collector, busy/idle/error
        // transition, and claim freeze all run strictly in hook-receipt order.
        // The runtime may fire-and-forget, so async hashing must never let a
        // later event's collector/claim jump ahead of an earlier one.  Raw
        // session id is only a transient in-memory queue key (like
        // _actionBuckets) and never leaves this process.
        await _enqueueSessionState(rawSessionId, async () => {
          const n = _normalizeWrappedEvent(wrapped);
          if (!n) return;
          n.receivedAtMs = receivedAtMs;
          n.receivedMonoMs = receivedMonoMs;

          // The wait collector runs before any action-notification filter or
          // early return so disabled notifications never produce false zero
          // wait statistics.  It only uses anonymous sessionRef + kind + the
          // immediately hashed request key; raw ids never enter the map.
          const actionKind: ActionKind | undefined = n.type === "permission.updated"
            ? "permission"
            : n.type === "question.asked"
              ? "question"
              : undefined;
          if (actionKind) {
            await _recordUserWaitAsked(n, actionKind);
            const enabled = actionKind === "permission"
              ? config.events.has("permission_asked")
              : config.events.has("question_asked");
            if (enabled) {
              await _queueActionEvent(n, input, config, diagnostics, actionKind);
            }
            handled = true;
            return;
          }

          if (n.type === "permission.replied") {
            await _recordUserWaitTerminal(
              n,
              "permission",
              n.reply === "reject" ? "rejected" : "replied",
            );
            _removeActionRequest(n, "permission");
            handled = true;
            return;
          }
          if (n.type === "question.replied" || n.type === "question.rejected") {
            await _recordUserWaitTerminal(
              n,
              "question",
              n.type === "question.rejected" ? "rejected" : "replied",
            );
            _removeActionRequest(n, "question");
            handled = true;
            return;
          }

          // Busy transition happens inside the serialized state phase so a
          // later event can never claim a cycle before its busy was recorded.
          const busy = n.type === "session.status" && n.status === "busy";
          if (busy) {
            isBusy = true;
            normalized = n;
            await _onEvent(n, config, _log);
            return;
          }

          // Claim and freeze idle timing before enrichment. The claim remains
          // valid even if a newer busy cycle starts while enrichment is pending.
          const isIdle = n.type === "session.idle"
            || (n.type === "session.status" && n.status === "idle");
          if (isIdle) {
            idleClaim = await _claimIdleEvent(n, config);
            if (idleClaim) normalized = n;
            return;
          }

          // session.error has the same pre-enrichment claim boundary as root
          // idle.
          if (n.type === "session.error") {
            errorClaim = await _claimErrorEvent(n, config);
            if (errorClaim) normalized = n;
            return;
          }
        });

        // The state phase is done and the queue is released.  Enrichment and
        // network sends are deliberately NOT in the serialized queue so they
        // never block a later event's state transition or claim freeze.
        if (handled || !normalized?.sessionId) return;

        await _enrichEvent(
          normalized,
          input,
          diagnostics,
          config.auxiliarySessionNames,
          idleClaim?.cycle ?? errorClaim?.cycle,
        );
        await _onEvent(
          normalized,
          config,
          _log,
          isBusy
            ? { statePrepared: true }
            : idleClaim
              ? { idleClaim }
              : errorClaim
                ? { errorClaim }
                : undefined,
        );
      } catch {
        if (idleClaim) _rollbackIdleClaim(idleClaim);
        if (errorClaim) _rollbackErrorClaim(errorClaim);
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
  SubagentTimelineEnvelope,
  SubagentTimelineItem,
  TimelinePartialReason,
  TimelineItemStatus,
  TimelineTimingQuality,
  UserWaitKind,
  UserWaitResult,
  UserWaitIntervalState,
  UserWaitPartialReason,
  UserWaitInterval,
  UserWaitTimelineEnvelope,
  UserWaitRecord,
  OpenCodeEvent,
  SessionState,
  AssistantMetadata,
  CategoryInfo,
  DiagnosticLog,
  MetadataDiagnosticContext,
  Hooks,
  PluginModule,
  PluginInput,
  PluginOptions,
  Plugin,
  Event,
};

export {
  _hashSessionRef,
  _setClockForTests,
  _setMonoClockForTests,
  _setClocksForTests,
  _generateId,
  _sanitiseName,
  _projectNameFromPath,
  _projectNameFromInput,
  _deriveSessionScope,
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
  _claimIdleEvent,
  _rollbackIdleClaim,
  _commitIdleClaim,
  _idleProcessing,
  _actionBuckets,
  _resetActionBuckets,
  _normalizeWrappedEvent,
  _consumeAssistantMetadata,
  _consumeSessionMetadata,
  _resetMetadataDiagnostics,
  _metadataDiagnosticSamples,
  _metadataDiagnosticAnomalyCounts,
  _metadataDiagnosticAnomalySeen,
  _diagnoseOutgoingEnvelope,
  _metadataSampleSessions,
  _assistantMetadata,
  _sessionMetadata,
  _timelineRuns,
  _timelineParents,
  _timelineCapacityDrops,
  _buildSubagentTimeline,
  _cacheAssistantMetadata,
  _cachedAssistantMetadata,
  _cleanupAssistantMetadata,
  _enrichEvent,
  _userWaits,
  _waitMissingRequestIds,
  _waitEvicted,
  _recordUserWaitAsked,
  _recordUserWaitTerminal,
  _cleanupUserWaits,
  _freezeUserWaitTimeline,
  _freezeUserWaitSnapshot,
  _waitRequestKey,
  _hashWaitRequestKey,
  _userWaitIntervalForCycle,
  _enqueueSessionState,
  _hookQueueTails,
  _hookQueueDepth,
  _setWaitCollectorTestDelay,
  _setWaitEvidenceOverflowForTests,
  _safeMonoValue,
  _intMonoMs,
};
