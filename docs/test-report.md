# 测试报告

> 日期：2026-10-21
> 代码：DurableFlow `main`
> 环境：Windows 10 + conda `durableflow` + Python 3.12.12

## 1. 执行

```bash
pytest -q
pytest -q --cov=app --cov-report=term
```

## 2. 结果

- **50 passed**，0 failed。
- 总覆盖率：**76%**（2453 statements，587 miss）。
- 警告：SQLite datetime adapter 弃用、Starlette TestClient 弃用；不影响结果。

核心模块覆盖：

| 模块 | 覆盖率 |
|---|---:|
| `app/models.py` | 100% |
| `app/schemas.py` | 100% |
| `app/core/retry.py` | 100% |
| `app/core/events.py` | 100% |
| `app/state_machine.py` | 97% |
| `app/worker/claim.py` | 90% |
| `app/core/outbox.py` | 88% |
| `app/main.py` | 86% |
| `app/services/workflows.py` | 79% |
| `app/worker/executor.py` | 72% |
| `app/services/runs.py` | 69% |
| `app/llm/async_batch.py` | 66% |
| `app/observability/redaction.py` | 100% |
| `app/core/prompt_guard.py` | 100% |
| `app/worker/lease.py` | 50% |

覆盖率不是越高越好；50% 左右的 `lease.py` / `worker/main.py` 主要是后台循环和信号处理，靠 `scripts/crash_recovery.py` 做真实进程级验证。

## 3. 覆盖范围

- 状态机合法/非法迁移。
- 重试退避、抖动、可重试/不可重试分类。
- 上下文压缩、长期记忆写入/召回/compact。
- 工具权限（dangerous 未审批拦截）、`calculate` 不使用 `eval`。
- `SKIP LOCKED` 抢占、依赖释放、租约回收、最大尝试进 DEAD。
- 运行级幂等、步骤级副作用去重、Outbox 成功/失败重试。
- Agent 工具调用、最终回答、危险工具暂停。
- API v1、旧 `/api` 兼容、分页 `X-Total-Count`、统一错误码、Pydantic 422、API Key 恒定时比较。
- SSE 事件推送、`Last-Event-ID` 续传、积压超过单批、非法游标 400。
- 异步批量：并发上限、重试、不可重试、超时、统计聚合。

## 4. 测试之外的真实验证

| 项 | 命令 | 结果 |
|---|---|---|
| 崩溃恢复 | `python scripts/crash_recovery.py` | kill Worker 后 9.38s 自动恢复 |
| 引擎吞吐 | `python scripts/benchmark.py --runs 40 --workers 3` | 135.32 步骤/秒，P95 796ms |
| 异步批量 | `python scripts/benchmark_async.py --requests 100 --delay-ms 50 --concurrency 1,4,8,16,32` | 16.03 → 400 req/s，服务 P95 63ms |
| API 并发 | `python scripts/load_test.py --requests 100 --concurrency 20` | 100/100 成功，创建 107.43 run/s |
| 索引对比 | `python scripts/db_benchmark.py` | 21.083ms → 2.869ms → 0.030ms |

## 5. 限制

- 单测默认 SQLite，多 Worker 抢占的真实行为用 PostgreSQL 演示脚本验证；SQLite 下用进程内锁模拟。
- 没有做全链路混沌测试（DB 闪断、Redis 挂、毒消息）。
- 没有接入 CI 覆盖率门禁；当前只跑 pytest。
