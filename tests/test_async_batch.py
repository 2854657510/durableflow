import asyncio

from app.core.retry import NonRetryableError, RetryableError
from app.llm.async_batch import AsyncMockLLMClient, run_batch
from app.llm.client import LLMResponse


class TrackingClient:
    def __init__(self, delay_ms: int = 10):
        self.delay_ms = delay_ms
        self.active = 0
        self.max_active = 0

    async def chat(self, messages, **_: object) -> LLMResponse:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay_ms / 1000)
            return LLMResponse(content="ok", model="tracking")
        finally:
            self.active -= 1

    async def aclose(self) -> None:
        return None


class FlakyClient:
    def __init__(self, failures: int, error: Exception | None = None):
        self.failures = failures
        self.error = error or RetryableError("temporary")
        self.calls = 0

    async def chat(self, messages, **_: object) -> LLMResponse:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return LLMResponse(content="recovered", model="flaky")

    async def aclose(self) -> None:
        return None


class SlowClient:
    async def chat(self, messages, **_: object) -> LLMResponse:
        await asyncio.sleep(0.2)
        return LLMResponse(content="late", model="slow")

    async def aclose(self) -> None:
        return None


def test_semaphore_caps_concurrency():
    client = TrackingClient()
    result = asyncio.run(run_batch(client, [f"q{i}" for i in range(20)], max_concurrency=3))

    assert result.stats.succeeded == 20
    assert client.max_active == 3


def test_retryable_error_is_retried():
    client = FlakyClient(failures=2)
    result = asyncio.run(
        run_batch(client, ["hello"], max_attempts=3, base_delay=0.001)
    )

    assert result.items[0].ok is True
    assert result.items[0].attempts == 3
    assert client.calls == 3


def test_non_retryable_error_fails_fast():
    client = FlakyClient(failures=5, error=NonRetryableError("bad args"))
    result = asyncio.run(
        run_batch(client, ["hello"], max_attempts=3, base_delay=0.001)
    )

    assert result.items[0].ok is False
    assert result.items[0].attempts == 1
    assert client.calls == 1


def test_timeout_is_marked_failed():
    result = asyncio.run(
        run_batch(
            SlowClient(),
            ["slow"],
            max_attempts=1,
            timeout_seconds=0.01,
        )
    )

    assert result.stats.failed == 1
    assert "TimeoutError" in result.items[0].error


def test_batch_stats_are_aggregated():
    client = AsyncMockLLMClient(delay_ms=5)
    result = asyncio.run(run_batch(client, [f"q{i}" for i in range(10)], max_concurrency=5))

    assert result.stats.total == 10
    assert result.stats.succeeded == 10
    assert result.stats.failed == 0
    assert result.stats.p95_latency_ms >= result.stats.p50_latency_ms >= 0
    assert result.stats.throughput_per_second > 0
    assert result.stats.total_attempts == 10
