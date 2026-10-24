#!/usr/bin/env python3
"""原生 OMP 隔离验收场景。

用法（在仓库根目录）::

    export OMP_NATIVE_VERIFY_FILE=<测试 Endpoint 的 KEY=VALUE 文件>
    uv run --frozen python scripts/omp_native/scenarios.py setup        # 生成隔离 HOME
    uv run --frozen python scripts/omp_native/scenarios.py proxies      # 前台运行本机代理（另开终端或后台）
    uv run --frozen python scripts/omp_native/scenarios.py S1 S2-failed # 执行 RPC 场景
    uv run --frozen python scripts/omp_native/scenarios.py plan S3-plan resolve|cancel|save|refine

``OMP_NATIVE_LOAD=installed`` 时不显式加载仓库包，改用隔离 HOME 中已安装的插件（marketplace 验收）。
每个场景在 ``logs/runs.jsonl`` 追加一行切片索引；会话名统一以“原生验收 <场景>”开头，便于接收端读回。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402
from harness import LOGS, MARKER, MODEL_PORT, WORK, event_summary, rows, webhook_summary  # noqa: E402


def fault(status: int | None = None, remaining: int = 1, body: str = "injected") -> Any:
    data = json.dumps({"status": status, "remaining": remaining, "body": body} if status else None).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{MODEL_PORT}/__fault", data=data, method="POST", headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())


def report(name: str, since: str, rc: int, result: Any, w0: int, e0: int, args: list[str]) -> None:
    w1, e1 = len(rows("webhook.jsonl")), len(rows("events.jsonl"))
    with (LOGS / "runs.jsonl").open("a") as f:
        f.write(json.dumps({"name": name, "since": since, "rc": rc, "result": result, "webhook": [w0, w1], "events": [e0, e1], "args": args}, ensure_ascii=False) + "\n")
    print(f"=== {name} rc={rc} result={json.dumps(result, ensure_ascii=False)[:400]}")
    print("\n".join(event_summary(rows("events.jsonl")[e0:e1])))
    print("--- webhook")
    print("\n".join(webhook_summary(rows("webhook.jsonl")[w0:w1])))


async def run(name: str, args: list[str], steps, expect: int, env: dict[str, str] | None = None, settle: float = 90) -> None:
    await asyncio.sleep(25)  # 上一场景的在途通知（服务端同步渲染约 10～20 秒）先落盘，避免切片串位
    w0, e0 = len(rows("webhook.jsonl")), len(rows("events.jsonl"))
    since = datetime.now(timezone.utc).isoformat(timespec="seconds")
    omp = harness.Omp(name, args, WORK / name.lower(), env)
    await omp.start()
    await omp.call({"type": "set_session_name", "name": f"{MARKER} {name}"})
    result = await steps(omp)
    if isinstance(result, dict) and result.get("sessionSettled") is False:
        await omp.settled(600)
    mine = lambda: [r for r in rows("webhook.jsonl")[w0:] if name in str(r["request"].get("session", {}).get("name"))]  # noqa: E731
    deadline = time.time() + settle
    while time.time() < deadline and len(mine()) < expect:
        await asyncio.sleep(1)
    rc = await omp.close(grace=2)
    report(name, since, rc, result, w0, e0, args)


def strip(frame: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in frame.items() if k != "_t"}


SCENARIOS: dict[str, Any] = {}


def scenario(name: str, args: list[str], expect: int, **kw):
    def wrap(fn):
        SCENARIOS[name] = lambda alias=name: run(alias, args, fn, expect, **kw)
        return fn
    return wrap


YOLO = ["--approval-mode", "yolo"]
ASK = ["--approval-mode", "always-ask"]


def ui_policy(o, answer, hold: float):
    async def handle(frame):
        await asyncio.sleep(hold)
        return answer(frame)
    o.ui = handle


def approve(frame):
    if frame["method"] == "confirm":
        return {"confirmed": True}
    if frame["method"] == "select":
        opts = frame.get("options", [])
        pick = next((x for x in opts if x.lower().startswith(("allow", "approve", "yes", "run"))), opts[0] if opts else "")
        return {"value": pick}
    return {"cancelled": True}


def deny(frame):
    if frame["method"] == "confirm":
        return {"confirmed": False}
    if frame["method"] == "select":
        opts = frame.get("options", [])
        pick = next((x for x in opts if x.lower().startswith(("deny", "reject", "no", "block"))), None)
        return {"value": pick} if pick else {"cancelled": True}
    return {"cancelled": True}


def ask_answer(frame):
    opts = frame.get("options", [])
    return {"value": opts[0]} if frame["method"] == "select" and opts else {"cancelled": True}


@scenario("S1", YOLO, 1)
async def s1(o):
    return strip(await o.prompt("Run the bash command `sleep 16` exactly once, then reply with exactly: done"))


@scenario("S2-failed", YOLO, 1)
async def s2_failed(o):
    fault(400, -1, "injected permanent failure")
    try:
        return strip(await o.prompt("Reply with exactly: ok", timeout=180))
    finally:
        fault(None)


@scenario("S2-aborted", YOLO, 1)
async def s2_aborted(o):
    mark = len(o.frames)
    rid = await o.send({"type": "prompt", "message": "Run the bash command `sleep 60` exactly once (foreground, not async), then reply with exactly: done"})
    await o.wait(lambda f: f.get("type") == "tool_execution_start", 120, since=mark)
    await asyncio.sleep(18)
    await o.call({"type": "abort"})
    return strip(await o.wait(lambda f: f.get("type") == "prompt_result" and f.get("id") == rid, 120, since=mark))


@scenario("S3-approval-resolved", ASK, 2)
async def s3_approval_ok(o):
    ui_policy(o, approve, 12)
    return strip(await o.prompt("Run the bash command `sleep 8 && echo approved-run` exactly once, then reply with exactly: done"))


@scenario("S3-approval-denied", ASK, 2)
async def s3_approval_no(o):
    ui_policy(o, deny, 12)
    return strip(await o.prompt("Run the bash command `sleep 8 && echo denied-run` exactly once. If it is denied, do not retry; reply with exactly: denied"))


@scenario("S3-ask-resolved", YOLO, 2)
async def s3_ask_ok(o):
    ui_policy(o, ask_answer, 20)
    return strip(await o.prompt("Use the ask tool exactly once to ask me which color I prefer, with the two options Red and Blue (header: Color). After the answer, reply with exactly: noted"))


@scenario("S3-ask-cancelled", YOLO, 2)
async def s3_ask_cancel(o):
    ui_policy(o, lambda f: {"cancelled": True}, 20)
    return strip(await o.prompt("Use the ask tool exactly once to ask me which color I prefer, with the two options Red and Blue (header: Color). If the question is cancelled, do not ask again; reply with exactly: skipped"))


@scenario("S4-retry-recovered", YOLO, 1)
async def s4_retry_ok(o):
    fault(503, 7, "injected transient failure")  # 每次自动重试内含 6 个传输层请求；第 7 个失败后由下一次自动重试恢复
    try:
        return strip(await o.prompt("Run the bash command `sleep 16` exactly once, then reply with exactly: done", timeout=300))
    finally:
        fault(None)


@scenario("S4-retry-exhausted", YOLO, 1)
async def s4_retry_fail(o):
    fault(503, -1, "injected transient failure")
    try:
        return strip(await o.prompt("Reply with exactly: ok", timeout=600))
    finally:
        fault(None)


@scenario("S4-background", YOLO, 1)
async def s4_background(o):
    return strip(await o.prompt("Start the bash command `sleep 20 && echo bg-finished` with async: true so it runs in the background, and end your turn immediately without waiting. When its result is delivered later, reply with exactly: background done", timeout=300))


@scenario("S5-task", YOLO, 1, settle=120)
async def s5_task(o):
    return strip(await o.prompt("Use the task tool once with two tasks in the same call (agent type: default task). Task A: run the bash command `sleep 12 && echo A`. Task B: run the bash command `sleep 6 && echo B`. Each task must only run its command and report the output. Wait for both results, then reply with exactly: tasks done", timeout=600))


@scenario("S5-eval", YOLO, 1, settle=120)
async def s5_eval(o):
    return strip(await o.prompt("Use the eval tool (JavaScript) exactly once. In that cell, read the docs at xd://eval/agents if needed, then call the eval agent() helper twice concurrently: one subagent runs the bash command `sleep 8 && echo E1`, the other runs `sleep 4 && echo E2`; await both and print their results. Then reply with exactly: eval done", timeout=600))


@scenario("S5-workpool", YOLO, 1, settle=120)
async def s5_workpool(o):
    return strip(await o.prompt("Call the eval tool exactly once with language js and exactly this code:\n"
                                "const pool = workpool({ name: \"accept-pool\" });\n"
                                "pool.push(\"Run the bash command `sleep 8 && echo W1` once and report its output.\", \"Run the bash command `sleep 4 && echo W2` once and report its output.\");\n"
                                "pool.close();\n"
                                "Then call the wait tool until both pool results are delivered. Finally reply with exactly: workpool done", timeout=600))


PLAN_PROMPT = ("Plan how to create a file named hello.txt containing the single word hi in the current directory. "
               "Keep the plan to two short steps, write it to the plan file, then request plan review immediately without asking questions.")
# 计划评审菜单（OMP 18.12.6）：Approve and execute／compact context／keep context／Refine plan／Save and quit。
PLAN_KEYS = {"resolve": b"\r", "cancel": b"\x1b", "refine": harness.Tui.DOWN * 3 + b"\r", "save": harness.Tui.DOWN * 4 + b"\r"}


def plan(name: str, mode: str) -> None:
    """计划评审：保持待处理 15 秒让提醒发出，再按 mode 应答；refine 输入细化意见后取消第二次评审，save 确认默认路径。"""
    w0, e0 = len(rows("webhook.jsonl")), len(rows("events.jsonl"))
    since = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tui = harness.Tui(name, "--approval-mode", "yolo")
    result: dict[str, Any] = {"ready": tui.wait_screen("π >", 90)}
    tui.pump(3)
    tui.type_line(f"/rename {MARKER} {name}")
    tui.pump(2)
    tui.type_line("/plan")
    tui.pump(3)
    tui.type_line(PLAN_PROMPT)
    review = tui.wait_event(e0, lambda e: e["type"] == "plan_review_requested", 300)
    result["review"] = bool(review)
    if review:
        tui.pump(15)
        for key in PLAN_KEYS[mode]:
            tui.keys(bytes([key]))
        resolved = tui.wait_event(e0, lambda e: e["type"] == "plan_review_resolved", 60)
        result["choice"] = resolved and resolved.get("choice")
        if mode == "save":
            tui.pump(2)
            tui.keys(b"\r")  # 保存路径对话框：接受默认路径
        if mode == "refine":
            tui.pump(3)
            tui.type_line("Add a third step that verifies the file content. Then request plan review again immediately.")
            second = tui.wait_event(e0, lambda e: e["type"] == "plan_review_requested" and e["reviewId"] != review["reviewId"], 300)
            result["second_review"] = bool(second)
            if second:
                tui.pump(15)
                tui.keys(b"\x1b")
                tui.wait_event(e0, lambda e: e["type"] == "plan_review_resolved" and e["reviewId"] == second["reviewId"], 60)
        tui.pump(60)  # 等待通知发送，并观察这一轮是否收束
    tui.quit()
    report(name, since, 0, result, w0, e0, ["tui", mode])


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit(__doc__)
    command = argv[0]
    if command == "setup":
        harness.setup()
    elif command == "proxies":
        harness.proxies()
    elif command == "plan":
        plan(argv[1], argv[2])
    else:
        # “场景:别名”以别名作为会话名与日志名，便于同一场景在不同部署形态下重复执行。
        for item in argv:
            name, _, alias = item.partition(":")
            asyncio.run(SCENARIOS[name](alias or name))


if __name__ == "__main__":
    main(sys.argv[1:])
