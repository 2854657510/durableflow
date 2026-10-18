from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import Settings, get_settings
from app.core.retry import NonRetryableError, RetryableError
from app.core.context import estimate_tokens
from app.llm.pricing import estimate_cost, pricing_for


@dataclass
class LLMResponse:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    cost: float = 0.0


class LLMClient(Protocol):
    def chat(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        tools: list[dict] | None = None,
    ) -> LLMResponse: ...


def _text_of(messages: list[dict]) -> str:
    return "\n".join(str(m.get("content", "")) for m in messages)


class MockLLMClient:
    """零成本、确定性的 LLM，用于测试和演示。

    它会根据 prompt 里的关键词返回固定结构：
    - 规划：返回 plan JSON
    - email / 邮件：返回 send_email 工具调用
    - Observation：返回最终答案
    - 其他：返回 search_docs 工具调用
    """

    def __init__(self, model: str = "mock", input_per_1m: float = 1.0, output_per_1m: float = 2.0):
        self.model = model
        self.input_per_1m = input_per_1m
        self.output_per_1m = output_per_1m

    def chat(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        started = time.perf_counter()
        text = _text_of(messages)
        lower = text.lower()
        # 只看最后一条 user 消息，避免系统提示里的工具名/规则误触发。
        last_user = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                last_user = str(message.get("content", ""))
                break
        user_lower = last_user.lower()
        is_json = bool(response_format and response_format.get("type") == "json_object")

        if "observation:" in lower or "上一轮工具结果" in text:
            content = (
                json.dumps(
                    {
                        "thought": "工具结果已经足够回答。",
                        "action": None,
                        "final_answer": "已完成：检索到相关资料，关键结论是项目需要在两周内完成评审并提交。",
                    },
                    ensure_ascii=False,
                )
                if is_json
                else "已完成：检索到相关资料，关键结论是项目需要在两周内完成评审并提交。"
            )
        elif "任务规划器" in text or "规划" in last_user or "plan" in user_lower or "拆解" in last_user:
            content = json.dumps(
                {
                    "reasoning": "先检索资料，再总结并给出结论。",
                    "steps": [
                        {"name": "search", "tool": "search_docs", "goal": "检索与目标相关的文档"},
                        {"name": "summarize", "tool": None, "goal": "总结检索结果"},
                    ],
                },
                ensure_ascii=False,
            )
        elif "邮件" in last_user or "email" in user_lower or "send_email" in user_lower:
            content = (
                json.dumps(
                    {
                        "thought": "需要发送邮件，属于高风险写操作。",
                        "action": {
                            "tool": "send_email",
                            "arguments": {
                                "to": "ops@example.com",
                                "subject": "任务完成报告",
                                "body": "任务已完成，请查收。",
                            },
                        },
                        "final_answer": None,
                    },
                    ensure_ascii=False,
                )
                if is_json
                else "需要发送邮件。"
            )
        elif "review" in user_lower or "审查" in last_user:
            content = json.dumps(
                {"summary": "材料基本完整，建议补充交付时间。", "risk": "medium", "citations": ["doc-1"]},
                ensure_ascii=False,
            )
        elif "summary" in user_lower or "总结" in last_user:
            content = json.dumps({"summary": "这是压缩后的历史摘要。", "key_points": ["目标", "约束", "结论"]}, ensure_ascii=False)
        else:
            content = (
                json.dumps(
                    {
                        "thought": "先检索资料。",
                        "action": {"tool": "search_docs", "arguments": {"query": "项目 资料"}},
                        "final_answer": None,
                    },
                    ensure_ascii=False,
                )
                if is_json
                else "mock response"
            )

        prompt_tokens = sum(estimate_tokens(str(m.get("content", ""))) for m in messages)
        completion_tokens = estimate_tokens(content)
        latency = int((time.perf_counter() - started) * 1000)
        cost = estimate_cost(prompt_tokens, completion_tokens, self.input_per_1m, self.output_per_1m)
        return LLMResponse(
            content=content,
            model=model or self.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency,
            cost=cost,
        )


class ScriptedLLMClient:
    """按脚本返回响应，测试用。"""

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.calls = 0

    def chat(self, messages: list[dict], **_: object) -> LLMResponse:
        self.calls += 1
        if not self.responses:
            return LLMResponse(content='{"answer":"done"}', model="scripted")
        return self.responses.pop(0)


class OpenAICompatibleClient:
    def __init__(self, settings: Settings):
        self.base_url = settings.llm_base_url.rstrip("/")
        self.api_key = settings.llm_api_key
        self.default_model = settings.llm_model
        self.input_per_1m = settings.llm_input_price_per_1m
        self.output_per_1m = settings.llm_output_price_per_1m

    def chat(
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
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(
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
        cost = estimate_cost(prompt_tokens, completion_tokens, self.input_per_1m, self.output_per_1m)
        return LLMResponse(
            content=content,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency,
            cost=cost,
        )


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    settings = settings or get_settings()
    if settings.llm_provider.lower() == "mock" or not settings.llm_api_key:
        pricing = pricing_for(
            settings.llm_model,
            settings.llm_input_price_per_1m,
            settings.llm_output_price_per_1m,
        )
        return MockLLMClient(
            model=settings.llm_model,
            input_per_1m=pricing.input_per_1m,
            output_per_1m=pricing.output_per_1m,
        )
    return OpenAICompatibleClient(settings)
