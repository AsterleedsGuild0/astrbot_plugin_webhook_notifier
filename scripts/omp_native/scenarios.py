#!/usr/bin/env python3
"""原生 OMP 隔离验收场景。

用法（在仓库根目录）::

    export OMP_NATIVE_VERIFY_FILE=<测试 Endpoint 的 KEY=VALUE 文件>
    uv run --frozen python scripts/omp_native/scenarios.py setup        # 生成隔离 HOME
    uv run --frozen python scripts/omp_native/scenarios.py proxies      # 前台运行本机代理（另开终端或后台）
    uv run --frozen python scripts/omp_native/scenarios.py S1 S2-failed # 执行 RPC 场景
    uv run --frozen python scripts/omp_native/scenarios.py plan S3-plan resolve|cancel|save|refine
    uv run --frozen python scripts/omp_native/scenarios.py tree S16-tree

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


async def run(name: str, args: list[str], steps, expect: int, env: dict[str, str] | None = None, settle: float = 90,
              cwd: str | None = None, config: str | None = None) -> None:
    """``cwd`` 让多个场景共用工作目录（恢复会话）；``config`` 是本场景的 config.yml 覆盖层内容。"""
    if config:
        overlay = harness.BASE / "configs" / f"{name}.yml"
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(config)
        args = [*args, "--config", str(overlay)]
    await asyncio.sleep(25)  # 上一场景的在途通知（服务端同步渲染约 10～20 秒）先落盘，避免切片串位
    w0, e0 = len(rows("webhook.jsonl")), len(rows("events.jsonl"))
    since = datetime.now(timezone.utc).isoformat(timespec="seconds")
    omp = harness.Omp(name, args, WORK / (cwd or name.lower()), env)
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


# 费用与计时取样（#39）：尽量短的提示；短运行通常不发通知，expect=0。
def speed_state(data: Any) -> dict[str, Any]:
    """RPC get_state 中与用量、费用、速度相关的字段（只取数值与结构）。"""
    keep = ("token", "cost", "second", "ttft", "usage", "stats")
    return {k: v for k, v in (data or {}).items() if any(s in k.lower() for s in keep)}


@scenario("S17-short", YOLO, 0, settle=5, cwd="s17")
async def s17_short(o):
    result = strip(await o.prompt("Reply with exactly: ok"))
    return {**result, "state": speed_state((await o.call({"type": "get_state"})).get("data"))}


@scenario("S17-resume", [*YOLO, "--continue"], 0, settle=5, cwd="s17")
async def s17_resume(o):
    """恢复 S17-short 的会话，核对会话累计值包含旧消息、本次差值只含新请求。"""
    return strip(await o.prompt("Reply with exactly: again"))


@scenario("S17-task", YOLO, 0, settle=5)
async def s17_task(o):
    return strip(await o.prompt("Use the task tool exactly once with context \"usage sample\" and a single task (agent type: default task) whose instructions are: "
                                "reply with exactly: child ok. Do not run any other tool. After the result, reply with exactly: parent ok", timeout=600))


@scenario("S17-task-sync", YOLO, 0, settle=5, config="async:\n  enabled: false\n")
async def s17_task_sync(o):
    """关闭 async 后 task 阻塞到合并结果，子代理用量随 toolResult 计入主会话 subagentCost。"""
    return await s17_task(o)


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


# ---- #34／#35：错误分类、会话创建时间、上下文与资源信号 ----

@scenario("S8-resume-a", YOLO, 1, cwd="s8-resume")
async def s8_resume_a(o):
    return strip(await o.prompt("Run the bash command `sleep 16` exactly once, then reply with exactly: first"))


@scenario("S8-resume-b", [*YOLO, "--continue"], 1, cwd="s8-resume")
async def s8_resume_b(o):
    """在同一工作目录以 --continue 恢复上一会话，核对会话创建时间不随恢复改变。"""
    await asyncio.sleep(30)
    return strip(await o.prompt("Run the bash command `sleep 16` exactly once, then reply with exactly: second"))


@scenario("S8-429", YOLO, 1)
async def s8_rate_limited(o):
    fault(429, 1, "injected rate limit")
    try:
        return strip(await o.prompt("Run the bash command `sleep 16` exactly once, then reply with exactly: done", timeout=300))
    finally:
        fault(None)


@scenario("S8-401", YOLO, 1)
async def s8_unauthorized(o):
    fault(401, -1, "injected unauthorized")
    try:
        return strip(await o.prompt("Reply with exactly: ok", timeout=300))
    finally:
        fault(None)


FALLBACK = "retry:\n  fallbackChains:\n    default:\n      - cpa/glm-5.3-flash\n"


@scenario("S8-fallback", YOLO, 1, config=FALLBACK)
async def s8_fallback(o):
    fault(400, 1, "injected hard failure")
    try:
        return strip(await o.prompt("Run the bash command `sleep 16` exactly once, then reply with exactly: done", timeout=300))
    finally:
        fault(None)


COMPACTION = "compaction:\n  thresholdTokens: 16000\n  keepRecentTokens: 2000\n  asyncEnabled: false\n"


@scenario("S8-compaction", YOLO, 1, config=COMPACTION, settle=180)
async def s8_compaction(o):
    return strip(await o.prompt("Run the bash command `seq 1 6000` and look at the output. Then run `seq 6001 12000` and look at it. "
                                "Then run `sleep 16`. Finally reply with exactly: compacted", timeout=900))


@scenario("S8-todo", YOLO, 1, settle=180)
async def s8_todo(o):
    return strip(await o.prompt("Use the todo tool to create a plan with three items: 'alpha', 'beta', 'gamma'. Do not work on any of them "
                                "and do not mark any as done. Run the bash command `sleep 16`, then end your turn by replying exactly: stopping. "
                                "If you are reminded about unfinished todos, reply exactly: still stopping, without changing the todos.", timeout=600))


@scenario("S8-goal", YOLO, 1, settle=180)
async def s8_goal(o):
    created = strip(await o.call({"type": "goal", "op": "create", "objective": "Run the bash command `sleep 16` once and report done",
                                  "token_budget": 200000}))
    result = strip(await o.prompt("Work on the active goal: run the bash command `sleep 16` exactly once, then mark the goal complete "
                                  "with the goal tool and reply with exactly: goal done", timeout=600))
    return {"goal": created.get("success"), "prompt": result}


@scenario("S9-subagent-failed", YOLO, 2, settle=180)
async def s9_subagent_failed(o):
    """task 子代理的首个模型请求被注入 400：子代理单独发失败通知（带所属主会话），主会话随后正常收束。"""
    mark = len(o.frames)
    rid = await o.send({"type": "prompt", "message": "Use the task tool exactly once with a single task (agent type: default task): run the bash command `echo child` and report the output. "
                                                     "If the task fails, do not retry it; run the bash command `sleep 16` and then reply with exactly: parent done"})
    events = len(rows("events.jsonl"))
    deadline = time.time() + 300
    # 子代理 agent_start 出现后再注入：此时下一次模型请求属于子代理，主会话在等待 task 结果。
    while time.time() < deadline and not any(e["type"] == "agent_start" and (e.get("agent") or {}).get("kind") == "sub" for e in rows("events.jsonl", events)):
        await asyncio.sleep(0.05)
    fault(400, 1, "injected subagent failure")
    try:
        return strip(await o.wait(lambda f: f.get("type") == "prompt_result" and f.get("id") == rid, 600, since=mark))
    finally:
        fault(None)


@scenario("S9-nested", YOLO, 1, settle=180)
async def s9_nested(o):
    return strip(await o.prompt("Use the task tool exactly once with a single task (agent type: default task). In that task's instructions, tell the subagent to "
                                "itself use the task tool exactly once with a single task that runs the bash command `sleep 8 && echo nested` and reports the output, "
                                "then report that output. Wait for the result, then reply with exactly: nested done", timeout=900))


# ---- #37 补测：为本会话批准、eval 间接 ask、会话分支 ----

def approve_session(seen: list[dict]):
    """优先选“本会话内都允许”类选项；记录实际看到的选项，便于核对 OMP 文案。"""
    def answer(frame):
        seen.append({k: frame.get(k) for k in ("method", "title", "options")})
        if frame["method"] == "select":
            opts = frame.get("options", [])
            pick = next((x for x in opts if "session" in x.lower()), None) or next((x for x in opts if x.lower().startswith(("allow", "approve", "yes"))), opts[0] if opts else "")
            return {"value": pick}
        return {"confirmed": True} if frame["method"] == "confirm" else {"cancelled": True}
    return answer


@scenario("S16-approval-session", ASK, 2, settle=120)
async def s16_approval_session(o):
    seen: list[dict] = []
    ui_policy(o, approve_session(seen), 12)
    first = strip(await o.prompt("Run the bash command `sleep 8 && echo first` exactly once, then reply with exactly: first done"))
    await asyncio.sleep(5)
    second = strip(await o.prompt("Run the bash command `sleep 12 && echo second` exactly once, then reply with exactly: second done"))
    return {"first": first.get("status"), "second": second.get("status"), "ui": seen}


@scenario("S16-eval-ask", YOLO, 2, settle=120)
async def s16_eval_ask(o):
    ui_policy(o, ask_answer, 20)
    return strip(await o.prompt("Call the eval tool exactly once with language js and exactly this code:\n"
                                "const answer = await tool.ask({ questions: [{ id: \"color\", question: \"Which color do you prefer?\", header: \"Color\", "
                                "options: [{ label: \"Red\" }, { label: \"Blue\" }] }] });\nprint(JSON.stringify(answer));\n"
                                "Then reply with exactly: eval asked", timeout=600))


@scenario("S16-branch", YOLO, 3, settle=150)
async def s16_branch(o):
    """首轮正常结束；第二轮启动后台任务并结束本轮（后台等待中周期未收束），随后 branch 回首条用户消息，观察旧周期收束。"""
    first = strip(await o.prompt("Run the bash command `sleep 16` exactly once, then reply with exactly: first"))
    entries = (await o.call({"type": "get_entries"}))["data"]["entries"]
    user = next(e for e in entries if e.get("type") == "message" and (e.get("message") or {}).get("role") == "user")
    second = strip(await o.prompt("Start the bash command `sleep 40 && echo late` with async: true so it runs in the background, "
                                  "and end your turn immediately without waiting.", timeout=300))
    await asyncio.sleep(3)
    branched = strip(await o.call({"type": "branch", "entryId": user["id"]}))
    await asyncio.sleep(5)
    third = strip(await o.prompt("Run the bash command `sleep 16` exactly once, then reply with exactly: after branch"))
    return {"first": first.get("status"), "second": second.get("status"), "branch": branched.get("success"), "third": third.get("status")}


@scenario("S16-fork", YOLO, 2, settle=150)
async def s16_fork(o):
    """后台等待中执行 fork：会话切换到新文件，旧周期应以 session_changed 收束。"""
    await o.prompt("Start the bash command `sleep 40 && echo late` with async: true so it runs in the background, "
                   "and end your turn immediately without waiting.", timeout=300)
    await asyncio.sleep(3)
    forked = strip(await o.call({"type": "fork"}))
    await asyncio.sleep(5)
    after = strip(await o.prompt("Run the bash command `sleep 16` exactly once, then reply with exactly: after fork"))
    return {"fork": forked.get("data"), "after": after.get("status")}


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


def tree(name: str) -> None:
    """/tree 只在 TUI 中可用：首轮正常结束，第二轮留下后台任务后结束本轮，再用 /tree 跳回较早节点，观察旧周期收束。"""
    w0, e0 = len(rows("webhook.jsonl")), len(rows("events.jsonl"))
    since = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tui = harness.Tui(name, "--approval-mode", "yolo")
    result: dict[str, Any] = {"ready": tui.wait_screen("π >", 90)}
    tui.pump(3)
    tui.type_line(f"/rename {MARKER} {name}")
    tui.pump(2)
    tui.type_line("Run the bash command `sleep 16` exactly once, then reply with exactly: first")
    result["first"] = bool(tui.wait_event(e0, lambda e: e["type"] == "agent_end", 300))
    tui.pump(5)
    mark = len(rows("events.jsonl"))
    tui.type_line("Start the bash command `sleep 60 && echo late` with async: true so it runs in the background, and end your turn immediately without waiting.")
    result["background"] = bool(tui.wait_event(mark, lambda e: e["type"] == "agent_end" and e.get("awaitingAsyncWork"), 300))
    tui.pump(3)
    tui.type_line("/tree")
    tui.pump(3)
    # 光标初始在当前叶子；上移两项到首轮的 assistant 回复（选用户消息会改为回填草稿，选当前叶子是空操作）。
    for _ in range(2):
        tui.keys(harness.Tui.UP)
    tui.keys(b"\r", 2)
    moved = tui.wait_event(mark, lambda e: e["type"] == "session_tree", 30)
    result["tree"] = bool(moved)
    tui.pump(45)  # 等待旧周期通知发送
    tui.quit()
    report(name, since, 0, result, w0, e0, ["tui", "tree"])


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
    elif command == "tree":
        tree(argv[1])
    else:
        # “场景:别名”以别名作为会话名与日志名，便于同一场景在不同部署形态下重复执行。
        for item in argv:
            name, _, alias = item.partition(":")
            asyncio.run(SCENARIOS[name](alias or name))


if __name__ == "__main__":
    main(sys.argv[1:])
