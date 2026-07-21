from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .models import NormalizedEvent


class ProviderError(Exception):
    """Adapter 可预见的处理错误，由 HTTP 层转换为对应响应。

    Attributes:
        code: 稳定错误码，如 ``invalid_payload``。
        retryable: 调用方是否可重试。
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class ProviderAdapter(ABC):
    """Provider adapter 首版契约。

    首版不做 provider 专属鉴权 hook，统一复用 Endpoint Bearer。
    Adapter 不接触 Token、Endpoint 目标地址、renderer 或 sender。
    """

    @property
    @abstractmethod
    def provider(self) -> str:
        """稳定 provider key，如 ``"omp"`` 或 ``"opencode"``。"""

    @abstractmethod
    def parse(
        self,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        received_at: str,
    ) -> NormalizedEvent:
        """将原始请求解析为标准化事件。

        Args:
            headers: HTTP 请求头字典（key 为小写）。
            payload: 已解析的 JSON body。
            received_at: 请求接收时间的 ISO-8601 字符串。

        Returns:
            NormalizedEvent 对象。

        Raises:
            ProviderError: payload/契约校验失败。
        """


class ProviderRegistry:
    """Provider adapter 注册中心。

    在插件/服务初始化时构造一次，通过构造参数依赖注入
    Webhook Server／HTTP handler。禁止模块级可变全局单例，
    禁止每次请求重新构造。多插件实例/测试实例不得共享可变状态。

    freeze() 后禁止注册新 adapter，确保运行时行为不变。
    """

    def __init__(self) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}
        self._frozen: bool = False

    def freeze(self) -> None:
        """冻结 Registry，禁止后续注册。

        重复调用幂等。冻结后 register() 将抛出 ProviderRegistryError。
        """
        self._frozen = True

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    def register(self, adapter: ProviderAdapter) -> None:
        """显式注册 adapter。

        Raises:
            ProviderRegistryError: 已冻结、重复 provider key 或 adapter.provider 无效。
        """
        if self._frozen:
            raise ProviderRegistryError(
                "ProviderRegistry 已冻结，禁止注册",
            )
        key = adapter.provider
        if not key:
            raise ProviderRegistryError("provider key 不能为空")
        if key in self._adapters:
            raise ProviderRegistryError(
                f"重复注册 provider: {key}",
            )
        self._adapters[key] = adapter

    def get(self, provider: str) -> ProviderAdapter | None:
        """按 provider key 精确获取 adapter，未注册时返回 None。"""
        return self._adapters.get(provider)

    @property
    def providers(self) -> frozenset[str]:
        """返回所有已注册的 provider key 集合。"""
        return frozenset(self._adapters)


class ProviderRegistryError(RuntimeError):
    """Registry 操作错误（重复注册、空 key 等）。"""
