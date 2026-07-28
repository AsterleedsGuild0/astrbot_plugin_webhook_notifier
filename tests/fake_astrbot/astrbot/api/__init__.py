"""Minimal stubs for astrbot.api module."""

import copy
import logging
from typing import Any


class AstrBotConfig(dict):
    """测试用配置字典。

    save_config() 记录快照供测试验证持久化契约。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.save_call_count: int = 0
        self.save_snapshots: list[dict[str, Any]] = []
        self._fail_save: bool = False

    def set_fail_save(self, fail: bool = True) -> None:
        """设置 save_config() 是否抛出异常（测试用）。"""
        self._fail_save = fail

    def save_config(self) -> None:
        """模拟持久化：记录当前快照；失败时抛出异常。"""
        self.save_call_count += 1
        if self._fail_save:
            raise RuntimeError("simulated save failure")
        self.save_snapshots.append(copy.deepcopy(dict(self)))


logger = logging.getLogger("astrbot")
