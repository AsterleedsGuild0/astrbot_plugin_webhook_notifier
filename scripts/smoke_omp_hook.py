#!/usr/bin/env python3
"""OMP 客户端包入口 → 真实回环 HTTP → Server/Adapter/文本，Bot 边界为内存接收器。

不启动原生 OMP，不读取用户配置或真实凭据。所有临时文件归仓库 tmp，退出清除。
--package 可指向测试 ZIP 提取的 integrations/omp；入口按 package.json#omp.extensions 解析，不访问真实部署副本。
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests" / "fake_astrbot")]

from aiohttp import ClientSession, web  # noqa: E402
from astrbot.api.star import Context  # noqa: E402
from core.models import ServerConfig  # noqa: E402
from core.omp import OmpProviderAdapter  # noqa: E402
from core.providers import ProviderRegistry  # noqa: E402
from core.registry import EndpointRegistry  # noqa: E402
from core.sender import Sender  # noqa: E402
from core.renderer import _timeline_coverage_union_ms, _timeline_reliable_interval  # noqa: E402
from core.server import WebhookServer  # noqa: E402

SENTINEL = "PRIVATE_OMP_SMOKE_SENTINEL"
DRIVER = r'''
const { default: register } = await import(process.env.SMOKE_HOOK);
const diagnostics = [];
const first = Promise.withResolvers();
const done = Promise.withResolvers();
const scenario = process.env.SMOKE_SCENARIO;
let elapsed = 0;
const base = Date.now();
Date.now = () => base + elapsed;
performance.now = () => elapsed;
function client(id, parent, depth, name) {
  const handlers = new Map();
  const jobs = [];
  const ctx = {
    agent: { kind: parent ? "sub" : "main", id, parentId: parent?.ctx.agent.id, name: parent ? "task" : "main", depth },
    hasUI: !parent, cwd: "/private/PRIVATE_OMP_SMOKE_SENTINEL/project",
    model: { provider: "fixture", id: "model" },
    sessionManager: { getSessionId: () => id, getSessionName: () => name, getCwd: () => "/private/PRIVATE_OMP_SMOKE_SENTINEL/project" },
    getAsyncJobSnapshot: () => ({ running: jobs, recent: [] }),
  };
  if (parent) parent.jobs.push({ id, agentId: id, ownerId: parent.ctx.agent.id });
  register({
    on: (name, fn) => handlers.set(name, fn), getThinkingLevel: () => "high",
    logger: { warn: (_, data) => {
      diagnostics.push(data);
      if (!["transient_error", "transport_unknown"].includes(data.category) || data.attempt === 3) {
        first.resolve();
        if (!scenario.startsWith("action_") || diagnostics.length >= 2) done.resolve();
      }
    } },
  });
  return { ctx, jobs, emit(name, event = {}) {
    const result = handlers.get(name)({ type: name, ...event }, ctx);
    if (result !== undefined) throw new Error("observer_control_value");
  } };
}
const root = client("RAW_PRIVATE_OMP_SMOKE_SENTINEL", undefined, 0, "隔离 HTTP 验收");
const ending = (stopReason = "stop", more = {}) => ({ messages: [{ role: "assistant", stopReason, content: "PRIVATE_OMP_SMOKE_SENTINEL" }], ...more });
root.emit("agent_start", { prompt: "PRIVATE_OMP_SMOKE_SENTINEL" });
const children = [];
if (scenario === "timeline") {
  elapsed = 1000; const a = client("PRIVATE_OMP_SMOKE_SENTINEL_A", root, 1, "并行子任务甲"); a.emit("agent_start");
  elapsed = 2000; const b = client("PRIVATE_OMP_SMOKE_SENTINEL_B", root, 1, "并行子任务乙"); b.emit("agent_start");
  elapsed = 3000; const nested = client("PRIVATE_OMP_SMOKE_SENTINEL_N", a, 2, "嵌套任务"); nested.emit("agent_start");
  elapsed = 4000; root.emit("tool_call", { toolName: "ask", toolCallId: "PRIVATE_OMP_SMOKE_SENTINEL", input: "PRIVATE_OMP_SMOKE_SENTINEL" });
  elapsed = 5000; root.emit("tool_result", { toolName: "ask", toolCallId: "PRIVATE_OMP_SMOKE_SENTINEL", content: "PRIVATE_OMP_SMOKE_SENTINEL" });
  elapsed = 6000; nested.emit("agent_end", ending()); nested.emit("session_shutdown");
  elapsed = 8000; a.emit("agent_end", ending("stop", { willContinue: true, awaitingAsyncWork: true }));
  elapsed = 11000; a.emit("agent_start");
  elapsed = 13000; a.emit("agent_end", ending());
  elapsed = 14000; b.emit("agent_end", ending());
  elapsed = 15000; a.emit("agent_start");
  elapsed = 16000; a.emit("agent_end", ending());
  children.push(a, b);
}
if (scenario.startsWith("action_")) {
  const kind = scenario.slice(7), sessionId = root.ctx.sessionManager.getSessionId();
  if (kind === "approval") root.emit("tool_approval_requested", { sessionId, toolCallId: "PRIVATE_OMP_SMOKE_SENTINEL", reason: "PRIVATE_OMP_SMOKE_SENTINEL" });
  if (kind === "plan_review") root.emit("plan_review_requested", { sessionId, reviewId: "PRIVATE_OMP_SMOKE_SENTINEL", planContent: "PRIVATE_OMP_SMOKE_SENTINEL" });
  if (kind === "ask") {
    root.emit("tool_call", { toolName: "eval", toolCallId: "eval", input: { code: "PRIVATE_OMP_SMOKE_SENTINEL" } });
    root.emit("tool_call", { toolName: "ask", toolCallId: "PRIVATE_OMP_SMOKE_SENTINEL", input: { questions: ["PRIVATE_OMP_SMOKE_SENTINEL"] } });
  }
  await first.promise;
  elapsed = 1000;
  if (kind === "approval") root.emit("tool_approval_resolved", { sessionId, toolCallId: "PRIVATE_OMP_SMOKE_SENTINEL", approved: false });
  if (kind === "plan_review") root.emit("plan_review_resolved", { sessionId, reviewId: "PRIVATE_OMP_SMOKE_SENTINEL" });
  if (kind === "ask") root.emit("tool_result", { toolName: "ask", toolCallId: "PRIVATE_OMP_SMOKE_SENTINEL", content: "PRIVATE_OMP_SMOKE_SENTINEL" });
}
elapsed = scenario === "policy" ? 1000 : 20000;
const stopReason = scenario === "failed" ? "error" : scenario === "aborted" ? "aborted" : "stop";
root.emit("session_stop", { session_id: "PRIVATE_OMP_SMOKE_SENTINEL", last_assistant_message: { role: "assistant", stopReason, content: "PRIVATE_OMP_SMOKE_SENTINEL" } });
root.emit("agent_end", ending(stopReason));
const timeout = setTimeout(() => done.reject(new Error("smoke_deadline")), 8000);
await done.promise;
clearTimeout(timeout);
for (const child of children) child.emit("session_shutdown");
root.emit("session_shutdown");
console.log(JSON.stringify(diagnostics));
'''


class Sink(Context):
    mode = "ok"

    async def send_message(self, umo, chain):
        if self.mode == "partial_failure":
            raise RuntimeError(SENTINEL)
        return await super().send_message(umo, chain)


def package_entry(package: Path) -> Path:
    """与 OMP 加载包目录一致：取 package.json#omp.extensions 声明的唯一入口。"""
    manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
    entries = manifest["omp"]["extensions"]
    if len(entries) != 1:
        raise RuntimeError("single_extension_entry_required")
    return (package / entries[0]).resolve(strict=True)


async def smoke(hook: Path) -> list[dict]:
    bun = shutil.which("bun")
    if not bun:
        raise RuntimeError("bun_unavailable")
    records = []
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logging.getLogger().addHandler(handler)
    try:
        with tempfile.TemporaryDirectory(prefix="omp-http-", dir=ROOT / "tmp") as directory:
            temp = Path(directory)
            driver = temp / "driver.ts"
            driver.write_text(DRIVER, encoding="utf-8")
            registry = EndpointRegistry(temp / "registry")
            endpoint, token = registry.create_private_endpoint(
                "aiocqhttp", "local-test", "isolated-omp", "aiocqhttp:GroupMessage:10001", provider="omp"
            )
            providers = ProviderRegistry()
            providers.register(OmpProviderAdapter())
            providers.freeze()
            sink = Sink()
            server = WebhookServer(ServerConfig(port=0), registry, Sender(sink), provider_registry=providers,
                                   plugin_config={"render_mode": "text", "min_completion_duration_seconds": 15})
            await server.start()
            port = server._site._server.sockets[0].getsockname()[1]
            counts = {}
            bodies = []
            async with ClientSession() as http:
                async def gateway(request):
                    case = request.match_info["case"]
                    body = await request.json()
                    bodies.append(body)
                    counts[case] = counts.get(case, 0) + 1
                    number = counts[case]
                    if case in {"401", "403", "429", "503"}:
                        return web.Response(status=int(case), text=SENTINEL)
                    if case == "unknown":
                        return web.Response(text=SENTINEL)
                    if case == "partial_delivery":
                        return web.json_response({"code": 0, "message": "partial_delivery", "data": {"delivered": True, "retryable": False}})
                    async with http.post(f"http://127.0.0.1:{port}/webhook/{endpoint.path}", json=body,
                                         headers={"Authorization": f"Bearer {token}", "X-OMP-Event": body["event"]}) as response:
                        result = await response.json()
                        status = response.status
                    if case == "replay" and number == 1:
                        return web.Response(status=503)
                    if case == "timeout" and number == 1:
                        await asyncio.sleep(0.5)
                    return web.json_response(result, status=status)

                app = web.Application()
                app.router.add_post("/{case}", gateway)
                runner = web.AppRunner(app, access_log=None)
                await runner.setup()
                site = web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                gateway_port = site._server.sockets[0].getsockname()[1]
                cases = {
                    "completed": ["delivered"], "failed": ["delivered"], "aborted": ["delivered"],
                    "401": ["permanent_error"], "403": ["permanent_error"],
                    "429": ["transient_error"] * 3, "503": ["transient_error"] * 3,
                    "policy": ["policy_skipped"], "partial_failure": ["partial_failure"],
                    "partial_delivery": ["partial_delivery"], "unknown": ["unknown"],
                    "replay": ["transient_error", "replayed"], "timeout": ["transport_unknown", "replayed"],
                    "timeline": ["delivered"],
                    "action_approval": ["delivered", "delivered"], "action_plan_review": ["delivered", "delivered"], "action_ask": ["delivered", "delivered"],
                }
                try:
                    for case, expected in cases.items():
                        sink.mode = case
                        before_messages = len(sink._sent_messages)
                        before_bodies = len(bodies)
                        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(temp),
                               "SMOKE_HOOK": hook.as_uri(), "SMOKE_SCENARIO": case,
                               "OMP_SESSION_WEBHOOK_URL": f"http://127.0.0.1:{gateway_port}/{case}",
                               "OMP_SESSION_WEBHOOK_TOKEN": "local-fixture-token",
                               "OMP_SESSION_WEBHOOK_TIMEOUT_MS": "100" if case == "timeout" else "1000"}
                        process = await asyncio.create_subprocess_exec(bun, str(driver), cwd=temp, env=env,
                                                                      stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        try:
                            stdout, stderr = await asyncio.wait_for(process.communicate(), 12)
                        except BaseException:
                            process.kill()
                            await process.wait()
                            raise
                        if process.returncode:
                            raise RuntimeError(f"driver_failed:{case}")
                        diagnostics = json.loads(stdout)
                        actual = [entry["category"] for entry in diagnostics]
                        assert actual == expected, (case, actual)
                        sent = bodies[before_bodies:]
                        expected_events = 2 if case.startswith("action_") else 1
                        assert len({item["id"] for item in sent}) == expected_events
                        assert all(item["version"] == 2 for item in sent)
                        if case.startswith("action_"):
                            assert [item["event"] for item in sent] == ["omp.action_required", "omp.run_finished"]
                            assert len(sink._sent_messages) - before_messages == 2
                        else:
                            assert all(item["event"] == "omp.run_finished" for item in sent)
                        if case == "timeline":
                            event = providers.get("omp").parse(headers={}, payload=sent[0], received_at=sent[0]["emittedAt"])
                            items = event.subagent_timeline["items"]
                            assert len(items) == 5
                            assert len({item["cycleRef"] for item in items}) == 4
                            intervals = [_timeline_reliable_interval(item) for item in items]
                            assert _timeline_coverage_union_ms(intervals, event.task_duration_ms) == 14000
                            assert {item["kind"] for item in event.user_wait_timeline["intervals"]} == {"ask", "background_wait"}
                        assert SENTINEL not in json.dumps(sent) + stdout.decode() + stderr.decode()
                        if case in {"completed", "failed", "aborted", "replay", "timeout", "timeline"}:
                            assert len(sink._sent_messages) - before_messages == 1
                            rendered = sink._sent_messages[-1][1].chain[0].text
                            assert SENTINEL not in rendered
                            if case in {"failed", "aborted"}:
                                assert "已完成" not in rendered
                        if case in {"policy", "partial_failure"}:
                            assert len(sink._sent_messages) == before_messages
                        records.append({"case": case, "classification": actual, "requests": counts[case], "stable_id": True})
                    assert SENTINEL not in stream.getvalue()
                finally:
                    await runner.cleanup()
                    await server.stop()
    finally:
        logging.getLogger().removeHandler(handler)
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=ROOT / "integrations/omp", help="OMP 客户端包目录")
    args = parser.parse_args()
    result = asyncio.run(smoke(package_entry(args.package.resolve())))
    print(json.dumps({"layer": "local_real_http", "native_omp": False, "online_astrbot": False, "scenarios": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
