"""异步批量 LLM 压测。

用固定延迟的 Mock 客户端模拟上游 API，观察信号量并发数对吞吐和 P95 的影响。
不产生真实 API 成本；数字只代表本机调度开销 + 模拟延迟。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.llm.async_batch import AsyncMockLLMClient, run_batch


async def run_one(requests: int, delay_ms: int, concurrency: int):
    client = AsyncMockLLMClient(delay_ms=delay_ms)
    result = await run_batch(
        client,
        [f"prompt-{i}" for i in range(requests)],
        max_concurrency=concurrency,
    )
    await client.aclose()
    return result.stats


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--delay-ms", type=int, default=50)
    parser.add_argument("--concurrency", default="1,4,8,16")
    parser.add_argument("--json", action="store_true", help="输出 JSON，便于写进文档")
    args = parser.parse_args()

    levels = [int(item) for item in args.concurrency.split(",") if item.strip()]
    rows = []
    for concurrency in levels:
        stats = await run_one(args.requests, args.delay_ms, concurrency)
        rows.append({"concurrency": concurrency, **stats.__dict__})

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    baseline = rows[0]["throughput_per_second"] or 1.0
    print(f"模拟延迟 {args.delay_ms}ms，请求 {args.requests} 次")
    print(
        f"{'并发':<6}{'成功':<8}{'失败':<8}{'吞吐(req/s)':<14}"
        f"{'服务P50(ms)':<14}{'服务P95(ms)':<14}{'排队P95(ms)':<14}{'重试':<8}{'加速比'}"
    )
    for row in rows:
        speedup = row["throughput_per_second"] / baseline
        retries = row["total_attempts"] - row["total"]
        print(
            f"{row['concurrency']:<6}{row['succeeded']:<8}{row['failed']:<8}"
            f"{row['throughput_per_second']:<14}{row['p50_latency_ms']:<14}"
            f"{row['p95_latency_ms']:<14}{row['p95_queue_ms']:<14}"
            f"{retries:<8}{speedup:.2f}x"
        )


if __name__ == "__main__":
    asyncio.run(main())
