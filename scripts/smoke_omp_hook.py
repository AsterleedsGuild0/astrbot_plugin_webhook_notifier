#!/usr/bin/env python3
"""维护源 Hook → 真实回环 HTTP → Server/Adapter/文本，Bot 边界为内存接收器。

不启动原生 OMP，不读取用户配置或真实凭据。所有临时文件归仓库 tmp，退出清除。
--hook 可指向测试 ZIP 提取的入口；不访问真实部署副本。
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
from core.server import WebhookServer  # noqa: E402

SENTINEL = "PRIVATE_OMP_SMOKE_SENTINEL"
DRIVER = r'''
const { default: register } = await import(process.env.SMOKE_HOOK);
const handlers = new Map();
const diagnostics = [];
const done = Promise.withResolvers();
const ctx = {
  agent: { kind: "main", id: "RAW_PRIVATE_OMP_SMOKE_SENTINEL", name: "main", depth: 0 },
  hasUI: true, cwd: "/private/PRIVATE_OMP_SMOKE_SENTINEL/project",
  model: { provider: "fixture", id: "model" },
  sessionManager: { getSessionId: () => "RAW_PRIVATE_OMP_SMOKE_SENTINEL", getSessionName: () => "隔离 HTTP 验收", getCwd: () => "/private/PRIVATE_OMP_SMOKE_SENTINEL/project" },
};
register({
  on: (name, fn) => handlers.set(name, fn), getThinkingLevel: () => "high",
  logger: { warn: (_, data) => {
    diagnostics.push(data);
    if (!["transient_error", "transport_unknown"].includes(data.category) || data.attempt === 3) done.resolve();
  } },
});
const emit = (name, event = {}) => {
  const result = handlers.get(name)({ type: name, ...event }, ctx);
  if (result !== undefined) throw new Error("observer_control_value");
};
const realNow = Date.now;
Date.now = () => realNow() - (process.env.SMOKE_SCENARIO === "policy" ? 1000 : 20000);
emit("agent_start", { prompt: "PRIVATE_OMP_SMOKE_SENTINEL" });
Date.now = realNow;
const stopReason = process.env.SMOKE_SCENARIO === "failed" ? "error" : process.env.SMOKE_SCENARIO === "aborted" ? "aborted" : "stop";
emit("session_stop", { session_id: "PRIVATE_OMP_SMOKE_SENTINEL", last_assistant_message: { role: "assistant", stopReason, content: "PRIVATE_OMP_SMOKE_SENTINEL" } });
emit("agent_end", { messages: [{ role: "assistant", stopReason, content: "PRIVATE_OMP_SMOKE_SENTINEL" }] });
const timeout = setTimeout(() => done.reject(new Error("smoke_deadline")), 8000);
await done.promise;
clearTimeout(timeout);
emit("session_shutdown");
console.log(JSON.stringify(diagnostics));
'''


class Sink(Context):
    mode = "ok"

    async def send_message(self, umo, chain):
        if self.mode == "partial_failure":
            raise RuntimeError(SENTINEL)
        return await super().send_message(umo, chain)


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
                        assert len({item["id"] for item in sent}) == 1
                        assert all(item["event"] == "omp.run_finished" and item["version"] == 2 for item in sent)
                        assert SENTINEL not in json.dumps(sent) + stdout.decode() + stderr.decode()
                        if case in {"completed", "failed", "aborted", "replay", "timeout"}:
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
    parser.add_argument("--hook", type=Path, default=ROOT / "integrations/omp/webhook-notifier.ts")
    args = parser.parse_args()
    result = asyncio.run(smoke(args.hook.resolve()))
    print(json.dumps({"layer": "local_real_http", "native_omp": False, "online_astrbot": False, "scenarios": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
