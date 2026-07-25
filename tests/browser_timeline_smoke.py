"""Offline Chrome/CDP geometry capture for the rendered subagent timeline."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

import aiohttp


def resolve_chrome_executable() -> str:
    candidates = [
        os.environ.get("CHROME_BIN"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError(
        "Timeline browser smoke requires a preinstalled Chrome/Chromium. "
        "Set CHROME_BIN to an executable path; the test never downloads a browser."
    )


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_debug_target(port: int, timeout: float = 12.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    endpoint = f"http://127.0.0.1:{port}/json/list"
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(endpoint, timeout=0.5) as response:
                targets = json.load(response)
            page = next(
                target
                for target in targets
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl")
            )
            return page
        except (OSError, ValueError, StopIteration) as exc:
            last_error = exc
            time.sleep(0.1)
    raise RuntimeError(f"Chrome DevTools target did not start: {last_error}")


async def _capture_with_cdp(
    websocket_url: str,
    *,
    html_uri: str,
    viewport_width: int,
    viewport_height: int,
) -> tuple[dict[str, Any], bytes]:
    message_id = 0
    async with (
        aiohttp.ClientSession() as session,
        session.ws_connect(websocket_url, max_msg_size=32 * 1024 * 1024) as ws,
    ):

        async def command(
            method: str, params: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            nonlocal message_id
            message_id += 1
            current_id = message_id
            await ws.send_json(
                {"id": current_id, "method": method, "params": params or {}}
            )
            while True:
                message = await asyncio.wait_for(ws.receive_json(), timeout=12)
                if message.get("id") != current_id:
                    continue
                if "error" in message:
                    raise RuntimeError(
                        f"CDP {method} failed: {message['error'].get('message')}"
                    )
                return message.get("result", {})

        await command("Page.enable")
        await command("Runtime.enable")
        await command(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": viewport_width,
                "height": viewport_height,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
        await command("Page.navigate", {"url": html_uri})
        for _ in range(120):
            ready = await command(
                "Runtime.evaluate",
                {
                    "expression": "document.readyState",
                    "returnByValue": True,
                },
            )
            if ready.get("result", {}).get("value") == "complete":
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError(
                "Timeline page did not reach document.readyState=complete"
            )

        expression = r"""
            (() => {
              const rect = (node) => {
                const value = node.getBoundingClientRect();
                return {
                  left: value.left, top: value.top, right: value.right,
                  bottom: value.bottom, width: value.width, height: value.height
                };
              };
              const visualStyle = (node) => {
                const value = getComputedStyle(node);
                return {
                  backgroundColor: value.backgroundColor,
                  backgroundImage: value.backgroundImage,
                  borderTopColor: value.borderTopColor,
                  borderRightColor: value.borderRightColor,
                  color: value.color,
                  boxShadow: value.boxShadow
                };
              };
              const body = document.body;
              const root = document.documentElement;
              const card = document.querySelector('.card');
              const timeline = document.querySelector('.timeline');
              const axis = document.querySelector('.axis');
              const axisTrack = document.querySelector('.axis-track');
              const rows = [...document.querySelectorAll('.timeline-row')];
              return {
                document: {
                  scrollWidth: Math.max(body.scrollWidth, root.scrollWidth),
                  scrollHeight: Math.max(body.scrollHeight, root.scrollHeight),
                  clientWidth: root.clientWidth,
                  clientHeight: root.clientHeight
                },
                body: {
                  scrollWidth: body.scrollWidth,
                  scrollHeight: body.scrollHeight,
                  clientWidth: body.clientWidth,
                  clientHeight: body.clientHeight
                },
                card: rect(card),
                timeline: rect(timeline),
                styles: {
                  body: visualStyle(body),
                  card: visualStyle(card),
                  timeline: visualStyle(timeline),
                  axis: visualStyle(axis),
                  axisTrack: visualStyle(axisTrack)
                },
                rows: rows.map((row) => {
                  const name = row.querySelector('.task-name');
                  const agent = row.querySelector('.task-agent');
                  const task = row.querySelector('.task');
                  const plot = row.querySelector('.track');
                  const state = row.querySelector('.state');
                  const stateLabel = row.querySelector('.state-label');
                  const bar = row.querySelector('.bar');
                  const style = getComputedStyle(name);
                  return {
                    row: rect(row), task: rect(task), name: rect(name),
                    plot: rect(plot), state: rect(state),
                    text: name.textContent,
                    styles: {
                      task: visualStyle(task),
                      name: visualStyle(name),
                      agent: agent ? visualStyle(agent) : null,
                      plot: visualStyle(plot),
                      state: visualStyle(state),
                      stateLabel: visualStyle(stateLabel),
                      bar: bar ? visualStyle(bar) : null
                    },
                    bar: bar ? rect(bar) : null,
                    barClass: bar ? bar.className : "",
                    nameMetrics: {
                      scrollWidth: name.scrollWidth,
                      scrollHeight: name.scrollHeight,
                      clientWidth: name.clientWidth,
                      clientHeight: name.clientHeight,
                      whiteSpace: style.whiteSpace,
                      overflowWrap: style.overflowWrap,
                      wordBreak: style.wordBreak
                    }
                  };
                })
              };
            })()
            """
        evaluated = await command(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
        )
        geometry = evaluated.get("result", {}).get("value")
        if not isinstance(geometry, dict):
            raise TypeError("Chrome returned no timeline geometry")

        scroll_width = int(geometry["document"]["scrollWidth"])
        scroll_height = int(geometry["document"]["scrollHeight"])
        await command(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": scroll_width,
                "height": scroll_height,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
        screenshot = await command(
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": True,
            },
        )
        return geometry, base64.b64decode(screenshot["data"])


def capture_timeline_browser_geometry(
    html: str,
    *,
    viewport_width: int,
    viewport_height: int,
    output_dir: Path,
    name: str,
) -> tuple[dict[str, Any], Path, str]:
    chrome = resolve_chrome_executable()
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / f"{name}.html"
    screenshot_path = output_dir / f"{name}.png"
    profile_path = output_dir / f"{name}-chrome-profile"
    html_path.write_text(html, encoding="utf-8")
    port = _free_local_port()
    process = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-sync",
            "--metrics-recording-only",
            "--no-first-run",
            "--no-default-browser-check",
            "--force-device-scale-factor=1",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_path}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        target = _wait_for_debug_target(port)
        geometry, screenshot = asyncio.run(
            _capture_with_cdp(
                target["webSocketDebuggerUrl"],
                html_uri=html_path.as_uri(),
                viewport_width=viewport_width,
                viewport_height=viewport_height,
            )
        )
        screenshot_path.write_bytes(screenshot)
        return geometry, screenshot_path, chrome
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
