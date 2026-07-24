/**
 * Bun native tests for the OpenCode Webhook Notifier Plugin.
 *
 * Black-box: mocks `@opencode-ai/plugin`, `fetch`, and timers.
 * No heavy JS toolchain.
 */

import { beforeEach, afterEach, describe, expect, it, mock } from "bun:test";

// ─── Import ──────────────────────────────────────────────────
const mod = await import("./webhook-notifier.ts");
const defaultModule = mod.default;

import type {
  Envelope,
  OpenCodeEvent,
  RawPluginOptions,
  ResolvedConfig,
  Hooks,
} from "./webhook-notifier.ts";

const {
  _hashSessionRef,
  _setClockForTests,
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
  _idleProcessing,
  _resetActionBuckets,
  _normalizeWrappedEvent,
  _consumeAssistantMetadata,
  _resetMetadataDiagnostics,
  _metadataDiagnosticSamples,
  _metadataSampleSessions,
  _assistantMetadata,
  _cacheAssistantMetadata,
  _cachedAssistantMetadata,
  _enrichEvent,
} = mod;

// ─── Helpers ─────────────────────────────────────────────────

function noopLog() {
  return { warn: () => {}, error: () => {} };
}

function makeEvent(overrides: Partial<OpenCodeEvent> & { type: string }): OpenCodeEvent {
  return {
    sessionId: "test-session-001",
    ...overrides,
  };
}

function makeConfig(overrides?: Partial<RawPluginOptions>): RawPluginOptions {
  return {
    url: "https://example.com/webhook",
    token: "test-token-123",
    timeoutMs: 5000,
    enabled: true,
    events: ["session_idle", "session_error", "permission_asked", "question_asked"],
    ...overrides,
  };
}

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** Global fetch mock helper — reassigns globalThis.fetch for a test body. */
let _originalFetch: typeof globalThis.fetch;

beforeEach(() => {
  _setClockForTests();
  _originalFetch = globalThis.fetch;
  _idleProcessing.clear();
  _resetActionBuckets();
  _sessionScopes.clear();
  _sessions.clear();
  _assistantMetadata.clear();
  _resetMetadataDiagnostics();
});

afterEach(() => {
  _setClockForTests();
  globalThis.fetch = _originalFetch;
});

// ─── Tests ────────────────────────────────────────────────────

describe("_resolveInterpolation", () => {
  it("returns value as-is when not an interpolation pattern", () => {
    expect(_resolveInterpolation("https://example.com/hook")).toBe("https://example.com/hook");
  });

  it("resolves {env:...} from process.env", () => {
    process.env.TEST_WEBHOOK_URL = "https://env-resolved/hook";
    expect(_resolveInterpolation("{env:TEST_WEBHOOK_URL}")).toBe("https://env-resolved/hook");
    delete process.env.TEST_WEBHOOK_URL;
  });

  it("returns null for unresolvable {env:...}", () => {
    expect(_resolveInterpolation("{env:MISSING_VAR_XXXX}")).toBeNull();
  });
});

describe("_shouldRetry", () => {
  it("retries network/timeout (no status)", () => {
    expect(_shouldRetry({ ok: false })).toBeTrue();
  });

  it("retries 429", () => {
    expect(_shouldRetry({ ok: false, status: 429 })).toBeTrue();
  });

  it("retries 5xx", () => {
    expect(_shouldRetry({ ok: false, status: 500 })).toBeTrue();
    expect(_shouldRetry({ ok: false, status: 502 })).toBeTrue();
    expect(_shouldRetry({ ok: false, status: 503 })).toBeTrue();
  });

  it("does NOT retry 401 / 403 / 413", () => {
    expect(_shouldRetry({ ok: false, status: 401 })).toBeFalse();
    expect(_shouldRetry({ ok: false, status: 403 })).toBeFalse();
    expect(_shouldRetry({ ok: false, status: 413 })).toBeFalse();
  });

  it("does NOT retry other 4xx", () => {
    expect(_shouldRetry({ ok: false, status: 404 })).toBeFalse();
    expect(_shouldRetry({ ok: false, status: 422 })).toBeFalse();
  });

  it("does NOT retry on success", () => {
    expect(_shouldRetry({ ok: true, status: 200 })).toBeFalse();
  });
});

describe("_backoffDelay", () => {
  it("increases with attempt number", () => {
    const d0 = _backoffDelay(0);
    const d1 = _backoffDelay(1);
    expect(d1).toBeGreaterThan(d0);
  });

  it("caps at MAX_BACKOFF_MS with jitter", () => {
    const d = _backoffDelay(4);
    // base: 400*16=6400, capped at 5000, +20% jitter → max 6000
    expect(d).toBeLessThanOrEqual(6000);
  });

  it("respects bounded Retry-After", () => {
    const d = _backoffDelay(0, "3");
    expect(d).toBeCloseTo(3000, -2);
  });

  it("ignores over-large Retry-After (capped at MAX_BACKOFF_MS)", () => {
    const d = _backoffDelay(0, "10");
    expect(d).toBeLessThanOrEqual(5000);
  });

  it("parses Retry-After HTTP-date format", () => {
    const future = new Date(Date.now() + 2000);
    const httpDate = future.toUTCString();
    const d = _backoffDelay(0, httpDate);
    expect(d).toBeGreaterThan(0);
    expect(d).toBeLessThanOrEqual(5000);
  });

  it("falls through to exponential backoff for invalid Retry-After", () => {
    const d = _backoffDelay(0, "not-a-number");
    expect(d).toBeGreaterThan(0);
    expect(d).toBeLessThanOrEqual(6000);
  });

  it("fall through for Retry-After with unparseable value", () => {
    const d = _backoffDelay(0, "abc");
    expect(d).toBeGreaterThan(0);
    expect(d).toBeLessThanOrEqual(6000);
  });

  it("fall through for Retry-After with negative numeric string", () => {
    // "-5" may be parsed as year -5 in some engines, yielding 0
    const d = _backoffDelay(0, "-5");
    // Must not throw; result can be 0 or a positive delay
    expect(typeof d).toBe("number");
    expect(Number.isFinite(d)).toBeTrue();
  });
});

describe("_mapEventType", () => {
  it("maps session.status idle → opencode.session_idle", () => {
    expect(_mapEventType(makeEvent({ type: "session.status", status: "idle" }))).toBe("opencode.session_idle");
  });

  it("maps session.idle → opencode.session_idle", () => {
    expect(_mapEventType(makeEvent({ type: "session.idle" }))).toBe("opencode.session_idle");
  });

  it("maps session.error → opencode.session_error", () => {
    expect(_mapEventType(makeEvent({ type: "session.error" }))).toBe("opencode.session_error");
  });

  it("maps permission.updated → opencode.permission_asked", () => {
    expect(_mapEventType(makeEvent({ type: "permission.updated" }))).toBe("opencode.permission_asked");
  });

  it("maps question.asked → opencode.question_asked", () => {
    expect(_mapEventType(makeEvent({ type: "question.asked" }))).toBe("opencode.question_asked");
  });

  it("does not map question completion events", () => {
    expect(_mapEventType(makeEvent({ type: "question.replied" }))).toBeNull();
    expect(_mapEventType(makeEvent({ type: "question.rejected" }))).toBeNull();
  });

  it("returns null for session.status busy (state only)", () => {
    expect(_mapEventType(makeEvent({ type: "session.status", status: "busy" }))).toBeNull();
  });

  it("returns null for non-target events", () => {
    expect(_mapEventType(makeEvent({ type: "command" }))).toBeNull();
    expect(_mapEventType(makeEvent({ type: "tool" }))).toBeNull();
    expect(_mapEventType(makeEvent({ type: "message" }))).toBeNull();
    expect(_mapEventType(makeEvent({ type: "diff" }))).toBeNull();
    expect(_mapEventType(makeEvent({ type: "todo" }))).toBeNull();
  });
});

describe("_hashSessionRef", () => {
  it("produces a deterministic 32-char hex string", async () => {
    const ref = await _hashSessionRef("session-abc");
    expect(ref).toMatch(/^[0-9a-f]{32}$/);
  });

  it("same input produces same hash", async () => {
    const [a, b] = await Promise.all([_hashSessionRef("same-id"), _hashSessionRef("same-id")]);
    expect(a).toBe(b);
  });

  it("different inputs produce different hashes", async () => {
    const [a, b] = await Promise.all([_hashSessionRef("id-1"), _hashSessionRef("id-2")]);
    expect(a).not.toBe(b);
  });

  it("is not trivially reversible", async () => {
    const ref = await _hashSessionRef("secret-session");
    expect(ref).not.toContain("secret");
    expect(ref).not.toContain("session");
  });
});

describe("_sanitiseName", () => {
  it("returns undefined for null/undefined/empty", () => {
    expect(_sanitiseName(null)).toBeUndefined();
    expect(_sanitiseName(undefined)).toBeUndefined();
    expect(_sanitiseName("")).toBeUndefined();
    expect(_sanitiseName("  ")).toBeUndefined();
  });

  it("trims whitespace", () => {
    expect(_sanitiseName("  hello  ")).toBe("hello");
  });

  it("removes dangerous Unicode (bidi, zero-width)", () => {
    expect(_sanitiseName("a\u202eb\u200bc")).toBe("abc");
  });

  it("normalises control characters to space", () => {
    expect(_sanitiseName("a\nb\tc")).toBe("a b c");
  });

  it("collapses multiple spaces", () => {
    expect(_sanitiseName("a    b")).toBe("a b");
  });

  it("truncates at 200 chars", () => {
    const long = "a".repeat(300);
    const r = _sanitiseName(long);
    expect(r).toBeDefined();
    expect(r!.length).toBeLessThanOrEqual(200);
  });

  it("preserves normal Unicode and HTML chars (server escapes)", () => {
    expect(_sanitiseName("<b>Hello</b>")).toBe("<b>Hello</b>");
    expect(_sanitiseName("你好🚀")).toBe("你好🚀");
  });

  it("returns undefined if only dangerous chars", () => {
    expect(_sanitiseName("\u202e\u200b")).toBeUndefined();
  });
});

describe("project and instance display names", () => {
  it("keeps only the safe basename using the documented input priority", () => {
    expect(_projectNameFromInput({
      client: {} as any,
      worktree: "/private/primary-project",
      project: { worktree: "/private/fallback-project" },
      directory: "/private/directory-project",
    } as any)).toBe("primary-project");
    expect(_projectNameFromInput({
      client: {} as any,
      project: { worktree: "/private/fallback-project" },
      directory: "/private/directory-project",
    } as any)).toBe("fallback-project");
    expect(_projectNameFromInput({
      client: {} as any,
      directory: "/private/directory-project",
    } as any)).toBe("directory-project");
  });

  it("omits roots and never returns the complete path", () => {
    expect(_projectNameFromPath("/")).toBeUndefined();
    expect(_projectNameFromPath(".")).toBeUndefined();
    expect(_projectNameFromPath("/private/project/")).toBe("project");
    expect(_projectNameFromPath("/private/project")).not.toContain("/private");
  });

  it("re-sanitises an internal project value before envelope construction", async () => {
    const envelope = await _buildEnvelope(
      makeEvent({ type: "session.idle", projectName: "/private/project" }),
      "evt-project-boundary",
    );
    expect(envelope!.projectName).toBe("project");
    expect(JSON.stringify(envelope)).not.toContain("/private/project");
  });

  it("includes the configured instanceDisplayName", async () => {
    const config = _resolveConfig(makeConfig({ instanceDisplayName: "Desktop A" }), noopLog())!;
    const envelope = await _buildEnvelope(
      makeEvent({ type: "session.idle", projectName: "actual-project" }),
      "evt-instance",
      config,
    );
    expect(envelope).toMatchObject({
      instanceDisplayName: "Desktop A",
      projectName: "actual-project",
    });
  });
});

describe("_deriveSessionScope", () => {
  it("recognises only the exact smartfetch auxiliary name", () => {
    expect(_deriveSessionScope({ title: "smartfetch-secondary" })).toBe("auxiliary");
    expect(_deriveSessionScope({ title: "foo-secondary" })).toBe("root");
  });

  it("prioritises a non-empty parentID over an auxiliary-looking name", () => {
    expect(_deriveSessionScope({ title: "smartfetch-secondary", parentID: "parent-1" })).toBe("subagent");
  });
});

describe("_deriveErrorCategory", () => {
  it("derives from error.name", () => {
    const r = _deriveErrorCategory({ name: "TimeoutError" });
    expect(r.category).toBe("timeouterror");
    expect(r.code).toBeUndefined();
  });

  it("includes status as code", () => {
    const r = _deriveErrorCategory({ name: "APIError", status: 500 });
    expect(r.category).toBe("apierror");
    expect(r.code).toBe("500");
  });

  it("falls back to 'unknown' for missing name", () => {
    const r = _deriveErrorCategory({});
    expect(r.category).toBe("unknown");
  });

  it("never reads message or responseBody", () => {
    const r = _deriveErrorCategory({
      name: "Error",
      message: "secret-token=abc123",
      responseBody: "very sensitive",
    } as any);
    expect(r.category).toBe("error");
    expect(r.code).toBeUndefined();
    expect(JSON.stringify(r)).not.toContain("secret");
    expect(JSON.stringify(r)).not.toContain("sensitive");
  });
});

describe("_derivePermissionCategory", () => {
  it("derives from permission.type", () => {
    expect(_derivePermissionCategory({ type: "file_access" })).toBe("file_access");
  });

  it("falls back to 'unknown'", () => {
    expect(_derivePermissionCategory({})).toBe("unknown");
  });

  it("never reads title or description", () => {
    const r = _derivePermissionCategory({
      type: "command",
      title: "rm -rf /",
      description: "delete everything",
    } as any);
    expect(r).toBe("command");
  });
});

describe("_resolveConfig", () => {
  it("returns null for null/undefined", () => {
    expect(_resolveConfig(undefined as any, noopLog())).toBeNull();
    expect(_resolveConfig(null as any, noopLog())).toBeNull();
  });

  it("returns null when enabled is false", () => {
    expect(_resolveConfig(makeConfig({ enabled: false }), noopLog())).toBeNull();
  });

  it("returns null for missing url", () => {
    expect(_resolveConfig(makeConfig({ url: "" }), noopLog())).toBeNull();
  });

  it("returns null for missing token", () => {
    expect(_resolveConfig(makeConfig({ token: "" }), noopLog())).toBeNull();
  });

  it("returns valid config with defaults", () => {
    const c = _resolveConfig(makeConfig(), noopLog());
    expect(c).not.toBeNull();
    expect(c!.url).toBe("https://example.com/webhook");
    expect(c!.token).toBe("test-token-123");
    expect(c!.timeoutMs).toBe(5000);
    expect(c!.enabled).toBeTrue();
    expect(c!.events.has("session_idle")).toBeTrue();
  });

  it("defaults events to all four when unspecified", () => {
    const c = _resolveConfig(makeConfig({ events: undefined }), noopLog());
    expect(c).not.toBeNull();
    expect(c!.events.has("session_idle")).toBeTrue();
    expect(c!.events.has("session_error")).toBeTrue();
    expect(c!.events.has("permission_asked")).toBeTrue();
    expect(c!.events.has("question_asked")).toBeTrue();
  });

  it("resolves {env:...} token from environment (example config pattern)", () => {
    process.env.OPENCODE_WEBHOOK_TOKEN = "env-resolved-secret";
    const c = _resolveConfig(
      makeConfig({ token: "{env:OPENCODE_WEBHOOK_TOKEN}" }),
      noopLog(),
    );
    expect(c).not.toBeNull();
    expect(c!.token).toBe("env-resolved-secret");
    delete process.env.OPENCODE_WEBHOOK_TOKEN;
  });

  it("does not silently enable on missing env var token", () => {
    delete process.env.OPENCODE_WEBHOOK_TOKEN;
    const c = _resolveConfig(
      makeConfig({ token: "{env:OPENCODE_WEBHOOK_TOKEN}" }),
      noopLog(),
    );
    expect(c).toBeNull();
  });

  it("defaults action content to strict and safely falls back for invalid values", () => {
    expect(_resolveConfig(makeConfig(), noopLog())!.actionContentMode).toBe("strict");
    expect(_resolveConfig(makeConfig({ actionContentMode: "invalid" }), noopLog())!.actionContentMode).toBe("strict");
    expect(_resolveConfig(makeConfig({ actionContentMode: "summary" }), noopLog())!.actionContentMode).toBe("summary");
    expect(_resolveConfig(makeConfig({ actionContentMode: "full" }), noopLog())!.actionContentMode).toBe("full");
  });

  it("defaults metadata diagnostics to off and accepts once/sample", () => {
    expect(_resolveConfig(makeConfig(), noopLog())!.metadataDiagnostics).toBe("off");
    expect(_resolveConfig(makeConfig({ metadataDiagnostics: "invalid" }), noopLog())!.metadataDiagnostics).toBe("off");
    expect(_resolveConfig(makeConfig({ metadataDiagnostics: "once" }), noopLog())!.metadataDiagnostics).toBe("once");
    expect(_resolveConfig(makeConfig({ metadataDiagnostics: "sample" }), noopLog())!.metadataDiagnostics).toBe("sample");
  });
});

describe("_buildEnvelope", () => {
  it("builds valid envelope with required fields", async () => {
    const e = await _buildEnvelope(
      makeEvent({ type: "session.idle" }),
      "evt-001",
    );
    expect(e).not.toBeNull();
    expect(e!.id).toBe("evt-001");
    expect(e!.event).toBe("opencode.session_idle");
    expect(e!.version).toBe(1);
    expect(e!.emittedAt).toBeTruthy();
    expect(e!.session.ref).toMatch(/^[0-9a-f]{32}$/);
  });

  it("derives durationMs only from taskStartedAt and endedAt", async () => {
    const e = await _buildEnvelope(
      makeEvent({
        type: "session.idle",
        agent: "my-agent",
        model: "gpt-5",
        durationMs: 999999,
        taskStartedAt: "2026-07-22T12:00:00Z",
        endedAt: "2026-07-22T12:00:15Z",
      }),
      "evt-002",
    );
    expect(e!.agent).toBe("my-agent");
    expect(e!.model).toBe("gpt-5");
    expect(e!.durationMs).toBe(15000);
    expect(e!.taskStartedAt).toBe("2026-07-22T12:00:00.000Z");
  });

  it("includes sanitised session.name", async () => {
    const e = await _buildEnvelope(
      makeEvent({ type: "session.idle", session: { name: "  My Task  " } }),
      "evt-003",
    );
    expect(e!.session.name).toBe("My Task");
  });

  it("omits session.name when empty", async () => {
    const e = await _buildEnvelope(
      makeEvent({ type: "session.idle", session: { name: undefined } }),
      "evt-004",
    );
    expect(e!.session.name).toBeUndefined();
  });

  it("uses the aggregate permission shape for permission_asked", async () => {
    const e = await _buildEnvelope(
      makeEvent({ type: "permission.updated", permission: { type: "file_write" } }),
      "evt-005",
    );
    expect(e!.permission).toEqual({ count: 1, items: [{ category: "file_write" }] });
  });

  it("question.asked envelope keeps only safe common fields", async () => {
    const e = await _buildEnvelope(
      makeEvent({
        type: "question.asked",
        questions: [{
          question: "send this secret question body",
          options: [{ label: "secret option", value: "/private/path" }],
        }],
        cwd: "/private/project",
        token: "secret-token",
      }),
      "evt-005-question",
    );
    expect(e).toMatchObject({
      id: "evt-005-question",
      event: "opencode.question_asked",
      version: 1,
      session: { ref: expect.stringMatching(/^[0-9a-f]{32}$/) },
    });
    expect(e!.permission).toBeUndefined();
    expect(e!.error).toBeUndefined();
    const json = JSON.stringify(e);
    expect(json).not.toContain("secret question body");
    expect(json).not.toContain("secret option");
    expect(json).not.toContain("/private/project");
    expect(json).not.toContain("secret-token");
  });

  it("includes error.category for session_error", async () => {
    const e = await _buildEnvelope(
      makeEvent({ type: "session.error", error: { name: "TimeoutError", status: 408 } }),
      "evt-006",
    );
    expect(e!.error).toBeDefined();
    expect(e!.error!.category).toBe("timeouterror");
    expect(e!.error!.code).toBe("408");
  });

  it("strict action mode keeps category/counts but no action text", async () => {
    const cfg = _resolveConfig(makeConfig(), noopLog())!;
    const e = await _buildEnvelope(
      makeEvent({
        type: "question.asked",
        questions: [{
          question: "Do the private operation?",
          options: [{ label: "Allow", description: "Sensitive description" }],
        }],
      }),
      "evt-strict",
      cfg,
    );
    expect(e!.question).toMatchObject({ count: 1, optionCount: 1 });
    expect(e!.question!.summary).toBeUndefined();
    expect(e!.question!.items).toBeUndefined();
    expect(JSON.stringify(e)).not.toContain("Sensitive description");
  });

  it("summary action mode sends cleaned summary and counts only", async () => {
    const cfg = _resolveConfig(makeConfig({ actionContentMode: "summary" }), noopLog())!;
    const e = await _buildEnvelope(
      makeEvent({
        type: "permission.updated",
        permission: {
          type: "file_access",
          title: "  Read private file\n  ",
          description: "Full permission description",
          target: "/private/project/file.txt",
          patterns: ["/private/project/**"],
        },
      }),
      "evt-summary",
      cfg,
    );
    expect(e!.permission).toEqual({
      count: 1,
      items: [{ category: "file_access", summary: "Read private file" }],
    });
    expect(JSON.stringify(e)).not.toContain("Full permission description");
  });

  it("full action mode sends only bounded allowlisted question/permission content", async () => {
    const cfg = _resolveConfig(makeConfig({ actionContentMode: "full" }), noopLog())!;
    const question = await _buildEnvelope(
      makeEvent({
        type: "question.asked",
        questions: [{
          question: "Choose an environment",
          header: "Environment",
          recommended: "staging",
          options: [
            { label: "Production", description: "Deploy to production", recommended: false },
            { label: "Staging", description: "Deploy to staging", recommended: true },
          ],
        }],
        cwd: "/private/project",
        token: "secret-token",
        headers: { authorization: "secret-header" },
      }),
      "evt-full-question",
      cfg,
    );
    expect(question!.question).toMatchObject({
      count: 1,
      optionCount: 2,
      summary: "Choose an environment",
    });
    expect(question!.question!.items![0]).toMatchObject({
      text: "Choose an environment",
      header: "Environment",
      recommended: "staging",
    });
    expect(question!.question!.items![0]!.options).toContainEqual({
      label: "Production",
      description: "Deploy to production",
      recommended: false,
    });
    const permission = await _buildEnvelope(
      makeEvent({
        type: "permission.updated",
        permission: {
          type: "command",
          title: "Run command",
          description: "Run the requested command",
          action: "execute",
          target: "/private/project",
          patterns: ["git *", "npm *"],
        },
      }),
      "evt-full-permission",
      cfg,
    );
    expect(permission!.permission).toEqual({
      count: 1,
      items: [{
        category: "command",
        title: "Run command",
        summary: "Run command",
        description: "Run the requested command",
        action: "execute",
        target: "/private/project",
        patterns: ["git *", "npm *"],
      }],
    });
    const json = JSON.stringify(question);
    expect(json).not.toContain("/private/project");
    expect(json).not.toContain("secret-token");
    expect(json).not.toContain("secret-header");
  });

  it("includes project/session/model/time and bounded low-sensitivity counts", async () => {
    const cfg = _resolveConfig(makeConfig({ instanceDisplayName: "Demo Instance" }), noopLog())!;
    const e = await _buildEnvelope(
      makeEvent({
        type: "session.idle",
        session: { name: "Session One" },
        agent: "build-agent",
        model: { providerID: "openai", modelID: "gpt-5" },
        durationMs: 65000,
        startedAt: "2026-07-22T12:00:00Z",
        taskStartedAt: "2026-07-22T12:00:00Z",
        endedAt: "2026-07-22T12:01:05Z",
        counts: { messages: 3, tools: 2, changes: 1 },
      }),
      "evt-rich",
      cfg,
    );
    expect(e).toMatchObject({
      instanceDisplayName: "Demo Instance",
      agent: "build-agent",
      model: "openai/gpt-5",
      durationMs: 65000,
      startedAt: "2026-07-22T12:00:00.000Z",
      taskStartedAt: "2026-07-22T12:00:00.000Z",
      endedAt: "2026-07-22T12:01:05.000Z",
      counts: { messages: 3, tools: 2, changes: 1 },
      session: { name: "Session One" },
    });
  });

  it("caps question and option arrays and total action text", async () => {
    const cfg = _resolveConfig(makeConfig({ actionContentMode: "full" }), noopLog())!;
    const e = await _buildEnvelope(
      makeEvent({
        type: "question.asked",
        questions: Array.from({ length: 20 }, (_, i) => ({
          question: `${i}-${"x".repeat(1000)}`,
          options: Array.from({ length: 20 }, (_, j) => ({ label: `${j}-${"y".repeat(1000)}` })),
        })),
      }),
      "evt-bounded",
      cfg,
    );
    expect(e!.question!.items).toHaveLength(8);
    expect(e!.question!.items![0]!.options).toHaveLength(12);
    expect(e!.question!.items![0]!.text!.length).toBeLessThanOrEqual(513);
    expect(JSON.stringify(e).length).toBeLessThan(64 * 1024);
  });
});

describe("_processEvent — state machine", () => {
  const config = _resolveConfig(makeConfig(), noopLog())!;

  it("busy → idle sends session_idle once", async () => {
    const busy = makeEvent({ type: "session.status", status: "busy", sessionId: "s1" });
    const idle = makeEvent({ type: "session.status", status: "idle", sessionId: "s1" });

    expect(await _processEvent(busy, config)).toBeNull();
    const env = await _processEvent(idle, config);
    expect(env).not.toBeNull();
    expect(env!.event).toBe("opencode.session_idle");

    // second idle → dedup
    expect(await _processEvent(idle, config)).toBeNull();
  });

  it("rolls back an idle claim when envelope construction throws", async () => {
    const sid = "idle-build-rollback";
    await _processEvent(makeEvent({ type: "session.status", status: "busy", sessionId: sid }), config);

    const brokenSession: Record<string, unknown> = {};
    Object.defineProperty(brokenSession, "name", {
      get: () => { throw new Error("synthetic envelope failure"); },
    });
    await expect(_processEvent(
      makeEvent({ type: "session.status", status: "idle", sessionId: sid, session: brokenSession as any }),
      config,
    )).rejects.toThrow("synthetic envelope failure");

    // The same cycle remains retryable after the failed build.
    expect(await _processEvent(
      makeEvent({ type: "session.status", status: "idle", sessionId: sid }),
      config,
    )).not.toBeNull();
  });

  it("does not let an old claim rollback mutate a newer busy cycle", async () => {
    const sid = "idle-rollback-new-cycle";
    await _processEvent(makeEvent({ type: "session.status", status: "busy", sessionId: sid }), config);
    const oldClaim = await _claimIdleEvent(makeEvent({ type: "session.status", status: "idle", sessionId: sid }), config);
    expect(oldClaim).not.toBeNull();

    await _processEvent(makeEvent({ type: "session.status", status: "busy", sessionId: sid }), config);
    _rollbackIdleClaim(oldClaim!);

    expect(await _processEvent(
      makeEvent({ type: "session.status", status: "idle", sessionId: sid }),
      config,
    )).not.toBeNull();
  });

  it("prefers complete busy→idle wall-clock timing over Assistant metadata", async () => {
    const sid = "timing-priority";
    await _processEvent(
      makeEvent({ type: "session.status", status: "busy", sessionId: sid, receivedAtMs: 1_000 }),
      config,
    );
    await _consumeAssistantMetadata({
      event: {
        id: "timing-priority-assistant",
        type: "message.updated",
        properties: {
          info: {
            sessionID: sid,
            role: "assistant",
            time: { created: 2_000, completed: 5_000 },
          },
        },
      },
    });

    const idle = makeEvent({ type: "session.status", status: "idle", sessionId: sid, receivedAtMs: 15_000 });
    await _enrichEvent(idle, { client: { session: { get: async () => ({ data: {} }) } } } as any);
    const env = await _processEvent(idle, config);

    expect(env).toMatchObject({
      taskStartedAt: "1970-01-01T00:00:01.000Z",
      endedAt: "1970-01-01T00:00:15.000Z",
      durationMs: 14_000,
    });
  });

  it("does not reset the cycle start on repeated busy events", async () => {
    const sid = "timing-repeated-busy";
    await _processEvent(
      makeEvent({ type: "session.status", status: "busy", sessionId: sid, receivedAtMs: 1_000 }),
      config,
    );
    await _processEvent(
      makeEvent({ type: "session.status", status: "busy", sessionId: sid, receivedAtMs: 5_000 }),
      config,
    );
    const env = await _processEvent(
      makeEvent({ type: "session.status", status: "idle", sessionId: sid, receivedAtMs: 10_000 }),
      config,
    );

    expect(env!.taskStartedAt).toBe("1970-01-01T00:00:01.000Z");
    expect(env!.endedAt).toBe("1970-01-01T00:00:10.000Z");
    expect(env!.durationMs).toBe(9_000);
  });

  it("resets timing for a new busy cycle", async () => {
    const sid = "timing-new-cycle";
    await _processEvent(
      makeEvent({ type: "session.status", status: "busy", sessionId: sid, receivedAtMs: 1_000 }),
      config,
    );
    await _processEvent(
      makeEvent({ type: "session.status", status: "idle", sessionId: sid, receivedAtMs: 2_000 }),
      config,
    );
    await _processEvent(
      makeEvent({ type: "session.status", status: "busy", sessionId: sid, receivedAtMs: 10_000 }),
      config,
    );
    const env = await _processEvent(
      makeEvent({ type: "session.status", status: "idle", sessionId: sid, receivedAtMs: 13_000 }),
      config,
    );

    expect(env!.taskStartedAt).toBe("1970-01-01T00:00:10.000Z");
    expect(env!.endedAt).toBe("1970-01-01T00:00:13.000Z");
    expect(env!.durationMs).toBe(3_000);
  });

  it("keeps Permission and Question events independent of busy-cycle timing", async () => {
    const sid = "timing-actions-independent";
    await _processEvent(
      makeEvent({ type: "session.status", status: "busy", sessionId: sid, receivedAtMs: 1_000 }),
      config,
    );
    expect(await _processEvent(
      makeEvent({ type: "permission.updated", sessionId: sid, permission: { type: "command" } }),
      config,
    )).not.toBeNull();
    expect(await _processEvent(
      makeEvent({ type: "question.asked", sessionId: sid, questions: [{ text: "Continue?" }] }),
      config,
    )).not.toBeNull();

    const env = await _processEvent(
      makeEvent({ type: "session.status", status: "idle", sessionId: sid, receivedAtMs: 10_000 }),
      config,
    );
    expect(env!.durationMs).toBe(9_000);
  });

  it("initial idle (no prior busy) is ignored", async () => {
    expect(await _processEvent(
      makeEvent({ type: "session.status", status: "idle", sessionId: "s2" }),
      config,
    )).toBeNull();
  });

  it("deprecated session.idle after busy sends and dedup", async () => {
    const busy = makeEvent({ type: "session.status", status: "busy", sessionId: "s3" });
    const legacyIdle = makeEvent({ type: "session.idle", sessionId: "s3" });

    await _processEvent(busy, config);
    expect(await _processEvent(legacyIdle, config)).not.toBeNull();
    expect(await _processEvent(legacyIdle, config)).toBeNull();
  });

  it("status idle + legacy idle dedup within same cycle", async () => {
    const busy = makeEvent({ type: "session.status", status: "busy", sessionId: "s4", receivedAtMs: 1_000 });
    const statusIdle = makeEvent({ type: "session.status", status: "idle", sessionId: "s4", receivedAtMs: 5_000 });
    const legacyIdle = makeEvent({ type: "session.idle", sessionId: "s4", receivedAtMs: 5_000 });

    await _processEvent(busy, config);
    const env = await _processEvent(statusIdle, config);
    expect(env).not.toBeNull();
    expect(env!.endedAt).toBe("1970-01-01T00:00:05.000Z");
    expect(env!.durationMs).toBe(4_000);
    expect(await _processEvent(legacyIdle, config)).toBeNull();
  });

  it("error sends immediately and suppresses subsequent idle", async () => {
    const busy = makeEvent({ type: "session.status", status: "busy", sessionId: "s5" });
    const err = makeEvent({ type: "session.error", sessionId: "s5", error: { name: "ExecError" } });
    const idle = makeEvent({ type: "session.status", status: "idle", sessionId: "s5" });

    await _processEvent(busy, config);
    const errEnv = await _processEvent(err, config);
    expect(errEnv).not.toBeNull();
    expect(errEnv!.event).toBe("opencode.session_error");

    // Idle after error → suppressed
    expect(await _processEvent(idle, config)).toBeNull();
  });

  it("new busy starts new cycle, clearing suppression", async () => {
    const busy1 = makeEvent({ type: "session.status", status: "busy", sessionId: "s6" });
    const err = makeEvent({ type: "session.error", sessionId: "s6", error: { name: "Err" } });
    const idle1 = makeEvent({ type: "session.status", status: "idle", sessionId: "s6" });
    const busy2 = makeEvent({ type: "session.status", status: "busy", sessionId: "s6" });
    const idle2 = makeEvent({ type: "session.status", status: "idle", sessionId: "s6" });

    await _processEvent(busy1, config);
    await _processEvent(err, config);
    expect(await _processEvent(idle1, config)).toBeNull();

    await _processEvent(busy2, config);
    const env = await _processEvent(idle2, config);
    expect(env).not.toBeNull();
    expect(env!.event).toBe("opencode.session_idle");
  });

  it("permission.updated sends regardless of state", async () => {
    const env = await _processEvent(
      makeEvent({ type: "permission.updated", sessionId: "s7", permission: { type: "command" } }),
      config,
    );
    expect(env).not.toBeNull();
    expect(env!.event).toBe("opencode.permission_asked");
  });

  it("question.asked sends action_required event regardless of state", async () => {
    const env = await _processEvent(
      makeEvent({
        type: "question.asked",
        sessionId: "s-question-asked",
        questions: [{ question: "secret", options: ["secret option"] }],
      }),
      config,
    );
    expect(env).not.toBeNull();
    expect(env!.event).toBe("opencode.question_asked");
  });

  it("question.replied and question.rejected are ignored", async () => {
    expect(await _processEvent(makeEvent({ type: "question.replied", sessionId: "s-question-replied" }), config)).toBeNull();
    expect(await _processEvent(makeEvent({ type: "question.rejected", sessionId: "s-question-rejected" }), config)).toBeNull();
  });

  it("processes configured full mode through the event state path", async () => {
    const fullConfig = _resolveConfig(makeConfig({ actionContentMode: "full" }), noopLog())!;
    const env = await _processEvent(
      makeEvent({
        type: "question.asked",
        sessionId: "s-question-full-process",
        questions: [{ question: "Full question" }],
      }),
      fullConfig,
    );
    expect(env!.question!.items![0]!.text).toBe("Full question");
  });

  it("different sessions are isolated", async () => {
    const aBusy = makeEvent({ type: "session.status", status: "busy", sessionId: "A" });
    const aIdle = makeEvent({ type: "session.status", status: "idle", sessionId: "A" });
    const bIdle = makeEvent({ type: "session.status", status: "idle", sessionId: "B" });

    await _processEvent(aBusy, config);
    expect(await _processEvent(bIdle, config)).toBeNull();
    expect(await _processEvent(aIdle, config)).not.toBeNull();
  });

  it("non-target events are ignored", async () => {
    for (const t of ["command", "tool", "message", "diff", "todo"]) {
      expect(await _processEvent(makeEvent({ type: t }), config)).toBeNull();
    }
  });

  it("event filter suppresses filtered event types", async () => {
    const cfg = _resolveConfig(makeConfig({ events: ["session_idle"] }), noopLog())!;
    const busy = makeEvent({ type: "session.status", status: "busy", sessionId: "s8" });
    const errEvt = makeEvent({ type: "session.error", sessionId: "s8", error: { name: "Err" } });
    const permEvt = makeEvent({ type: "permission.updated", sessionId: "s8", permission: { type: "cmd" } });

    await _processEvent(busy, cfg);
    expect(await _processEvent(errEvt, cfg)).toBeNull();
    expect(await _processEvent(permEvt, cfg)).toBeNull();
  });

  it("errors without prior busy still send", async () => {
    const env = await _processEvent(
      makeEvent({ type: "session.error", sessionId: "s9", error: { name: "StartupError" } }),
      config,
    );
    expect(env).not.toBeNull();
    expect(env!.event).toBe("opencode.session_error");
  });

  it("concurrent status idle + legacy idle only send one envelope (race)", async () => {
    const sid = "race-s1";
    await _processEvent(
      makeEvent({ type: "session.status", status: "busy", sessionId: sid }),
      config,
    );

    const statusIdle = makeEvent({ type: "session.status", status: "idle", sessionId: sid });
    const legacyIdle = makeEvent({ type: "session.idle", sessionId: sid });

    // Both dispatched before either completes — _hashSessionRef is async
    // so both calls will be in-flight simultaneously.
    const [r1, r2] = await Promise.all([
      _processEvent(statusIdle, config),
      _processEvent(legacyIdle, config),
    ]);

    const sent = [r1, r2].filter((r) => r !== null);
    expect(sent.length).toBe(1);
    if (sent[0]) {
      expect(sent[0].event).toBe("opencode.session_idle");
    }
  });

  it("concurrent same-type idle events only send one envelope (race)", async () => {
    const sid = "race-s2";
    await _processEvent(
      makeEvent({ type: "session.status", status: "busy", sessionId: sid }),
      config,
    );

    const idle = makeEvent({ type: "session.status", status: "idle", sessionId: sid });

    const [r1, r2] = await Promise.all([
      _processEvent(idle, config),
      _processEvent(idle, config),
    ]);

    const sent = [r1, r2].filter((r) => r !== null);
    expect(sent.length).toBe(1);
  });

  it("concurrent idle for different sessions both send", async () => {
    const sidA = "race-sA";
    const sidB = "race-sB";
    await Promise.all([
      _processEvent(
        makeEvent({ type: "session.status", status: "busy", sessionId: sidA }),
        config,
      ),
      _processEvent(
        makeEvent({ type: "session.status", status: "busy", sessionId: sidB }),
        config,
      ),
    ]);

    const idleA = makeEvent({ type: "session.status", status: "idle", sessionId: sidA });
    const idleB = makeEvent({ type: "session.status", status: "idle", sessionId: sidB });

    const [rA, rB] = await Promise.all([
      _processEvent(idleA, config),
      _processEvent(idleB, config),
    ]);

    expect(rA).not.toBeNull();
    expect(rB).not.toBeNull();
  });

  it("error suppression not broken by concurrent idle guard", async () => {
    const sid = "race-s3";
    await _processEvent(
      makeEvent({ type: "session.status", status: "busy", sessionId: sid }),
      config,
    );

    // Error should still send and suppress subsequent idle
    const errEnv = await _processEvent(
      makeEvent({ type: "session.error", sessionId: sid, error: { name: "Err" } }),
      config,
    );
    expect(errEnv).not.toBeNull();
    expect(errEnv!.event).toBe("opencode.session_error");

    // Idle suppressed by error, even concurrently
    const [idleR] = await Promise.all([
      _processEvent(
        makeEvent({ type: "session.status", status: "idle", sessionId: sid }),
        config,
      ),
    ]);
    expect(idleR).toBeNull();
  });

  it("new busy cycle works after concurrent idle guard claimed", async () => {
    const sid = "race-s4";
    await _processEvent(
      makeEvent({ type: "session.status", status: "busy", sessionId: sid }),
      config,
    );

    // Claim and release from idleProcessing via processing one idle
    const [r1] = await Promise.all([
      _processEvent(
        makeEvent({ type: "session.status", status: "idle", sessionId: sid }),
        config,
      ),
    ]);
    expect(r1).not.toBeNull();

    // New busy starts new cycle
    await _processEvent(
      makeEvent({ type: "session.status", status: "busy", sessionId: sid }),
      config,
    );

    // Next idle should send again
    const r2 = await _processEvent(
      makeEvent({ type: "session.status", status: "idle", sessionId: sid }),
      config,
    );
    expect(r2).not.toBeNull();
  });
});

describe("HTTP send — _sendSingle", () => {
  it("sends correct payload and headers", async () => {
    let captured: any = null;
    globalThis.fetch = mock(async (url: string, opts: any) => {
      captured = { url, headers: opts.headers, body: opts.body, method: opts.method };
      return new Response("ok", { status: 200 });
    });

    const envelope: Envelope = {
      id: "test-id",
      event: "opencode.session_idle",
      version: 1,
      emittedAt: "2026-07-22T12:00:00.000Z",
      session: { ref: "abcdef1234567890abcdef1234567890" },
    };

    const result = await _sendSingle(
      "https://hook.example.com/wh",
      "tok_abc",
      envelope,
      5000,
      1,
      noopLog(),
    );

    expect(result.ok).toBeTrue();
    expect(captured).not.toBeNull();
    expect(captured!.url).toBe("https://hook.example.com/wh");
    expect(captured!.method).toBe("POST");
    expect(captured!.headers["Content-Type"]).toBe("application/json");
    expect(captured!.headers["X-OpenCode-Event"]).toBe("opencode.session_idle");
    expect(captured!.headers["Authorization"]).toBe("Bearer tok_abc");

    const body = JSON.parse(captured!.body);
    expect(body.id).toBe("test-id");
    expect(body.event).toBe("opencode.session_idle");
    expect(body.version).toBe(1);
    expect(body.session.ref).toBe("abcdef1234567890abcdef1234567890");
  });

  it("extracts Retry-After header from 429 response", async () => {
    globalThis.fetch = mock(async () => {
      return new Response("rate limited", {
        status: 429,
        headers: { "Retry-After": "5" },
      });
    });

    const envelope: Envelope = {
      id: "ra-test-1",
      event: "opencode.session_idle",
      version: 1,
      emittedAt: "2026-07-22T12:00:00.000Z",
      session: { ref: "a".repeat(32) },
    };

    const result = await _sendSingle(
      "https://hook.example.com/wh",
      "tok",
      envelope,
      5000,
      1,
      noopLog(),
    );
    expect(result.ok).toBeFalse();
    expect(result.status).toBe(429);
    expect(result.retryAfter).toBe("5");
  });

  it("extracts Retry-After HTTP-date from 503 response", async () => {
    const future = new Date(Date.now() + 3000);
    const httpDate = future.toUTCString();
    globalThis.fetch = mock(async () => {
      return new Response("retry later", {
        status: 503,
        headers: { "Retry-After": httpDate },
      });
    });

    const envelope: Envelope = {
      id: "ra-test-2",
      event: "opencode.session_idle",
      version: 1,
      emittedAt: "2026-07-22T12:00:00.000Z",
      session: { ref: "a".repeat(32) },
    };

    const result = await _sendSingle(
      "https://hook.example.com/wh",
      "tok",
      envelope,
      5000,
      1,
      noopLog(),
    );
    expect(result.ok).toBeFalse();
    expect(result.status).toBe(503);
    expect(result.retryAfter).toBe(httpDate);
  });

  it("does not include retryAfter for 200 responses", async () => {
    globalThis.fetch = mock(async () => {
      return new Response("ok", { status: 200 });
    });

    const envelope: Envelope = {
      id: "ra-test-3",
      event: "opencode.session_idle",
      version: 1,
      emittedAt: "2026-07-22T12:00:00.000Z",
      session: { ref: "a".repeat(32) },
    };

    const result = await _sendSingle(
      "https://hook.example.com/wh",
      "tok",
      envelope,
      5000,
      1,
      noopLog(),
    );
    expect(result.ok).toBeTrue();
    expect(result.retryAfter).toBeUndefined();
  });
});

describe("_sendWithRetry — retry behaviour", () => {
  let config: ResolvedConfig;

  beforeEach(() => {
    config = _resolveConfig(makeConfig(), noopLog())!;
  });

  function makeEnv(overrides?: Partial<Envelope>): Envelope {
    return {
      id: "evt-retry",
      event: "opencode.session_idle",
      version: 1,
      emittedAt: "2026-07-22T12:00:00.000Z",
      session: { ref: "a".repeat(32) },
      ...overrides,
    };
  }

  it("succeeds on first attempt", async () => {
    let callCount = 0;
    globalThis.fetch = mock(async () => {
      callCount++;
      return new Response("ok", { status: 200 });
    });

    await _sendWithRetry(makeEnv(), config, noopLog());
    expect(callCount).toBe(1);
  });

  it("skips an oversized UTF-8 body before the first fetch", async () => {
    let callCount = 0;
    globalThis.fetch = mock(async () => {
      callCount++;
      return new Response("should not send", { status: 200 });
    });

    await _sendWithRetry(
      makeEnv({ question: { count: 1, optionCount: 1, summary: "界".repeat(40_000) } }),
      config,
      noopLog(),
    );
    expect(callCount).toBe(0);
  });

  it("retries on 500 up to 3 total attempts", async () => {
    let callCount = 0;
    globalThis.fetch = mock(async () => {
      callCount++;
      return new Response("error", { status: 500 });
    });

    await _sendWithRetry(makeEnv({ event: "opencode.session_error" }), config, noopLog());
    expect(callCount).toBe(3);
  });

  it("retries on network error up to 3 total attempts", async () => {
    let callCount = 0;
    globalThis.fetch = mock(async () => {
      callCount++;
      throw new TypeError("fetch failed");
    });

    await _sendWithRetry(makeEnv(), config, noopLog());
    expect(callCount).toBe(3);
  });

  it("does NOT retry on 401", async () => {
    let callCount = 0;
    globalThis.fetch = mock(async () => {
      callCount++;
      return new Response("unauthorized", { status: 401 });
    });

    await _sendWithRetry(makeEnv(), config, noopLog());
    expect(callCount).toBe(1);
  });

  it("does NOT retry on 403", async () => {
    let callCount = 0;
    globalThis.fetch = mock(async () => {
      callCount++;
      return new Response("forbidden", { status: 403 });
    });

    await _sendWithRetry(makeEnv(), config, noopLog());
    expect(callCount).toBe(1);
  });

  it("does NOT retry on 413", async () => {
    let callCount = 0;
    globalThis.fetch = mock(async () => {
      callCount++;
      return new Response("too large", { status: 413 });
    });

    await _sendWithRetry(makeEnv(), config, noopLog());
    expect(callCount).toBe(1);
  });

  it("retries on 429 with backoff", async () => {
    let callCount = 0;
    globalThis.fetch = mock(async () => {
      callCount++;
      return new Response("rate limited", { status: 429 });
    });

    await _sendWithRetry(makeEnv(), config, noopLog());
    expect(callCount).toBe(3);
  });

  it("429 with Retry-After header passes through to backoff", async () => {
    let callCount = 0;
    globalThis.fetch = mock(async () => {
      callCount++;
      // Use Retry-After: 0 which is valid but minimal for fast test
      if (callCount < 3) {
        return new Response("rate limited", {
          status: 429,
          headers: { "Retry-After": "1" },
        });
      }
      return new Response("ok", { status: 200 });
    });

    // This should retry with Retry-After-affected backoff and eventually succeed
    await _sendWithRetry(makeEnv(), config, noopLog());
    expect(callCount).toBe(3);
  });

  it("503 with Retry-After HTTP-date header passes through to backoff", async () => {
    const future = new Date(Date.now() + 1000);
    const httpDate = future.toUTCString();
    let callCount = 0;
    globalThis.fetch = mock(async () => {
      callCount++;
      if (callCount < 3) {
        return new Response("retry later", {
          status: 503,
          headers: { "Retry-After": httpDate },
        });
      }
      return new Response("ok", { status: 200 });
    });

    await _sendWithRetry(makeEnv(), config, noopLog());
    expect(callCount).toBe(3);
  });
});

// ─── Privacy: no sensitive data in logs ─────────────────────

describe("_idleProcessing — concurrent idle guard", () => {
  it("is a Set instance", () => {
    expect(_idleProcessing).toBeInstanceOf(Set);
  });

  it("is initially empty before tests", () => {
    expect(_idleProcessing.size).toBe(0);
  });
});

describe("privacy — no sensitive data in payloads or derivation", () => {
  it("_buildEnvelope does not include raw sessionId", async () => {
    const envelope = await _buildEnvelope(
      makeEvent({ type: "session.idle", sessionId: "super-secret-raw-id" }),
      "evt-p1",
    );
    const json = JSON.stringify(envelope);
    expect(json).not.toContain("super-secret-raw-id");
    expect(envelope!.session.ref).not.toBe("super-secret-raw-id");
  });

  it("_hashSessionRef does not leak raw ID", async () => {
    const ref = await _hashSessionRef("secret-raw-session");
    expect(ref).not.toContain("secret");
    expect(ref).not.toContain("raw");
    expect(ref).not.toContain("session");
  });

  it("error category derivation does not include raw message", () => {
    const r = _deriveErrorCategory({
      name: "Error",
      message: "Connection to db://prod-db:5432 failed",
    } as any);
    expect(JSON.stringify(r)).not.toContain("prod-db");
    expect(JSON.stringify(r)).not.toContain("5432");
  });

  it("permission category does not include title/description", () => {
    const r = _derivePermissionCategory({
      type: "file_access",
      title: "/etc/shadow",
    } as any);
    expect(r).not.toContain("shadow");
    expect(r).toBe("file_access");
  });
});

// ─── Cross-boundary payload structure check ─────────────────

describe("envelope structure — compatible with Python OpenCodeProviderAdapter", () => {
  it("produces correct top-level fields for session_idle", async () => {
    const envelope = await _buildEnvelope(
      makeEvent({
        type: "session.idle",
        session: { name: "My Session" },
        agent: "test-agent",
        model: "test-model",
        taskStartedAt: "2026-07-22T12:00:00Z",
        endedAt: "2026-07-22T12:00:01.234Z",
      }),
      "evt-xb-1",
    );

    expect(envelope).toMatchObject({
      id: "evt-xb-1",
      event: "opencode.session_idle",
      version: 1,
      session: { name: "My Session", ref: expect.stringMatching(/^[0-9a-f]{32}$/) },
      agent: "test-agent",
      model: "test-model",
      durationMs: 1234,
      taskStartedAt: "2026-07-22T12:00:00.000Z",
      endedAt: "2026-07-22T12:00:01.234Z",
    });
  });

  it("permission envelope matches Python schema", async () => {
    const envelope = await _buildEnvelope(
      makeEvent({ type: "permission.updated", permission: { type: "file_access" } }),
      "evt-xb-2",
    );

    expect(envelope).toMatchObject({
      id: "evt-xb-2",
      event: "opencode.permission_asked",
      version: 1,
      permission: { count: 1, items: [{ category: "file_access" }] },
      session: { ref: expect.any(String) },
    });
  });

  it("error envelope matches Python schema", async () => {
    const envelope = await _buildEnvelope(
      makeEvent({ type: "session.error", error: { name: "APIError", status: 500 } }),
      "evt-xb-3",
    );

    expect(envelope).toMatchObject({
      id: "evt-xb-3",
      event: "opencode.session_error",
      version: 1,
      error: { category: "apierror", code: "500" },
      session: { ref: expect.any(String) },
    });
  });

  it("does not include forbidden top-level fields", async () => {
    const envelope = await _buildEnvelope(
      makeEvent({ type: "session.idle" }),
      "evt-xb-4",
    );

    const keys = Object.keys(envelope!);
    expect(keys).not.toContain("cwd");
    expect(keys).not.toContain("project");
    expect(keys).not.toContain("raw");
    expect(keys).not.toContain("messages");
    expect(keys).not.toContain("tool");
    expect(keys).not.toContain("diff");
  });
});

// ─── Black-box: V1 Plugin loader path ───────────────────────

describe("PluginModule — default export shape", () => {
  it("has id and server properties", () => {
    expect(defaultModule).toHaveProperty("id");
    expect(defaultModule).toHaveProperty("server");
  });

  it("server is a function (Plugin signature)", () => {
    expect(typeof defaultModule.server).toBe("function");
  });

  it("has no tui property (file plugin)", () => {
    expect(defaultModule).not.toHaveProperty("tui");
  });

  it("id is 'webhook-notifier'", () => {
    expect(defaultModule.id).toBe("webhook-notifier");
  });
});

describe("Server — V1 loader integration path", () => {
  const mockInput = {
    worktree: "/private/My-Project",
    client: {
      session: {
        get: async () => ({ data: { title: "My Session" } }),
      },
    },
  };

  beforeEach(() => {
    globalThis.fetch = mock((url: string, opts: any) => {
      return new Response("ok", { status: 200 });
    });
  });

  // afterEach from the outer scope restores globalThis.fetch

  it("invalid config (empty) returns empty hooks, sends nothing", async () => {
    const hooks = await defaultModule.server(mockInput as any, {});
    expect(hooks).toEqual({});
  });

  it("invalid config (missing url) returns empty hooks", async () => {
    const hooks = await defaultModule.server(
      mockInput as any,
      makeConfig({ url: "" }),
    );
    expect(hooks).toEqual({});
  });

  it("invalid config (missing token) returns empty hooks", async () => {
    const hooks = await defaultModule.server(
      mockInput as any,
      makeConfig({ token: "" }),
    );
    expect(hooks).toEqual({});
  });

  it("valid config returns hooks with event function", async () => {
    const hooks = await defaultModule.server(
      mockInput as any,
      makeConfig(),
    );
    expect(hooks).toHaveProperty("event");
    expect(typeof hooks.event).toBe("function");
  });

  it("session.status busy→idle sends session_idle via official wrapper", async () => {
    const hooks = await defaultModule.server(
      mockInput as any,
      makeConfig(),
    );

    await hooks.event!({
      event: { id: "e1", type: "session.status", properties: { sessionID: "bb-s1", status: { type: "busy" } } },
    });
    await hooks.event!({
      event: { id: "e2", type: "session.status", properties: { sessionID: "bb-s1", status: { type: "idle" } } },
    });

    // fetch should have been called once (busy→idle transition)
    expect((globalThis.fetch as any).mock.calls.length).toBe(1);
    const body = JSON.parse((globalThis.fetch as any).mock.calls[0][1].body);
    expect(body.event).toBe("opencode.session_idle");
    expect(body.session.ref).toMatch(/^[0-9a-f]{32}$/);
    // Session name enriched from client.session.get
    expect(body.session.name).toBe("My Session");
    expect(body.instanceDisplayName).toBeUndefined();
    expect(body.projectName).toBe("My-Project");
  });

  it("session.status busy→idle with legacy string status via wrapper", async () => {
    // Also clears any prior state from "bb-s2" test
    const hooks = await defaultModule.server(
      { client: { session: { get: async () => ({}) } } } as any,
      makeConfig(),
    );

    await hooks.event!({
      event: { id: "e1", type: "session.status", properties: { sessionID: "bb-s2", status: "busy" } },
    });
    await hooks.event!({
      event: { id: "e2", type: "session.status", properties: { sessionID: "bb-s2", status: "idle" } },
    });

    expect((globalThis.fetch as any).mock.calls.length).toBe(1);
    const body = JSON.parse((globalThis.fetch as any).mock.calls[0][1].body);
    expect(body.event).toBe("opencode.session_idle");
  });

  it("session.idle (legacy) sends session_idle via official wrapper", async () => {
    const hooks = await defaultModule.server(
      mockInput as any,
      makeConfig(),
    );

    await hooks.event!({
      event: { id: "e1", type: "session.status", properties: { sessionID: "bb-s3", status: { type: "busy" } } },
    });
    await hooks.event!({
      event: { id: "e2", type: "session.idle", properties: { sessionID: "bb-s3" } },
    });

    expect((globalThis.fetch as any).mock.calls.length).toBe(1);
    const body = JSON.parse((globalThis.fetch as any).mock.calls[0][1].body);
    expect(body.event).toBe("opencode.session_idle");
    // Enriched session name from client.session.get
    expect(body.session.name).toBe("My Session");
  });

  it("session.error sends session_error via official wrapper", async () => {
    const hooks = await defaultModule.server(
      mockInput as any,
      makeConfig(),
    );

    await hooks.event!({
      event: {
        id: "e1",
        type: "session.error",
        properties: {
          sessionID: "bb-s4",
          error: { name: "TestError", status: 500 },
        },
      },
    });

    expect((globalThis.fetch as any).mock.calls.length).toBe(1);
    const body = JSON.parse((globalThis.fetch as any).mock.calls[0][1].body);
    expect(body.event).toBe("opencode.session_error");
    expect(body.error.category).toBe("testerror");
    expect(body.error.code).toBe("500");
  });

  it("permission.updated sends permission_asked via official wrapper", async () => {
    const hooks = await defaultModule.server(
      { client: { session: { get: async () => ({}) } } } as any,
      makeConfig(),
    );

    await hooks.event!({
      event: {
        id: "e1",
        type: "permission.updated",
        properties: { sessionID: "bb-s5", type: "file_access" },
      },
    });

    await wait(200);

    expect((globalThis.fetch as any).mock.calls.length).toBe(1);
    const body = JSON.parse((globalThis.fetch as any).mock.calls[0][1].body);
    expect(body.event).toBe("opencode.permission_asked");
    expect(body.permission).toEqual({ count: 1, items: [{ category: "file_access" }] });
  });

  it("question.asked sends question_asked without question data via official wrapper", async () => {
    const hooks = await defaultModule.server(
      { client: { session: { get: async () => ({}) } } } as any,
      makeConfig(),
    );

    await hooks.event!({
      event: {
        id: "e1",
        type: "question.asked",
        properties: {
          sessionID: "bb-s-question",
          questions: [{
            question: "secret question body",
            options: [{ label: "secret option", value: "/private/path" }],
          }],
          cwd: "/private/project",
          token: "secret-token",
        },
      },
    });

    await wait(200);

    expect((globalThis.fetch as any).mock.calls.length).toBe(1);
    const body = JSON.parse((globalThis.fetch as any).mock.calls[0][1].body);
    expect(body.event).toBe("opencode.question_asked");
    expect(body.session.ref).toMatch(/^[0-9a-f]{32}$/);
    const json = JSON.stringify(body);
    expect(json).not.toContain("secret question body");
    expect(json).not.toContain("secret option");
    expect(json).not.toContain("/private/project");
    expect(json).not.toContain("secret-token");
  });

  it("debounces same-session permissions, keeps sessions isolated, and deduplicates request IDs", async () => {
    const hooks = await defaultModule.server(
      { client: { session: { get: async () => ({}) } } } as any,
      makeConfig(),
    );
    await hooks.event!({
      event: { id: "outer-p1", type: "permission.asked", properties: { id: "p1", sessionID: "agg-A", type: "read" } },
    });
    await hooks.event!({
      event: { id: "outer-p1-duplicate", type: "permission.updated", properties: { id: "p1", sessionID: "agg-A", type: "read" } },
    });
    await hooks.event!({
      event: { id: "outer-p2", type: "permission.updated", properties: { id: "p2", sessionID: "agg-A", type: "write" } },
    });
    await hooks.event!({
      event: { id: "outer-p3", type: "permission.updated", properties: { id: "p3", sessionID: "agg-B", type: "execute" } },
    });
    expect((globalThis.fetch as any).mock.calls.length).toBe(0);
    await wait(220);

    expect((globalThis.fetch as any).mock.calls.length).toBe(2);
    const bodies = (globalThis.fetch as any).mock.calls.map((call: any[]) => JSON.parse(call[1].body));
    expect(bodies.map((body: any) => body.permission.count).sort()).toEqual([1, 2]);
    const aggregate = bodies.find((body: any) => body.permission.count === 2);
    expect(aggregate.permission.items.map((item: any) => item.category)).toEqual(["read", "write"]);
    expect(aggregate.session.ref).not.toBe(bodies.find((body: any) => body.permission.count === 1).session.ref);
  });

  it("uses a fixed 150ms action window without resetting on later events", async () => {
    const hooks = await defaultModule.server(
      { client: { session: { get: async () => ({}) } } } as any,
      makeConfig(),
    );
    await hooks.event!({
      event: { id: "fixed-window-1", type: "permission.asked", properties: { id: "fixed-p1", sessionID: "fixed-window", type: "read" } },
    });
    await wait(80);
    await hooks.event!({
      event: { id: "fixed-window-2", type: "permission.updated", properties: { id: "fixed-p2", sessionID: "fixed-window", type: "write" } },
    });

    // If the second event reset the timer, the bucket would not flush yet.
    await wait(90);
    expect((globalThis.fetch as any).mock.calls.length).toBe(1);
    const body = JSON.parse((globalThis.fetch as any).mock.calls[0][1].body);
    expect(body.permission.count).toBe(2);
  });

  it("withdraws a permission before flush and sends nothing for an empty bucket", async () => {
    const hooks = await defaultModule.server(
      { client: { session: { get: async () => ({}) } } } as any,
      makeConfig(),
    );
    await hooks.event!({
      event: { id: "outer-p1", type: "permission.asked", properties: { id: "p1", sessionID: "withdraw-perm", type: "read" } },
    });
    await hooks.event!({
      event: { id: "reply-p1", type: "permission.replied", properties: { sessionID: "withdraw-perm", requestID: "p1", reply: "once" } },
    });
    await wait(220);
    expect((globalThis.fetch as any).mock.calls.length).toBe(0);
  });

  it("keeps strict aggregated permissions to category-only items and caps details", async () => {
    const hooks = await defaultModule.server(
      { client: { session: { get: async () => ({}) } } } as any,
      makeConfig(),
    );
    for (let index = 0; index < 20; index++) {
      await hooks.event!({
        event: {
          id: `permission-outer-${index}`,
          type: "permission.asked",
          properties: {
            id: `permission-${index}`,
            sessionID: "permission-cap",
            type: `type-${index}`,
            title: "must not leave strict mode",
          },
        },
      });
    }
    await wait(220);
    expect((globalThis.fetch as any).mock.calls.length).toBe(1);
    const body = JSON.parse((globalThis.fetch as any).mock.calls[0][1].body);
    expect(body.permission.count).toBe(20);
    expect(body.permission.items).toHaveLength(16);
    expect(body.permission.items[0]).toEqual({ category: "type-0" });
    expect(JSON.stringify(body)).not.toContain("must not leave strict mode");
  });

  it("aggregates questions and preserves stable numbering in full mode", async () => {
    const hooks = await defaultModule.server(
      { client: { session: { get: async () => ({}) } } } as any,
      makeConfig({ actionContentMode: "full" }),
    );
    await hooks.event!({
      event: {
        id: "q-outer-1",
        type: "question.asked",
        properties: {
          id: "q1",
          sessionID: "agg-question",
          questions: [{ question: "First" }, { question: "Second", options: [{ label: "A" }, { label: "B" }] }],
        },
      },
    });
    await hooks.event!({
      event: {
        id: "q-outer-2",
        type: "question.asked",
        properties: { id: "q2", sessionID: "agg-question", questions: [{ question: "Third", options: [{ label: "C" }] }] },
      },
    });
    await wait(220);
    expect((globalThis.fetch as any).mock.calls.length).toBe(1);
    const body = JSON.parse((globalThis.fetch as any).mock.calls[0][1].body);
    expect(body.question).toMatchObject({ count: 3, optionCount: 3 });
    expect(body.question.items.map((item: any) => item.text)).toEqual(["First", "Second", "Third"]);
  });

  it("checks UTF-8 bytes and sends one stable-ID question fallback without the original body", async () => {
    const bodies: any[] = [];
    globalThis.fetch = mock(async (_url: string, options: any) => {
      bodies.push(JSON.parse(options.body));
      return new Response("ok", { status: 200 });
    });
    const hooks = await defaultModule.server(
      { client: { session: { get: async () => ({}) } } } as any,
      makeConfig({ actionContentMode: "full" }),
    );
    const unicodeQuestion = "问题".repeat(256);
    const unicodeOption = "选项".repeat(256);
    for (let index = 0; index < 8; index++) {
      await hooks.event!({
        event: {
          id: `oversized-question-${index}`,
          type: "question.asked",
          properties: {
            id: `oversized-q-${index}`,
            sessionID: "oversized-question-session",
            questions: [{
              question: unicodeQuestion,
              options: Array.from({ length: 12 }, () => ({ label: unicodeOption, description: unicodeOption })),
            }],
          },
        },
      });
    }
    await wait(220);

    expect(bodies).toHaveLength(1);
    expect(bodies[0]!.question).toEqual({ count: 8, optionCount: 96 });
    expect(new TextEncoder().encode(JSON.stringify(bodies[0])).length).toBeLessThanOrEqual(64 * 1024);
    expect(JSON.stringify(bodies[0])).not.toContain(unicodeQuestion);
    expect(JSON.stringify(bodies[0])).not.toContain(unicodeOption);
    expect(bodies[0]!.id).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("degrades oversized permission aggregates to count and safe categories", async () => {
    const bodies: any[] = [];
    globalThis.fetch = mock(async (_url: string, options: any) => {
      bodies.push(JSON.parse(options.body));
      return new Response("ok", { status: 200 });
    });
    const hooks = await defaultModule.server(
      { client: { session: { get: async () => ({}) } } } as any,
      makeConfig({ actionContentMode: "full" }),
    );
    const privateText = "路径内容".repeat(256);
    for (let index = 0; index < 16; index++) {
      await hooks.event!({
        event: {
          id: `oversized-permission-${index}`,
          type: "permission.asked",
          properties: {
            id: `oversized-p-${index}`,
            sessionID: "oversized-permission-session",
            type: `category-${index}`,
            title: privateText,
            description: privateText,
            action: privateText,
            target: privateText,
            patterns: Array.from({ length: 16 }, () => privateText),
          },
        },
      });
    }
    await wait(220);

    expect(bodies).toHaveLength(1);
    expect(bodies[0]!.permission.count).toBe(16);
    expect(bodies[0]!.permission.items).toEqual(
      Array.from({ length: 16 }, (_, index) => ({ category: `category-${index}` })),
    );
    expect(JSON.stringify(bodies[0])).not.toContain(privateText);
  });

  it("warns once for action members missing both IDs and keeps reply withdrawal fail-open", async () => {
    const warnings: string[] = [];
    const originalWarn = console.warn;
    console.warn = (...args: unknown[]) => warnings.push(args.map(String).join(" "));
    try {
      const hooks = await defaultModule.server(
        { client: { session: { get: async () => ({}) } } } as any,
        makeConfig(),
      );
      await hooks.event!({
        event: { id: "", type: "permission.asked", properties: { sessionID: "missing-action-id", type: "read" } },
      });
      await hooks.event!({
        event: { id: "outer-write", type: "permission.updated", properties: { id: "write-request", sessionID: "missing-action-id", type: "write" } },
      });
      await hooks.event!({
        event: { id: "missing-reply-id", type: "permission.replied", properties: { sessionID: "missing-action-id" } },
      });
      await wait(220);

      expect((globalThis.fetch as any).mock.calls.length).toBe(1);
      const body = JSON.parse((globalThis.fetch as any).mock.calls[0][1].body);
      expect(body.permission.count).toBe(2);
      expect(body.permission.items.map((item: any) => item.category)).toEqual(["read", "write"]);
    } finally {
      console.warn = originalWarn;
    }

    const missingIdWarnings = warnings.filter((line) => line.includes("no reliable request id"));
    expect(missingIdWarnings).toHaveLength(1);
    expect(missingIdWarnings[0]).not.toContain("missing-action-id");
    expect(missingIdWarnings[0]).not.toContain("read");
  });

  it("withdraws replied and rejected questions independently", async () => {
    const hooks = await defaultModule.server(
      { client: { session: { get: async () => ({}) } } } as any,
      makeConfig(),
    );
    await hooks.event!({
      event: { id: "q-outer-1", type: "question.asked", properties: { id: "q1", sessionID: "withdraw-question", questions: [{ question: "First" }] } },
    });
    await hooks.event!({
      event: { id: "q-outer-2", type: "question.asked", properties: { id: "q2", sessionID: "withdraw-question", questions: [{ question: "Second" }] } },
    });
    await hooks.event!({
      event: { id: "q-reply", type: "question.replied", properties: { sessionID: "withdraw-question", requestID: "q1" } },
    });
    await hooks.event!({
      event: { id: "q-reject", type: "question.rejected", properties: { sessionID: "withdraw-question", requestID: "q2" } },
    });
    await wait(220);
    expect((globalThis.fetch as any).mock.calls.length).toBe(0);
  });

  it("keeps permission and question buckets separate and assigns a new ID after flush", async () => {
    const hooks = await defaultModule.server(
      { client: { session: { get: async () => ({}) } } } as any,
      makeConfig(),
    );
    await hooks.event!({
      event: { id: "p-outer", type: "permission.asked", properties: { id: "p1", sessionID: "separate-kinds", type: "read" } },
    });
    await hooks.event!({
      event: { id: "q-outer", type: "question.asked", properties: { id: "q1", sessionID: "separate-kinds", questions: [{ question: "Answer" }] } },
    });
    await wait(220);
    expect((globalThis.fetch as any).mock.calls.length).toBe(2);
    const firstBodies = (globalThis.fetch as any).mock.calls.map((call: any[]) => JSON.parse(call[1].body));
    const firstIds = new Set(firstBodies.map((body: any) => body.id));
    expect(firstIds.size).toBe(2);

    await hooks.event!({
      event: { id: "p-outer-2", type: "permission.asked", properties: { id: "p2", sessionID: "separate-kinds", type: "write" } },
    });
    await wait(220);
    expect((globalThis.fetch as any).mock.calls.length).toBe(3);
    const secondPermission = JSON.parse((globalThis.fetch as any).mock.calls[2][1].body);
    expect(secondPermission.event).toBe("opencode.permission_asked");
    expect(secondPermission.id).not.toBe(firstBodies.find((body: any) => body.event === "opencode.permission_asked").id);
  });

  it("keeps the aggregate ID stable across retries", async () => {
    const bodies: any[] = [];
    let attempts = 0;
    globalThis.fetch = mock(async (_url: string, options: any) => {
      bodies.push(JSON.parse(options.body));
      attempts++;
      return attempts < 3
        ? new Response("retry", { status: 503, headers: { "Retry-After": "0" } })
        : new Response("ok", { status: 200 });
    });
    const hooks = await defaultModule.server(
      { client: { session: { get: async () => ({}) } } } as any,
      makeConfig(),
    );
    await hooks.event!({
      event: { id: "retry-outer", type: "permission.asked", properties: { id: "retry-p1", sessionID: "retry-session", type: "read" } },
    });
    await wait(260);
    expect(bodies).toHaveLength(3);
    expect(new Set(bodies.map((body) => body.id)).size).toBe(1);
  });

  it("question.replied and question.rejected do not send via official wrapper", async () => {
    const hooks = await defaultModule.server(
      { client: { session: { get: async () => ({}) } } } as any,
      makeConfig(),
    );

    await hooks.event!({ event: { id: "e1", type: "question.replied", properties: { sessionID: "bb-s-question-end" } } });
    await hooks.event!({ event: { id: "e2", type: "question.rejected", properties: { sessionID: "bb-s-question-end" } } });

    expect((globalThis.fetch as any).mock.calls.length).toBe(0);
  });

  it("unrecognized event types are silently ignored", async () => {
    const hooks = await defaultModule.server(
      mockInput as any,
      makeConfig(),
    );

    await hooks.event!({
      event: { id: "e1", type: "command", properties: {} },
    });
    await hooks.event!({
      event: { id: "e2", type: "tool", properties: {} },
    });

    expect((globalThis.fetch as any).mock.calls.length).toBe(0);
  });

  it("enrichment failure does not block event sending", async () => {
    const brokenInput = {
      client: {
        session: {
          get: async () => { throw new Error("API unavailable"); },
        },
      },
    };

    const hooks = await defaultModule.server(brokenInput as any, makeConfig());

    await hooks.event!({
      event: { id: "e1", type: "session.status", properties: { sessionID: "bb-s6", status: { type: "busy" } } },
    });
    await hooks.event!({
      event: { id: "e2", type: "session.status", properties: { sessionID: "bb-s6", status: { type: "idle" } } },
    });

    expect((globalThis.fetch as any).mock.calls.length).toBe(1);
    const body = JSON.parse((globalThis.fetch as any).mock.calls[0][1].body);
    expect(body.event).toBe("opencode.session_idle");
    // No session name since enrichment failed
    expect(body.session.name).toBeUndefined();
  });

  it("keeps an idle timing snapshot isolated when a new busy arrives during enrichment", async () => {
    let now = 1_000;
    _setClockForTests(() => now);
    let getCalls = 0;
    let releaseIdle!: (value: unknown) => void;
    let idleGetStarted!: () => void;
    const idleGetStartedPromise = new Promise<void>((resolve) => {
      idleGetStarted = resolve;
    });
    const pendingIdleGet = new Promise<unknown>((resolve) => {
      releaseIdle = resolve;
    });
    const input = {
      client: {
        session: {
          get: async () => {
            getCalls++;
            if (getCalls === 2) {
              idleGetStarted();
              return pendingIdleGet;
            }
            return { data: {} };
          },
        },
      },
    };
    const hooks = await defaultModule.server(input as any, makeConfig());
    const sid = "server-timing-isolation";

    await hooks.event!({
      event: { id: "cycle-1-busy", type: "session.status", properties: { sessionID: sid, status: { type: "busy" } } },
    });
    now = 5_000;
    const oldIdle = hooks.event!({
      event: { id: "cycle-1-idle", type: "session.status", properties: { sessionID: sid, status: { type: "idle" } } },
    });
    await idleGetStartedPromise;

    now = 9_000;
    await hooks.event!({
      event: { id: "cycle-2-busy", type: "session.status", properties: { sessionID: sid, status: { type: "busy" } } },
    });
    releaseIdle({ data: {} });
    await oldIdle;

    const firstBody = JSON.parse((globalThis.fetch as any).mock.calls[0][1].body);
    expect(firstBody).toMatchObject({
      taskStartedAt: "1970-01-01T00:00:01.000Z",
      endedAt: "1970-01-01T00:00:05.000Z",
      durationMs: 4_000,
    });

    now = 12_000;
    await hooks.event!({
      event: { id: "cycle-2-idle", type: "session.status", properties: { sessionID: sid, status: { type: "idle" } } },
    });
    const secondBody = JSON.parse((globalThis.fetch as any).mock.calls[1][1].body);
    expect(secondBody).toMatchObject({
      taskStartedAt: "1970-01-01T00:00:09.000Z",
      endedAt: "1970-01-01T00:00:12.000Z",
      durationMs: 3_000,
    });
  });

  it("empty properties object is handled safely", async () => {
    const hooks = await defaultModule.server(
      { client: { session: { get: async () => ({}) } } } as any,
      makeConfig(),
    );

    // session.status without status field should be ignored
    await hooks.event!({
      event: { id: "e1", type: "session.status", properties: {} },
    });
    expect((globalThis.fetch as any).mock.calls.length).toBe(0);
  });

  it("server-level concurrent idle guard works through wrapper", async () => {
    // Create a new server instance with a fresh fetch counter
    let callCount = 0;
    const freshFetch = mock((url: string, opts: any) => {
      callCount++;
      return new Response("ok", { status: 200 });
    });
    globalThis.fetch = freshFetch;

    const hooks = await defaultModule.server(
      { client: { session: { get: async () => ({}) } } } as any,
      makeConfig(),
    );

    await hooks.event!({
      event: { id: "e1", type: "session.status", properties: { sessionID: "bb-s7", status: { type: "busy" } } },
    });

    // Reset counter
    callCount = 0;

    const [r1, r2] = await Promise.all([
      hooks.event!({ event: { id: "e2", type: "session.status", properties: { sessionID: "bb-s7", status: { type: "idle" } } } }),
      hooks.event!({ event: { id: "e3", type: "session.idle", properties: { sessionID: "bb-s7" } } }),
    ]);

    // Only one webhook call for concurrent idle
    expect(callCount).toBe(1);
  });

  it("different sessions are isolated through the server wrapper", async () => {
    const hooks = await defaultModule.server(
      { client: { session: { get: async () => ({}) } } } as any,
      makeConfig(),
    );

    // Busy session A only
    await hooks.event!({
      event: { id: "e1", type: "session.status", properties: { sessionID: "bb-A", status: { type: "busy" } } },
    });
    // Idle for B (no prior busy) should be ignored
    await hooks.event!({
      event: { id: "e2", type: "session.status", properties: { sessionID: "bb-B", status: { type: "idle" } } },
    });
    // Idle for A should send
    await hooks.event!({
      event: { id: "e3", type: "session.status", properties: { sessionID: "bb-A", status: { type: "idle" } } },
    });

    expect((globalThis.fetch as any).mock.calls.length).toBe(1);
  });
});

describe("_normalizeWrappedEvent — wrapper event mapping", () => {
  it("session.status with object status maps correctly", () => {
    const result = _normalizeWrappedEvent({
      event: { id: "e1", type: "session.status", properties: { sessionID: "s1", status: { type: "busy" } } },
    });
    expect(result).not.toBeNull();
    expect(result!.type).toBe("session.status");
    expect(result!.sessionId).toBe("s1");
    expect(result!.status).toBe("busy");
  });

  it("session.status with legacy string status maps correctly", () => {
    const result = _normalizeWrappedEvent({
      event: { id: "e1", type: "session.status", properties: { sessionID: "s1", status: "idle" } },
    });
    expect(result).not.toBeNull();
    expect(result!.type).toBe("session.status");
    expect(result!.status).toBe("idle");
  });

  it("session.status without status field returns null", () => {
    const result = _normalizeWrappedEvent({
      event: { id: "e1", type: "session.status", properties: { sessionID: "s1" } },
    });
    expect(result).toBeNull();
  });

  it("session.idle maps correctly", () => {
    const result = _normalizeWrappedEvent({
      event: { id: "e1", type: "session.idle", properties: { sessionID: "s1" } },
    });
    expect(result).not.toBeNull();
    expect(result!.type).toBe("session.idle");
    expect(result!.sessionId).toBe("s1");
  });

  it("session.error maps correctly", () => {
    const result = _normalizeWrappedEvent({
      event: {
        id: "e1",
        type: "session.error",
        properties: { sessionID: "s1", error: { name: "Err", status: 500 } },
      },
    });
    expect(result).not.toBeNull();
    expect(result!.type).toBe("session.error");
    expect(result!.sessionId).toBe("s1");
    expect(result!.error?.name).toBe("Err");
    expect(result!.error?.status).toBe(500);
  });

  it("permission.updated maps correctly", () => {
    const result = _normalizeWrappedEvent({
      event: {
        id: "e1",
        type: "permission.updated",
        properties: { sessionID: "s1", type: "file_access" },
      },
    });
    expect(result).not.toBeNull();
    expect(result!.type).toBe("permission.updated");
    expect(result!.sessionId).toBe("s1");
    expect(result!.permission?.type).toBe("file_access");
  });

  it("question.asked maps sessionID and only allowlisted bounded question data", () => {
    const result = _normalizeWrappedEvent({
      event: {
        id: "e1",
        type: "question.asked",
        properties: {
          sessionID: "s-question",
          questions: [{ question: "secret", options: ["secret option"] }],
          cwd: "/private/project",
        },
      },
    });
    expect(result).not.toBeNull();
    expect(result!.type).toBe("question.asked");
    expect(result!.sessionId).toBe("s-question");
    expect(result!.questions).toHaveLength(1);
    expect(result!.questions![0]!.text).toBe("secret");
    expect(result!.questions![0]!.options![0]!.label).toBe("secret option");
    expect(result).not.toHaveProperty("cwd");
  });

  it("question.replied and question.rejected are normalized for bucket withdrawal", () => {
    expect(_normalizeWrappedEvent({
      event: { id: "e1", type: "question.replied", properties: { sessionID: "s-question" } },
    })).toMatchObject({ type: "question.replied", sessionId: "s-question" });
    expect(_normalizeWrappedEvent({
      event: { id: "e2", type: "question.rejected", properties: { sessionID: "s-question" } },
    })).toMatchObject({ type: "question.rejected", sessionId: "s-question" });
  });

  it("permission.asked compatibility event maps to permission.updated", () => {
    const result = _normalizeWrappedEvent({
      event: {
        id: "e1",
        type: "permission.asked",
        properties: { sessionID: "s1", type: "bash" },
      },
    });
    expect(result).not.toBeNull();
    expect(result!.type).toBe("permission.updated");
    expect(result!.sessionId).toBe("s1");
    expect(result!.permission?.type).toBe("bash");
  });

  it("unrecognized event types return null", () => {
    const result = _normalizeWrappedEvent({
      event: { id: "e1", type: "command", properties: {} },
    });
    expect(result).toBeNull();
  });

  it("null/undefined properties handled safely", () => {
    const result = _normalizeWrappedEvent({
      event: { id: "e1", type: "session.idle", properties: null as any },
    });
    expect(result).not.toBeNull();
    expect(result!.sessionId).toBeUndefined();
  });

  it("missing type returns null", () => {
    const result = _normalizeWrappedEvent({
      event: { id: "e1", type: "", properties: {} },
    });
    expect(result).toBeNull();
  });

  it("properties.sessionID is mapped to sessionId (key rename, no direct leak)", () => {
    const result = _normalizeWrappedEvent({
      event: {
        id: "e1",
        type: "session.idle",
        properties: { sessionID: "raw-session-xyz" },
      },
    });
    // The properties key "sessionID" (capital D) is NOT in the output
    expect(JSON.stringify(result)).not.toContain('"sessionID"');
    // The raw value is mapped to "sessionId" (lowercase d) for internal use
    expect(result!.sessionId).toBe("raw-session-xyz");
    // The value is hashed later by _buildEnvelope / _processEvent
    // and only the hash (session.ref) appears in the webhook payload
  });
});

describe("v1.18.4 message.updated assistant metadata", () => {
  it("caches only cleaned assistant metadata and ignores user/malformed updates", async () => {
    const sessionId = "assistant-cache-session";
    await _consumeAssistantMetadata({
      event: {
        id: "message-raw-id",
        type: "message.updated",
        properties: {
          info: {
            sessionID: sessionId,
            messageID: "message-secret",
            role: "assistant",
            mode: " build-agent ",
            providerID: "provider-a",
            modelID: "model-a",
            variant: "medium",
            time: { created: 1_749_999_999_000, completed: 1_750_000_000_000 },
            parts: [{ text: "private body" }],
            tokens: { input: 10 },
            cost: 42,
          },
        },
      },
    });

    const ref = await _hashSessionRef(sessionId);
    expect(_assistantMetadata.get(ref)).toEqual({
      agent: "build-agent",
      providerID: "provider-a",
      modelID: "model-a",
      modelVariant: "medium",
      created: "2025-06-15T15:06:39.000Z",
      completed: "2025-06-15T15:06:40.000Z",
    });
    expect(JSON.stringify(_assistantMetadata)).not.toContain(sessionId);
    expect(JSON.stringify(_assistantMetadata)).not.toContain("message-secret");
    expect(JSON.stringify(_assistantMetadata)).not.toContain("private body");
    expect(JSON.stringify(_assistantMetadata)).not.toContain("tokens");
    expect(JSON.stringify(_assistantMetadata)).not.toContain("cost");

    await _consumeAssistantMetadata({
      event: { id: "user-message", type: "message.updated", properties: { info: { sessionID: sessionId, role: "user", mode: "user" } } },
    });
    await _consumeAssistantMetadata({
      event: { id: "malformed", type: "message.updated", properties: { info: { sessionID: sessionId, role: "assistant", parts: [] } } },
    });
    expect(_assistantMetadata.size).toBe(1);
    expect(_assistantMetadata.get(ref)).toEqual({
      agent: "build-agent",
      providerID: "provider-a",
      modelID: "model-a",
      modelVariant: "medium",
      created: "2025-06-15T15:06:39.000Z",
      completed: "2025-06-15T15:06:40.000Z",
    });
  });

  it("is consumed by the plugin entry without sending or entering the state machine", async () => {
    let sendCount = 0;
    globalThis.fetch = mock(async () => {
      sendCount++;
      return new Response("ok", { status: 200 });
    });
    const hooks = await defaultModule.server(
      { client: { session: { get: async () => ({ data: {} }) } } } as any,
      makeConfig(),
    );

    await hooks.event!({
      event: {
        id: "message-event",
        type: "message.updated",
        properties: {
          info: { sessionID: "message-only", role: "assistant", mode: "agent", providerID: "p", modelID: "m" },
        },
      },
    });

    expect(sendCount).toBe(0);
    expect(_sessions.size).toBe(0);
  });

  it("prefers Assistant info.variant over session.model.variant", async () => {
    globalThis.fetch = mock(async () => new Response("ok", { status: 200 }));
    const hooks = await defaultModule.server(
      {
        client: {
          session: {
            get: async () => ({
              data: {
                model: { providerID: "provider-a", modelID: "model-a", variant: "medium" },
                time: { created: 1_750_000_000_000 },
              },
            }),
          },
        },
      } as any,
      makeConfig(),
    );

    await hooks.event!({
      event: {
        id: "message-variant-priority",
        type: "message.updated",
        properties: {
          info: {
            sessionID: "variant-priority-session",
            role: "assistant",
            mode: "agent",
            providerID: "provider-a",
            modelID: "model-a",
            variant: "max",
            time: { created: 1_750_000_000_000, completed: 1_750_000_001_000 },
          },
        },
      },
    });
    await hooks.event!({
      event: {
        id: "variant-priority-busy",
        type: "session.status",
        properties: { sessionID: "variant-priority-session", status: { type: "busy" } },
      },
    });
    await hooks.event!({
      event: {
        id: "variant-priority-idle",
        type: "session.status",
        properties: { sessionID: "variant-priority-session", status: { type: "idle" } },
      },
    });

    const body = JSON.parse((globalThis.fetch as any).mock.calls[0][1].body);
    expect(body.modelVariant).toBe("max");
  });

  it("falls back to session.model.variant when Assistant variant is missing", async () => {
    globalThis.fetch = mock(async () => new Response("ok", { status: 200 }));
    const hooks = await defaultModule.server(
      {
        client: {
          session: {
            get: async () => ({
              data: {
                model: { providerID: "deepseek", modelID: "deepseek-v4-flash", variant: "default" },
              },
            }),
          },
        },
      } as any,
      makeConfig(),
    );

    await hooks.event!({
      event: {
        id: "variant-fallback-busy",
        type: "session.status",
        properties: { sessionID: "variant-fallback-session", status: { type: "busy" } },
      },
    });
    await hooks.event!({
      event: {
        id: "variant-fallback-idle",
        type: "session.status",
        properties: { sessionID: "variant-fallback-session", status: { type: "idle" } },
      },
    });

    const body = JSON.parse((globalThis.fetch as any).mock.calls[0][1].body);
    expect(body.modelVariant).toBe("default");
  });

  it("omits modelVariant when neither safe source provides it", async () => {
    globalThis.fetch = mock(async () => new Response("ok", { status: 200 }));
    const hooks = await defaultModule.server(
      { client: { session: { get: async () => ({ data: {} }) } } } as any,
      makeConfig(),
    );

    await hooks.event!({
      event: {
        id: "variant-missing-busy",
        type: "session.status",
        properties: { sessionID: "variant-missing-session", status: { type: "busy" } },
      },
    });
    await hooks.event!({
      event: {
        id: "variant-missing-idle",
        type: "session.status",
        properties: { sessionID: "variant-missing-session", status: { type: "idle" } },
      },
    });

    const body = JSON.parse((globalThis.fetch as any).mock.calls[0][1].body);
    expect(body).not.toHaveProperty("modelVariant");
  });
});

describe("metadata diagnostics", () => {
  const prefix = "[webhook-notifier][metadata-diagnostic]";

  function diagnosticLines(warnings: string[]): string[] {
    return warnings.filter((line) => line.startsWith(prefix));
  }

  it("default and invalid modes add no diagnostic logs", async () => {
    for (const options of [makeConfig(), makeConfig({ metadataDiagnostics: "invalid" })]) {
      const warnings: string[] = [];
      const originalWarn = console.warn;
      console.warn = (...args: unknown[]) => warnings.push(args.map(String).join(" "));
      try {
        const hooks = await defaultModule.server(
          { client: { session: { get: async () => ({ data: {} }) } } } as any,
          options,
        );
        await hooks.event!({
          event: {
            id: "diagnostic-off-message",
            type: "message.updated",
            properties: { info: { role: "assistant", mode: "agent" } },
          },
        });
      } finally {
        console.warn = originalWarn;
      }
      expect(diagnosticLines(warnings)).toHaveLength(0);
    }
  });

  it("once emits each phase at most once and excludes forbidden values", async () => {
    const warnings: string[] = [];
    const originalWarn = console.warn;
    console.warn = (...args: unknown[]) => warnings.push(args.map(String).join(" "));
    globalThis.fetch = mock(async () => new Response("ok", { status: 200 }));

    const input = {
      client: {
        session: {
          get: async ({ path: { id } }: any) => {
            if (id === "diagnostic-fallback") {
              return {
                data: {
                  title: "FALLBACK_TITLE_BODY",
                  parentID: null,
                  time: { created: 1_750_000_000_000, updated: 1_750_000_001_000 },
                },
              };
            }
            return {
              data: {
                title: "TITLE_BODY",
                parentID: "PARENT_VALUE",
                agent: "build-agent",
                model: { providerID: "provider-a", modelID: "model-a" },
                time: { created: 1_750_000_000_000, updated: 1_750_000_001_000 },
                safeExtra: "safe",
                token: "TOKEN_VALUE",
                url: "https://private.example.invalid/hook",
                headers: { authorization: "HEADER_VALUE" },
                cwd: "/private/cwd",
                responseBody: "RESPONSE_BODY",
                name: "NAME_BODY",
              },
            };
          },
          messages: async () => ({
            data: [
              { info: { role: "user", mode: "user" }, parts: [{ text: "USER_PART" }] },
              {
                info: {
                  role: "assistant",
                  mode: "fallback-agent",
                  providerID: "fallback-provider",
                  modelID: "fallback-model",
                  messageID: "MESSAGE_ID",
                  parentID: "PARENT_MESSAGE_VALUE",
                  parts: [{ text: "PARTS_BODY" }],
                  path: "/private/path",
                  tokens: { input: 1 },
                  cost: 2,
                  reasoning: "REASONING_BODY",
                  time: { created: 1_750_000_000_000, completed: 1_750_000_001_000 },
                },
              },
            ],
          }),
        },
      },
    };

    try {
      const hooks = await defaultModule.server(input as any, makeConfig({ metadataDiagnostics: "once" }));

      await hooks.event!({
        event: {
          id: "message-diagnostic-1",
          type: "message.updated",
          properties: {
            info: {
              sessionID: "RAW_SESSION_ID",
              role: "assistant",
              mode: "build-agent",
              providerID: "provider-a",
              modelID: "model-a",
              messageID: "MESSAGE_ID",
              parentID: "PARENT_VALUE",
              parts: [{ text: "PARTS_BODY" }],
              tokens: { input: 1 },
              cost: 2,
              time: { created: 1_750_000_000_000, completed: 1_750_000_001_000 },
            },
          },
        },
      });
      await hooks.event!({
        event: {
          id: "message-diagnostic-2",
          type: "message.updated",
          properties: { info: { role: "assistant", mode: "second-agent" } },
        },
      });
      expect((globalThis.fetch as any).mock.calls.length).toBe(0);

      await hooks.event!({
        event: {
          id: "get-busy",
          type: "session.status",
          properties: { sessionID: "diagnostic-session", status: { type: "busy" } },
        },
      });
      await hooks.event!({
        event: {
          id: "get-idle",
          type: "session.status",
          properties: { sessionID: "diagnostic-session", status: { type: "idle" } },
        },
      });

      await hooks.event!({
        event: {
          id: "messages-busy",
          type: "session.status",
          properties: { sessionID: "diagnostic-fallback", status: { type: "busy" } },
        },
      });
      await hooks.event!({
        event: {
          id: "messages-idle",
          type: "session.status",
          properties: { sessionID: "diagnostic-fallback", status: { type: "idle" } },
        },
      });
    } finally {
      console.warn = originalWarn;
    }

    const lines = diagnosticLines(warnings);
    const records = lines.map((line) => JSON.parse(line.slice(prefix.length).trim()) as Record<string, unknown>);
    expect(records.map((record) => record.phase)).toEqual([
      "message_updated",
      "session_get",
      "session_messages",
      "outgoing_envelope",
    ]);
    expect(records.filter((record) => record.phase === "message_updated")).toHaveLength(1);
    expect(records.filter((record) => record.phase === "session_get")).toHaveLength(1);
    expect(records.filter((record) => record.phase === "session_messages")).toHaveLength(1);
    expect(records.filter((record) => record.phase === "outgoing_envelope")).toHaveLength(1);

    const messageDiagnostic = records.find((record) => record.phase === "message_updated")!;
    expect(messageDiagnostic).toMatchObject({
      role: "assistant",
      mode: "build-agent",
      providerID: "provider-a",
      modelID: "model-a",
      parentIDState: "string",
      timeKeys: ["completed", "created"],
    });
    expect(messageDiagnostic.infoKeys).toEqual(["mode", "modelID", "providerID", "role", "time"]);

    const sessionDiagnostic = records.find((record) => record.phase === "session_get")!;
    expect(sessionDiagnostic).toMatchObject({
      responseShape: "data-wrapper",
      titlePresent: true,
      titleLength: 10,
      agent: "build-agent",
      modelShape: "object",
      modelProviderID: "provider-a",
      modelID: "model-a",
      parentIDState: "string",
      timeKeys: ["created", "updated"],
    });
    expect(sessionDiagnostic.sessionKeys).toEqual(["agent", "model", "safeExtra", "time"]);

    const messagesDiagnostic = records.find((record) => record.phase === "session_messages")!;
    expect(messagesDiagnostic).toMatchObject({
      responseShape: "data-wrapper",
      itemCount: 2,
      assistantFound: true,
      mode: "fallback-agent",
      providerID: "fallback-provider",
      modelID: "fallback-model",
      timeKeys: ["completed", "created"],
    });
    expect(messagesDiagnostic.assistantInfoKeys).toEqual([
      "mode",
      "modelID",
      "providerID",
      "role",
      "time",
    ]);

    const outgoingDiagnostic = records.find((record) => record.phase === "outgoing_envelope")!;
    expect(outgoingDiagnostic).toMatchObject({
      event: "opencode.session_idle",
      sessionNamePresent: true,
      sessionNameLength: 10,
      agent: "build-agent",
      model: "provider-a/model-a",
      sessionScope: "subagent",
      startedAtPresent: true,
      taskStartedAtPresent: true,
      endedAtPresent: true,
      durationMsPresent: true,
      questionPresent: false,
      permissionPresent: false,
      errorPresent: false,
    });

    const diagnosticText = lines.join("\n");
    for (const forbidden of [
      "TOKEN_VALUE",
      "https://private.example.invalid/hook",
      "HEADER_VALUE",
      "RAW_SESSION_ID",
      "MESSAGE_ID",
      "PARENT_VALUE",
      "PARENT_MESSAGE_VALUE",
      "TITLE_BODY",
      "NAME_BODY",
      "PARTS_BODY",
      "USER_PART",
      "/private/cwd",
      "/private/path",
      "REASONING_BODY",
      "RESPONSE_BODY",
    ]) {
      expect(diagnosticText).not.toContain(forbidden);
    }
  });

  it("sample deduplicates payloads, keeps variants, correlates sessions, and bounds phases", async () => {
    const warnings: string[] = [];
    const originalWarn = console.warn;
    console.warn = (...args: unknown[]) => warnings.push(args.map(String).join(" "));
    globalThis.fetch = mock(async () => new Response("ok", { status: 200 }));

    const nestedModel = {
      providerID: "sample-provider",
      modelID: "sample-model",
      variant: "model-high",
      reasoningEffort: 3,
      reasoning_effort: { hidden: "MODEL_REASONING_OBJECT" },
      providerOptions: { apiKey: "MODEL_API_KEY" },
      unknownObject: { hidden: "UNKNOWN_MODEL_VALUE" },
      ...Object.fromEntries(Array.from({ length: 26 }, (_, i) => [`extraKey${i}`, i])),
    };
    const input = {
      client: {
        session: {
          get: async ({ path: { id } }: any) => {
            if (id === "sample-session-b") {
              return {
                data: {
                  title: "Fallback title",
                  parentID: null,
                  time: { created: 1_750_000_000_000, updated: 1_750_000_001_000 },
                },
              };
            }
            return {
              data: {
                title: "Sample title",
                parentID: "PARENT_VALUE",
                agent: "sample-agent",
                model: nestedModel,
                variant: "top-high",
                reasoningEffort: "top-effort",
                reasoning_effort: false,
                time: { created: 1_750_000_000_000, updated: 1_750_000_001_000 },
              },
            };
          },
          messages: async () => ({
            data: [{
              info: {
                role: "assistant",
                mode: "fallback-agent",
                providerID: "fallback-provider",
                modelID: "fallback-model",
                variant: ["array-variant"],
                reasoningEffort: 7,
                reasoning_effort: false,
                reasoning: "REASONING_CONTENT",
                parts: [{ text: "PARTS_CONTENT" }],
                unknownField: { value: "UNKNOWN_INFO_VALUE" },
                time: { completed: 1_750_000_001_000 },
              },
            }],
          }),
        },
      },
    };

    try {
      const hooks = await defaultModule.server(input as any, makeConfig({ metadataDiagnostics: "sample" }));

      const baseInfo = {
        sessionID: "sample-session-a",
        role: "assistant",
        mode: "sample-agent",
        providerID: "sample-provider",
        modelID: "sample-model",
        reasoningEffort: { hidden: "MESSAGE_REASONING_OBJECT" },
        reasoning_effort: true,
        reasoning: "REASONING_CONTENT",
        parts: [{ text: "PARTS_CONTENT" }],
        unknownField: { value: "UNKNOWN_INFO_VALUE" },
        time: { created: 1_750_000_000_000, completed: 1_750_000_001_000 },
      };
      for (const variant of ["high", "high", "low", "v0", "v1", "v2", "v3", "v4", "v5", "v6"]) {
        await hooks.event!({
          event: {
            id: `sample-message-${variant}`,
            type: "message.updated",
            properties: { info: { ...baseInfo, variant } },
          },
        });
      }
      expect((globalThis.fetch as any).mock.calls.length).toBe(0);

      await hooks.event!({
        event: {
          id: "sample-a-busy",
          type: "session.status",
          properties: { sessionID: "sample-session-a", status: { type: "busy" } },
        },
      });
      await hooks.event!({
        event: {
          id: "sample-a-idle",
          type: "session.status",
          properties: { sessionID: "sample-session-a", status: { type: "idle" } },
        },
      });

      await hooks.event!({
        event: {
          id: "sample-b-busy",
          type: "session.status",
          properties: { sessionID: "sample-session-b", status: { type: "busy" } },
        },
      });
      await hooks.event!({
        event: {
          id: "sample-b-idle",
          type: "session.status",
          properties: { sessionID: "sample-session-b", status: { type: "idle" } },
        },
      });
    } finally {
      console.warn = originalWarn;
    }

    const lines = diagnosticLines(warnings);
    const records = lines.map((line) => JSON.parse(line.slice(prefix.length).trim()) as Record<string, unknown>);
    const messageRecords = records.filter((record) => record.phase === "message_updated");
    const getRecords = records.filter((record) => record.phase === "session_get");
    const messageRecordsForA = messageRecords.filter((record) => record.sampleSession !== undefined);
    expect(messageRecords).toHaveLength(8);
    expect(new Set(messageRecords.map((record) => (record as any).variant)).size).toBe(8);
    expect(messageRecordsForA.every((record) => record.sampleSession === messageRecordsForA[0]!.sampleSession)).toBeTrue();
    expect(getRecords.length).toBeLessThanOrEqual(8);
    expect(records.filter((record) => record.phase === "session_messages").length).toBeLessThanOrEqual(8);
    expect(records.filter((record) => record.phase === "outgoing_envelope").length).toBeLessThanOrEqual(8);

    const messageDiagnostic = messageRecords[0]!;
    expect(messageDiagnostic).toMatchObject({
      sampleSession: expect.any(Number),
      variant: "high",
      reasoningEffort: "object",
      reasoning_effort: true,
    });
    const fallbackDiagnostic = records.find((record) => record.phase === "session_messages")!;
    expect(fallbackDiagnostic).toMatchObject({
      sampleSession: expect.any(Number),
      variant: "array",
      reasoningEffort: 7,
      reasoning_effort: false,
    });
    expect(fallbackDiagnostic.sampleSession).not.toBe(messageDiagnostic.sampleSession);

    const getDiagnostic = getRecords.find((record) => record.modelShape === "object")!;
    expect(getDiagnostic).toMatchObject({
      sampleSession: messageDiagnostic.sampleSession,
      modelShape: "object",
      modelVariant: "model-high",
      modelReasoningEffort: 3,
      modelReasoning_effort: "object",
      topLevelVariant: "top-high",
      topLevelReasoningEffort: "top-effort",
      topLevelReasoning_effort: false,
    });
    expect((getDiagnostic.modelKeys as string[]).length).toBeLessThanOrEqual(24);
    expect((getDiagnostic.modelKeys as string[])).not.toContain("providerOptions");

    const diagnosticText = lines.join("\n");
    for (const forbidden of [
      "sample-session-a",
      "sample-session-b",
      "MODEL_REASONING_OBJECT",
      "MODEL_API_KEY",
      "UNKNOWN_MODEL_VALUE",
      "MESSAGE_REASONING_OBJECT",
      "REASONING_CONTENT",
      "PARTS_CONTENT",
      "UNKNOWN_INFO_VALUE",
      "PARENT_VALUE",
    ]) {
      expect(diagnosticText).not.toContain(forbidden);
    }
  });

  it("sample session mapping is bounded and resettable", async () => {
    const originalWarn = console.warn;
    console.warn = () => {};
    try {
      const hooks = await defaultModule.server(
        { client: { session: { get: async () => ({ data: {} }) } } } as any,
        makeConfig({ metadataDiagnostics: "sample" }),
      );
      for (let i = 0; i < 1001; i++) {
        await hooks.event!({
          event: {
            id: `bounded-message-${i}`,
            type: "message.updated",
            properties: { info: { sessionID: `bounded-session-${i}`, role: "assistant", mode: "agent", variant: `v-${i}` } },
          },
        });
      }
      expect(_metadataSampleSessions.size).toBeLessThanOrEqual(1000);
      expect(_metadataDiagnosticSamples.get("message_updated")?.count).toBeLessThanOrEqual(8);
    } finally {
      console.warn = originalWarn;
    }
    _resetMetadataDiagnostics();
    expect(_metadataSampleSessions.size).toBe(0);
    expect(_metadataDiagnosticSamples.size).toBe(0);
  });
});

describe("_enrichEvent — session metadata enrichment", () => {
  it("uses session.time.created only for startedAt and never session.time.updated", async () => {
    const event = { type: "session.idle", sessionId: "session-time-contract" } as OpenCodeEvent;
    await _enrichEvent(
      event,
      {
        client: {
          session: {
            get: async () => ({
              data: { time: { created: 1_750_000_000_000, updated: 1_750_000_099_000 } },
            }),
          },
        },
      } as any,
    );

    const envelope = await _buildEnvelope(event, "time-contract");
    expect(envelope!.startedAt).toBe("2025-06-15T15:06:40.000Z");
    expect(envelope!.taskStartedAt).toBeUndefined();
    expect(envelope!.endedAt).toBeUndefined();
    expect(envelope!.durationMs).toBeUndefined();
  });

  it("uses assistant created/completed for task timing and derives task duration", async () => {
    await _consumeAssistantMetadata({
      event: {
        id: "assistant-completed",
        type: "message.updated",
        properties: {
          info: {
            sessionID: "assistant-time-contract",
            role: "assistant",
            time: { created: 1_750_000_000_000, completed: 1_750_000_005_000 },
          },
        },
      },
    });
    const event = { type: "session.idle", sessionId: "assistant-time-contract" } as OpenCodeEvent;
    await _enrichEvent(
      event,
      {
        client: {
          session: {
            get: async () => ({ data: { time: { created: 1_749_999_900_000, updated: 1_750_000_099_000 } } }),
          },
        },
      } as any,
    );

    const envelope = await _buildEnvelope(event, "assistant-time-contract");
    expect(envelope!.startedAt).toBe("2025-06-15T15:05:00.000Z");
    expect(envelope!.taskStartedAt).toBe("2025-06-15T15:06:40.000Z");
    expect(envelope!.endedAt).toBe("2025-06-15T15:06:45.000Z");
    expect(envelope!.durationMs).toBe(5000);
  });

  it("action_required with an incomplete assistant omits endedAt and durationMs", async () => {
    await _consumeAssistantMetadata({
      event: {
        id: "assistant-incomplete",
        type: "message.updated",
        properties: {
          info: {
            sessionID: "action-required-time-contract",
            role: "assistant",
            time: { created: 1_750_000_000_000 },
          },
        },
      },
    });
    const event = { type: "question.asked", sessionId: "action-required-time-contract" } as OpenCodeEvent;
    await _enrichEvent(
      event,
      {
        client: {
          session: {
            get: async () => ({ data: { time: { created: 1_749_999_900_000, updated: 1_750_000_099_000 } } }),
          },
        },
      } as any,
    );

    const envelope = await _buildEnvelope(event, "action-required-time-contract");
    expect(envelope!.event).toBe("opencode.question_asked");
    expect(envelope!.startedAt).toBe("2025-06-15T15:05:00.000Z");
    expect(envelope!.taskStartedAt).toBe("2025-06-15T15:06:40.000Z");
    expect(envelope!.endedAt).toBeUndefined();
    expect(envelope!.durationMs).toBeUndefined();
  });

  it("does not invent duration from incomplete, invalid, or negative Assistant timing", async () => {
    const cases = [
      { sessionId: "assistant-time-missing", time: { created: 1_000 } },
      { sessionId: "assistant-time-invalid", time: { created: "not-a-time", completed: "also-not-a-time" } },
      { sessionId: "assistant-time-negative", time: { created: 2_000, completed: 1_000 } },
    ];

    for (const item of cases) {
      await _consumeAssistantMetadata({
        event: {
          id: item.sessionId,
          type: "message.updated",
          properties: {
            info: {
              sessionID: item.sessionId,
              role: "assistant",
              time: item.time,
            },
          },
        },
      });
      const event = { type: "session.idle", sessionId: item.sessionId } as OpenCodeEvent;
      await _enrichEvent(
        event,
        { client: { session: { get: async () => ({ data: {} }) } } } as any,
      );
      const envelope = await _buildEnvelope(event, item.sessionId);
      expect(envelope!.durationMs).toBeUndefined();
    }
  });

  it("fills session name from title when available", async () => {
    const event = { type: "session.idle", sessionId: "s1" } as OpenCodeEvent;
    const input = {
      client: { session: { get: async () => ({ data: { title: "My Task" } }) } },
    };

    await _enrichEvent(event, input as any);
    expect(event.session?.name).toBe("My Task");
  });

  it("fills session name from name field when title absent", async () => {
    const event = { type: "session.idle", sessionId: "s1" } as OpenCodeEvent;
    const input = {
      client: { session: { get: async () => ({ data: { name: "Task Name" } }) } },
    };

    await _enrichEvent(event, input as any);
    expect(event.session?.name).toBe("Task Name");
  });

  it("does not override existing event session name", async () => {
    const event = { type: "session.idle", sessionId: "s1", session: { name: "Existing" } } as OpenCodeEvent;
    const input = {
      client: { session: { get: async () => ({ data: { title: "New Title" } }) } },
    };

    await _enrichEvent(event, input as any);
    expect(event.session?.name).toBe("Existing");
  });

  it("fills agent/model from session data", async () => {
    const event = { type: "session.idle", sessionId: "s1" } as OpenCodeEvent;
    const input = {
      client: { session: { get: async () => ({ data: { agent: "my-agent", model: "gpt-5" } }) } },
    };

    await _enrichEvent(event, input as any);
    expect(event.agent).toBe("my-agent");
    expect(event.model).toBe("gpt-5");
  });

  it("combines provider-like model field with modelID from session data", async () => {
    const event = { type: "session.idle", sessionId: "s-provider-model" } as OpenCodeEvent;
    const input = {
      client: {
        session: {
          get: async () => ({ data: { model: "cpa", modelID: "gpt-5.6-sol" } }),
        },
      },
    };

    await _enrichEvent(event, input as any);
    expect(event.model).toBe("cpa/gpt-5.6-sol");
  });

  it("keeps provider-only model metadata for display fallback", async () => {
    const event = { type: "session.idle", sessionId: "s-provider-only" } as OpenCodeEvent;
    const input = {
      client: { session: { get: async () => ({ data: { providerID: "cpa" } }) } },
    };

    await _enrichEvent(event, input as any);
    expect(event.model).toBe("cpa");
  });

  it("does not override existing agent/model", async () => {
    const event = { type: "session.idle", sessionId: "s1", agent: "existing", model: "existing" } as OpenCodeEvent;
    const input = {
      client: { session: { get: async () => ({ data: { agent: "new", model: "new" } }) } },
    };

    await _enrichEvent(event, input as any);
    expect(event.agent).toBe("existing");
    expect(event.model).toBe("existing");
  });

  it("handles direct response (no data wrapper)", async () => {
    const event = { type: "session.idle", sessionId: "s1" } as OpenCodeEvent;
    const input = {
      client: { session: { get: async () => ({ title: "Direct Title" }) } },
    };

    await _enrichEvent(event, input as any);
    expect(event.session?.name).toBe("Direct Title");
  });

  it("handles API failure gracefully", async () => {
    const event = { type: "session.idle", sessionId: "s1" } as OpenCodeEvent;
    const input = {
      client: { session: { get: async () => { throw new Error("fail"); } } },
    };

    // Must not throw
    await _enrichEvent(event, input as any);
    expect(event.session).toBeUndefined();
  });

  it("no-op when sessionId is missing", async () => {
    const event = { type: "session.idle" } as OpenCodeEvent;
    const input = {
      client: { session: { get: async () => ({ data: { title: "X" } }) } },
    };

    await _enrichEvent(event, input as any);
    expect(event.session).toBeUndefined();
  });

  it("uses the last assistant from a .data messages response", async () => {
    let request: unknown;
    const event = { type: "session.idle", sessionId: "fallback-data" } as OpenCodeEvent;
    const input = {
      client: {
        session: {
          get: async () => ({ data: { title: "Fallback", parentID: null } }),
          messages: async (params: unknown) => {
            request = params;
            return {
              data: [
                { info: { role: "assistant", mode: "old-agent", providerID: "old-p", modelID: "old-m" }, parts: [{ text: "old" }] },
                { info: { role: "user", mode: "user" }, parts: [{ text: "ignore" }] },
                { info: { role: "assistant", mode: "last-agent", providerID: "last-p", modelID: "last-m", time: { created: 1_750_000_000_000, completed: 1_750_000_001_000 } }, parts: [{ text: "secret" }] },
              ],
            };
          },
        },
      },
    };

    await _enrichEvent(event, input as any);
    expect(request).toEqual({ path: { id: "fallback-data" }, query: { limit: 10 } });
    expect(event.agent).toBe("last-agent");
    expect(event.model).toBe("last-p/last-m");
    expect(event.taskStartedAt).toBe("2025-06-15T15:06:40.000Z");
    expect(event.endedAt).toBe("2025-06-15T15:06:41.000Z");
    expect(event.durationMs).toBe(1000);
  });

  it("supports a direct array messages response and keeps provider/model rules", async () => {
    const event = { type: "session.idle", sessionId: "fallback-array" } as OpenCodeEvent;
    const input = {
      client: {
        session: {
          get: async () => ({ data: { agent: "", model: undefined, providerID: undefined } }),
          messages: async () => [
            { info: { role: "assistant", mode: "array-agent", providerID: "array-provider" }, parts: [{ path: "/private" }] },
          ],
        },
      },
    };

    await _enrichEvent(event, input as any);
    expect(event.agent).toBe("array-agent");
    expect(event.model).toBe("array-provider");
  });

  it("never overrides existing event agent/model values or calls fallback", async () => {
    let messageCalls = 0;
    const event = {
      type: "session.idle",
      sessionId: "existing-values",
      agent: "event-agent",
      model: "event/model",
      taskStartedAt: "2026-07-22T12:00:00Z",
      endedAt: "2026-07-22T12:00:01Z",
    } as OpenCodeEvent;
    await _enrichEvent(event, {
      client: {
        session: {
          get: async () => ({ data: { agent: "session-agent", model: { providerID: "session-p", modelID: "session-m" } } }),
          messages: async () => {
            messageCalls++;
            return [];
          },
        },
      },
    } as any);
    expect(event.agent).toBe("event-agent");
    expect(event.model).toBe("event/model");
    expect(messageCalls).toBe(0);
  });

  it("logs fixed warnings for get/messages failures without sensitive details", async () => {
    const warnings: unknown[][] = [];
    const originalWarn = console.warn;
    console.warn = (...args: unknown[]) => warnings.push(args);
    try {
      const event = { type: "session.idle", sessionId: "warning-secret-session" } as OpenCodeEvent;
      await _enrichEvent(event, {
        client: {
          session: {
            get: async () => { throw new Error("secret response body"); },
            messages: async () => { throw new Error("private message body"); },
          },
        },
      } as any);
    } finally {
      console.warn = originalWarn;
    }

    expect(warnings).toEqual([
      ["[webhook-notifier] session.get enrichment failed"],
      ["[webhook-notifier] session.messages enrichment failed"],
    ]);
    expect(JSON.stringify(warnings)).not.toContain("warning-secret-session");
    expect(JSON.stringify(warnings)).not.toContain("secret response body");
    expect(JSON.stringify(warnings)).not.toContain("private message body");
  });

  it("keeps assistant cache sessions isolated, refreshes LRU order, and retains only 500", async () => {
    const refA = await _hashSessionRef("cache-a");
    const refB = await _hashSessionRef("cache-b");
    _cacheAssistantMetadata(refA, { agent: "a" });
    _cacheAssistantMetadata(refB, { agent: "b" });
    expect(_cachedAssistantMetadata(refA)?.agent).toBe("a");
    expect([..._assistantMetadata.keys()]).toEqual([refB, refA]);
    expect(_cachedAssistantMetadata(refB)?.agent).toBe("b");
    expect([..._assistantMetadata.keys()]).toEqual([refA, refB]);

    for (let i = 0; i < 1001; i++) {
      _cacheAssistantMetadata(`anonymous-ref-${i}`, { agent: `agent-${i}` });
    }
    expect(_assistantMetadata.size).toBeLessThanOrEqual(1000);
    while (_assistantMetadata.size <= 1000) {
      _assistantMetadata.set(`cleanup-ref-${_assistantMetadata.size}`, { agent: "cleanup" });
    }
    _cleanupSessions();
    expect(_assistantMetadata.size).toBeLessThanOrEqual(500);
    expect(JSON.stringify(_assistantMetadata)).not.toContain("cache-a");
  });

  it("keeps fallback metadata out of the envelope", async () => {
    const event = { type: "session.idle", sessionId: "raw-session-secret" } as OpenCodeEvent;
    await _enrichEvent(event, {
      client: {
        session: {
          get: async () => ({ data: {} }),
          messages: async () => ({ data: [{
            info: { sessionID: "raw-session-secret", messageID: "raw-message-secret", role: "assistant", mode: "safe-agent", providerID: "safe-provider", modelID: "safe-model" },
            parts: [{ text: "private body", path: "/private/path" }],
            tokens: { input: 1 },
            cost: 2,
          }] }),
        },
      },
    } as any);
    const envelope = await _buildEnvelope(event, "envelope-safe");
    const json = JSON.stringify(envelope);
    expect(json).toContain("safe-provider/safe-model");
    expect(json).not.toContain("raw-session-secret");
    expect(json).not.toContain("raw-message-secret");
    expect(json).not.toContain("private body");
    expect(json).not.toContain("/private/path");
    expect(json).not.toContain("tokens");
    expect(json).not.toContain("cost");
  });
});

describe("session scope contract", () => {
  it("builds root, subagent, auxiliary and unknown scopes without parentID", async () => {
    for (const scope of ["root", "subagent", "auxiliary", "unknown"] as const) {
      const envelope = await _buildEnvelope(
        makeEvent({ type: "session.idle", sessionScope: scope }),
        `scope-${scope}`,
      );
      expect(envelope!.session.scope).toBe(scope);
      expect(JSON.stringify(envelope)).not.toContain("parentID");
    }
  });

  it("derives root from missing, undefined and null parentID", async () => {
    for (const parentID of [undefined, null]) {
      const event = { type: "session.idle", sessionId: `root-${String(parentID)}` } as OpenCodeEvent;
      await _enrichEvent(
        event,
        { client: { session: { get: async () => ({ data: { parentID } }) } } } as any,
      );
      expect(event.sessionScope).toBe("root");
    }
    const missing = { type: "session.idle", sessionId: "root-missing" } as OpenCodeEvent;
    await _enrichEvent(
      missing,
      { client: { session: { get: async () => ({ data: { title: "Root" } }) } } } as any,
    );
    expect(missing.sessionScope).toBe("root");
  });

  it("derives subagent only from a non-empty parentID string", async () => {
    const event = { type: "session.idle", sessionId: "subagent-1" } as OpenCodeEvent;
    await _enrichEvent(
      event,
      { client: { session: { get: async () => ({ data: { parentID: "parent-1" } }) } } } as any,
    );
    expect(event.sessionScope).toBe("subagent");
    expect(JSON.stringify(event)).not.toContain("parent-1");
  });

  it("derives auxiliary only for exact smartfetch-secondary and keeps parent priority", async () => {
    const auxiliary = { type: "session.idle", sessionId: "auxiliary-1" } as OpenCodeEvent;
    await _enrichEvent(
      auxiliary,
      { client: { session: { get: async () => ({ data: { title: "smartfetch-secondary" } }) } } } as any,
    );
    expect(auxiliary.sessionScope).toBe("auxiliary");

    const ordinary = { type: "session.idle", sessionId: "ordinary-secondary" } as OpenCodeEvent;
    await _enrichEvent(
      ordinary,
      { client: { session: { get: async () => ({ data: { title: "foo-secondary" } }) } } } as any,
    );
    expect(ordinary.sessionScope).toBe("root");

    const child = { type: "session.idle", sessionId: "child-secondary" } as OpenCodeEvent;
    await _enrichEvent(
      child,
      { client: { session: { get: async () => ({ data: { title: "smartfetch-secondary", parentID: "parent" } }) } } } as any,
    );
    expect(child.sessionScope).toBe("subagent");
  });

  it("uses unknown for API failure, non-object data, empty and invalid parentID", async () => {
    const cases: Array<{ id: string; get: () => Promise<unknown> }> = [
      { id: "scope-failure", get: async () => { throw new Error("unavailable"); } },
      { id: "scope-array", get: async () => ({ data: [] }) },
      { id: "scope-empty", get: async () => ({ data: { parentID: "" } }) },
      { id: "scope-type", get: async () => ({ data: { parentID: 123 } }) },
    ];
    for (const item of cases) {
      const event = { type: "session.idle", sessionId: item.id } as OpenCodeEvent;
      await _enrichEvent(event, { client: { session: { get: item.get } } } as any);
      expect(event.sessionScope).toBe("unknown");
      expect(_sessionScopes.has(await _hashSessionRef(item.id))).toBeFalse();
    }
  });

  it("keeps scope state and cache bounded on ignored events", () => {
    for (let i = 0; i < 1001; i++) {
      _getState(`state-${i}`);
      _sessionScopes.set(`scope-${i}`, "root");
    }
    _cleanupSessions();
    expect(_sessions.size).toBeLessThanOrEqual(500);
    expect(_sessionScopes.size).toBeLessThanOrEqual(500);
  });
});
