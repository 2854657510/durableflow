from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

RUNS_TOTAL = Counter("durableflow_runs_total", "运行实例数量", ["status"])
STEPS_TOTAL = Counter("durableflow_steps_total", "步骤数量", ["status"])
QUEUE_DEPTH = Gauge("durableflow_queue_depth", "待执行步骤数")
STEP_DURATION = Histogram(
    "durableflow_step_duration_seconds",
    "步骤耗时",
    ["node_type", "status"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300),
)
LEASE_EXPIRED_TOTAL = Counter("durableflow_lease_expired_total", "租约过期次数")
RETRY_TOTAL = Counter("durableflow_retry_total", "重试次数", ["reason"])
LLM_TOKENS_TOTAL = Counter("durableflow_llm_tokens_total", "LLM token 数", ["model", "type"])
LLM_COST_TOTAL = Counter("durableflow_llm_cost_total", "LLM 估算成本")
OUTBOX_LAG = Gauge("durableflow_outbox_lag_seconds", "Outbox 最老待投递事件延迟")


def inc_run(status: str) -> None:
    RUNS_TOTAL.labels(status=status).inc()


def inc_step(status: str) -> None:
    STEPS_TOTAL.labels(status=status).inc()


def set_queue_depth(value: int) -> None:
    QUEUE_DEPTH.set(value)


def observe_step(node_type: str, status: str, duration: float) -> None:
    STEP_DURATION.labels(node_type=node_type, status=status).observe(duration)


def inc_lease_expired() -> None:
    LEASE_EXPIRED_TOTAL.inc()


def inc_retry(reason: str) -> None:
    RETRY_TOTAL.labels(reason=reason).inc()


def observe_llm(model: str, prompt_tokens: int, completion_tokens: int, cost: float) -> None:
    LLM_TOKENS_TOTAL.labels(model=model, type="prompt").inc(prompt_tokens)
    LLM_TOKENS_TOTAL.labels(model=model, type="completion").inc(completion_tokens)
    LLM_COST_TOTAL.inc(cost)


def set_outbox_lag(seconds: float) -> None:
    OUTBOX_LAG.set(max(0.0, seconds))


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
