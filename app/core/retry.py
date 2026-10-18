from __future__ import annotations

import random
from dataclasses import dataclass, field


class DurableFlowError(Exception):
    """项目内所有可识别异常的基类。"""


class RetryableError(DurableFlowError):
    """瞬时错误：超时、429、5xx、连接失败。"""


class NonRetryableError(DurableFlowError):
    """确定性错误：参数错误、鉴权失败、Schema 不匹配、业务拒绝。"""


class ApprovalRequired(NonRetryableError):
    """高风险操作需要人工审批。"""

    def __init__(self, tool: str, arguments: dict, reason: str = ""):
        self.tool = tool
        self.arguments = arguments
        self.reason = reason
        super().__init__(f"工具 {tool} 需要人工审批: {reason}")


class ToolExecutionError(RetryableError):
    """工具执行失败，默认按可重试处理。"""


class ToolArgumentError(NonRetryableError):
    """工具参数不合法，重试没有意义。"""


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    initial_delay_s: float = 1.0
    multiplier: float = 2.0
    max_delay_s: float = 60.0
    jitter: float = 0.2
    retry_on: tuple[str, ...] = field(
        default_factory=lambda: ("RetryableError", "TimeoutError", "RateLimitError", "ConnectionError")
    )

    @classmethod
    def from_config(cls, config: dict | None, max_attempts: int | None = None) -> RetryPolicy:
        config = config or {}
        return cls(
            max_attempts=max_attempts or int(config.get("max_attempts", 3)),
            initial_delay_s=float(config.get("initial_delay_s", 1.0)),
            multiplier=float(config.get("multiplier", 2.0)),
            max_delay_s=float(config.get("max_delay_s", 60.0)),
            jitter=float(config.get("jitter", 0.2)),
            retry_on=tuple(config.get("retry_on", cls().retry_on)),
        )


def compute_delay(attempt: int, policy: RetryPolicy) -> float:
    """attempt 从 1 开始。返回带抖动的秒数。"""
    attempt = max(1, attempt)
    delay = min(policy.max_delay_s, policy.initial_delay_s * (policy.multiplier ** (attempt - 1)))
    if policy.jitter:
        delay *= 1 + random.uniform(-policy.jitter, policy.jitter)
    return max(0.0, delay)


def is_retryable(exc: BaseException, policy: RetryPolicy) -> bool:
    if isinstance(exc, NonRetryableError):
        return False
    if isinstance(exc, RetryableError):
        return True
    names = {cls.__name__ for cls in type(exc).__mro__}
    return bool(names.intersection(policy.retry_on)) or "all" in policy.retry_on
