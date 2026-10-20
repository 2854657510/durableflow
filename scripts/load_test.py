"""API 级并发压测：并发创建运行，等待终态，统计吞吐和端到端 P95。

用法（先启动 API 和 Worker）：
    python scripts/load_test.py --requests 50 --concurrency 10
    python scripts/load_test.py --requests 100 --concurrency 20 --create-only

注意：默认限流 120 次/分钟，压测前可把 RATE_LIMIT_PER_MINUTE 调大。
"""

from __future__ import annotations

import argparse
import asyncio
import math
import time

import httpx

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED"}


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(ratio * len(ordered)) - 1))
    return ordered[index]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--workflow-id", type=int, default=None)
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--create-only", action="store_true")
    args = parser.parse_args()

    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    semaphore = asyncio.Semaphore(args.concurrency)
    limits = httpx.Limits(max_connections=args.concurrency + 5)

    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        timeout=30.0,
        limits=limits,
    ) as client:
        workflow_id = args.workflow_id
        if workflow_id is None:
            response = await client.get("/api/workflows")
            response.raise_for_status()
            workflows = response.json()
            if not workflows:
                raise SystemExit("没有可用工作流，先运行 python scripts/seed.py")
            workflow_id = workflows[0]["id"]

        async def create_one(index: int) -> dict:
            async with semaphore:
                started = time.perf_counter()
                response = await client.post(
                    f"/api/workflows/{workflow_id}/runs",
                    json={"input": {"index": index}},
                    headers={"Idempotency-Key": f"load-{time.time_ns()}-{index}"},
                )
                latency = (time.perf_counter() - started) * 1000
                if response.status_code >= 400:
                    return {"ok": False, "error": response.text[:200], "latency_ms": latency}
                return {
                    "ok": True,
                    "run_id": response.json()["run"]["id"],
                    "latency_ms": latency,
                }

        create_started = time.perf_counter()
        created = await asyncio.gather(*(create_one(i) for i in range(args.requests)))
        create_duration = time.perf_counter() - create_started

        ok_runs = [item for item in created if item["ok"]]
        print(
            f"创建：{len(ok_runs)}/{args.requests} 成功，"
            f"{create_duration:.2f}s，"
            f"吞吐 {len(ok_runs) / create_duration:.2f} run/s"
        )
        create_latencies = [item["latency_ms"] for item in ok_runs]
        print(
            f"创建延迟：P50 {percentile(create_latencies, 0.5):.0f}ms，"
            f"P95 {percentile(create_latencies, 0.95):.0f}ms"
        )
        if args.create_only or not ok_runs:
            return

        async def wait_one(item: dict) -> dict:
            run_id = item["run_id"]
            started = time.perf_counter()
            deadline = time.monotonic() + args.timeout
            status = "UNKNOWN"
            while time.monotonic() < deadline:
                response = await client.get(f"/api/runs/{run_id}")
                if response.status_code == 200:
                    status = response.json()["status"]
                    if status in TERMINAL:
                        break
                await asyncio.sleep(0.5)
            return {
                "run_id": run_id,
                "status": status,
                "e2e_ms": (time.perf_counter() - started) * 1000,
            }

        wait_started = time.perf_counter()
        results = await asyncio.gather(*(wait_one(item) for item in ok_runs))
        wait_duration = time.perf_counter() - wait_started

        counts: dict[str, int] = {}
        for result in results:
            counts[result["status"]] = counts.get(result["status"], 0) + 1
        e2e = [result["e2e_ms"] for result in results if result["status"] in TERMINAL]
        print(
            f"完成：{len(e2e)}/{len(results)}，总耗时 {wait_duration:.2f}s，"
            f"状态 {counts}"
        )
        print(
            f"端到端：P50 {percentile(e2e, 0.5):.0f}ms，"
            f"P95 {percentile(e2e, 0.95):.0f}ms"
        )


if __name__ == "__main__":
    asyncio.run(main())
