"""Minimal astrbot.api.web request/response stubs."""

from __future__ import annotations

from typing import Any


class _Request:
    def __init__(self) -> None:
        self.json: Any = None
        self.view_args: dict[str, Any] = {}
        self.method = "GET"


request = _Request()


def json_response(data: Any, status: int = 200):
    return data, status


def error_response(message: str, status: int = 400):
    return {"error": message}, status
