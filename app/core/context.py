from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

Summarizer = Callable[[list[dict]], str]


def estimate_tokens(text: str) -> int:
    """粗略估算 token。

    中文约 1 字 1 token，英文约 4 字符 1 token。这里用 len/3 做混合估算，
    目的是做压缩阈值判断，不用于账单。真实账单以模型返回的 usage 为准。
    """
    if not text:
        return 0
    return max(1, len(text) // 3)


def deterministic_summary(messages: list[dict], max_chars: int = 800) -> str:
    parts = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = str(msg.get("content", "")).strip().replace("\n", " ")
        if content:
            parts.append(f"{role}: {content}")
    text = " | ".join(parts)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


@dataclass
class ContextResult:
    messages: list[dict]
    original_tokens: int
    final_tokens: int
    compressed: bool
    summary: str | None = None


class ContextManager:
    """上下文窗口管理：超阈值时把旧消息压成摘要，保留 system 和最近消息。"""

    def __init__(
        self,
        max_tokens: int = 4000,
        keep_last_messages: int = 6,
        summarizer: Summarizer | None = None,
    ):
        self.max_tokens = max_tokens
        self.keep_last_messages = keep_last_messages
        self.summarizer = summarizer

    def _tokens(self, messages: list[dict]) -> int:
        return sum(estimate_tokens(str(m.get("content", ""))) for m in messages)

    def build(self, messages: list[dict]) -> ContextResult:
        original = self._tokens(messages)
        if original <= self.max_tokens or len(messages) <= self.keep_last_messages + 1:
            return ContextResult(messages=messages, original_tokens=original, final_tokens=original, compressed=False)

        system_messages = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        keep = max(1, self.keep_last_messages)
        older = non_system[:-keep]
        recent = non_system[-keep:]

        summary = self.summarizer(older) if self.summarizer else deterministic_summary(older)
        summary_msg = {"role": "system", "content": f"[历史上下文摘要] {summary}"}
        new_messages = system_messages + [summary_msg] + recent

        # 如果还是超，先丢掉最旧的 recent（保留最后一条），再截短摘要。
        while self._tokens(new_messages) > self.max_tokens and len(recent) > 1:
            recent = recent[1:]
            new_messages = system_messages + [summary_msg] + recent
        while self._tokens(new_messages) > self.max_tokens and len(summary) > 0:
            summary = summary[: max(0, len(summary) - 40)]
            summary_msg["content"] = f"[历史上下文摘要] {summary}"
            new_messages = system_messages + [summary_msg] + recent

        final = self._tokens(new_messages)
        return ContextResult(
            messages=new_messages,
            original_tokens=original,
            final_tokens=final,
            compressed=True,
            summary=summary,
        )
