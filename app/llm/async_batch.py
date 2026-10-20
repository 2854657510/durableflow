"""异步批量 LLM 调用。

用途：一次要打几十/上百个模型请求时，用 asyncio + 信号量控制并发，
避免把线程池打满，也避免对上游造成 429。每个请求独立超时、独立重试，
单个失败不会拖垮整批。
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from app.config import Settings
from app.core.context import estimate_tokens
from app.core.retry import NonRetryableError, RetryableError
from app.llm.client import LLMResponse
from app.llm.pricing import estimate_cost


class AsyncLLMClient(Protocol):
    async def chat(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        tools: list[dict] | None = None,
    ) -> LLMResponse: ...

    async def aclose(self) -> None: ...


@dataclass
class BatchItem:
    index: int
    ok: bool
    response: LLMResponse | None = None
    error: str | None = None
    attempts: int = 1
    latency_ms: int = 0
    queue_ms: int = 0

    def to_dict(self) -> dict:
        data = {
            "index": self.index,
            "ok": self.ok,
            "attempts": self.attempts,
            "latency_ms": self.latency_ms,
            "queue_ms": self.queue_ms,
        }
        if self.response is not None:
            data["content"] = self.response.content
            data["model"] = self.response.model
            data["prompt_tokens"] = self.response.prompt_tokens
            data["completion_tokens"] = self.response.completion_tokens
            data["cost"] = self.response.cost
        if self.error is not None:
            data["error"] = self.error
        return data


@dataclass
class BatchStats:
    total: int
    succeeded: int
    failed: int
    total_attempts: int
    duration_ms: int
    throughput_per_second: float
    p50_latency_ms: int
    p95_latency_ms: int
    p95_queue_ms: int
    prompt_tokens: int
    completion_tokens: int
    cost: float


@dataclass
class BatchResult:
    items: list[BatchItem] = field(default_factory=list)
    stats: BatchStats | None = None

    def to_dict(self) -> dict:
        return {
            "stats": self.stats.__dict__ if self.stats else None,
            "items": [item.to_dict() for item in self.items],
        }


class AsyncMockLLMClient:
    """模拟固定延迟的异步 LLM，用于压测和单测，不产生 API 成本。"""

    def __init__(self, delay_ms: int = 50, model: str = "mock-async"):
        self.delay_ms = delay_ms
        self.model = model

    async def chat(self, messages: list[dict], **_: object) -> LLMResponse:
        await asyncio.sleep(self.delay_ms / 1000)
        prompt_tokens = sum(estimate_tokens(str(m.get("content", ""))) for m in messages)
        content = "mock async response"
        return LLMResponse(
            content=content,
            model=self.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=estimate_tokens(content),
            latency_ms=self.delay_ms,
            cost=0.0,
        )

    async def aclose(self) -> None:
        return None


class AsyncOpenAICompatibleClient:
    """OpenAI 兼容接口的异步客户端，复用连接池。"""

    def __init__(self, settings: Settings, *, timeout_seconds: float = 60.0):
        self.base_url = settings.llm_base_url.rstrip("/")
        self.api_key = settings.llm_api_key
        self.default_model = settings.llm_model
        self.input_per_1m = settings.llm_input_price_per_1m
        self.output_per_1m = settings.llm_output_price_per_1m
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def chat(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        if not self.api_key:
            raise NonRetryableError("LLM_API_KEY 未配置")
        model = model or self.default_model
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if response_format:
            payload["response_format"] = response_format
        if tools:
            payload["tools"] = tools

        started = time.perf_counter()
        try:
            resp = await self._client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
        except httpx.TimeoutException as exc:
            raise RetryableError(f"LLM 超时: {exc}") from exc
        except httpx.HTTPError as exc:
            raise RetryableError(f"LLM 连接失败: {exc}") from exc

        if resp.status_code == 429:
            raise RetryableError("LLM 限流 429")
        if resp.status_code >= 500:
            raise RetryableError(f"LLM 服务端错误 {resp.status_code}")
        if resp.status_code >= 400:
            raise NonRetryableError(f"LLM 请求错误 {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        content = data["choices"][0]["message"].get("content") or ""
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        latency = int((time.perf_counter() - started) * 1000)
        return LLMResponse(
            content=content,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency,
            cost=estimate_cost(prompt_tokens, completion_tokens, self.input_per_1m, self.output_per_1m),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def create_async_client(settings: Settings) -> AsyncLLMClient:
    if settings.llm_provider.lower() == "mock" or not settings.llm_api_key:
        return AsyncMockLLMClient()
    return AsyncOpenAICompatibleClient(settings)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, NonRetryableError):
        return False
    if isinstance(exc, (RetryableError, TimeoutError, httpx.TimeoutException, httpx.TransportError)):
        return True
    return False


def _percentile(values: list[int], ratio: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(ratio * len(ordered)) - 1))
    return ordered[index]


async def run_batch(
    client: AsyncLLMClient,
    prompts: list[str],
    *,
    max_concurrency: int = 8,
    timeout_seconds: float = 30.0,
    max_attempts: int = 3,
    base_delay: float = 0.2,
    max_delay: float = 5.0,
) -> BatchResult:
    """并发执行一批 prompt。

    - 信号量限制同时在途的请求数；
    - 重试退避期间释放信号量，不占着并发槽空等；
    - 单个请求失败只影响自己，最后统一汇总。
    """
    if max_concurrency < 1:
        raise ValueError("max_concurrency 必须 >= 1")

    semaphore = asyncio.Semaphore(max_concurrency)
    started = time.perf_counter()

    async def one(index: int, prompt: str) -> BatchItem:
        attempts = 0
        queue_ms = 0
        last_error: Exception | None = None
        last_latency_ms = 0
        while True:
            attempts += 1
            wait_started = time.perf_counter()
            async with semaphore:
                queue_ms += int((time.perf_counter() - wait_started) * 1000)
                call_started = time.perf_counter()
                try:
                    response = await asyncio.wait_for(
                        client.chat([{"role": "user", "content": prompt}]),
                        timeout=timeout_seconds,
                    )
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    last_latency_ms = int((time.perf_counter() - call_started) * 1000)
                else:
                    last_latency_ms = int((time.perf_counter() - call_started) * 1000)
                    return BatchItem(
                        index=index,
                        ok=True,
                        response=response,
                        attempts=attempts,
                        latency_ms=last_latency_ms,
                        queue_ms=queue_ms,
                    )

            # 退避期间不占并发槽，避免重试把整批拖慢。
            if last_error is None or not _is_retryable(last_error) or attempts >= max_attempts:
                return BatchItem(
                    index=index,
                    ok=False,
                    error=f"{type(last_error).__name__}: {last_error}",
                    attempts=attempts,
                    latency_ms=last_latency_ms,
                    queue_ms=queue_ms,
                )
            delay = min(max_delay, base_delay * (2 ** (attempts - 1)))
            # 抖动避免一批请求同时重试再次打爆上游。
            await asyncio.sleep(delay * (1 + random.uniform(0, 0.2)))

    items = await asyncio.gather(*(one(i, prompt) for i, prompt in enumerate(prompts)))
    duration_ms = int((time.perf_counter() - started) * 1000)
    successes = [item for item in items if item.ok]
    latencies = [item.latency_ms for item in successes]
    queue_times = [item.queue_ms for item in items]
    prompt_tokens = sum(item.response.prompt_tokens for item in successes if item.response)
    completion_tokens = sum(item.response.completion_tokens for item in successes if item.response)
    cost = sum(item.response.cost for item in successes if item.response)

    stats = BatchStats(
        total=len(items),
        succeeded=len(successes),
        failed=len(items) - len(successes),
        total_attempts=sum(item.attempts for item in items),
        duration_ms=duration_ms,
        throughput_per_second=round(len(items) / (duration_ms / 1000), 2) if duration_ms else 0.0,
        p50_latency_ms=_percentile(latencies, 0.50),
        p95_latency_ms=_percentile(latencies, 0.95),
        p95_queue_ms=_percentile(queue_times, 0.95),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost=round(cost, 6),
    )
    return BatchResult(items=list(items), stats=stats)
